"""Create bounded, no-follow workspace snapshots for untrusted task code."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .access_policy import AccessPolicy
from .config import Settings
from .task_config import TaskLimits

_COPY_CHUNK_BYTES = 64 * 1024


class SnapshotError(RuntimeError):
    """Raised when a workspace cannot be snapshotted within the safety contract."""


@dataclass(slots=True)
class WorkspaceSnapshot:
    """A disposable workspace tree owned by one task execution."""

    path: Path
    file_count: int
    total_bytes: int
    _temporary: tempfile.TemporaryDirectory[str] = field(repr=False)
    _cleaned: bool = field(default=False, init=False, repr=False)

    def cleanup(self) -> None:
        """Remove only this snapshot's private temporary directory."""

        if self._cleaned:
            return
        self._temporary.cleanup()
        self._cleaned = True


class SnapshotBuilder:
    """Copy allowed regular files without following workspace symlinks."""

    def __init__(self, settings: Settings, limits: TaskLimits) -> None:
        self.root = settings.root
        self.policy = AccessPolicy(settings.blocked_patterns)
        self.ignored_dirs = settings.ignored_dirs
        self.limits = limits
        self._entries = 0
        self._bytes = 0
        self._deadline: float | None = None
        self._cancellation_event: threading.Event | None = None

    def create(
        self,
        *,
        deadline: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> WorkspaceSnapshot:
        """Create one isolated snapshot or clean it completely on failure."""

        self._deadline = deadline
        self._cancellation_event = cancellation_event
        self._check_active()
        temporary = tempfile.TemporaryDirectory(prefix="sandboxed-workspace-mcp-task-")
        snapshot_root = Path(temporary.name).resolve() / "workspace"
        try:
            snapshot_root.mkdir(mode=self._directory_mode())
            self._entries = 0
            self._bytes = 0
            self._check_active()
            if os.name == "posix":
                self._copy_posix(snapshot_root)
            else:  # pragma: no cover - Docker task mode is normally POSIX-hosted
                self._copy_portable(snapshot_root)
            return WorkspaceSnapshot(
                path=snapshot_root,
                file_count=self._entries,
                total_bytes=self._bytes,
                _temporary=temporary,
            )
        except BaseException:
            temporary.cleanup()
            raise

    def _copy_posix(self, destination: Path) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.root, directory_flags)
        except OSError as exc:
            raise SnapshotError(f"cannot open workspace root safely: {exc}") from exc
        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise SnapshotError(
                    "workspace root changed and is no longer a directory"
                )
            self._copy_directory_fd(
                source_fd=root_fd,
                destination=destination,
                relative=PurePosixPath(),
                directory_flags=directory_flags,
            )
        finally:
            os.close(root_fd)

    def _copy_directory_fd(
        self,
        *,
        source_fd: int,
        destination: Path,
        relative: PurePosixPath,
        directory_flags: int,
    ) -> None:
        try:
            iterator = os.scandir(source_fd)
        except OSError as exc:
            raise SnapshotError(
                f"cannot scan workspace snapshot source: {exc}"
            ) from exc
        with iterator:
            for entry in iterator:
                self._check_active()
                name = entry.name
                child_relative = relative / name
                relative_text = child_relative.as_posix()
                if self.policy.is_blocked(relative_text):
                    continue
                try:
                    metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                except OSError as exc:
                    raise SnapshotError(
                        f"workspace changed while inspecting {relative_text}: {exc}"
                    ) from exc
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode):
                    if name in self.ignored_dirs:
                        continue
                    self._claim_entry(relative_text)
                    target_directory = destination / name
                    target_directory.mkdir(mode=self._directory_mode())
                    child_fd = self._open_child_directory(
                        source_fd, name, relative_text, directory_flags
                    )
                    try:
                        self._copy_directory_fd(
                            source_fd=child_fd,
                            destination=target_directory,
                            relative=child_relative,
                            directory_flags=directory_flags,
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if stat.S_ISREG(mode):
                    self._claim_entry(relative_text)
                    self._copy_file_fd(
                        source_fd=source_fd,
                        name=name,
                        destination=destination / name,
                        relative=relative_text,
                        expected=metadata,
                    )
                    continue
                # FIFO, sockets, devices, and all other special files are excluded.

    def _open_child_directory(
        self, parent_fd: int, name: str, relative: str, flags: int
    ) -> int:
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise SnapshotError(
                f"workspace directory changed while snapshotting {relative}: {exc}"
            ) from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise SnapshotError(
                f"workspace directory changed type while snapshotting {relative}"
            )
        return descriptor

    def _copy_file_fd(
        self,
        *,
        source_fd: int,
        name: str,
        destination: Path,
        relative: str,
        expected: os.stat_result,
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=source_fd)
        except OSError as exc:
            raise SnapshotError(
                f"workspace file changed while snapshotting {relative}: {exc}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise SnapshotError(
                    f"workspace file changed type while snapshotting {relative}"
                )
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise SnapshotError(
                    f"workspace file changed while snapshotting {relative}"
                )
            self._copy_open_descriptor(
                descriptor, destination, relative, opened.st_mode
            )
        finally:
            os.close(descriptor)

    def _copy_open_descriptor(
        self, descriptor: int, destination: Path, relative: str, source_mode: int
    ) -> None:
        output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        output_flags |= getattr(os, "O_CLOEXEC", 0)
        output = os.open(destination, output_flags, 0o600)
        try:
            while True:
                self._check_active()
                chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if self._bytes + len(chunk) > self.limits.max_snapshot_bytes:
                    raise SnapshotError(
                        "workspace snapshot exceeds max_snapshot_bytes while copying "
                        f"{relative}"
                    )
                view = memoryview(chunk)
                while view:
                    self._check_active()
                    written = os.write(output, view)
                    view = view[written:]
                self._bytes += len(chunk)
            os.fchmod(output, self._file_mode(source_mode))
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            os.close(output)

    def _copy_portable(self, destination: Path) -> None:
        pending = [(self.root, destination, PurePosixPath())]
        while pending:
            self._check_active()
            source_directory, target_directory, relative = pending.pop()
            try:
                entries = os.scandir(source_directory)
            except OSError as exc:
                raise SnapshotError(
                    f"cannot scan workspace snapshot source: {exc}"
                ) from exc
            with entries:
                for entry in entries:
                    self._check_active()
                    child_relative = relative / entry.name
                    relative_text = child_relative.as_posix()
                    if self.policy.is_blocked(relative_text) or entry.is_symlink():
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise SnapshotError(
                            f"workspace changed while inspecting {relative_text}: {exc}"
                        ) from exc
                    if stat.S_ISDIR(metadata.st_mode):
                        if entry.name in self.ignored_dirs:
                            continue
                        self._claim_entry(relative_text)
                        child_target = target_directory / entry.name
                        child_target.mkdir(mode=self._directory_mode())
                        pending.append((Path(entry.path), child_target, child_relative))
                    elif stat.S_ISREG(metadata.st_mode):
                        self._claim_entry(relative_text)
                        self._copy_portable_file(
                            Path(entry.path),
                            target_directory / entry.name,
                            relative_text,
                            metadata.st_mode,
                        )

    def _copy_portable_file(
        self, source: Path, destination: Path, relative: str, source_mode: int
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise SnapshotError(f"cannot open snapshot file {relative}: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SnapshotError(f"snapshot source is not regular: {relative}")
            self._copy_open_descriptor(descriptor, destination, relative, source_mode)
        finally:
            os.close(descriptor)

    def _claim_entry(self, relative: str) -> None:
        self._check_active()
        self._entries += 1
        if self._entries > self.limits.max_snapshot_files:
            raise SnapshotError(
                f"workspace snapshot exceeds max_snapshot_files at {relative}"
            )

    def _check_active(self) -> None:
        if self._cancellation_event is not None and self._cancellation_event.is_set():
            raise SnapshotError("workspace snapshot was cancelled")
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise SnapshotError("task timed out while creating workspace snapshot")

    @staticmethod
    def _directory_mode() -> int:
        if getattr(os, "geteuid", lambda: 1)() == 0:
            return 0o777
        return 0o700

    @staticmethod
    def _file_mode(source_mode: int) -> int:
        ordinary = stat.S_IMODE(source_mode) & 0o777
        ordinary |= stat.S_IRUSR | stat.S_IWUSR
        if getattr(os, "geteuid", lambda: 1)() == 0:
            ordinary |= stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
        return ordinary
