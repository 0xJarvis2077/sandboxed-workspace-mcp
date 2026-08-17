from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sandboxed_workspace_mcp.config import ConfigurationError, Settings
from sandboxed_workspace_mcp.git_reader import GitError, GitReader
from sandboxed_workspace_mcp.git_writer import GitWriter
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.workspace import Workspace


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
            expected_sha256=versioned["sha256"],
        )
        diff = GitReader(self.settings).diff()
        self.assertIn("-baseline", diff)
        self.assertIn("+bug", diff)

        historical = self.writer.read_file_at_revision(source.name, "HEAD")
        self.assertEqual(historical["content"], "baseline\n")
        self.assertEqual(historical["commit"], baseline["commit"])
        self.assertEqual(historical["mode"], "100644")
        current = workspace.read_file_versioned(source.name)
        workspace.write_file(
            source.name,
            historical["content"],
            overwrite=True,
            expected_sha256=current["sha256"],
        )
        self.assertEqual(GitReader(self.settings).diff(), "(no output)")
        self.assertNotIn("source file", GitReader(self.settings).status())

    def test_mcp_end_to_end_baseline_bug_diff_and_restore_chain(self) -> None:
        server = create_server(self.settings)

        async def exercise() -> None:
            created = await server.call_tool(
                "write_file", {"path": "source.txt", "content": "baseline\n"}
            )
            self.assertFalse(created.is_error)
            initialized = await server.call_tool("git_init", {})
            self.assertFalse(initialized.is_error)
            baseline = await server.call_tool("git_create_baseline", {})
            self.assertFalse(baseline.is_error)
            self.assertIn(
                "sandboxed-workspace-mcp baseline",
                (await server.call_tool("git_log", {"count": 1})).content[0].text,
            )
            self.assertEqual(
                (await server.call_tool("git_diff", {})).content[0].text,
                "(no output)",
            )
            original = await server.call_tool(
                "read_file_versioned", {"path": "source.txt"}
            )
            await server.call_tool(
                "write_file",
                {
                    "path": "source.txt",
                    "content": "bug\n",
                    "overwrite": True,
                    "expected_sha256": original.structured_content["sha256"],
                },
            )
            diff = await server.call_tool("git_diff", {})
            self.assertIn("+bug", diff.content[0].text)
            changed = await server.call_tool(
                "read_file_versioned", {"path": "source.txt"}
            )
            historical = await server.call_tool(
                "git_read_file_at_revision", {"path": "source.txt", "commit": "HEAD"}
            )
            self.assertEqual(historical.structured_content["content"], "baseline\n")
            restored = await server.call_tool(
                "write_file",
                {
                    "path": "source.txt",
                    "content": historical.structured_content["content"],
                    "overwrite": True,
                    "expected_sha256": changed.structured_content["sha256"],
                },
            )
            self.assertFalse(restored.is_error)
            self.assertEqual(
                (await server.call_tool("git_diff", {})).content[0].text,
                "(no output)",
            )
            self.assertNotIn(
                "source.txt",
                (await server.call_tool("git_status", {})).content[0].text,
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

        def fail_ref(args: list[str], **kwargs: object) -> bytes:
            if args and args[0] == "update-ref":
                raise GitError("simulated ref failure")
            return original_run(args, **kwargs)

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
        self.assertFalse(by_name["git_init"].annotations.read_only_hint)
        self.assertFalse(by_name["git_init"].annotations.destructive_hint)
        self.assertTrue(by_name["git_init"].annotations.idempotent_hint)
        self.assertFalse(by_name["git_create_baseline"].annotations.idempotent_hint)
        for arguments in ({"path": "."}, {"template": "/tmp"}, {"argv": ["status"]}):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(ValueError, "unexpected argument"),
            ):
                asyncio.run(enabled_server.call_tool("git_init", arguments))


if __name__ == "__main__":
    unittest.main()
