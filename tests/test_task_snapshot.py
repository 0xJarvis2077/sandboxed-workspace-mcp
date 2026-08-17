from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.task_config import TaskLimits
from workspace_guard_mcp.task_snapshot import SnapshotBuilder, SnapshotError


class TaskSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _builder(
        self,
        *,
        max_snapshot_files: int = 100,
        max_snapshot_bytes: int = 100_000,
    ) -> SnapshotBuilder:
        limits = TaskLimits(
            max_snapshot_files=max_snapshot_files,
            max_snapshot_bytes=max_snapshot_bytes,
        )
        return SnapshotBuilder(Settings.create(self.root), limits)

    def test_snapshot_excludes_blocked_ignored_symlink_and_special_paths(self) -> None:
        visible = self.root / "run.sh"
        visible.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        visible.chmod(0o4755)
        (self.root / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dependency.js").write_text(
            "secret", encoding="utf-8"
        )
        nested = self.root / "src"
        nested.mkdir()
        (nested / "main.py").write_text("print('ok')\n", encoding="utf-8")
        link_path = self.root / "source-link"
        try:
            link_path.symlink_to(nested / "main.py")
        except (OSError, NotImplementedError):
            link: Path | None = None
        else:
            link = link_path

        snapshot = self._builder().create()
        snapshot_parent = snapshot.path.parent
        try:
            self.assertNotEqual(snapshot.path, self.root)
            self.assertEqual(
                (snapshot.path / "src" / "main.py").read_text(encoding="utf-8"),
                "print('ok')\n",
            )
            self.assertFalse((snapshot.path / ".env").exists())
            self.assertFalse((snapshot.path / ".git").exists())
            self.assertFalse((snapshot.path / "node_modules").exists())
            if link is not None:
                self.assertFalse((snapshot.path / link.name).exists())
            copied_mode = (snapshot.path / "run.sh").stat().st_mode
            self.assertTrue(copied_mode & stat.S_IXUSR)
            self.assertFalse(copied_mode & stat.S_ISUID)
            self.assertFalse(copied_mode & stat.S_ISGID)
            (snapshot.path / "src" / "main.py").write_text(
                "changed\n", encoding="utf-8"
            )
            self.assertEqual(
                (self.root / "src" / "main.py").read_text(encoding="utf-8"),
                "print('ok')\n",
            )
        finally:
            snapshot.cleanup()
            snapshot.cleanup()
        self.assertFalse(snapshot_parent.exists())

    def test_custom_blocked_paths_are_excluded_at_every_depth(self) -> None:
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "token.txt").write_text("secret", encoding="utf-8")
        (self.root / "visible.txt").write_text("visible", encoding="utf-8")
        settings = Settings.create(self.root, blocked_patterns=("secrets/**",))
        snapshot = SnapshotBuilder(settings, TaskLimits()).create()
        try:
            self.assertFalse((snapshot.path / "secrets").exists())
            self.assertTrue((snapshot.path / "visible.txt").is_file())
        finally:
            snapshot.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX special files are not portable")
    def test_fifo_and_socket_are_omitted_without_being_opened(self) -> None:
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        socket_path = self.root / "socket"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
        except PermissionError as exc:
            listener.close()
            self.skipTest(f"Unix sockets unavailable: {exc}")
        try:
            snapshot = self._builder().create()
            try:
                self.assertFalse((snapshot.path / fifo.name).exists())
                self.assertFalse((snapshot.path / socket_path.name).exists())
            finally:
                snapshot.cleanup()
        finally:
            listener.close()

    def test_snapshot_file_and_byte_limits_are_strict(self) -> None:
        (self.root / "one.txt").write_text("1", encoding="utf-8")
        (self.root / "two.txt").write_text("2", encoding="utf-8")
        with self.assertRaisesRegex(SnapshotError, "max_snapshot_files"):
            self._builder(max_snapshot_files=1).create()

        (self.root / "two.txt").unlink()
        (self.root / "one.txt").write_bytes(b"12345")
        with self.assertRaisesRegex(SnapshotError, "max_snapshot_bytes"):
            self._builder(max_snapshot_bytes=4).create()

    def test_snapshot_checks_cancellation_between_copy_chunks_and_deadline(
        self,
    ) -> None:
        (self.root / "large.bin").write_bytes(b"x" * (128 * 1024))
        cancellation = threading.Event()
        real_read = os.read
        reads = 0

        def cancelling_read(descriptor: int, size: int) -> bytes:
            nonlocal reads
            data = real_read(descriptor, size)
            reads += 1
            if reads == 1:
                cancellation.set()
            return data

        before = set(Path(tempfile.gettempdir()).glob("workspace-guard-mcp-task-*"))
        with (
            patch(
                "workspace_guard_mcp.task_snapshot.os.read",
                side_effect=cancelling_read,
            ),
            self.assertRaisesRegex(SnapshotError, "cancelled"),
        ):
            self._builder(max_snapshot_bytes=256 * 1024).create(
                cancellation_event=cancellation
            )
        after = set(Path(tempfile.gettempdir()).glob("workspace-guard-mcp-task-*"))
        self.assertEqual(after, before)

        moments = iter((0.0, 2.0))
        with (
            patch(
                "workspace_guard_mcp.task_snapshot.time.monotonic",
                side_effect=lambda: next(moments, 2.0),
            ),
            self.assertRaisesRegex(SnapshotError, "timed out"),
        ):
            self._builder().create(deadline=1.0)

    def test_portable_snapshot_path_has_the_same_policy(self) -> None:
        (self.root / "visible").mkdir()
        (self.root / "visible" / "file.txt").write_text("data", encoding="utf-8")
        (self.root / ".env").write_text("secret", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "ignored.txt").write_text(
            "ignored", encoding="utf-8"
        )
        link_path = self.root / "link"
        try:
            link_path.symlink_to(self.root / "visible" / "file.txt")
        except (OSError, NotImplementedError):
            link: Path | None = None
        else:
            link = link_path

        builder = self._builder()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "workspace"
            destination.mkdir()
            builder._copy_portable(destination)

            self.assertEqual(
                (destination / "visible" / "file.txt").read_text(encoding="utf-8"),
                "data",
            )
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / "node_modules").exists())
            if link is not None:
                self.assertFalse((destination / link.name).exists())

    @unittest.skipIf(os.name != "posix", "O_NOFOLLOW race test is POSIX-only")
    def test_concurrent_file_replacement_cannot_copy_a_symlink_target(self) -> None:
        source = self.root / "race.txt"
        source.write_text("safe", encoding="utf-8")
        outside = self.root.parent / f"outside-{self.root.name}.txt"
        outside.write_text("outside-secret", encoding="utf-8")
        real_open = os.open
        raced = False

        def racing_open(path, flags, *args, **kwargs):
            nonlocal raced
            if path == "race.txt" and kwargs.get("dir_fd") is not None and not raced:
                raced = True
                source.unlink()
                source.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)

        try:
            with (
                patch(
                    "workspace_guard_mcp.task_snapshot.os.open",
                    side_effect=racing_open,
                ),
                self.assertRaisesRegex(SnapshotError, "changed"),
            ):
                self._builder().create()
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
