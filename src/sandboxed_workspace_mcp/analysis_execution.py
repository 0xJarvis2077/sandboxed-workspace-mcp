"""Server-generated helpers for one-shot static analysis executions."""

from __future__ import annotations

_COVERAGE_HARNESS = r"""
import json
import pathlib
import sys

import coverage
import pytest

BRANCH = __BRANCH__
FAIL_UNDER = __FAIL_UNDER__
MARKER = "SWMCP_COVERAGE:"

pytest_args = sys.argv[1:]
if pytest_args[:1] == ["--"]:
    pytest_args = pytest_args[1:]

cov = coverage.Coverage(
    branch=BRANCH,
    data_file="/tmp/.coverage",
    source=["."],
)
test_exit_code = 1
coverage_error = None
try:
    cov.start()
    test_exit_code = int(pytest.main(pytest_args))
finally:
    try:
        cov.stop()
        cov.save()
    except BaseException as exc:
        coverage_error = f"{type(exc).__name__}: {exc}"

summary = {}
if coverage_error is None:
    try:
        report_path = pathlib.Path("/tmp/sandboxed-workspace-mcp-coverage.json")
        cov.json_report(outfile=str(report_path))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        totals = report.get("totals", {})
        statements = int(totals.get("num_statements", 0))
        covered = int(totals.get("covered_lines", 0))
        summary = {
            "percent": round(float(totals.get("percent_covered", 0.0)), 2),
            "covered": covered,
            "missing": max(0, statements - covered),
        }
        if BRANCH:
            branches = int(totals.get("num_branches", 0))
            covered_branches = int(totals.get("covered_branches", 0))
            branch_percent = float(
                totals.get(
                    "percent_covered_branches",
                    100.0 * covered_branches / branches if branches else 100.0,
                )
            )
            summary["branches"] = {
                "percent": round(branch_percent, 2),
                "covered": covered_branches,
                "missing": max(0, branches - covered_branches),
            }
        if FAIL_UNDER is not None and summary["percent"] < FAIL_UNDER:
            test_exit_code = test_exit_code or 1
            summary["fail_under_failed"] = True
    except BaseException:
        coverage_error = "coverage report unavailable"

if coverage_error is not None:
    test_exit_code = test_exit_code or 1
payload = {
    "tests_exit_code": test_exit_code,
    "coverage": summary,
}
if coverage_error is not None:
    payload["error"] = coverage_error
print(MARKER + json.dumps(payload, separators=(",", ":")), flush=True)
raise SystemExit(test_exit_code)
"""


def coverage_harness(branch: bool, fail_under: float | None) -> str:
    """Return fixed Python source for a single pytest + coverage execution."""

    branch_literal = "True" if branch else "False"
    threshold_literal = repr(fail_under) if fail_under is not None else "None"
    return _COVERAGE_HARNESS.replace("__BRANCH__", branch_literal).replace(
        "__FAIL_UNDER__", threshold_literal
    )
