from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sandboxed_workspace_mcp.access_policy import (
    DEFAULT_GIT_BASELINE_IGNORE_RULES,
    GIT_BASELINE_NOISE_MANAGED_BLOCK_BEGIN,
    GIT_BASELINE_NOISE_MANAGED_BLOCK_END,
    GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES,
    is_git_baseline_noise,
)
from sandboxed_workspace_mcp.config import ConfigurationError, Settings
from sandboxed_workspace_mcp.git_reader import GitError, GitReader
from sandboxed_workspace_mcp.git_writer import GitWriter
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.workspace import Workspace

from _mcp_assertions import (
    require_call_tool_result,
    require_structured_content,
    require_text_content,
)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.settings = Settings.create(self.root, allow_git_writes=True)
        self.writer = GitWriter(self.settings)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            [shutil.which("git") or "git", *args],
            cwd=cwd or self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    @staticmethod
    def _str_field(payload: dict[str, object], key: str) -> str:
        value = payload[key]
        assert isinstance(value, str)
        return value

    def test_init_baseline_diff_history_read_and_versioned_restore(self) -> None:
        source = self.root / "source file Ω [literal].txt"
        source.write_text("baseline\n", encoding="utf-8")

        self.assertEqual(
            self.writer.init(),
            {"status": "initialized", "repository": ".", "initial_branch": "main"},
        )
        self.assertEqual(self.writer.init()["status"], "already_initialized")

        baseline = self.writer.create_baseline()
        self.assertEqual(baseline["branch"], "main")
        self.assertEqual(baseline["files"], 1)
        self.assertEqual(baseline["bytes"], len(b"baseline\n"))
        self.assertIn(
            "sandboxed-workspace-mcp baseline", GitReader(self.settings).log()
        )
        self.assertEqual(GitReader(self.settings).diff(), "(no output)")

        workspace = Workspace(self.settings)
        versioned = workspace.read_file_versioned(source.name)
        workspace.write_file(
            source.name,
            "bug\n",
            overwrite=True,
            expected_sha256=self._str_field(versioned, "sha256"),
        )
        diff = GitReader(self.settings).diff()
        self.assertIn("-baseline", diff)
        self.assertIn("+bug", diff)

        historical = self.writer.read_file_at_revision(source.name, "HEAD")
        historical_content = self._str_field(historical, "content")
        self.assertEqual(historical_content, "baseline\n")
        self.assertEqual(historical["commit"], baseline["commit"])
        self.assertEqual(historical["mode"], "100644")
        current = workspace.read_file_versioned(source.name)
        workspace.write_file(
            source.name,
            historical_content,
            overwrite=True,
            expected_sha256=self._str_field(current, "sha256"),
        )
        self.assertEqual(GitReader(self.settings).diff(), "(no output)")
        self.assertNotIn("source file", GitReader(self.settings).status())

    def test_mcp_end_to_end_baseline_bug_diff_and_restore_chain(self) -> None:
        server = create_server(self.settings)

        async def exercise() -> None:
            created = require_call_tool_result(
                await server.call_tool(
                    "write_file", {"path": "source.txt", "content": "baseline\n"}
                )
            )
            self.assertFalse(created.is_error)
            initialized = require_call_tool_result(await server.call_tool("git_init", {}))
            self.assertFalse(initialized.is_error)
            baseline = require_call_tool_result(
                await server.call_tool("git_create_baseline", {})
            )
            self.assertFalse(baseline.is_error)
            log_result = require_call_tool_result(
                await server.call_tool("git_log", {"count": 1})
            )
            self.assertTrue(log_result.content)
            self.assertIn(
                "sandboxed-workspace-mcp baseline",
                require_text_content(log_result.content[0]).text,
            )
            clean_diff = require_call_tool_result(
                await server.call_tool("git_diff", {})
            )
            self.assertTrue(clean_diff.content)
            self.assertEqual(
                require_text_content(clean_diff.content[0]).text,
                "(no output)",
            )
            original = require_call_tool_result(
                await server.call_tool(
                    "read_file_versioned", {"path": "source.txt"}
                )
            )
            original_structured = require_structured_content(original)
            require_call_tool_result(
                await server.call_tool(
                    "write_file",
                    {
                        "path": "source.txt",
                        "content": "bug\n",
                        "overwrite": True,
                        "expected_sha256": original_structured["sha256"],
                    },
                )
            )
            diff = require_call_tool_result(await server.call_tool("git_diff", {}))
            self.assertTrue(diff.content)
            self.assertIn("+bug", require_text_content(diff.content[0]).text)
            changed = require_call_tool_result(
                await server.call_tool(
                    "read_file_versioned", {"path": "source.txt"}
                )
            )
            changed_structured = require_structured_content(changed)
            historical = require_call_tool_result(
                await server.call_tool(
                    "git_read_file_at_revision",
                    {"path": "source.txt", "commit": "HEAD"},
                )
            )
            historical_structured = require_structured_content(historical)
            self.assertEqual(historical_structured["content"], "baseline\n")
            restored = require_call_tool_result(
                await server.call_tool(
                    "write_file",
                    {
                        "path": "source.txt",
                        "content": historical_structured["content"],
                        "overwrite": True,
                        "expected_sha256": changed_structured["sha256"],
                    },
                )
            )
            self.assertFalse(restored.is_error)
            final_diff = require_call_tool_result(
                await server.call_tool("git_diff", {})
            )
            self.assertTrue(final_diff.content)
            self.assertEqual(
                require_text_content(final_diff.content[0]).text,
                "(no output)",
            )
            status = require_call_tool_result(await server.call_tool("git_status", {}))
            self.assertTrue(status.content)
            self.assertNotIn(
                "source.txt",
                require_text_content(status.content[0]).text,
            )

        asyncio.run(exercise())

    def test_baseline_filters_blocked_ignored_symlink_special_and_literal_names(
        self,
    ) -> None:
        literal = self.root / "-leading :name [x] Ω.txt"
        literal.write_text("visible\n", encoding="utf-8")
        (self.root / ".env").write_text("secret\n", encoding="utf-8")
        (self.root / "private.pem").write_text("secret\n", encoding="utf-8")
        ignored = self.root / "node_modules"
        ignored.mkdir()
        (ignored / "dependency.txt").write_text("ignored\n", encoding="utf-8")
        symlink = self.root / "alias.txt"
        symlink.symlink_to(literal.name)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")

        self.writer.init()
        baseline = self.writer.create_baseline()
        self.assertEqual(baseline["files"], 1)
        tracked = (
            subprocess.run(
                [shutil.which("git") or "git", "ls-files", "-z"],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
            .stdout.decode("utf-8")
            .split("\0")
        )
        self.assertIn(literal.name, tracked)
        for hidden in (".env", "private.pem", "dependency.txt", "alias.txt", "pipe"):
            self.assertNotIn(hidden, tracked)
        self.assertEqual(
            self.writer.read_file_at_revision(literal.name)["content"], "visible\n"
        )

    def test_baseline_filters_noise_at_any_depth_and_reports_real_bytes(self) -> None:
        (self.root / "source.py").write_text("print('ok')\n", encoding="utf-8")
        noise = (
            ".DS_Store",
            "Thumbs.db",
            "Desktop.ini",
            "._source.py",
            ".coverage",
            ".coverage.worker-1",
            "root.pyc",
            "root.pyo",
            "__pycache__/module.pyc",
            "src/__pycache__/nested.pyc",
            ".pytest_cache/state",
            "tests/.pytest_cache/state",
            ".mypy_cache/state",
            ".ruff_cache/state",
            "nested/.DS_Store",
            "nested/Thumbs.db",
        )
        for relative in noise:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"noise\n")

        self.writer.init()
        baseline = self.writer.create_baseline()
        reader = GitReader(self.settings)
        tracked = set(reader.ls_files().splitlines())

        self.assertEqual(baseline["files"], 1)
        self.assertEqual(baseline["bytes"], len(b"print('ok')\n"))
        self.assertEqual(tracked, {"source.py"})
        status = reader.status("porcelain")
        self.assertEqual(status, "(no output)")
        for relative in noise:
            self.assertNotIn(relative, status)

        post_baseline_noise = (
            ".DS_Store",
            "nested/.DS_Store",
            "Thumbs.db",
            ".coverage",
            ".coverage.parallel",
            "module.pyc",
            "__pycache__/later.pyc",
            ".pytest_cache/new-state",
        )
        for relative in post_baseline_noise:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"new-noise\n")
        (self.root / "source.py").write_text("print('changed')\n", encoding="utf-8")
        status = reader.status("porcelain")
        self.assertIn("source.py", status)
        for relative in post_baseline_noise:
            self.assertNotIn(relative, status)

    def test_baseline_noise_policy_does_not_hide_similar_normal_names(self) -> None:
        normal = (
            "coverage.py",
            "mycoverage.txt",
            "module.pyc.txt",
            "Thumbs.db.keep",
            "DS_Store",
            "cache.py",
            "pytest_cache_notes.md",
            "normal.coverage",
        )
        for relative in normal:
            (self.root / relative).write_text("normal\n", encoding="utf-8")

        self.assertNotIn(".DS_Store", self.settings.blocked_patterns)
        self.assertNotIn("*.pyc", self.settings.blocked_patterns)
        self.assertIn(".DS_Store", DEFAULT_GIT_BASELINE_IGNORE_RULES)
        self.assertFalse(is_git_baseline_noise(""))
        self.assertTrue(is_git_baseline_noise("nested/.DS_Store"))
        self.writer.init()
        self.writer.create_baseline()
        tracked = set(GitReader(self.settings).ls_files().splitlines())
        self.assertEqual(tracked, set(normal))

        (self.root / "coverage.py").write_text("changed\n", encoding="utf-8")
        (self.root / "normal.coverage").write_text("changed\n", encoding="utf-8")
        status = GitReader(self.settings).status("porcelain")
        self.assertIn("coverage.py", status)
        self.assertIn("normal.coverage", status)
        diff = GitReader(self.settings).diff()
        self.assertIn("coverage.py", diff)
        self.assertIn("normal.coverage", diff)

    def test_git_init_installs_one_managed_noise_block_idempotently(self) -> None:
        self.writer.init()
        exclude = self.root / ".git" / "info" / "exclude"
        first = exclude.read_bytes()
        first_mtime = exclude.stat().st_mtime_ns
        block = "\n".join(GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES) + "\n"
        self.assertIn(block.encode("ascii"), first)
        self.assertEqual(
            first.count(GIT_BASELINE_NOISE_MANAGED_BLOCK_BEGIN.encode("ascii")), 1
        )
        self.assertEqual(
            first.count(GIT_BASELINE_NOISE_MANAGED_BLOCK_END.encode("ascii")), 1
        )

        self.assertEqual(self.writer.init()["status"], "already_initialized")
        self.assertEqual(exclude.read_bytes(), first)
        self.assertEqual(exclude.stat().st_mtime_ns, first_mtime)

        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        self.writer.create_baseline()
        self.assertEqual(exclude.read_bytes(), first)
        self.assertEqual(exclude.stat().st_mtime_ns, first_mtime)

    def test_external_baseline_preserves_custom_exclude_without_trailing_newline(
        self,
    ) -> None:
        self._git("init", "-q", "--initial-branch=main")
        exclude = self.root / ".git" / "info" / "exclude"
        custom = b"custom-user-rule/\nkeep-me.tmp"
        exclude.write_bytes(custom)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")

        GitWriter(Settings.create(self.root, allow_git_writes=True)).create_baseline()
        updated = exclude.read_bytes()
        block = ("\n".join(GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES) + "\n").encode(
            "ascii"
        )
        self.assertTrue(updated.startswith(custom + b"\n"))
        self.assertIn(block, updated)
        self.assertEqual(updated.count(block), 1)
        self.assertEqual(
            updated.count(GIT_BASELINE_NOISE_MANAGED_BLOCK_BEGIN.encode("ascii")), 1
        )

    def test_baseline_recreates_missing_info_and_exclude(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        info = self.root / ".git" / "info"
        shutil.rmtree(info)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")

        GitWriter(Settings.create(self.root, allow_git_writes=True)).create_baseline()
        exclude = info / "exclude"
        self.assertTrue(info.is_dir())
        self.assertIn(
            GIT_BASELINE_NOISE_MANAGED_BLOCK_BEGIN.encode("ascii"),
            exclude.read_bytes(),
        )

    def test_baseline_failure_removes_new_info_and_exclude(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        info = self.root / ".git" / "info"
        shutil.rmtree(info)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        writer = GitWriter(Settings.create(self.root, allow_git_writes=True))
        original_run = writer._run

        def fail_ref(
            args: list[str],
            *,
            cwd: Path | None = None,
            stdin: bytes | None = None,
            environment: dict[str, str] | None = None,
            output_limit: int | None = None,
            allow_failure: bool = False,
        ) -> bytes:
            if args and args[0] == "update-ref":
                raise GitError("simulated ref failure")
            return original_run(
                args,
                cwd=cwd,
                stdin=stdin,
                environment=environment,
                output_limit=output_limit,
                allow_failure=allow_failure,
            )

        with patch.object(writer, "_run", side_effect=fail_ref):
            with self.assertRaisesRegex(GitError, "simulated ref failure"):
                writer.create_baseline()
        self.assertFalse(info.exists())
        self.assertFalse((self.root / ".git" / "index").exists())

    def test_baseline_rejects_oversized_exclude_without_mutating_it(self) -> None:
        self.writer.init()
        exclude = self.root / ".git" / "info" / "exclude"
        oversized = b"x" * (64 * 1024 + 1)
        exclude.write_bytes(oversized)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        with self.assertRaisesRegex(GitError, "exceeds the permitted size"):
            self.writer.create_baseline()
        self.assertEqual(exclude.read_bytes(), oversized)
        self.assertFalse((self.root / ".git" / "index").exists())

    def test_exclude_atomic_write_failures_preserve_data_and_clean_temps(self) -> None:
        failures = (
            ("replace", "sandboxed_workspace_mcp.git_writer.os.replace"),
            ("fsync", "sandboxed_workspace_mcp.git_writer.os.fsync"),
        )
        for name, target in failures:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self._git("init", "-q", "--initial-branch=main", cwd=root)
                    exclude = root / ".git" / "info" / "exclude"
                    original = b"user-rule/\n"
                    exclude.write_bytes(original)
                    (root / "source.py").write_text("source\n", encoding="utf-8")
                    writer = GitWriter(Settings.create(root, allow_git_writes=True))
                    with patch(target, side_effect=OSError("simulated write failure")):
                        with self.assertRaisesRegex(
                            GitError, "simulated write failure"
                        ):
                            writer.create_baseline()
                    self.assertEqual(exclude.read_bytes(), original)
                    self.assertEqual(
                        list((root / ".git" / "info").glob(".sandboxed_git_exclude_*")),
                        [],
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git("init", "-q", "--initial-branch=main", cwd=root)
            exclude = root / ".git" / "info" / "exclude"
            exclude.unlink()
            (root / "source.py").write_text("source\n", encoding="utf-8")
            writer = GitWriter(Settings.create(root, allow_git_writes=True))
            with patch(
                "sandboxed_workspace_mcp.git_writer.os.link",
                side_effect=FileExistsError("simulated create race"),
            ):
                with self.assertRaisesRegex(GitError, "appeared before"):
                    writer.create_baseline()
            self.assertFalse(exclude.exists())
            self.assertEqual(
                list((root / ".git" / "info").glob(".sandboxed_git_exclude_*")),
                [],
            )

    def test_exclude_read_and_absent_file_races_are_rejected(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.write_bytes(b"user-rule/\n")
        with patch(
            "sandboxed_workspace_mcp.git_writer.os.open",
            side_effect=OSError("simulated read failure"),
        ):
            with self.assertRaisesRegex(GitError, "simulated read failure"):
                self.writer._read_exclude_snapshot(exclude.parent)

        exclude.unlink()
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        original_write = self.writer._write_exclude_content

        def create_race(
            info: Path,
            expected: object,
            data: bytes,
            mode: int,
        ) -> tuple[int, int, int, int]:
            exclude.write_bytes(b"appeared-during-race\n")
            return original_write(info, expected, data, mode)  # type: ignore[arg-type]

        with patch.object(
            self.writer, "_write_exclude_content", side_effect=create_race
        ):
            with self.assertRaisesRegex(GitError, "changed before"):
                self.writer.create_baseline()
        self.assertEqual(exclude.read_bytes(), b"appeared-during-race\n")

    def test_exclude_update_verification_failure_rolls_back(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        exclude = self.root / ".git" / "info" / "exclude"
        original = b"user-rule/\n"
        exclude.write_bytes(original)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        writer = GitWriter(Settings.create(self.root, allow_git_writes=True))
        original_read = writer._read_exclude_snapshot
        calls = 0

        def mismatch(info: Path) -> object:
            nonlocal calls
            calls += 1
            snapshot = original_read(info)
            if calls == 3:
                return replace(snapshot, data=b"verification-mismatch\n")
            return snapshot

        with patch.object(writer, "_read_exclude_snapshot", side_effect=mismatch):
            with self.assertRaisesRegex(GitError, "verification failed"):
                writer.create_baseline()
        self.assertEqual(exclude.read_bytes(), original)

    def test_baseline_rejects_malformed_exclude_without_commit_or_temp_files(
        self,
    ) -> None:
        block = ("\n".join(GIT_BASELINE_NOISE_MANAGED_BLOCK_LINES) + "\n").encode(
            "ascii"
        )
        malformed = (
            b"# BEGIN sandboxed-workspace-mcp baseline noise\n",
            b"# END sandboxed-workspace-mcp baseline noise\n",
            block + block,
            block.replace(b"*.pyc\n", b"*.txt\n"),
        )
        for content in malformed:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "source.py").write_text("source\n", encoding="utf-8")
                    self._git("init", "-q", "--initial-branch=main", cwd=root)
                    exclude = root / ".git" / "info" / "exclude"
                    exclude.write_bytes(content)
                    writer = GitWriter(Settings.create(root, allow_git_writes=True))
                    with self.assertRaisesRegex(GitError, "managed noise block"):
                        writer.create_baseline()
                    self.assertEqual(exclude.read_bytes(), content)
                    self.assertFalse((root / ".git" / "index").exists())
                    self.assertNotEqual(
                        subprocess.run(
                            [
                                shutil.which("git") or "git",
                                "rev-parse",
                                "--verify",
                                "HEAD",
                            ],
                            cwd=root,
                            capture_output=True,
                        ).returncode,
                        0,
                    )
                    self.assertEqual(
                        list((root / ".git" / "info").glob(".sandboxed_git_exclude_*")),
                        [],
                    )

    def test_baseline_rejects_exclude_symlink_directory_and_info_symlink(self) -> None:
        for kind in ("info-symlink", "exclude-symlink", "exclude-directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "source.py").write_text("source\n", encoding="utf-8")
                self._git("init", "-q", "--initial-branch=main", cwd=root)
                info = root / ".git" / "info"
                exclude = info / "exclude"
                if kind == "info-symlink":
                    shutil.rmtree(info)
                    target = root / "other-info"
                    target.mkdir()
                    info.symlink_to(target, target_is_directory=True)
                elif kind == "exclude-symlink":
                    target = root / "other-exclude"
                    target.write_text("user\n", encoding="utf-8")
                    exclude.unlink()
                    exclude.symlink_to(target)
                else:
                    exclude.unlink()
                    exclude.mkdir()
                writer = GitWriter(Settings.create(root, allow_git_writes=True))
                with self.assertRaisesRegex(GitError, r"Git \.git/info"):
                    writer.create_baseline()
                self.assertFalse((root / ".git" / "index").exists())
                self.assertNotEqual(
                    subprocess.run(
                        [
                            shutil.which("git") or "git",
                            "rev-parse",
                            "--verify",
                            "HEAD",
                        ],
                        cwd=root,
                        capture_output=True,
                    ).returncode,
                    0,
                )

    def test_baseline_rejects_exclude_fifo_when_supported(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is not supported on this platform")
        self._git("init", "-q", "--initial-branch=main")
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.unlink()
        os.mkfifo(exclude)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        with self.assertRaisesRegex(GitError, "regular file"):
            self.writer.create_baseline()
        self.assertFalse((self.root / ".git" / "index").exists())

    def test_baseline_rejects_exclude_replacement_race(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.write_bytes(b"user-rule/\n")
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        writer = GitWriter(Settings.create(self.root, allow_git_writes=True))
        original_write = writer._write_exclude_content

        def race(
            info: Path,
            expected: object,
            data: bytes,
            mode: int,
        ) -> tuple[int, int, int, int]:
            exclude.write_bytes(b"changed-by-race\n")
            return original_write(info, expected, data, mode)  # type: ignore[arg-type]

        with patch.object(writer, "_write_exclude_content", side_effect=race):
            with self.assertRaisesRegex(GitError, "changed before"):
                writer.create_baseline()
        self.assertEqual(exclude.read_bytes(), b"changed-by-race\n")
        self.assertFalse((self.root / ".git" / "index").exists())

    def test_baseline_failure_restores_original_exclude_and_visible_git_state(
        self,
    ) -> None:
        self._git("init", "-q", "--initial-branch=main")
        exclude = self.root / ".git" / "info" / "exclude"
        original_exclude = b"user-rule/\n"
        exclude.write_bytes(original_exclude)
        (self.root / "source.py").write_text("source\n", encoding="utf-8")
        writer = GitWriter(Settings.create(self.root, allow_git_writes=True))
        original_run = writer._run

        def fail_ref(
            args: list[str],
            *,
            cwd: Path | None = None,
            stdin: bytes | None = None,
            environment: dict[str, str] | None = None,
            output_limit: int | None = None,
            allow_failure: bool = False,
        ) -> bytes:
            if args and args[0] == "update-ref":
                raise GitError("simulated ref failure")
            return original_run(
                args,
                cwd=cwd,
                stdin=stdin,
                environment=environment,
                output_limit=output_limit,
                allow_failure=allow_failure,
            )

        with patch.object(writer, "_run", side_effect=fail_ref):
            with self.assertRaisesRegex(GitError, "simulated ref failure"):
                writer.create_baseline()
        self.assertEqual(exclude.read_bytes(), original_exclude)
        self.assertFalse((self.root / ".git" / "index").exists())
        self.assertNotEqual(
            subprocess.run(
                [shutil.which("git") or "git", "rev-parse", "--verify", "HEAD"],
                cwd=self.root,
                capture_output=True,
            ).returncode,
            0,
        )
        self.assertEqual(
            list((self.root / ".git" / "info").glob(".sandboxed_git_exclude_*")),
            [],
        )

    def test_first_baseline_is_not_a_general_commit_and_rejects_staged_index(
        self,
    ) -> None:
        source = self.root / "source.txt"
        source.write_text("one\n", encoding="utf-8")
        self.writer.init()
        self.writer.create_baseline()
        with self.assertRaisesRegex(GitError, "only allowed before the first commit"):
            self.writer.create_baseline()

        other = tempfile.TemporaryDirectory()
        try:
            root = Path(other.name)
            (root / "source.txt").write_text("one\n", encoding="utf-8")
            settings = Settings.create(root, allow_git_writes=True)
            writer = GitWriter(settings)
            writer.init()
            self._git("add", "source.txt", cwd=root)
            with self.assertRaisesRegex(GitError, "existing Git index"):
                writer.create_baseline()
        finally:
            other.cleanup()

    def test_external_committed_repository_is_rejected_without_new_commit(self) -> None:
        (self.root / "source.txt").write_text("one\n", encoding="utf-8")
        self._git("init", "-q", "--initial-branch=main")
        self._git("add", "source.txt")
        self._git(
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "external",
        )
        writer = GitWriter(Settings.create(self.root, allow_git_writes=True))
        with self.assertRaisesRegex(GitError, "only allowed before the first commit"):
            writer.create_baseline()
        self.assertIn("external", GitReader(Settings.create(self.root)).log())

    def test_empty_baseline_and_git_start_failure_leave_no_visible_repository(
        self,
    ) -> None:
        (self.root / ".env").write_text("secret\n", encoding="utf-8")
        self.writer.init()
        with self.assertRaisesRegex(GitError, "no policy-approved"):
            self.writer.create_baseline()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.create(root, allow_git_writes=True)
            writer = GitWriter(settings, executable="missing-git")
            with self.assertRaisesRegex(GitError, "failed to start"):
                writer.init()
            self.assertFalse((root / ".git").exists())

    def test_git_command_guardrails_reject_invalid_internal_inputs(self) -> None:
        self.writer.executable = None
        with self.assertRaisesRegex(GitError, "executable was not found"):
            self.writer._run([])

        self.writer.executable = shutil.which("git")
        with self.assertRaisesRegex(GitError, "argument validation"):
            self.writer._run(["bad\x00argument"])
        with self.assertRaisesRegex(GitError, "output limit"):
            self.writer._run([], output_limit=0)

    def test_baseline_rolls_back_index_when_ref_update_fails(self) -> None:
        (self.root / "source.txt").write_text("one\n", encoding="utf-8")
        self.writer.init()
        original_run = self.writer._run

        def fail_ref(
            args: list[str],
            *,
            cwd: Path | None = None,
            stdin: bytes | None = None,
            environment: dict[str, str] | None = None,
            output_limit: int | None = None,
            allow_failure: bool = False,
        ) -> bytes:
            if args and args[0] == "update-ref":
                raise GitError("simulated ref failure")
            return original_run(
                args,
                cwd=cwd,
                stdin=stdin,
                environment=environment,
                output_limit=output_limit,
                allow_failure=allow_failure,
            )

        with patch.object(self.writer, "_run", side_effect=fail_ref):
            with self.assertRaisesRegex(GitError, "simulated ref failure"):
                self.writer.create_baseline()
        self.assertFalse((self.root / ".git" / "index").exists())
        self.assertFalse(
            subprocess.run(
                [shutil.which("git") or "git", "rev-parse", "--verify", "HEAD"],
                cwd=self.root,
                capture_output=True,
            ).returncode
            == 0
        )

    def test_baseline_rolls_back_ref_after_post_install_verification_failure(
        self,
    ) -> None:
        (self.root / "source.txt").write_text("one\n", encoding="utf-8")
        self.writer.init()
        with patch.object(
            self.writer, "_resolve_commit", side_effect=GitError("verification")
        ):
            with self.assertRaisesRegex(GitError, "verification"):
                self.writer.create_baseline()
        self.assertFalse((self.root / ".git" / "index").exists())
        self.assertFalse(
            subprocess.run(
                [shutil.which("git") or "git", "rev-parse", "--verify", "HEAD"],
                cwd=self.root,
                capture_output=True,
            ).returncode
            == 0
        )

    def test_baseline_does_not_run_hooks_or_clean_filters(self) -> None:
        source = self.root / "source.txt"
        source.write_text("one\n", encoding="utf-8")
        marker = self.root.parent / "git-side-effect-marker"
        filter_script = self.root.parent / "git-filter-side-effect.sh"
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        self.addCleanup(lambda: filter_script.unlink(missing_ok=True))
        filter_script.write_text(
            f"#!/bin/sh\nprintf ran > {marker}\ncat\n", encoding="utf-8"
        )
        filter_script.chmod(0o700)
        self.writer.init()
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\nprintf ran > {marker}\n", encoding="utf-8")
        hook.chmod(0o700)
        (self.root / ".gitattributes").write_text(
            "*.txt filter=marker\n", encoding="utf-8"
        )
        self._git("config", "filter.marker.clean", str(filter_script))
        self.writer.create_baseline()
        self.assertFalse(marker.exists())
        filter_script.unlink()

    def test_init_rejects_existing_git_conflicts_and_ignores_template_environment(
        self,
    ) -> None:
        marker = self.root / "marker-from-template"
        template = self.root / "hostile-template"
        template.mkdir()
        (template / "marker-from-template").write_text("must not copy")
        old = os.environ.get("GIT_TEMPLATE_DIR")
        os.environ["GIT_TEMPLATE_DIR"] = str(template)
        try:
            result = self.writer.init()
        finally:
            if old is None:
                os.environ.pop("GIT_TEMPLATE_DIR", None)
            else:
                os.environ["GIT_TEMPLATE_DIR"] = old
        self.assertEqual(result["status"], "initialized")
        self.assertFalse(marker.exists())

        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    conflict = root / ".git"
                    if kind == "file":
                        conflict.write_text("gitdir: /outside")
                    elif kind == "directory":
                        conflict.mkdir()
                    else:
                        target = root / "elsewhere"
                        target.mkdir()
                        conflict.symlink_to(target, target_is_directory=True)
                    settings = Settings.create(root, allow_git_writes=True)
                    with self.assertRaises(GitError):
                        GitWriter(settings).init()

    def test_init_targets_inner_root_without_touching_outer_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory)
            inner = outer / "workspace"
            inner.mkdir()
            self._git("init", "-q", "--initial-branch=outer", cwd=outer)
            (inner / "source.txt").write_text("source\n", encoding="utf-8")

            writer = GitWriter(Settings.create(inner, allow_git_writes=True))
            result = writer.init()

            self.assertEqual(result["initial_branch"], "main")
            self.assertEqual(
                self._git("rev-parse", "--show-toplevel", cwd=outer).strip(),
                str(outer.resolve()),
            )
            self.assertEqual(
                self._git("symbolic-ref", "--short", "HEAD", cwd=outer).strip(),
                "outer",
            )
            self.assertTrue((inner / ".git").is_dir())
            self.assertEqual((inner / "source.txt").read_text(), "source\n")

    def test_limits_and_replacement_race_are_rejected(self) -> None:
        (self.root / "one.txt").write_text("one\n", encoding="utf-8")
        limited = Settings.create(
            self.root,
            allow_git_writes=True,
            max_git_baseline_files=1,
            max_git_baseline_bytes=2,
        )
        writer = GitWriter(limited)
        writer.init()
        with self.assertRaisesRegex(GitError, "max_git_baseline_bytes"):
            writer.create_baseline()
        self.assertFalse(
            subprocess.run(
                [shutil.which("git") or "git", "rev-parse", "--verify", "HEAD"],
                cwd=self.root,
                capture_output=True,
            ).returncode
            == 0
        )

    def test_historical_reader_rejects_injection_blocked_non_utf8_and_symlink_blobs(
        self,
    ) -> None:
        (self.root / "safe.txt").write_text("safe\n", encoding="utf-8")
        (self.root / ".env").write_text("secret\n", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\xff\x00")
        (self.root / "target.txt").write_text("target\n", encoding="utf-8")
        (self.root / "link.txt").symlink_to("target.txt")
        self._git("init", "-q", "--initial-branch=main")
        self._git(
            "add", "--", "safe.txt", ".env", "binary.bin", "target.txt", "link.txt"
        )
        self._git(
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        reader = GitWriter(Settings.create(self.root))
        self.assertEqual(reader.read_file_at_revision("safe.txt")["content"], "safe\n")
        with self.assertRaisesRegex(GitError, "not an allowed regular file"):
            reader.read_file_at_revision(".git")
        (self.root / "safe.txt").unlink()
        self.assertEqual(reader.read_file_at_revision("safe.txt")["content"], "safe\n")
        for value in (
            "../safe.txt",
            "/etc/hosts",
            "",
            ":",
            ":(glob)secret",
            "HEAD:safe.txt",
        ):
            with self.subTest(path=value), self.assertRaises(GitError):
                reader.read_file_at_revision(value)
        with self.assertRaisesRegex(GitError, "blocked"):
            reader.read_file_at_revision(".env")
        with self.assertRaisesRegex(GitError, "not valid UTF-8"):
            reader.read_file_at_revision("binary.bin")
        with self.assertRaisesRegex(GitError, "symbolic link"):
            reader.read_file_at_revision("link.txt")
        (self.root / "link.txt").unlink()
        with self.assertRaisesRegex(GitError, "regular file"):
            reader.read_file_at_revision("link.txt")
        for commit in ("HEAD^", "HEAD~1", "HEAD:.env", "deadbeef"):
            with self.subTest(commit=commit), self.assertRaises(GitError):
                reader.read_file_at_revision("safe.txt", commit)

    def test_baseline_rejects_wrong_branch_locks_and_limits(self) -> None:
        self._git("init", "-q", "--initial-branch=legacy")
        (self.root / "source.txt").write_text("source\n", encoding="utf-8")
        with self.assertRaisesRegex(GitError, "main"):
            self.writer.create_baseline()

        limited_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, limited_root)
        limited_root.joinpath("one.txt").write_text("one\n", encoding="utf-8")
        limited_root.joinpath("two.txt").write_text("two\n", encoding="utf-8")
        limited_settings = Settings.create(
            limited_root,
            allow_git_writes=True,
            max_git_baseline_files=1,
        )
        limited_writer = GitWriter(limited_settings)
        limited_writer.init()
        with self.assertRaisesRegex(GitError, "max_git_baseline_files"):
            limited_writer.create_baseline()

        locked_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, locked_root)
        locked_root.joinpath("source.txt").write_text("source\n", encoding="utf-8")
        locked_writer = GitWriter(Settings.create(locked_root, allow_git_writes=True))
        locked_writer.init()
        locked_root.joinpath(".git", "index.lock").touch()
        self.addCleanup(locked_root.joinpath(".git", "index.lock").unlink)
        with self.assertRaisesRegex(GitError, "mutation lock"):
            locked_writer.create_baseline()

        sized_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, sized_root)
        sized_root.joinpath("large.txt").write_text("large\n", encoding="utf-8")
        sized_writer = GitWriter(
            Settings.create(
                sized_root,
                allow_git_writes=True,
                max_file_size=2,
            )
        )
        sized_writer.init()
        with self.assertRaisesRegex(GitError, "max_file_size"):
            sized_writer.create_baseline()

    def test_baseline_rejects_all_mutation_lock_locations(self) -> None:
        lock_paths = (
            Path("index.lock"),
            Path("HEAD.lock"),
            Path("refs/heads/main.lock"),
        )
        for relative_lock in lock_paths:
            with (
                self.subTest(lock=relative_lock),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "source.txt").write_text("source\n", encoding="utf-8")
                writer = GitWriter(Settings.create(root, allow_git_writes=True))
                writer.init()
                lock = root / ".git" / relative_lock
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.touch()

                with self.assertRaisesRegex(GitError, "mutation lock"):
                    writer.create_baseline()

                self.assertFalse(
                    subprocess.run(
                        [shutil.which("git") or "git", "rev-parse", "--verify", "HEAD"],
                        cwd=root,
                        capture_output=True,
                    ).returncode
                    == 0
                )

    def test_baseline_scan_entry_limit_and_nonregular_index_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            writer = GitWriter(
                Settings.create(
                    root,
                    allow_git_writes=True,
                    max_scan_entries=1,
                )
            )
            writer.init()
            with self.assertRaisesRegex(GitError, "directory-entry limit"):
                writer.create_baseline()

        for kind in ("directory", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "source.txt").write_text("source\n", encoding="utf-8")
                writer = GitWriter(Settings.create(root, allow_git_writes=True))
                writer.init()
                index = root / ".git" / "index"
                if kind == "directory":
                    index.mkdir()
                else:
                    target = root / "index-target"
                    target.write_text("outside", encoding="utf-8")
                    index.symlink_to(target)

                with self.assertRaisesRegex(GitError, "index is not a regular file"):
                    writer.create_baseline()

    def test_baseline_rejects_detached_unborn_head_when_main_is_required(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        git_dir = self.root / ".git"
        (git_dir / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")
        (self.root / "source.txt").write_text("source\n", encoding="utf-8")

        with self.assertRaisesRegex(GitError, "symbolic branch"):
            self.writer.create_baseline()

    def test_historical_missing_and_oversized_blobs_are_bounded(self) -> None:
        (self.root / "large.txt").write_text("large\n", encoding="utf-8")
        self._git("init", "-q", "--initial-branch=main")
        self._git("add", "large.txt")
        self._git(
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        with self.assertRaisesRegex(GitError, "does not name exactly one"):
            GitWriter(Settings.create(self.root)).read_file_at_revision("missing.txt")
        bounded = GitWriter(Settings.create(self.root, max_file_size=1))
        with self.assertRaisesRegex(GitError, "(too large|output exceeded)"):
            bounded.read_file_at_revision("large.txt")

    def test_concurrent_baseline_has_one_winner(self) -> None:
        (self.root / "source.txt").write_text("one\n", encoding="utf-8")
        self.writer.init()
        barrier = threading.Barrier(2)
        results: list[object] = []

        def call() -> None:
            barrier.wait()
            try:
                results.append(self.writer.create_baseline())
            except GitError as exc:
                results.append(exc)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, GitError) for result in results), 1)

    def test_configuration_and_server_registration_are_separate_from_workspace_write(
        self,
    ) -> None:
        with self.assertRaisesRegex(ConfigurationError, "allow_git_writes"):
            Settings.create(self.root, allow_writes=False, allow_git_writes=True)
        default_server = create_server(Settings.create(self.root))
        default_names = {tool.name for tool in asyncio.run(default_server.list_tools())}
        self.assertIn("git_read_file_at_revision", default_names)
        self.assertNotIn("git_init", default_names)
        enabled_server = create_server(self.settings)
        by_name = {tool.name: tool for tool in asyncio.run(enabled_server.list_tools())}
        self.assertIn("git_init", by_name)
        self.assertIn("git_create_baseline", by_name)
        init_annotations = by_name["git_init"].annotations
        baseline_annotations = by_name["git_create_baseline"].annotations
        assert init_annotations is not None
        assert baseline_annotations is not None
        self.assertFalse(init_annotations.read_only_hint)
        self.assertFalse(init_annotations.destructive_hint)
        self.assertTrue(init_annotations.idempotent_hint)
        self.assertFalse(baseline_annotations.idempotent_hint)
        for arguments in ({"path": "."}, {"template": "/tmp"}, {"argv": ["status"]}):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(ValueError, "unexpected argument"),
            ):
                asyncio.run(enabled_server.call_tool("git_init", arguments))


if __name__ == "__main__":
    unittest.main()
