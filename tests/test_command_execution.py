from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workspace_guard_mcp.command_execution import (
    CommandCompiler,
    CommandExecutionError,
)
from workspace_guard_mcp.config import Settings


class CommandCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / ".venv").mkdir()
        (self.root / ".env").mkdir()
        self.compiler = CommandCompiler(Settings.create(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_program_args_and_cwd_compile_without_shell_or_host_lookup(self) -> None:
        root = self.compiler.compile("missing-in-host-path", ["--check", ""], ".")
        nested = self.compiler.compile(
            "ruff",
            ["check", "--fix", "."],
            "src",
        )

        self.assertEqual(root.argv, ("missing-in-host-path", "--check", ""))
        self.assertEqual(root.workdir, "/workspace")
        self.assertEqual(nested.argv, ("ruff", "check", "--fix", "."))
        self.assertEqual(nested.workdir, "/workspace/src")
        self.assertEqual(self.compiler.compile("python").argv, ("python",))
        self.assertEqual(
            self.compiler.compile("sh", ["-c", "printf ok"]).argv,
            ("sh", "-c", "printf ok"),
        )

    def test_invalid_programs_are_rejected(self) -> None:
        invalid = (
            "",
            ".",
            "..",
            "python\x00bad",
            "/usr/bin/python",
            "bin/python",
            "bin\\python",
            "x" * 257,
            None,
        )
        for program in invalid:
            with (
                self.subTest(program=program),
                self.assertRaises(CommandExecutionError),
            ):
                self.compiler.compile(program)  # type: ignore[arg-type]

    def test_invalid_args_are_rejected_and_leading_dashes_are_preserved(self) -> None:
        self.assertEqual(
            self.compiler.compile("tool", ["--network=host", "-c"]).argv,
            ("tool", "--network=host", "-c"),
        )
        invalid = (
            ("not-an-array", "array"),
            (["ok", 1], "must be a string"),
            (["x"] * 129, "at most 128"),
            (["x" * 4097], "4096"),
            (["x\x00y"], "NUL"),
            (["x" * 4096] * 9, "total"),
        )
        for args, message in invalid:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(CommandExecutionError, message),
            ):
                self.compiler.compile("tool", args)  # type: ignore[arg-type]

    def test_cwd_rejects_escape_links_blocked_ignored_files_and_bad_syntax(
        self,
    ) -> None:
        link_path = self.root / "linked-src"
        try:
            link_path.symlink_to(self.root / "src", target_is_directory=True)
        except (OSError, NotImplementedError):
            link: Path | None = None
        else:
            link = link_path

        invalid = [
            "",
            "../outside",
            str(self.root / "src"),
            "/tmp",
            "~",
            "C:/workspace",
            "src\\nested",
            "missing",
            "src/app.py",
            ".venv",
            ".env",
            "x" * 1025,
            "bad\x00cwd",
            None,
        ]
        if link is not None:
            invalid.append("linked-src")
        for cwd in invalid:
            with self.subTest(cwd=cwd), self.assertRaises(CommandExecutionError):
                self.compiler.compile("python", cwd=cwd)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
