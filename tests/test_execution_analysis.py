from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict

from workspace_guard_mcp.analysis_execution import coverage_harness
from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.diagnostics import (
    adapt_coverage_result,
    adapt_mypy_result,
    adapt_pytest_result,
    adapt_ruff_result,
    parse_mypy_diagnostics,
    parse_ruff_diagnostics,
)
from workspace_guard_mcp.python_execution import PythonCommandCompiler


class _BranchCoverage(TypedDict):
    missing: int


class _CoverageSummary(TypedDict, total=False):
    percent: float
    missing: int
    branches: _BranchCoverage
    fail_under_failed: bool


class _CoveragePayload(TypedDict):
    tests_exit_code: int
    coverage: _CoverageSummary


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
        self.assertIn("workspace_guard_mcp_debug_plugin", pytest_argv)
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


@unittest.skipUnless(
    importlib.util.find_spec("pytest") is not None,
    "pytest is required for coverage harness integration tests",
)
class CoverageHarnessTests(unittest.TestCase):
    def test_pytest_and_fail_under_exit_codes_are_separate(self) -> None:
        cases = (
            ("passing tests and passing gate", False, False, 0, 0),
            ("passing tests and failing gate", True, False, 0, 1),
            ("failing tests and passing gate", False, True, 1, 0),
            ("failing tests and failing gate", True, True, 1, 1),
        )
        for (
            name,
            has_uncovered_code,
            tests_fail,
            expected_tests,
            expected_gate,
        ) in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self._write_project(
                        root,
                        has_uncovered_code=has_uncovered_code,
                        tests_fail=tests_fail,
                    )
                    return_code, payload = self._run_harness(root, fail_under=80)

                self.assertEqual(payload["tests_exit_code"], expected_tests)
                coverage = payload["coverage"]
                self.assertIsInstance(coverage, dict)
                self.assertEqual(
                    coverage.get("fail_under_failed", False), bool(expected_gate)
                )
                self.assertEqual(
                    return_code == 0, not expected_tests and not expected_gate
                )

    def test_project_source_configuration_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_project(root, has_uncovered_code=False, tests_fail=False)
            (root / "tests" / "not_business_code.py").write_text(
                "def never_called() -> str:\n    return 'uncovered'\n",
                encoding="utf-8",
            )
            return_code, payload = self._run_harness(root)

            self.assertEqual(return_code, 0)
            self.assertEqual(payload["tests_exit_code"], 0)
            self.assertEqual(payload["coverage"]["percent"], 100.0)
            self.assertEqual(payload["coverage"]["missing"], 0)
            self._assert_workspace_is_free_of_coverage_artifacts(root)

    def test_project_branch_configuration_is_honored_when_api_branch_is_false(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_branch_project(root, branch_in_config=True)
            return_code, payload = self._run_harness(root, branch=False)

            self.assertEqual(return_code, 0)
            branches = payload["coverage"].get("branches")
            assert branches is not None
            self.assertGreater(branches["missing"], 0)

    def test_api_branch_true_enables_branch_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_branch_project(root, branch_in_config=False)
            return_code, payload = self._run_harness(root, branch=True)

            self.assertEqual(return_code, 0)
            branches = payload["coverage"].get("branches")
            assert branches is not None
            self.assertGreater(branches["missing"], 0)

    @staticmethod
    def _write_project(
        root: Path, *, has_uncovered_code: bool, tests_fail: bool
    ) -> None:
        (root / "src").mkdir()
        (root / "tests").mkdir()
        source = "def covered() -> int:\n    return 1\n"
        if has_uncovered_code:
            source += "\ndef uncovered() -> int:\n    return 2\n"
        (root / "src" / "pkg.py").write_text(source, encoding="utf-8")
        test_source = (
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
            "from pkg import covered\n\n"
            "def test_covered() -> None:\n"
            "    assert covered() == 1\n"
        )
        if tests_fail:
            test_source += "    assert False\n"
        (root / "tests" / "test_pkg.py").write_text(test_source, encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[tool.coverage.run]\nsource = ['src']\n", encoding="utf-8"
        )

    @staticmethod
    def _write_branch_project(root: Path, *, branch_in_config: bool) -> None:
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "pkg.py").write_text(
            "def choose(value: bool) -> int:\n"
            "    if value:\n"
            "        return 1\n"
            "    return 2\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_pkg.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
            "from pkg import choose\n\n"
            "def test_choose() -> None:\n"
            "    assert choose(True) == 1\n",
            encoding="utf-8",
        )
        branch = "branch = true\n" if branch_in_config else ""
        (root / "pyproject.toml").write_text(
            "[tool.coverage.run]\nsource = ['src']\n" + branch,
            encoding="utf-8",
        )

    @staticmethod
    def _run_harness(
        root: Path, *, branch: bool = False, fail_under: float | None = None
    ) -> tuple[int, _CoveragePayload]:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                coverage_harness(branch=branch, fail_under=fail_under),
                "--",
                "tests",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        marker = next(
            (
                line
                for line in result.stdout.splitlines()
                if line.startswith("SWMCP_COVERAGE:")
            ),
            None,
        )
        if marker is None:
            raise AssertionError(
                f"coverage marker missing; stdout={result.stdout!r}, "
                f"stderr={result.stderr!r}"
            )
        raw_payload = json.loads(marker.removeprefix("SWMCP_COVERAGE:"))
        assert isinstance(raw_payload, dict)
        tests_exit_code = raw_payload.get("tests_exit_code")
        raw_coverage = raw_payload.get("coverage")
        assert isinstance(tests_exit_code, int)
        assert isinstance(raw_coverage, dict)

        coverage: _CoverageSummary = {}
        percent = raw_coverage.get("percent")
        if percent is not None:
            assert isinstance(percent, float)
            coverage["percent"] = percent
        missing = raw_coverage.get("missing")
        if missing is not None:
            assert isinstance(missing, int)
            coverage["missing"] = missing
        fail_under_failed = raw_coverage.get("fail_under_failed")
        if fail_under_failed is not None:
            assert isinstance(fail_under_failed, bool)
            coverage["fail_under_failed"] = fail_under_failed
        raw_branches = raw_coverage.get("branches")
        if raw_branches is not None:
            assert isinstance(raw_branches, dict)
            branch_missing = raw_branches.get("missing")
            assert isinstance(branch_missing, int)
            coverage["branches"] = {"missing": branch_missing}

        payload: _CoveragePayload = {
            "tests_exit_code": tests_exit_code,
            "coverage": coverage,
        }
        return result.returncode, payload

    @staticmethod
    def _assert_workspace_is_free_of_coverage_artifacts(root: Path) -> None:
        for name in (".coverage", "coverage.json", "htmlcov"):
            assert not (root / name).exists(), name


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
        self.assertEqual(frame["locals"][0]["repr"], "<redacted>")
        self.assertEqual(frame["locals"][0]["redacted"], True)
        self.assertNotIn("super-secret", repr(result))
        self.assertEqual(failure["frames"][1]["path"], "<external>")
        self.assertNotIn("/Users/host", repr(result))
        stdout = result["stdout"]
        assert isinstance(stdout, str)
        self.assertNotIn("SWMCP_FAILURES", stdout)

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

    def test_coverage_gate_failure_does_not_become_a_test_failure(self) -> None:
        result = adapt_coverage_result(
            {
                "status": "failed",
                "exit_code": 1,
                "stdout": (
                    "SWMCP_COVERAGE:"
                    + json.dumps(
                        {
                            "tests_exit_code": 0,
                            "coverage": {
                                "percent": 79.0,
                                "covered": 79,
                                "missing": 21,
                                "fail_under_failed": True,
                            },
                        }
                    )
                ),
            }
        )
        self.assertEqual(result["tests"]["exit_code"], 0)  # type: ignore[index]
        self.assertTrue(result["coverage"]["fail_under_failed"])  # type: ignore[index]

    def test_coverage_numeric_strings_remain_accepted(self) -> None:
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
                                "percent": "91.2",
                                "covered": "10",
                                "missing": "1",
                            },
                        }
                    )
                ),
            }
        )

        coverage = result["coverage"]
        self.assertIsInstance(coverage, dict)
        if not isinstance(coverage, dict):
            self.fail("coverage result should be a mapping")
        self.assertEqual(coverage["percent"], 91.2)
        self.assertEqual(coverage["covered"], 10)
        self.assertEqual(coverage["missing"], 1)

    def test_invalid_coverage_numeric_payload_is_reported(self) -> None:
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
                                "percent": {},
                                "covered": 10,
                                "missing": 1,
                            },
                        }
                    )
                ),
            }
        )

        self.assertIsNone(result["coverage"])
        self.assertEqual(
            result["coverage_parser_error"], "coverage summary output was malformed"
        )

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
