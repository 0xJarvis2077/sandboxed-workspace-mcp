"""Controlled Git repository mutation and revision-file reads.

This module deliberately owns the small set of Git write operations exposed by
the server.  It is not a general Git runner: every command and every value that
reaches Git is server generated, while workspace files are read through the
existing no-follow Workspace contract.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from .access_policy import (
    GIT_BASELINE_NOISE_MANAGED_BLOCK_BEGIN,
    GIT_BASELINE_NOISE_MANAGED_BLOCK_END,
    GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES,
    AccessPolicy,
    is_git_baseline_noise,
)
from .config import Settings
from .git_reader import GitError
from .workspace import Workspace, WorkspaceError

_HEX_COMMIT = re.compile(r"[0-9a-fA-F]{7,40}\Z")
_FULL_OBJECT = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_BASELINE_BRANCH = "main"
_BASELINE_MESSAGE = "sandboxed-workspace-mcp baseline"
_IDENTITY_NAME = "Sandboxed Workspace MCP"
_IDENTITY_EMAIL = "sandboxed-workspace-mcp@example.invalid"
_MAX_SMALL_OUTPUT = 64 * 1024
_MAX_EXCLUDE_SIZE = 64 * 1024
_EXCLUDE_BEGIN_BYTES = GIT_BASELINE_NOISE_MANAGED_BLOCK_BEGIN.encode("ascii")
_EXCLUDE_END_BYTES = GIT_BASELINE_NOISE_MANAGED_BLOCK_END.encode("ascii")
_EXCLUDE_BLOCK_BYTES = (
    "\n".join(GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES) + "\n"
).encode("ascii")

_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[Path, threading.RLock] = {}


def _rename_directory_without_replacing(source: Path, target: Path) -> None:
    """Atomically rename a directory when the host exposes a no-replace call."""

    function = None
    flags = 0
    if sys.platform.startswith("linux"):
        function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        flags = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":  # pragma: no cover - exercised on macOS here
        function = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
        flags = 4  # RENAME_EXCL
    if function is not None:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(target),
            flags,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), str(target))
        raise OSError(error, os.strerror(error), str(target))
    # Very old or unusual platforms have no no-replace primitive.  The process
    # lock still protects all server instances in this process; the remaining
    # same-user race is explicitly documented rather than hidden.
    os.rename(source, target)


@dataclass(frozen=True, slots=True)
class _CandidateFile:
    path: Path
    relative: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class _RepositoryInfo:
    branch: str
    top_level: Path


@dataclass(frozen=True, slots=True)
class _ExcludeSnapshot:
    path: Path
    exists: bool
    data: bytes
    signature: tuple[int, int, int, int] | None
    mode: int


@dataclass(frozen=True, slots=True)
class _ExcludeChange:
    original: _ExcludeSnapshot
    installed_data: bytes
    installed_signature: tuple[int, int, int, int]
    info: Path
    info_signature: tuple[int, int]
    info_created: bool


def _root_lock(root: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(root, threading.RLock())


class GitWriter:
    """Provide fixed, policy-bounded Git initialization and baseline operations."""

    def __init__(self, settings: Settings, executable: str | None = None) -> None:
        self.settings = settings
        self.root = settings.root
        self.git_dir = self.root / ".git"
        self.workspace = Workspace(settings)
        self.policy = AccessPolicy(settings.blocked_patterns)
        self.executable = executable or shutil.which("git")
        self._lock = _root_lock(self.root)

    def init(self) -> dict[str, object]:
        """Initialize exactly ``root/.git`` as an ordinary ``main`` repository."""

        with self._lock:
            existing = self._existing_repository_or_none()
            if existing is not None:
                return {
                    "status": "already_initialized",
                    "repository": ".",
                    "initial_branch": existing.branch,
                }

            staging: Path | None = None
            installed_identity: tuple[int, int] | None = None
            try:
                staging = Path(
                    tempfile.mkdtemp(prefix=".sandboxed_git_init_", dir=self.root)
                )
                os.chmod(staging, stat.S_IRWXU)
                worktree = staging / "worktree"
                template = staging / "template"
                worktree.mkdir()
                template.mkdir()
                self._run(
                    [
                        "init",
                        "--quiet",
                        f"--initial-branch={_BASELINE_BRANCH}",
                        f"--template={template}",
                        str(worktree),
                    ],
                    cwd=self.root,
                    environment={"GIT_TEMPLATE_DIR": str(template)},
                    output_limit=_MAX_SMALL_OUTPUT,
                )
                staged_git = worktree / ".git"
                self._install_baseline_noise_exclude(staged_git)
                self._validate_repository_at(worktree, staged_git, _BASELINE_BRANCH)

                # Use the host's no-replace directory rename where available so a
                # late-created .git is never overwritten.  The per-root lock also
                # serializes all server instances in this process.
                self._require_git_absent()
                _rename_directory_without_replacing(staged_git, self.git_dir)
                installed = self.git_dir.lstat()
                installed_identity = (installed.st_dev, installed.st_ino)
                info = self._validate_repository(_BASELINE_BRANCH)
                return {
                    "status": "initialized",
                    "repository": ".",
                    "initial_branch": info.branch,
                }
            except GitError:
                if installed_identity is not None:
                    self._remove_installed_git(installed_identity)
                raise
            except (OSError, RuntimeError) as exc:
                if installed_identity is not None:
                    self._remove_installed_git(installed_identity)
                raise GitError(f"git initialization failed: {exc}") from exc
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)

    def create_baseline(self) -> dict[str, object]:
        """Create the one server-owned initial commit for this workspace."""

        with self._lock:
            self._validate_repository(_BASELINE_BRANCH)
            self._require_unborn_head()
            self._require_unused_index()

            deadline = time.monotonic() + self.settings.git_timeout
            candidates = self._scan_candidates(deadline)
            if not candidates:
                raise GitError(
                    "cannot create baseline: no policy-approved regular files"
                )

            temp_index: Path | None = None
            index_identity: tuple[int, int] | None = None
            commit_oid: str | None = None
            ref_updated = False
            exclude_change: _ExcludeChange | None = None
            try:
                temp_index = self._temporary_index()
                total_bytes = 0
                for candidate in candidates:
                    self._check_deadline(deadline)
                    data, state = self.workspace._read_bytes_and_state(candidate.path)
                    self._check_deadline(deadline)
                    current_identity = (
                        state.device,
                        state.inode,
                        state.size,
                        state.mtime_ns,
                    )
                    if current_identity != candidate.identity:
                        raise GitError(
                            "workspace file changed while creating baseline: "
                            f"{candidate.relative}"
                        )
                    total_bytes += len(data)
                    if total_bytes > self.settings.max_git_baseline_bytes:
                        raise GitError(
                            "git baseline exceeds max_git_baseline_bytes "
                            f"({self.settings.max_git_baseline_bytes})"
                        )
                    oid = self._hash_blob(data)
                    mode = "100755" if state.mode & 0o111 else "100644"
                    self._run(
                        [
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            f"{mode},{oid},{candidate.relative}",
                        ],
                        environment={"GIT_INDEX_FILE": str(temp_index)},
                        output_limit=_MAX_SMALL_OUTPUT,
                    )

                tree_oid = self._run_text(
                    ["write-tree"],
                    environment={"GIT_INDEX_FILE": str(temp_index)},
                    output_limit=_MAX_SMALL_OUTPUT,
                ).strip()
                if not _FULL_OBJECT.fullmatch(tree_oid):
                    raise GitError("git write-tree returned an invalid tree id")
                commit_oid = self._create_commit(tree_oid)

                # Revalidate after all potentially slow reads and object writes.
                self._validate_repository(_BASELINE_BRANCH)
                self._require_unborn_head()
                self._require_unused_index()
                exclude_change = self._install_baseline_noise_exclude()
                index_identity = self._install_index_without_replacing(temp_index)
                temp_index = None
                try:
                    self._run(
                        [
                            "update-ref",
                            f"refs/heads/{_BASELINE_BRANCH}",
                            commit_oid,
                            "",
                        ],
                        output_limit=_MAX_SMALL_OUTPUT,
                    )
                    ref_updated = True
                except GitError:
                    self._remove_index(index_identity)
                    index_identity = None
                    raise

                resolved = self._resolve_commit("HEAD")
                if resolved != commit_oid:
                    raise GitError("baseline ref verification failed")
                return {
                    "status": "created",
                    "commit": commit_oid,
                    "branch": _BASELINE_BRANCH,
                    "files": len(candidates),
                    "bytes": total_bytes,
                }
            except GitError:
                if ref_updated and commit_oid is not None:
                    try:
                        self._run(
                            [
                                "update-ref",
                                "-d",
                                f"refs/heads/{_BASELINE_BRANCH}",
                                commit_oid,
                            ],
                            output_limit=_MAX_SMALL_OUTPUT,
                        )
                    except GitError:
                        # A failed rollback is surfaced below as a visible error;
                        # Git object data remains unreachable but the ref is not
                        # silently claimed to be clean.
                        pass
                if index_identity is not None:
                    self._remove_index(index_identity)
                if exclude_change is not None:
                    try:
                        self._rollback_baseline_noise_exclude(exclude_change)
                    except GitError as rollback_error:
                        raise GitError(
                            "baseline failed and Git exclude rollback also failed: "
                            f"{rollback_error}"
                        ) from rollback_error
                raise
            finally:
                if temp_index is not None:
                    try:
                        temp_index.unlink(missing_ok=True)
                    except OSError:
                        pass

    def read_file_at_revision(
        self, path: str, commit: str = "HEAD"
    ) -> dict[str, object]:
        """Read one literal, policy-approved regular blob from a safe revision."""

        relative = self._revision_path(path)
        resolved_commit = self._resolve_commit(commit)
        record = self._resolve_tree_entry(resolved_commit, relative)
        mode, object_type, blob_oid, stored_path = record
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise GitError("revision path is not a regular file")
        if stored_path != os.fsencode(relative):
            raise GitError("revision path did not resolve uniquely")
        data = self._run(
            ["cat-file", "blob", blob_oid],
            output_limit=self.settings.max_file_size + 1,
        )
        if len(data) > self.settings.max_file_size:
            raise GitError(
                "revision blob is too large: more than "
                f"{self.settings.max_file_size} bytes"
            )
        try:
            content = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitError("revision blob is not valid UTF-8") from exc
        return {
            "path": relative,
            "commit": resolved_commit,
            "blob": blob_oid,
            "content": content,
            "source_truncated": False,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": mode,
        }

    def _existing_repository_or_none(self) -> _RepositoryInfo | None:
        try:
            status = self.git_dir.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GitError(f"cannot inspect workspace .git: {exc}") from exc
        if stat.S_ISLNK(status.st_mode):
            raise GitError("workspace .git is a symbolic link and cannot be used")
        if not stat.S_ISDIR(status.st_mode):
            raise GitError("workspace .git is not a directory and cannot be replaced")
        return self._validate_repository()

    def _validate_repository(
        self, required_branch: str | None = None
    ) -> _RepositoryInfo:
        return self._validate_repository_at(self.root, self.git_dir, required_branch)

    def _validate_repository_at(
        self, cwd: Path, git_dir: Path, required_branch: str | None = None
    ) -> _RepositoryInfo:
        try:
            bare = self._run_text(
                ["rev-parse", "--is-bare-repository"], cwd=cwd, output_limit=128
            ).strip()
            inside = self._run_text(
                ["rev-parse", "--is-inside-work-tree"], cwd=cwd, output_limit=128
            ).strip()
            top_text = self._run_text(
                ["rev-parse", "--show-toplevel"],
                cwd=cwd,
                output_limit=_MAX_SMALL_OUTPUT,
            ).strip()
            branch = self._run_text(
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=cwd,
                output_limit=256,
                allow_failure=True,
            ).strip()
            top = Path(top_text).resolve(strict=True)
        except (GitError, OSError, RuntimeError) as exc:
            raise GitError(
                "workspace .git is not a valid ordinary Git repository"
            ) from exc
        if bare != "false" or inside != "true" or top != cwd.resolve(strict=True):
            raise GitError("workspace .git must be a non-bare repository rooted here")
        if not branch:
            if required_branch is not None:
                raise GitError("workspace repository HEAD is not a symbolic branch")
            branch = "HEAD"
        if required_branch is not None and branch != required_branch:
            raise GitError(f"workspace repository branch must be {required_branch}")
        if not git_dir.is_dir() or git_dir.is_symlink():
            raise GitError("workspace .git must be a real directory")
        return _RepositoryInfo(branch=branch, top_level=top)

    def _require_git_absent(self) -> None:
        try:
            self.git_dir.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GitError(f"cannot inspect workspace .git: {exc}") from exc
        raise GitError("workspace .git appeared during initialization")

    def _remove_installed_git(self, identity: tuple[int, int]) -> None:
        try:
            current = self.git_dir.lstat()
            if (
                stat.S_ISDIR(current.st_mode)
                and (current.st_dev, current.st_ino) == identity
            ):
                shutil.rmtree(self.git_dir)
        except OSError:
            pass

    def _require_unborn_head(self) -> None:
        result = self._run(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            allow_failure=True,
            output_limit=_MAX_SMALL_OUTPUT,
        )
        if result.strip():
            raise GitError("git baseline is only allowed before the first commit")

    def _require_unused_index(self) -> None:
        for lock_path in (
            self.git_dir / "index.lock",
            self.git_dir / "HEAD.lock",
            self.git_dir / "refs" / "heads" / f"{_BASELINE_BRANCH}.lock",
        ):
            if os.path.lexists(lock_path):
                raise GitError(f"Git mutation lock already exists: {lock_path.name}")
        try:
            status = (self.git_dir / "index").lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GitError(f"cannot inspect Git index: {exc}") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise GitError("existing Git index is not a regular file")
        raise GitError("git baseline refuses to replace an existing Git index")

    @staticmethod
    def _file_signature(status: os.stat_result) -> tuple[int, int, int, int]:
        return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)

    def _ensure_info_directory(
        self, git_dir: Path
    ) -> tuple[Path, bool, tuple[int, int]]:
        info = git_dir / "info"
        created = False
        try:
            status = info.lstat()
        except FileNotFoundError:
            try:
                info.mkdir(mode=stat.S_IRWXU)
                created = True
                status = info.lstat()
            except FileExistsError:
                try:
                    status = info.lstat()
                except OSError as exc:
                    raise GitError(f"cannot inspect Git info directory: {exc}") from exc
            except OSError as exc:
                raise GitError(f"cannot create Git info directory: {exc}") from exc
        except OSError as exc:
            raise GitError(f"cannot inspect Git info directory: {exc}") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise GitError("Git .git/info must be a real directory")
        return info, created, (status.st_dev, status.st_ino)

    def _read_exclude_snapshot(self, info: Path) -> _ExcludeSnapshot:
        path = info / "exclude"
        try:
            status = path.lstat()
        except FileNotFoundError:
            return _ExcludeSnapshot(path, False, b"", None, 0o600)
        except OSError as exc:
            raise GitError(f"cannot inspect Git exclude file: {exc}") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise GitError("Git .git/info/exclude must be a regular file")
        expected_signature = self._file_signature(status)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                stat.S_ISLNK(opened.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino)
            ):
                raise GitError("Git exclude file changed while it was opened")
            if opened.st_size > _MAX_EXCLUDE_SIZE:
                raise GitError(
                    "Git .git/info/exclude exceeds the permitted size "
                    f"({_MAX_EXCLUDE_SIZE} bytes)"
                )
            data = bytearray()
            while True:
                chunk = os.read(descriptor, _MAX_EXCLUDE_SIZE + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_EXCLUDE_SIZE:
                    raise GitError(
                        "Git .git/info/exclude exceeds the permitted size "
                        f"({_MAX_EXCLUDE_SIZE} bytes)"
                    )
        except GitError:
            raise
        except OSError as exc:
            raise GitError(f"cannot read Git exclude file: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            current = path.lstat()
        except OSError as exc:
            raise GitError("Git exclude file changed while it was read") from exc
        if self._file_signature(current) != expected_signature:
            raise GitError("Git exclude file changed while it was read")
        return _ExcludeSnapshot(
            path=path,
            exists=True,
            data=bytes(data),
            signature=expected_signature,
            mode=stat.S_IMODE(status.st_mode),
        )

    @staticmethod
    def _managed_exclude_block_state(data: bytes) -> bool:
        lines = data.splitlines()
        begin_indexes = [
            index
            for index, line in enumerate(lines)
            if line.startswith(_EXCLUDE_BEGIN_BYTES)
        ]
        end_indexes = [
            index
            for index, line in enumerate(lines)
            if line.startswith(_EXCLUDE_END_BYTES)
        ]
        if not begin_indexes and not end_indexes:
            return False
        if len(begin_indexes) != 1 or len(end_indexes) != 1:
            raise GitError("Git exclude contains a malformed managed noise block")
        begin, end = begin_indexes[0], end_indexes[0]
        expected = tuple(
            line.encode("ascii") for line in GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES
        )
        if begin >= end or tuple(lines[begin : end + 1]) != expected:
            raise GitError("Git exclude contains a conflicting managed noise block")
        return True

    @staticmethod
    def _exclude_data_with_block(existing: bytes) -> bytes:
        if not existing:
            return _EXCLUDE_BLOCK_BYTES
        separator = b"" if existing.endswith(b"\n") else b"\n"
        return existing + separator + _EXCLUDE_BLOCK_BYTES

    @staticmethod
    def _unlink_owned_temp(path: Path, signature: tuple[int, int, int, int]) -> None:
        try:
            status = path.lstat()
            if (status.st_dev, status.st_ino) == signature[:2]:
                path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return

    def _create_exclude_temp(
        self, info: Path, data: bytes, mode: int
    ) -> tuple[Path, tuple[int, int, int, int]]:
        descriptor, name = tempfile.mkstemp(prefix=".sandboxed_git_exclude_", dir=info)
        path = Path(name)
        temp_status = os.fstat(descriptor)
        initial_signature = self._file_signature(temp_status)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, mode)
            return path, self._file_signature(path.lstat())
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            self._unlink_owned_temp(path, initial_signature)
            raise GitError(f"cannot stage Git exclude update: {exc}") from exc

    def _verify_exclude_snapshot(self, expected: _ExcludeSnapshot) -> _ExcludeSnapshot:
        current = self._read_exclude_snapshot(expected.path.parent)
        if current.exists != expected.exists:
            raise GitError("Git exclude file changed before it could be updated")
        if expected.exists and (
            current.signature != expected.signature or current.data != expected.data
        ):
            raise GitError("Git exclude file changed before it could be updated")
        return current

    def _write_exclude_content(
        self,
        info: Path,
        expected: _ExcludeSnapshot,
        data: bytes,
        mode: int,
    ) -> tuple[int, int, int, int]:
        try:
            temp, temp_signature = self._create_exclude_temp(info, data, mode)
        except (
            OSError
        ) as exc:  # pragma: no cover - mkstemp failure is platform-specific
            raise GitError(f"cannot stage Git exclude update: {exc}") from exc
        keep_temp = True
        try:
            self._verify_exclude_snapshot(expected)
            if expected.exists:
                os.replace(temp, expected.path)
            else:
                try:
                    os.link(temp, expected.path, follow_symlinks=False)
                except FileExistsError as exc:
                    raise GitError(
                        "Git exclude file appeared before it could be created"
                    ) from exc
                self._unlink_owned_temp(temp, temp_signature)
            keep_temp = False
            installed = self._read_exclude_snapshot(info)
            if not installed.exists or installed.data != data:
                raise GitError("Git exclude update verification failed")
            if installed.signature is None:  # pragma: no cover - guarded above
                raise GitError("Git exclude update returned no file identity")
            return installed.signature
        except GitError:
            raise
        except OSError as exc:
            raise GitError(f"cannot atomically update Git exclude: {exc}") from exc
        finally:
            if keep_temp:
                self._unlink_owned_temp(temp, temp_signature)

    def _remove_created_info_directory(
        self, info: Path, signature: tuple[int, int]
    ) -> None:
        try:
            status = info.lstat()
            if (status.st_dev, status.st_ino) != signature:
                raise GitError("Git .git/info changed during baseline rollback")
            if any(info.iterdir()):
                raise GitError("Git .git/info is no longer empty during rollback")
            info.rmdir()
        except FileNotFoundError as exc:
            raise GitError(
                "Git .git/info disappeared during baseline rollback"
            ) from exc
        except OSError as exc:
            raise GitError(f"cannot remove created Git .git/info: {exc}") from exc

    def _install_baseline_noise_exclude(
        self, git_dir: Path | None = None
    ) -> _ExcludeChange | None:
        target_git_dir = self.git_dir if git_dir is None else git_dir
        info, info_created, info_signature = self._ensure_info_directory(target_git_dir)
        original: _ExcludeSnapshot | None = None
        new_data: bytes | None = None
        installed_signature: tuple[int, int, int, int] | None = None
        try:
            original = self._read_exclude_snapshot(info)
            if self._managed_exclude_block_state(original.data):
                return None
            new_data = self._exclude_data_with_block(original.data)
            installed_signature = self._write_exclude_content(
                info,
                original,
                new_data,
                original.mode,
            )
            return _ExcludeChange(
                original=original,
                installed_data=new_data,
                installed_signature=installed_signature,
                info=info,
                info_signature=info_signature,
                info_created=info_created,
            )
        except GitError:
            if original is not None and new_data is not None:
                try:
                    current = self._read_exclude_snapshot(info)
                    if current.exists and current.data == new_data:
                        if current.signature is None:  # pragma: no cover
                            raise GitError(
                                "Git exclude update returned no file identity"
                            )
                        self._rollback_baseline_noise_exclude(
                            _ExcludeChange(
                                original=original,
                                installed_data=new_data,
                                installed_signature=current.signature,
                                info=info,
                                info_signature=info_signature,
                                info_created=info_created,
                            )
                        )
                        raise
                except GitError:
                    raise
            if info_created:
                self._remove_created_info_directory(info, info_signature)
            raise

    def _rollback_baseline_noise_exclude(self, change: _ExcludeChange) -> None:
        current = self._read_exclude_snapshot(change.info)
        if (
            not current.exists
            or current.signature != change.installed_signature
            or current.data != change.installed_data
        ):
            raise GitError("Git exclude changed during baseline rollback")
        if change.original.exists:
            self._write_exclude_content(
                change.info,
                current,
                change.original.data,
                change.original.mode,
            )
        else:
            self._verify_exclude_snapshot(current)
            try:
                status = change.original.path.lstat()
                if self._file_signature(status) != change.installed_signature:
                    raise GitError("Git exclude changed during baseline rollback")
                change.original.path.unlink()
            except FileNotFoundError as exc:
                raise GitError(
                    "Git exclude disappeared during baseline rollback"
                ) from exc
            except OSError as exc:
                raise GitError(
                    f"cannot remove Git exclude during rollback: {exc}"
                ) from exc
            if self._read_exclude_snapshot(change.info).exists:
                raise GitError("Git exclude rollback left a file behind")
        if change.info_created:
            self._remove_created_info_directory(change.info, change.info_signature)

    def _scan_candidates(self, deadline: float) -> list[_CandidateFile]:
        max_entries = self.settings.max_scan_entries
        pending = [self.root]
        candidates: list[_CandidateFile] = []
        scanned = 0
        while pending:
            directory = pending.pop()
            directory_fd: int | None = None
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
                    iterator = os.scandir(directory)
                else:
                    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    directory_flags |= getattr(os, "O_CLOEXEC", 0)
                    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
                    directory_fd = os.open(directory, directory_flags)
                    iterator = os.scandir(directory_fd)
                try:
                    for entry in iterator:
                        self._check_deadline(deadline)
                        scanned += 1
                        if scanned > max_entries:
                            raise GitError(
                                "git baseline exceeds the bounded directory-entry "
                                f"limit ({max_entries})"
                            )
                        entry_path = directory / entry.name
                        relative_path = entry_path.relative_to(self.root)
                        relative = relative_path.as_posix()
                        if is_git_baseline_noise(relative):
                            continue
                        if self.policy.is_blocked(relative):
                            continue
                        try:
                            entry_status = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISLNK(entry_status.st_mode):
                            continue
                        if stat.S_ISDIR(entry_status.st_mode):
                            if entry.name in self.settings.ignored_dirs:
                                continue
                            pending.append(entry_path)
                            continue
                        if not stat.S_ISREG(entry_status.st_mode):
                            continue
                        if entry_status.st_size > self.settings.max_file_size:
                            raise GitError(
                                f"git baseline file exceeds max_file_size: {relative}"
                            )
                        if len(candidates) >= self.settings.max_git_baseline_files:
                            raise GitError(
                                "git baseline exceeds max_git_baseline_files "
                                f"({self.settings.max_git_baseline_files})"
                            )
                        candidates.append(
                            _CandidateFile(
                                path=entry_path,
                                relative=relative,
                                identity=(
                                    entry_status.st_dev,
                                    entry_status.st_ino,
                                    entry_status.st_size,
                                    entry_status.st_mtime_ns,
                                ),
                            )
                        )
                finally:
                    iterator.close()
            except GitError:
                raise
            except OSError as exc:
                raise GitError(
                    f"cannot scan workspace while creating baseline: {exc}"
                ) from exc
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
        candidates.sort(key=lambda item: os.fsencode(item.relative))
        return candidates

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise GitError("git baseline exceeded its time budget")

    def _temporary_index(self) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=".sandboxed_git_index_", dir=self.git_dir
        )
        os.close(descriptor)
        path = Path(name)
        path.unlink()
        return path

    def _install_index_without_replacing(self, source: Path) -> tuple[int, int]:
        target = self.git_dir / "index"
        if os.path.lexists(target):
            raise GitError("Git index appeared before baseline commit")
        try:
            source_status = source.lstat()
            os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise GitError("Git index appeared before baseline commit") from exc
        except OSError as exc:
            raise GitError(f"cannot install baseline Git index: {exc}") from exc
        source.unlink(missing_ok=True)
        return (source_status.st_dev, source_status.st_ino)

    def _remove_index(self, identity: tuple[int, int]) -> None:
        target = self.git_dir / "index"
        try:
            status = target.lstat()
            if (
                stat.S_ISREG(status.st_mode)
                and (
                    status.st_dev,
                    status.st_ino,
                )
                == identity
            ):
                target.unlink()
        except OSError:
            pass

    def _hash_blob(self, data: bytes) -> str:
        oid = self._run_text(
            ["hash-object", "-w", "--no-filters", "--stdin"],
            stdin=data,
            output_limit=_MAX_SMALL_OUTPUT,
        ).strip()
        if _FULL_OBJECT.fullmatch(oid) is None:
            raise GitError("git hash-object returned an invalid blob id")
        return oid

    def _create_commit(self, tree_oid: str) -> str:
        environment = {
            "GIT_AUTHOR_NAME": _IDENTITY_NAME,
            "GIT_AUTHOR_EMAIL": _IDENTITY_EMAIL,
            "GIT_COMMITTER_NAME": _IDENTITY_NAME,
            "GIT_COMMITTER_EMAIL": _IDENTITY_EMAIL,
        }
        commit = self._run_text(
            ["commit-tree", tree_oid, "-F", "-"],
            stdin=(_BASELINE_MESSAGE + "\n").encode("utf-8"),
            environment=environment,
            output_limit=_MAX_SMALL_OUTPUT,
        ).strip()
        if _FULL_OBJECT.fullmatch(commit) is None:
            raise GitError("git commit-tree returned an invalid commit id")
        return commit

    def _resolve_commit(self, commit: str) -> str:
        if not isinstance(commit, str) or (
            commit != "HEAD" and _HEX_COMMIT.fullmatch(commit) is None
        ):
            raise GitError(
                "commit must be HEAD or a 7-40 character hexadecimal commit id"
            )
        expression = "HEAD^{commit}" if commit == "HEAD" else f"{commit}^{{commit}}"
        resolved = self._run_text(
            ["rev-parse", "--verify", "--quiet", expression],
            output_limit=_MAX_SMALL_OUTPUT,
        ).strip()
        if _FULL_OBJECT.fullmatch(resolved) is None:
            raise GitError("Git did not resolve a full commit id")
        return resolved

    def _revision_path(self, value: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise GitError("revision path must be a non-empty workspace-relative path")
        supplied = Path(value)
        if supplied.is_absolute() or PureWindowsPath(value).drive or "\\" in value:
            raise GitError("revision path must be workspace-relative and literal")
        if any(part in {"..", ""} for part in value.split("/")):
            raise GitError(
                "revision path must not contain traversal or empty components"
            )
        if value == ":" or value.startswith(":("):
            raise GitError("revision path must not contain Git pathspec magic")
        try:
            lexical, lexical_relative = self.workspace._lexical_workspace_path(value)
            self.workspace._reject_symlink_components(lexical, lexical_relative, value)
        except WorkspaceError as exc:
            raise GitError("revision path contains a symbolic link") from exc
        except (OSError, RuntimeError) as exc:
            raise GitError("revision path is outside the workspace") from exc
        relative = lexical_relative.as_posix()
        if (
            not relative
            or relative == "."
            or relative.startswith(".")
            and relative == ".git"
        ):
            raise GitError("revision path is not an allowed regular file")
        if self.policy.is_blocked(relative):
            raise GitError("revision path is blocked by workspace policy")
        return relative

    def _resolve_tree_entry(
        self, commit: str, relative: str
    ) -> tuple[str, str, str, bytes]:
        output = self._run(
            ["ls-tree", "-z", "--full-tree", commit, "--", f":(literal){relative}"],
            output_limit=_MAX_SMALL_OUTPUT,
        )
        records = [record for record in output.split(b"\0") if record]
        if len(records) != 1:
            raise GitError("revision path does not name exactly one Git entry")
        try:
            header, stored_path = records[0].split(b"\t", 1)
            mode_bytes, object_type_bytes, oid_bytes = header.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            object_type = object_type_bytes.decode("ascii")
            blob_oid = oid_bytes.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitError("Git returned a malformed tree entry") from exc
        if _FULL_OBJECT.fullmatch(blob_oid) is None:
            raise GitError("Git returned an invalid object id")
        return mode, object_type, blob_oid, stored_path

    def _run_text(self, args: list[str], **kwargs: object) -> str:
        return self._run(args, **kwargs).decode("utf-8", errors="replace")

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        environment: dict[str, str] | None = None,
        output_limit: int | None = None,
        allow_failure: bool = False,
    ) -> bytes:
        if not self.executable:
            raise GitError("git executable was not found")
        if any(
            not isinstance(argument, str) or "\x00" in argument for argument in args
        ):
            raise GitError("internal Git argument validation failed")
        limit = self.settings.max_output_size if output_limit is None else output_limit
        if limit <= 0:
            raise GitError("Git output limit must be positive")
        command = [
            self.executable,
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            "-c",
            "diff.external=",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            *args,
        ]
        safe_tempdir = "/tmp" if os.name != "nt" else str(self.root)
        env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": os.devnull,
            "GIT_SEQUENCE_EDITOR": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "HOME": str(self.root),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.defpath,
            "TMPDIR": safe_tempdir,
            "TEMP": safe_tempdir,
            "TMP": safe_tempdir,
        }
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            env["USERPROFILE"] = str(self.root)
        if environment:
            env.update(environment)
        process_cwd = self.root if cwd is None else cwd
        try:
            process = subprocess.Popen(
                command,
                cwd=process_cwd,
                env=env,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise GitError(f"failed to start git executable: {exc}") from exc
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            process.kill()
            raise GitError("failed to capture Git output")

        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        output_lock = threading.Lock()

        def consume(stream: BinaryIO, destination: bytearray) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    with output_lock:
                        remaining = limit + 1 - len(stdout) - len(stderr)
                        if remaining > 0:
                            destination.extend(chunk[:remaining])
                        if len(chunk) > remaining or len(stdout) + len(stderr) > limit:
                            overflow.set()
                    if overflow.is_set():
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        return
            except OSError:
                return

        readers = [
            threading.Thread(
                target=consume, args=(process.stdout, stdout), daemon=True
            ),
            threading.Thread(
                target=consume, args=(process.stderr, stderr), daemon=True
            ),
        ]
        for reader in readers:
            reader.start()
        if process.stdin is not None:
            try:
                process.stdin.write(stdin or b"")
                process.stdin.close()
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass

        timed_out = False
        try:
            process.wait(timeout=self.settings.git_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        finally:
            for reader in readers:
                reader.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()

        if overflow.is_set():
            raise GitError(f"git output exceeded {limit} bytes")
        if timed_out:
            raise GitError(
                f"git command timed out after {self.settings.git_timeout:g} seconds"
            )
        if process.returncode and not allow_failure:
            diagnostic = bytes(stderr).decode("utf-8", errors="replace").strip()
            message = f"git command failed with exit code {process.returncode}"
            if diagnostic:
                message += f": {diagnostic[: max(0, limit - len(message) - 2)]}"
            raise GitError(message)
        return bytes(stdout)
