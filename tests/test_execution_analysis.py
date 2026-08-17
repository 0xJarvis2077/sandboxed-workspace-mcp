from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.diagnostics import (
    adapt_coverage_result,
    adapt_mypy_result,
    adapt_pytest_result,
    adapt_ruff_result,
    parse_mypy_diagnostics,
    parse_ruff_diagnostics,
)
from sandboxed_workspace_mcp.python_execution import PythonCommandCompiler


class ExecutionCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "example.py").write_text("value = 1\n", encoding="utf-8")
        self.compiler = PythonCommandCompiler(Settings.create(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_structured_argv_is_server_generated_and_bounded(self) -> None:
        pytest_argv = self.compiler.pytest(
            targets=["src/example.py::test_value"],
            show_locals=True,
            max_failures=3,
            include_failure_plugin=True,
        )
        self.assertIn("sandboxed_workspace_mcp_debug_plugin", pytest_argv)
        self.assertIn("--showlocals", pytest_argv)
        self.assertIn("--maxfail=3", pytest_argv)

        self.assertEqual(
            self.compiler.ruff()[0:4],
            ("ruff", "check", "--output-format=json", "--"),
        )
        mypy_argv = self.compiler.mypy(paths=["src"], strict=True)
        self.assertIn("--strict", mypy_argv)
        self.assertEqual(mypy_argv[-2:], ("--", "src"))

        coverage_argv = self.compiler.pytest_coverage(
            targets=["src"], branch=True, fail_under=85
        )
        self.assertEqual(coverage_argv[:2], ("python", "-c"))
        self.assertIn("BRANCH = True", coverage_argv[2])
        self.assertIn("FAIL_UNDER = 85", coverage_argv[2])
        self.assertEqual(coverage_argv[-2:], ("--", "src"))

    def test_analysis_paths_and_pytest_debug_options_reject_unsafe_values(self) -> None:
        for paths in (["../outside"], [".venv"], ["x" * 1025], ["src"] * 33):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.compiler.ruff(paths=paths)
        self.assertIn("--fix", self.compiler.ruff(fix=True))
        for value in (0, -1, 21, True, "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.compiler.pytest(max_failures=value)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            self.compiler.pytest(exit_first=True, max_failures=3)
        for value in (-1, 101, True, "85"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.compiler.pytest_coverage(fail_under=value)  # type: ignore[arg-type]


class DiagnosticsTests(unittest.TestCase):
    def test_ruff_and_mypy_json_machine_results_are_stable(self) -> None:
        ruff = adapt_ruff_result(
            {
                "status": "failed",
                "exit_code": 1,
                "stdout": json.dumps(
                    [
                        {
                            "filename": "/workspace/src/example.py",
                            "location": {
                                "row": 10,
                                "column": 5,
                                "end_row": 10,
                                "end_column": 8,
                            },
                            "code": "F821",
                            "message": "Undefined name `foo`",
                            "fix": None,
                        }
                    ]
                ),
                "stderr": "",
                "truncated": False,
                "timed_out": False,
                "duration_ms": 1,
            }
        )
        self.assertEqual(ruff["diagnostics"][0]["path"], "src/example.py")  # type: ignore[index]
        self.assertFalse(ruff["diagnostics"][0]["fixable"])  # type: ignore[index]

        mypy = adapt_mypy_result(
            {
                "status": "failed",
                "exit_code": 1,
                "stdout": (
                    "src/example.py:12:4: error: Incompatible types [assignment]\n"
                ),
            }
        )
        self.assertEqual(mypy["diagnostics"][0]["code"], "assignment")  # type: ignore[index]
        self.assertEqual(mypy["diagnostics"][0]["column"], 4)  # type: ignore[index]

    def test_malformed_diagnostics_are_explicit_and_not_server_errors(self) -> None:
        for adapter in (adapt_ruff_result, adapt_mypy_result):
            result = adapter({"status": "failed", "exit_code": 1, "stdout": "not json"})
            self.assertEqual(result["diagnostics"], [])
            self.assertIn("parser_error", " ".join(result))

    def test_pytest_failure_result_redacts_and_hides_private_paths(self) -> None:
        payload = {
            "failures": [
                {
                    "node_id": "tests/test_example.py::test_failure",
                    "exception": {"type": "ValueError", "message": "invalid value"},
                    "frames": [
                        {
                            "path": "/workspace/src/example.py",
                            "line": 42,
                            "function": "calculate",
                            "source": "result = total / count",
                            "locals": [
                                {
                                    "name": "api_token",
                                    "type": "str",
                                    "repr": "super-secret",
                                    "redacted": True,
                                    "truncated": False,
                                },
                                {
                                    "name": "normal_value",
                                    "type": "int",
                                    "repr": "42",
                                    "redacted": False,
                                    "truncated": False,
                                },
                            ],
                        },
                        {
                            "path": "/Users/host/project/src/example.py",
                            "line": 3,
                            "function": "private",
                            "source": "",
                            "locals": [],
                        },
                    ],
                }
            ],
            "failures_truncated": False,
            "frames_truncated": False,
            "locals_truncated": False,
        }
        result = adapt_pytest_result(
            {
                "status": "failed",
                "exit_code": 1,
                "stdout": "trace\nSWMCP_FAILURES:" + json.dumps(payload) + "\n",
            }
        )
        failure = result["failures"][0]  # type: ignore[index]
        frame = failure["frames"][0]
        self.assertEqual(frame["path"], "src/example.py")
        self.assertEqual(frame["line"], 42)
        self.assertEqual(frame["locals"][0]["repr"], "super-secret")
        self.assertEqual(frame["locals"][0]["redacted"], True)
        self.assertEqual(failure["frames"][1]["path"], "<external>")
        self.assertNotIn("/Users/host", repr(result))
        self.assertNotIn("SWMCP_FAILURES", result["stdout"])

    def test_coverage_result_is_compact_and_one_shot(self) -> None:
        result = adapt_coverage_result(
            {
                "status": "succeeded",
                "exit_code": 0,
                "stdout": (
                    "SWMCP_COVERAGE:"
                    + json.dumps(
                        {
                            "tests_exit_code": 0,
                            "coverage": {
                                "percent": 91.4,
                                "covered": 1234,
                                "missing": 115,
                                "branches": {"percent": 88.2},
                            },
                        }
                    )
                ),
            }
        )
        self.assertEqual(result["tests"]["exit_code"], 0)  # type: ignore[index]
        self.assertEqual(result["coverage"]["percent"], 91.4)  # type: ignore[index]
        self.assertNotIn(".coverage", repr(result))

    def test_parser_rejects_host_paths_and_caps_diagnostics(self) -> None:
        with self.assertRaises(ValueError):
            parse_ruff_diagnostics(
                json.dumps(
                    [
                        {
                            "filename": "/Users/host/project/a.py",
                            "location": {"row": 1, "column": 1},
                            "code": "F401",
                            "message": "unused",
                        }
                    ]
                )
            )
        with self.assertRaises(ValueError):
            parse_mypy_diagnostics("/Users/host/a.py:1:1: error: bad")


if __name__ == "__main__":
    unittest.main()
