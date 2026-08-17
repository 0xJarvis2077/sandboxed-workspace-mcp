from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_guard_mcp.config import ConfigurationError, Settings
from workspace_guard_mcp.workspace import Workspace, WorkspaceError, truncate_utf8


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.settings = Settings.create(self.root)
        self.workspace = Workspace(self.settings)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _sha256(self, path: str) -> str:
        value = self.workspace.read_file_versioned(path)["sha256"]
        assert isinstance(value, str)
        return value

    def test_paths_cannot_escape_root(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "escapes workspace"):
            self.workspace.safe_path("../outside.txt")
        with self.assertRaisesRegex(WorkspaceError, "escapes workspace"):
            self.workspace.safe_path("/etc/hosts")
        with self.assertRaisesRegex(WorkspaceError, "NUL"):
            self.workspace.safe_path("bad\x00path")

    def test_external_symlink_is_blocked(self) -> None:
        outside = (
            Path(self.temporary_directory.name).parent
            / "outside-workspace-guard-mcp.txt"
        )
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "external-link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        try:
            with self.assertRaisesRegex(WorkspaceError, "escapes workspace"):
                self.workspace.read_file("external-link")
            self.assertIn("[BLOCKED EXTERNAL SYMLINK]", self.workspace.list_directory())
            self.assertNotIn("external-link", self.workspace.tree())
        finally:
            link.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_utf8_output_limit_is_measured_in_bytes(self) -> None:
        output = truncate_utf8("🙂" * 100, 40)

        self.assertLessEqual(len(output.encode("utf-8")), 40)
        self.assertIn("OUTPUT TRUNCATED", output)

    def test_read_file_honors_ranges_and_actual_byte_limit(self) -> None:
        (self.root / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        limited = Workspace(Settings.create(self.root, max_file_size=4))

        self.assertEqual(self.workspace.read_file("lines.txt", 2, 2), "two\n")
        with self.assertRaisesRegex(WorkspaceError, "too large"):
            limited.read_file("lines.txt")
        with self.assertRaisesRegex(WorkspaceError, "end_line"):
            self.workspace.read_file("lines.txt", 3, 2)

    def test_read_file_bytes_is_bounded_and_policy_checked(self) -> None:
        (self.root / "payload.bin").write_bytes(b"\x00\xffpayload")
        (self.root / ".env").write_bytes(b"SECRET=value")

        self.assertEqual(
            self.workspace.read_file_bytes("payload.bin"), b"\x00\xffpayload"
        )
        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.read_file_bytes(".env")

    def test_count_and_find_are_bounded_policy_checked_operations(self) -> None:
        (self.root / "notes.txt").write_text("one two\nthree\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "module.py").write_text(
            "VALUE = 'Needle'", encoding="utf-8"
        )
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "hidden.py").write_text(
            "hidden", encoding="utf-8"
        )

        self.assertEqual(self.workspace.count_file("notes.txt", "lines"), 2)
        self.assertEqual(self.workspace.count_file("notes.txt", "words"), 3)
        self.assertEqual(self.workspace.count_file("notes.txt", "bytes"), 14)
        with self.assertRaisesRegex(WorkspaceError, "unsupported count"):
            self.workspace.count_file("notes.txt", "characters")

        files = self.workspace.find_paths(kind="file")
        self.assertIn("notes.txt", files)
        self.assertIn("src/module.py", files)
        self.assertNotIn("hidden.py", files)
        self.assertEqual(
            self.workspace.find_paths("src", max_depth=1, name="*.py"),
            "src/module.py",
        )
        self.assertEqual(
            self.workspace.find_paths("src/module.py", kind="file"),
            "src/module.py",
        )
        self.assertEqual(
            self.workspace.find_paths(".", max_depth=0, kind="file"),
            "No paths found.",
        )
        with self.assertRaisesRegex(WorkspaceError, "path does not exist"):
            self.workspace.find_paths("missing")
        with self.assertRaisesRegex(WorkspaceError, "unsupported path kind"):
            self.workspace.find_paths(kind="socket")

        self.assertIn(
            "src/module.py:1: VALUE = 'Needle'",
            self.workspace.search_text("needle", ignore_case=True),
        )

    def test_precise_writes_are_atomic_and_preserve_mode(self) -> None:
        target = self.root / "module.py"
        target.write_text("value = 1\n", encoding="utf-8")
        target.chmod(0o640)

        result = self.workspace.replace_text(
            "module.py", "1", "2", self._sha256("module.py")
        )

        self.assertIn("Replacements: 1", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(list(self.root.glob(".workspace_guard_mcp_*")), [])

    def test_write_refuses_accidental_overwrite_and_ambiguous_replace(self) -> None:
        target = self.root / "notes.txt"
        target.write_text("same same", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "already exists"):
            self.workspace.write_file("notes.txt", "new")
        with self.assertRaisesRegex(WorkspaceError, "ambiguous"):
            self.workspace.replace_text(
                "notes.txt", "same", "other", self._sha256("notes.txt")
            )

        self.workspace.write_file(
            "notes.txt",
            "new",
            overwrite=True,
            expected_sha256=self._sha256("notes.txt"),
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_read_only_mode_blocks_every_mutating_operation(self) -> None:
        target = self.root / "existing.txt"
        target.write_text("old", encoding="utf-8")
        workspace = Workspace(Settings.create(self.root, allow_writes=False))

        operations = (
            lambda: workspace.create_directory("new-directory"),
            lambda: workspace.write_file("new.txt", "content"),
            lambda: workspace.replace_text("existing.txt", "old", "new"),
            lambda: workspace.append_file("existing.txt", "new"),
        )
        for operation in operations:
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(WorkspaceError, "read-only"),
            ):
                operation()

        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertFalse((self.root / "new-directory").exists())

    def test_search_is_streamed_bounded_and_skips_ignored_directories(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "one.py").write_text("needle\n", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "hidden.js").write_text(
            "needle\n", encoding="utf-8"
        )
        workspace = Workspace(Settings.create(self.root, max_output_size=60))

        result = workspace.search_text("needle")

        self.assertIn("src/one.py:1", result)
        self.assertNotIn("hidden.js", result)
        self.assertEqual(
            workspace.read_file("node_modules/hidden.js"),
            "needle\n",
        )
        self.assertLessEqual(len(result.encode("utf-8")), 60)
        with self.assertRaisesRegex(WorkspaceError, "does not exist"):
            workspace.search_text("needle", "missing")

    def test_list_and_tree_have_entry_limits(self) -> None:
        for index in range(10):
            (self.root / f"file-{index}.txt").write_text("x", encoding="utf-8")
        workspace = Workspace(Settings.create(self.root, max_tree_entries=3))

        self.assertIn("listing return limit reached", workspace.list_directory())
        self.assertIn("tree return limit reached", workspace.tree())

    def test_append_creates_file_and_enforces_byte_limit(self) -> None:
        workspace = Workspace(Settings.create(self.root, max_file_size=5))

        workspace.append_file("log.txt", "12")
        version = workspace.read_file_versioned("log.txt")["sha256"]
        assert isinstance(version, str)
        workspace.append_file("log.txt", "345", version)
        self.assertEqual((self.root / "log.txt").read_text(encoding="utf-8"), "12345")
        with self.assertRaisesRegex(WorkspaceError, "too large"):
            version = workspace.read_file_versioned("log.txt")["sha256"]
            assert isinstance(version, str)
            workspace.append_file("log.txt", "6", version)

    def test_new_files_are_private_by_default(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode bits are not portable to Windows")

        self.workspace.write_file("private.txt", "content")

        mode = stat.S_IMODE((self.root / "private.txt").stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_empty_listing_tail_grep_and_missing_replacement(self) -> None:
        (self.root / "empty").mkdir()
        (self.root / "sample.txt").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")

        self.assertEqual(self.workspace.list_directory("empty"), "(empty directory)")
        self.assertEqual(self.workspace.tail_file("sample.txt", 1), "alpha")
        self.assertEqual(
            self.workspace.grep_file("missing", "sample.txt"), "No matches found."
        )
        self.assertIn("1: alpha", self.workspace.grep_file("alpha", "sample.txt"))
        with self.assertRaisesRegex(WorkspaceError, "was not found"):
            self.workspace.replace_text(
                "sample.txt", "gamma", "delta", self._sha256("sample.txt")
            )
        with self.assertRaisesRegex(WorkspaceError, "cannot be empty"):
            self.workspace.replace_text("sample.txt", "", "delta")

    def test_directory_and_content_type_errors_are_actionable(self) -> None:
        (self.root / "directory").mkdir()
        (self.root / "missing.txt").write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "not a regular file"):
            self.workspace.read_file("directory")
        with self.assertRaisesRegex(WorkspaceError, "not a directory"):
            self.workspace.list_directory("missing.txt")

        workspace = Workspace(Settings.create(self.root, max_file_size=3))
        with self.assertRaisesRegex(WorkspaceError, "content is too large"):
            workspace.write_file("large.txt", "🙂")

    def test_default_blocked_paths_cannot_be_read_searched_or_modified(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text(
            "blocked-git-secret", encoding="utf-8"
        )
        (self.root / ".env").write_text("blocked-env-secret", encoding="utf-8")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "id_rsa").write_text(
            "blocked-key-secret", encoding="utf-8"
        )
        (self.root / ".env.example").write_text("SAFE_EXAMPLE=value", encoding="utf-8")

        blocked_files = (".git/config", ".env", "nested/id_rsa")
        for blocked in blocked_files:
            with self.subTest(path=blocked):
                with self.assertRaisesRegex(WorkspaceError, "blocked"):
                    self.workspace.read_file(blocked)
                with self.assertRaisesRegex(WorkspaceError, "blocked"):
                    self.workspace.search_text("secret", blocked)
                with self.assertRaisesRegex(WorkspaceError, "blocked"):
                    self.workspace.write_file(blocked, "changed", overwrite=True)
                with self.assertRaisesRegex(WorkspaceError, "blocked"):
                    self.workspace.replace_text(blocked, "secret", "changed")
                with self.assertRaisesRegex(WorkspaceError, "blocked"):
                    self.workspace.append_file(blocked, "changed")

        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.create_directory(".git/new-directory")
        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.list_directory(".git")
        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.tree(".git")

        listing = self.workspace.list_directory()
        tree = self.workspace.tree()
        search = self.workspace.search_text("secret")
        self.assertNotIn(".git", listing)
        self.assertNotIn(".env\n", listing + "\n")
        self.assertNotIn("id_rsa", tree)
        self.assertNotIn("blocked-", search)
        self.assertEqual(self.workspace.read_file(".env.example"), "SAFE_EXAMPLE=value")

    def test_blocked_paths_reject_absolute_and_symlink_aliases(self) -> None:
        secret = self.root / ".env"
        secret.write_text("secret", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.read_file(str(secret))

        alias = self.root / "allowed-looking-link"
        try:
            alias.symlink_to(secret)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.read_file("allowed-looking-link")
        self.assertNotIn("allowed-looking-link", self.workspace.list_directory())

    @unittest.skipIf(os.name == "nt", "POSIX special files are not portable")
    def test_fifo_and_socket_are_rejected_without_being_opened_as_text(self) -> None:
        fifo = self.root / "named-pipe"
        os.mkfifo(fifo)
        socket_path = self.root / "unix-socket"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(socket_path))
        except PermissionError as exc:
            listener.close()
            self.skipTest(f"sandbox does not permit Unix sockets: {exc}")
        try:
            for special in (fifo.name, socket_path.name):
                with self.subTest(path=special):
                    with self.assertRaisesRegex(WorkspaceError, "regular file"):
                        self.workspace.read_file(special)
                    with self.assertRaisesRegex(WorkspaceError, "regular file"):
                        self.workspace.search_text("anything", special)
            result = self.workspace.search_text("anything")
            self.assertIn("skipped 2", result)
        finally:
            listener.close()

    def test_directory_scans_stop_at_the_global_budget(self) -> None:
        for index in range(100):
            (self.root / f"entry-{index:03}.txt").write_text("needle", encoding="utf-8")
        workspace = Workspace(
            Settings.create(
                self.root,
                max_tree_entries=2,
                max_scan_entries=5,
            )
        )
        real_scandir = os.scandir

        class CountingScandir:
            def __init__(self, path: str | os.PathLike[str]) -> None:
                self.inner = real_scandir(path)
                self.count = 0

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                self.count += 1
                return next(self.inner)

        for operation in (
            workspace.list_directory,
            workspace.tree,
            lambda: workspace.search_text("needle"),
        ):
            wrappers: list[CountingScandir] = []

            def counting_scandir(path, collected=wrappers):
                wrapper = CountingScandir(path)
                collected.append(wrapper)
                return wrapper

            with (
                self.subTest(operation=operation),
                patch(
                    "workspace_guard_mcp.workspace.os.scandir",
                    side_effect=counting_scandir,
                ),
            ):
                result = operation()
            self.assertIn("scan budget exhausted", result)
            self.assertLessEqual(sum(wrapper.count for wrapper in wrappers), 5)

    def test_atomic_replace_failure_preserves_original_and_cleans_temp_file(
        self,
    ) -> None:
        target = self.root / "stable.txt"
        target.write_text("before", encoding="utf-8")

        with (
            patch(
                "workspace_guard_mcp.workspace.os.replace",
                side_effect=OSError("simulated replace failure"),
            ),
            self.assertRaisesRegex(WorkspaceError, "atomic write failed"),
        ):
            self.workspace.write_file(
                "stable.txt",
                "after",
                overwrite=True,
                expected_sha256=self._sha256("stable.txt"),
            )

        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        self.assertEqual(list(self.root.glob(".workspace_guard_mcp_*")), [])

    def test_search_and_grep_validate_inputs_and_return_limits(self) -> None:
        target = self.root / "matches.txt"
        target.write_text("needle one\nneedle two\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "search text is empty"):
            self.workspace.search_text("")
        with self.assertRaisesRegex(WorkspaceError, "search text is empty"):
            self.workspace.grep_file("", "matches.txt")

        direct = self.workspace.search_text("needle", "matches.txt", max_results=1)
        grep = self.workspace.grep_file("needle", "matches.txt", max_results=1)
        self.assertIn("search return limit reached", direct)
        self.assertIn("results truncated", grep)

        limited = Workspace(Settings.create(self.root, max_output_size=20))
        output = limited.search_text("needle", "matches.txt")
        self.assertLessEqual(len(output.encode("utf-8")), 20)

    def test_invalid_utf8_can_be_read_lossily_but_not_modified(self) -> None:
        target = self.root / "binary.txt"
        target.write_bytes(b"before\xffafter")

        self.assertIn("before", self.workspace.read_file("binary.txt"))
        with self.assertRaisesRegex(WorkspaceError, "valid UTF-8"):
            self.workspace.replace_text(
                "binary.txt", "before", "changed", self._sha256("binary.txt")
            )

    def test_broken_and_directory_symlinks_are_localized(self) -> None:
        broken = self.root / "broken-link"
        directory = self.root / "real-directory"
        directory.mkdir()
        (directory / "hidden.txt").write_text("needle", encoding="utf-8")
        directory_link = self.root / "directory-link"
        try:
            broken.symlink_to(self.root / "missing-target")
            directory_link.symlink_to(directory, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")

        listing = self.workspace.list_directory()
        tree = self.workspace.tree()
        search = self.workspace.search_text("needle")
        self.assertIn("broken-link -> [BROKEN SYMLINK]", listing)
        self.assertIn("directory-link", listing)
        self.assertNotIn("directory-link", tree)
        self.assertNotIn("directory-link/hidden.txt", search)
        self.assertIn("real-directory/hidden.txt", search)

    def test_scan_permission_errors_are_diagnostic(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("content", encoding="utf-8")
        real_scandir = os.scandir

        with (
            patch(
                "workspace_guard_mcp.workspace.os.scandir",
                side_effect=PermissionError("root denied"),
            ),
            self.assertRaisesRegex(WorkspaceError, "cannot scan directory"),
        ):
            self.workspace.list_directory()

        def selective_scandir(path):
            if Path(path).resolve() == nested.resolve():
                raise PermissionError("nested denied")
            return real_scandir(path)

        with patch(
            "workspace_guard_mcp.workspace.os.scandir",
            side_effect=selective_scandir,
        ):
            result = self.workspace.tree()
        self.assertIn("skipped 1", result)

    def test_safe_open_rechecks_descriptor_type_and_reports_open_failure(self) -> None:
        target = self.root / "regular.txt"
        target.write_text("content", encoding="utf-8")
        opened_status = target.stat()
        special_status = os.stat_result(
            (stat.S_IFIFO | 0o600, *tuple(opened_status)[1:])
        )

        with (
            patch(
                "workspace_guard_mcp.workspace.os.fstat",
                return_value=special_status,
            ),
            self.assertRaisesRegex(WorkspaceError, "after open"),
        ):
            self.workspace.read_file("regular.txt")

        with (
            patch.object(
                self.workspace,
                "_open_relative_posix",
                side_effect=OSError("simulated no-follow failure"),
            ),
            self.assertRaisesRegex(WorkspaceError, "cannot open file safely"),
        ):
            self.workspace.read_file("regular.txt")

    def test_versioned_writes_detect_external_and_concurrent_edits(self) -> None:
        target = self.root / "shared.txt"
        target.write_text("before\n", encoding="utf-8")
        version = self.workspace.read_file_versioned("shared.txt")

        self.assertEqual(version["content"], "before\n")
        self.assertEqual(version["size"], 7)
        self.assertIsInstance(version["mtime_ns"], int)
        digest = version["sha256"]
        assert isinstance(digest, str)

        target.write_text("external\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "conflict"):
            self.workspace.write_file(
                "shared.txt", "stale\n", overwrite=True, expected_sha256=digest
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "external\n")

        current = self._sha256("shared.txt")
        barrier = threading.Barrier(3)
        successes: list[str] = []
        conflicts: list[Exception] = []

        def write(content: str) -> None:
            barrier.wait()
            try:
                successes.append(
                    self.workspace.write_file(
                        "shared.txt",
                        content,
                        overwrite=True,
                        expected_sha256=current,
                    )
                )
            except Exception as exc:
                conflicts.append(exc)

        workers = [
            threading.Thread(target=write, args=(content,))
            for content in ("first\n", "second\n")
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("conflict", str(conflicts[0]))
        self.assertIn(target.read_text(encoding="utf-8"), {"first\n", "second\n"})

    def test_path_locks_use_fixed_stripes_and_serialize_same_path(self) -> None:
        target = self.root / "shared.txt"
        first_entered = threading.Event()
        second_ready = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()

        def first_writer() -> None:
            with self.workspace._lock_for_path(target):
                first_entered.set()
                release_first.wait(timeout=2)

        def second_writer() -> None:
            second_ready.set()
            with self.workspace._lock_for_path(target):
                second_entered.set()

        first = threading.Thread(target=first_writer)
        second = threading.Thread(target=second_writer)
        first.start()
        self.assertTrue(first_entered.wait(timeout=2))
        second.start()
        try:
            self.assertTrue(second_ready.wait(timeout=2))
            self.assertFalse(second_entered.is_set())
        finally:
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())

        for index in range(1024):
            with self.workspace._lock_for_path(self.root / f"unique-{index}"):
                pass
        self.assertEqual(len(self.workspace._path_lock_stripes), 64)

    def test_existing_writes_require_a_valid_version_token(self) -> None:
        target = self.root / "existing.txt"
        target.write_text("original\n", encoding="utf-8")

        for operation in (
            lambda: self.workspace.write_file(
                "existing.txt", "overwrite\n", overwrite=True
            ),
            lambda: self.workspace.replace_text("existing.txt", "original", "changed"),
            lambda: self.workspace.append_file("existing.txt", "changed"),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(WorkspaceError, "expected_sha256 is required"),
            ):
                operation()
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")

    def test_search_configuration_has_finite_upper_bounds(self) -> None:
        with (
            self.subTest(field="max_search_bytes"),
            self.assertRaisesRegex(ConfigurationError, "max_search_bytes"),
        ):
            Settings.create(self.root, max_search_bytes=1024**3 + 1)
        with (
            self.subTest(field="search_timeout_seconds"),
            self.assertRaisesRegex(ConfigurationError, "search_timeout_seconds"),
        ):
            Settings.create(self.root, search_timeout_seconds=301)
        with (
            self.subTest(field="max_concurrent_searches"),
            self.assertRaisesRegex(ConfigurationError, "max_concurrent_searches"),
        ):
            Settings.create(self.root, max_concurrent_searches=33)

    def test_search_byte_timeout_utf8_and_cross_chunk_diagnostics(self) -> None:
        (self.root / "one.txt").write_text("abcdef", encoding="utf-8")
        (self.root / "two.txt").write_text("ghijkl", encoding="utf-8")
        byte_limited = Workspace(
            Settings.create(self.root, max_search_bytes=5, max_output_size=200)
        )

        byte_result = byte_limited.search_text("missing")

        self.assertIn("byte budget exhausted after 5 bytes", byte_result)
        self.assertNotIn("time budget", byte_result)

        target = self.root / "unicode.txt"
        target.write_text("prefix🙂needle suffix\n", encoding="utf-8")
        chunked = Workspace(
            Settings.create(self.root, max_search_bytes=1024, max_output_size=500)
        )
        with patch("workspace_guard_mcp.workspace._SEARCH_CHUNK_BYTES", 4):
            result = chunked.search_text("🙂needle", "unicode.txt")
        self.assertIn("unicode.txt:1", result)
        self.assertIn("🙂needle", result)

        timed = Workspace(
            Settings.create(self.root, search_timeout_seconds=1, max_output_size=200)
        )
        moments = iter((0.0, 0.0, 2.0))

        def monotonic() -> float:
            return next(moments, 2.0)

        with patch(
            "workspace_guard_mcp.workspace.time.monotonic", side_effect=monotonic
        ):
            timeout_result = timed.search_text("missing", "unicode.txt")
        self.assertIn("time budget exhausted", timeout_result)
        self.assertNotIn("byte budget", timeout_result)

    def test_concurrent_search_limit_fails_fast(self) -> None:
        (self.root / "search.txt").write_text("needle\n", encoding="utf-8")
        workspace = Workspace(Settings.create(self.root, max_concurrent_searches=1))
        entered = threading.Event()
        resume = threading.Event()
        original = workspace._stream_search_file

        def pause(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(timeout=2))
            return original(*args, **kwargs)

        results: list[str] = []
        with patch.object(workspace, "_stream_search_file", side_effect=pause):
            worker = threading.Thread(
                target=lambda: results.append(workspace.search_text("needle"))
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaisesRegex(WorkspaceError, "concurrent search"):
                workspace.search_text("needle")
            resume.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertIn("search.txt:1", results[0])

    def test_path_helpers_and_create_failures_are_actionable(self) -> None:
        self.assertTrue(self.workspace.is_inside(self.root / "inside"))
        self.assertFalse(self.workspace.is_inside(self.root.parent / "outside"))
        with self.assertRaisesRegex(WorkspaceError, "outside"):
            self.workspace.relative_path(self.root.parent / "outside")

        with (
            patch.object(
                Path, "mkdir", side_effect=PermissionError("simulated denial")
            ),
            self.assertRaisesRegex(WorkspaceError, "cannot create directory"),
        ):
            self.workspace.create_directory("denied")


if __name__ == "__main__":
    unittest.main()
