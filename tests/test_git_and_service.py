from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.git_reader import GitError, GitReader
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.service import CommandError, SandboxedWorkspace

from _mcp_assertions import require_call_tool_result, require_text_content


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitAndServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Sandboxed Workspace MCP Tests")
        (self.root / "tracked.txt").write_text("before\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "initial")
        self.settings = Settings.create(self.root)
        self.git = GitReader(self.settings)
        self.computer = SandboxedWorkspace(self.settings, git=self.git)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            [shutil.which("git") or "git", *args],
            cwd=self.root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_read_only_git_queries_work(self) -> None:
        (self.root / "tracked.txt").write_text("after\n", encoding="utf-8")

        self.assertIn("tracked.txt", self.git.status())
        self.assertIn("-before", self.git.diff())
        self.assertIn("initial", self.git.log())
        self.assertIn("true", self.git.rev_parse("--is-inside-work-tree"))

    def test_git_argument_injection_cannot_read_external_files(self) -> None:
        command = "git diff --no-index tracked.txt /etc/hosts"

        with self.assertRaises(CommandError):
            self.computer.run_shell(command)

    def test_shell_like_interface_never_interprets_operators(self) -> None:
        special = self.root / "semi;colon.txt"
        special.write_text("literal $(value) & text", encoding="utf-8")

        self.assertEqual(
            self.computer.run_shell("cat 'semi;colon.txt'"),
            "literal $(value) & text",
        )
        with self.assertRaises(CommandError):
            self.computer.run_shell("cat tracked.txt; pwd")

    def test_git_output_overflow_is_a_bounded_error(self) -> None:
        (self.root / "tracked.txt").write_text("changed\n" * 200, encoding="utf-8")
        settings = Settings.create(self.root, max_output_size=100)

        with self.assertRaisesRegex(GitError, "output exceeded") as raised:
            GitReader(settings).diff()

        self.assertLessEqual(len(str(raised.exception).encode("utf-8")), 100)

    def test_command_grammar_checks_counts_and_git_queries(self) -> None:
        with self.assertRaisesRegex(CommandError, "line count"):
            self.computer.run_shell("head tracked.txt nope")
        with self.assertRaisesRegex(CommandError, "at most 50"):
            self.computer.run_shell("git log -51")
        with self.assertRaisesRegex(CommandError, "not allowed"):
            self.computer.run_shell("git rev-parse --git-dir")

    def test_common_read_only_command_forms_are_supported(self) -> None:
        (self.root / "tracked.txt").write_text(
            "First needle\nsecond NEEDLE\nthird\n", encoding="utf-8"
        )
        (self.root / "other.py").write_text("print('ok')\n", encoding="utf-8")

        self.assertIn("tracked.txt", self.computer.run_shell("ls -lah"))
        self.assertEqual(
            self.computer.run_shell("head -n 1 tracked.txt"), "First needle\n"
        )
        self.assertEqual(self.computer.run_shell("tail -n 1 tracked.txt"), "third")
        self.assertIn("tracked.txt", self.computer.run_shell("tree -L 1"))

        search = self.computer.run_shell("rg -niF needle .")
        self.assertIn("tracked.txt:1: First needle", search)
        self.assertIn("tracked.txt:2: second NEEDLE", search)
        self.assertIn("other.py", self.computer.run_shell("rg --files"))

        found = self.computer.run_shell("find . -maxdepth 1 -type f -name '*.txt'")
        self.assertIn("tracked.txt", found)
        self.assertNotIn("other.py", found)
        self.assertEqual(self.computer.run_shell("wc -l tracked.txt"), "3 tracked.txt")
        self.assertEqual(self.computer.run_shell("wc -w tracked.txt"), "5 tracked.txt")
        self.assertEqual(
            self.computer.run_shell("sed -n '2,3p' tracked.txt"),
            "second NEEDLE\nthird\n",
        )

    def test_rg_regex_fixed_case_and_glob_contract(self) -> None:
        (self.root / "search.py").write_text(
            "add value\nresult value\nNeedle\nNEEDLE\nadd|result\n",
            encoding="utf-8",
        )
        (self.root / "search.txt").write_text("add text\nneedle\n", encoding="utf-8")
        (self.root / ".env.py").write_text("add secret\n", encoding="utf-8")

        regex = self.computer.run_shell('rg -n "add|result" .')
        self.assertIn("search.py:1: add value", regex)
        self.assertIn("search.py:2: result value", regex)

        fixed = self.computer.run_shell('rg -n -F "add|result" .')
        self.assertIn("search.py:5: add|result", fixed)
        self.assertNotIn("search.py:1:", fixed)

        insensitive = self.computer.run_shell("rg -ni needle search.py")
        self.assertIn("search.py:3: Needle", insensitive)
        self.assertIn("search.py:4: NEEDLE", insensitive)
        smart_lower = self.computer.run_shell("rg -nS needle search.py")
        self.assertIn("search.py:3: Needle", smart_lower)
        smart_upper = self.computer.run_shell("rg -nS Needle search.py")
        self.assertIn("search.py:3: Needle", smart_upper)
        self.assertNotIn("search.py:4:", smart_upper)

        python_only = self.computer.run_shell("rg -g '*.py' add .")
        self.assertIn("search.py", python_only)
        self.assertNotIn("search.txt", python_only)
        self.assertNotIn(".env.py", python_only)
        files = self.computer.run_shell("rg --files -g '*.py' .")
        self.assertIn("search.py", files)
        self.assertNotIn("search.txt", files)
        self.assertNotIn(".env.py", files)

    def test_rg_complex_patterns_are_bounded_and_unsupported_syntax_is_rejected(
        self,
    ) -> None:
        (self.root / "long.txt").write_text("a" * 100_000 + "!\n", encoding="utf-8")
        started = time.monotonic()
        result = self.computer.run_shell("rg '(a+)+$' long.txt")
        self.assertEqual(result, "No matches found.")
        self.assertLess(time.monotonic() - started, 2)

        for command in (
            r"rg '(?=a)' .",
            r"rg '\1' .",
            "rg '' .",
            "rg -g '[ab].py' add .",
            "rg -g '*.py' -g '*.txt' add .",
            "rg --files -i .",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.computer.run_shell(command)

        (self.root / "operators.txt").write_text("search value\n", encoding="utf-8")
        self.assertIn(
            "search value",
            self.computer.run_shell("rg 'search|missing' operators.txt"),
        )
        with self.assertRaisesRegex(CommandError, "operators"):
            self.computer.run_shell("rg needle . | cat")

    def test_expanded_commands_reject_unapproved_options_and_operators(self) -> None:
        commands = (
            "ls --recursive",
            "rg --regexp needle .",
            "find . -exec cat {} +",
            "wc -L tracked.txt",
            "sed -e 1p tracked.txt",
            "head -c 2 tracked.txt",
        )
        for command in commands:
            with self.subTest(command=command), self.assertRaises(CommandError):
                self.computer.run_shell(command)

        malformed = (
            "rg",
            "rg --files . extra",
            "find . -maxdepth nope",
            "find . -type x",
            "find . -name ''",
            "sed -n nope tracked.txt",
            "sed -n 3,2p tracked.txt",
            "sed -n 1000001p tracked.txt",
            "tree -L 1 . extra",
            "ls . extra",
        )
        for command in malformed:
            with self.subTest(command=command), self.assertRaises(CommandError):
                self.computer.run_shell(command)

    def test_long_option_aliases_and_single_line_sed_are_supported(self) -> None:
        (self.root / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")

        self.assertIn("tracked.txt", self.computer.run_shell("ls --all -- ."))
        self.assertIn("tracked.txt", self.computer.run_shell("tree --max-depth 1"))
        self.assertIn(
            "tracked.txt:2: two",
            self.computer.run_shell("rg --ignore-case --line-number TWO ."),
        )
        self.assertIn(".", self.computer.run_shell("find -type d"))
        self.assertEqual(self.computer.run_shell("wc -c tracked.txt"), "8 tracked.txt")
        self.assertEqual(self.computer.run_shell("sed -n 2p tracked.txt"), "two\n")

    def test_every_allowed_shell_form_dispatches(self) -> None:
        (self.root / "tracked.txt").write_text(
            "first\nsecond needle\n", encoding="utf-8"
        )

        commands = {
            "pwd": str(self.root),
            "ls": "tracked.txt",
            "cat tracked.txt": "second needle",
            "head tracked.txt 1": "first",
            "tail tracked.txt 1": "second needle",
            "tree": "tracked.txt",
            "grep needle tracked.txt": "2: second needle",
            "git status": "tracked.txt",
            "git diff --staged": "(no output)",
            "git log -1": "initial",
            "git branch": "* ",
            "git rev-parse HEAD": "\n",
        }
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertIn(expected, self.computer.run_shell(command))

        with self.assertRaisesRegex(CommandError, "empty command"):
            self.computer.run_shell(" ")
        with self.assertRaisesRegex(CommandError, "not allowed"):
            self.computer.run_shell("python script.py")

    def test_git_reader_reports_missing_executable_and_invalid_query(self) -> None:
        reader = GitReader(self.settings, executable="")
        reader.executable = None
        with self.assertRaisesRegex(RuntimeError, "not found"):
            reader.status()
        with self.assertRaisesRegex(RuntimeError, "not allowed"):
            self.git.rev_parse("--git-dir")

    def test_non_git_directory_reports_nonzero_exit_as_git_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = GitReader(Settings.create(directory))
            for operation in (
                reader.status,
                reader.diff,
                reader.log,
                reader.workspace_diff,
            ):
                with (
                    self.subTest(operation=operation),
                    self.assertRaisesRegex(GitError, "exit code"),
                ):
                    operation()

    def test_git_start_failure_and_timeout_have_distinct_errors(self) -> None:
        with (
            patch(
                "sandboxed_workspace_mcp.git_reader.subprocess.Popen",
                side_effect=FileNotFoundError("missing executable"),
            ),
            self.assertRaisesRegex(GitError, "failed to start"),
        ):
            GitReader(self.settings, executable="missing-git").status()

        process = Mock()
        process.stdout = io.BytesIO(b"partial stdout")
        process.stderr = io.BytesIO(b"partial stderr")
        process.wait.side_effect = (
            subprocess.TimeoutExpired(["git"], 0.01),
            None,
        )
        process.returncode = -9
        settings = Settings.create(self.root, git_timeout=0.01)
        with (
            patch(
                "sandboxed_workspace_mcp.git_reader.subprocess.Popen",
                return_value=process,
            ),
            self.assertRaisesRegex(GitError, "timed out") as raised,
        ):
            GitReader(settings).status()

        self.assertIn("partial stderr", str(raised.exception))
        process.kill.assert_called_once()

    def test_git_status_and_diff_exclude_blocked_paths(self) -> None:
        (self.root / ".env").write_text("SECRET=before\n", encoding="utf-8")
        (self.root / "visible.txt").write_text("before\n", encoding="utf-8")
        self._git("add", ".env", "visible.txt")
        self._git("commit", "-qm", "add policy fixtures")
        (self.root / ".env").write_text("SECRET=after\n", encoding="utf-8")
        (self.root / "visible.txt").write_text("after\n", encoding="utf-8")

        status = self.git.status()
        diff = self.git.diff()

        self.assertNotIn(".env", status)
        self.assertNotIn("SECRET", diff)
        self.assertIn("visible.txt", status)
        self.assertIn("-before", diff)

    def test_scoped_git_paths_keep_blocked_descendant_exclusions(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ".env").write_text("NESTED_SECRET=before\n", encoding="utf-8")
        (nested / "private.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nbefore\n", encoding="utf-8"
        )
        (nested / "visible.txt").write_text("visible before\n", encoding="utf-8")
        self._git("add", "--", "nested")
        self._git("commit", "-qm", "nested policy fixture")

        (nested / ".env").write_text("NESTED_SECRET=after\n", encoding="utf-8")
        (nested / "private.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nafter\n", encoding="utf-8"
        )
        (nested / "visible.txt").write_text("visible after\n", encoding="utf-8")

        diff = self.git.diff(path="nested")
        shown = self.git.show("HEAD", path="nested")
        for output in (diff, shown):
            self.assertNotIn(".env", output)
            self.assertNotIn("private.pem", output)
            self.assertNotIn("NESTED_SECRET", output)
            self.assertNotIn("BEGIN PRIVATE KEY", output)
        self.assertIn("visible.txt", diff)
        self.assertIn("visible after", diff)
        self.assertIn("visible.txt", shown)
        self.assertIn("visible before", shown)

        with self.assertRaisesRegex(ValueError, "blocked"):
            self.git.diff(path="nested/.env")
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.git.show("HEAD", path="nested/private.pem")

        server = create_server(self.settings)

        async def exercise_native_tools() -> None:
            native_diff = require_call_tool_result(
                await server.call_tool("git_diff", {"path": "nested"})
            )
            native_show = require_call_tool_result(
                await server.call_tool(
                    "git_show", {"commit": "HEAD", "path": "nested"}
                )
            )
            self.assertFalse(native_diff.is_error)
            self.assertFalse(native_show.is_error)
            self.assertTrue(native_diff.content)
            self.assertTrue(native_show.content)
            diff_text = require_text_content(native_diff.content[0]).text
            show_text = require_text_content(native_show.content[0]).text
            self.assertIn("visible after", diff_text)
            self.assertIn("visible before", show_text)
            self.assertNotIn("NESTED_SECRET", diff_text)
            self.assertNotIn("NESTED_SECRET", show_text)

        asyncio.run(exercise_native_tools())

    def test_workspace_diff_combines_final_tracked_and_untracked_state(self) -> None:
        for name, content in {
            "staged.txt": "staged initial\n",
            "mixed.txt": "mixed initial\n",
            "deleted.txt": "delete me\n",
        }.items():
            (self.root / name).write_text(content, encoding="utf-8")
        self._git("add", "--", "staged.txt", "mixed.txt", "deleted.txt")
        self._git("commit", "-qm", "workspace diff fixture")

        (self.root / "staged.txt").write_text("staged final\n", encoding="utf-8")
        (self.root / "mixed.txt").write_text("mixed staged\n", encoding="utf-8")
        self._git("add", "--", "staged.txt", "mixed.txt")
        (self.root / "mixed.txt").write_text("mixed working final\n", encoding="utf-8")
        (self.root / "deleted.txt").unlink()
        (self.root / "new.txt").write_text("untracked text\n", encoding="utf-8")

        status_before = subprocess.run(
            [shutil.which("git") or "git", "status", "--porcelain=v1", "-z"],
            cwd=self.root,
            check=True,
            capture_output=True,
        ).stdout
        index_before = (self.root / ".git" / "index").read_bytes()

        output = self.git.workspace_diff()

        self.assertIn("Tracked changes (HEAD vs working tree):", output)
        self.assertIn("staged.txt", output)
        self.assertIn("+staged final", output)
        self.assertIn("+mixed working final", output)
        self.assertIn("deleted.txt", output)
        self.assertIn("Untracked changes:", output)
        self.assertIn("new.txt", output)
        self.assertIn("+untracked text", output)
        self.assertNotIn("+mixed staged", output)
        self.assertNotIn("untracked text", self.git.diff())
        self.assertEqual(
            status_before,
            subprocess.run(
                [shutil.which("git") or "git", "status", "--porcelain=v1", "-z"],
                cwd=self.root,
                check=True,
                capture_output=True,
            ).stdout,
        )
        self.assertEqual(index_before, (self.root / ".git" / "index").read_bytes())

    def test_workspace_diff_untracked_paths_are_sorted_and_git_ignored(self) -> None:
        review = self.root / "review"
        review.mkdir()
        (review / "b.txt").write_text("b\n", encoding="utf-8")
        (review / "a file.txt").write_text("a\n", encoding="utf-8")
        (review / ".env").write_text("REVIEW_SECRET=hidden\n", encoding="utf-8")
        (review / "nested").mkdir()
        (review / "nested" / ".env").write_text(
            "NESTED_REVIEW_SECRET=hidden\n", encoding="utf-8"
        )
        (review / "private.pem").write_text("PRIVATE_REVIEW_KEY\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("review/ignored.txt\n", encoding="utf-8")
        (review / "ignored.txt").write_text("ignored content\n", encoding="utf-8")
        exclude = self.root / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("review/info-ignored.txt\n")
        (review / "info-ignored.txt").write_text(
            "info ignored content\n", encoding="utf-8"
        )

        output = self.git.workspace_diff(path="review")

        self.assertLess(output.index("a file.txt"), output.index("b.txt"))
        self.assertIn("+a\n", output)
        self.assertIn("+b\n", output)
        for hidden in (
            ".env",
            "REVIEW_SECRET",
            "NESTED_REVIEW_SECRET",
            "private.pem",
            "PRIVATE_REVIEW_KEY",
            "ignored.txt",
            "info-ignored.txt",
            "ignored content",
            "info ignored content",
        ):
            self.assertNotIn(hidden, output)

    @unittest.skipIf(os.name != "posix", "symlink and FIFO fixtures are POSIX-only")
    def test_workspace_diff_omits_symlink_special_binary_and_oversized_files(
        self,
    ) -> None:
        (self.root / "link.txt").symlink_to(self.root / "tracked.txt")
        os.mkfifo(self.root / "special.pipe")
        (self.root / "binary.bin").write_bytes(b"\x00x")
        (self.root / "invalid.txt").write_bytes(b"\xffx")
        (self.root / "large.txt").write_text("oversized-secret\n", encoding="utf-8")
        settings = Settings.create(self.root, max_file_size=4, max_output_size=10_000)

        output = GitReader(settings).workspace_diff()

        self.assertIn("symlink", output)
        self.assertIn("binary", output)
        self.assertIn("oversized", output)
        self.assertNotIn("special.pipe", output)
        for secret in ("oversized-secret",):
            self.assertNotIn(secret, output)

    def test_workspace_diff_has_safe_new_file_headers_and_a_strict_output_budget(
        self,
    ) -> None:
        (self.root / "-dash.txt").write_text("dash\n", encoding="utf-8")
        (self.root / ":colon.txt").write_text("colon", encoding="utf-8")
        newline_name = self.root / "line\nname.txt"
        newline_name.write_text("newline name\n", encoding="utf-8")
        output = self.git.workspace_diff()
        self.assertIn('b/"-dash.txt"', output)
        self.assertIn('b/":colon.txt"', output)
        self.assertIn("line\\nname.txt", output)
        self.assertIn("No newline at end of file", output)

        large = self.root / "large-untracked.txt"
        large.write_text("line\n" * 200, encoding="utf-8")
        bounded = GitReader(
            Settings.create(self.root, max_output_size=350)
        ).workspace_diff()
        self.assertLessEqual(len(bounded.encode("utf-8")), 350)
        self.assertIn("truncated", bounded)

        (self.root / "x").write_text("x\n", encoding="utf-8")
        tiny = GitReader(Settings.create(self.root, max_output_size=45)).workspace_diff(
            path="x"
        )
        self.assertLessEqual(len(tiny.encode("utf-8")), 45)

    def test_workspace_diff_reports_scan_budget_omissions(self) -> None:
        for name in ("one.txt", "two.txt"):
            (self.root / name).write_text(name, encoding="utf-8")

        output = GitReader(
            Settings.create(self.root, max_scan_entries=1)
        ).workspace_diff()

        self.assertIn("scan limit", output)

    def test_workspace_diff_supports_unborn_heads_and_empty_results(self) -> None:
        self.assertEqual(self.git.workspace_diff(), "(no output)")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                [shutil.which("git") or "git", "init", "-q"],
                cwd=root,
                check=True,
            )
            (root / "staged.txt").write_text("staged unborn\n", encoding="utf-8")
            subprocess.run(
                [shutil.which("git") or "git", "add", "--", "staged.txt"],
                cwd=root,
                check=True,
            )
            (root / "new.txt").write_text("new unborn\n", encoding="utf-8")
            output = GitReader(Settings.create(root)).workspace_diff()

        self.assertIn("relative to empty HEAD", output)
        self.assertIn("staged unborn", output)
        self.assertIn("new unborn", output)

    def test_expanded_git_grammar_and_literal_paths(self) -> None:
        dash = self.root / "-dash.txt"
        colon = self.root / ":colon.txt"
        dash.write_text("before\n", encoding="utf-8")
        colon.write_text("before\n", encoding="utf-8")
        self._git("add", "--", "-dash.txt", "./:colon.txt")
        self._git("commit", "-qm", "add unusual names")
        dash.write_text("after dash\n", encoding="utf-8")
        colon.write_text("after colon\n", encoding="utf-8")

        allowed = {
            "git status": "-dash.txt",
            "git status --short": "-dash.txt",
            "git status --porcelain": "-dash.txt",
            "git diff": "after dash",
            "git diff -- -dash.txt": "after dash",
            "git diff -- ':colon.txt'": "after colon",
            "git diff --cached": "(no output)",
            "git diff --staged": "(no output)",
            "git log": "add unusual names",
            "git log --oneline": "add unusual names",
            "git log -n 1": "add unusual names",
            "git log --oneline -n 1": "add unusual names",
            "git show HEAD": "add unusual names",
            "git show HEAD -- -dash.txt": "before",
            "git branch": "* ",
            "git branch --show-current": "master",
            "git rev-parse HEAD": "\n",
            "git rev-parse --show-toplevel": str(self.root),
            "git ls-files": "tracked.txt",
        }
        for command, expected in allowed.items():
            with self.subTest(command=command):
                output = self.computer.run_shell(command)
                if command == "git branch --show-current":
                    self.assertTrue(output.strip())
                else:
                    self.assertIn(expected, output)

        porcelain = self.computer.run_shell("git status --porcelain")
        self.assertNotIn("##", porcelain)

    def test_git_history_and_pathspecs_cannot_bypass_blocked_policy(self) -> None:
        (self.root / ".env").write_text("HISTORICAL_SECRET=value\n", encoding="utf-8")
        (self.root / "visible.txt").write_text("visible history\n", encoding="utf-8")
        self._git("add", ".env", "visible.txt")
        self._git("commit", "-qm", "history policy fixture")

        shown = self.computer.run_shell("git show HEAD")
        self.assertIn("visible history", shown)
        self.assertNotIn("HISTORICAL_SECRET", shown)
        self.assertNotIn(".env", self.computer.run_shell("git ls-files"))
        blob = subprocess.run(
            [shutil.which("git") or "git", "rev-parse", "HEAD:.env"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with self.assertRaisesRegex(GitError, "exit code"):
            self.computer.run_shell(f"git show {blob}")

        rejected = (
            "git show HEAD:.env",
            "git show :path",
            "git show HEAD^",
            "git show HEAD~1",
            "git show HEAD@{1}",
            "git show HEAD..HEAD",
            "git show HEAD -- .env",
            "git diff --no-index",
            "git diff --stat",
            "git status --ignored",
            "git ls-files --stage",
        )
        for command in rejected:
            with (
                self.subTest(command=command),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                self.computer.run_shell(command)

    def test_native_git_tools_share_the_allowlisted_reader_contract(self) -> None:
        (self.root / "tracked.txt").write_text("after\n", encoding="utf-8")
        server = create_server(self.settings)
        by_name = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(
            set(by_name["git_status"].input_schema["properties"]), {"style"}
        )
        self.assertFalse(by_name["git_status"].input_schema["additionalProperties"])
        self.assertEqual(
            set(by_name["git_diff"].input_schema["properties"]),
            {"staged", "path"},
        )
        self.assertEqual(
            set(by_name["workspace_diff"].input_schema["properties"]),
            {"path"},
        )
        self.assertFalse(by_name["workspace_diff"].input_schema["additionalProperties"])

        async def exercise() -> None:
            calls: tuple[
                tuple[str, dict[str, str | int | bool], str], ...
            ] = (
                ("git_status", {"style": "porcelain"}, "tracked.txt"),
                ("git_diff", {"path": "tracked.txt"}, "after"),
                ("workspace_diff", {}, "Tracked changes"),
                ("git_log", {"count": 1, "oneline": True}, "initial"),
                ("git_show", {"commit": "HEAD"}, "initial"),
                ("git_branch", {"show_current": True}, ""),
                ("git_rev_parse", {"query": "HEAD"}, ""),
                ("git_ls_files", {}, "tracked.txt"),
            )
            for name, arguments, expected in calls:
                with self.subTest(tool=name):
                    result = require_call_tool_result(
                        await server.call_tool(name, arguments)
                    )
                    self.assertFalse(result.is_error)
                    if expected:
                        self.assertTrue(result.content)
                        self.assertIn(
                            expected,
                            require_text_content(result.content[0]).text,
                        )
            with self.assertRaisesRegex(ValueError, "unexpected argument"):
                await server.call_tool("git_show", {"commit": "HEAD", "raw": True})
            with self.assertRaisesRegex(ValueError, "unexpected argument"):
                await server.call_tool("workspace_diff", {"raw": True})

        asyncio.run(exercise())

    def test_compatibility_commands_cannot_bypass_blocked_policy(self) -> None:
        (self.root / ".env").write_text("SECRET=value", encoding="utf-8")
        commands = (
            "cat .env",
            "head .env",
            "tail .env",
            "grep SECRET .env",
            "rg SECRET .env",
            "wc -c .env",
            "sed -n 1p .env",
            "ls .git",
            "tree .git",
            "find .git",
        )
        for command in commands:
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(ValueError, "blocked"),
            ):
                self.computer.run_shell(command)


if __name__ == "__main__":
    unittest.main()
