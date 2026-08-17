"""Bounded, public-safe result adapters for structured execution tools."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from .pytest_debug_plugin import DEBUG_MARKER

MAX_DIAGNOSTICS = 1000
MAX_FAILURES = 20
MAX_FRAMES = 50
MAX_LOCALS = 100
MAX_DIAGNOSTIC_TEXT_BYTES = 4096
_MYpy_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>[0-9]+):(?P<column>[0-9]+): "
    r"(?P<severity>error|warning|note): (?P<message>.*)$"
)
_COVERAGE_MARKER = "SWMCP_COVERAGE:"


class DiagnosticsParseError(ValueError):
    """Raised when a tool did not produce its promised machine-readable output."""


def parse_ruff_diagnostics(stdout: str) -> tuple[list[dict[str, object]], bool]:
    payload = _parse_json(stdout, "ruff")
    if not isinstance(payload, list):
        raise DiagnosticsParseError("ruff diagnostics must be a JSON array")
    truncated = len(payload) > MAX_DIAGNOSTICS
    diagnostics: list[dict[str, object]] = []
    for item in payload[:MAX_DIAGNOSTICS]:
        if not isinstance(item, dict):
            raise DiagnosticsParseError("ruff diagnostic entry must be an object")
        location = item.get("location")
        if not isinstance(location, dict):
            raise DiagnosticsParseError("ruff diagnostic location is invalid")
        diagnostics.append(
            {
                "path": _public_path(item.get("filename")),
                "line": _positive_int(location.get("row"), "ruff line"),
                "column": _positive_int(location.get("column"), "ruff column"),
                "end_line": _positive_int(
                    location.get("end_row", location.get("row")), "ruff end line"
                ),
                "end_column": _positive_int(
                    location.get("end_column", location.get("column")),
                    "ruff end column",
                ),
                "code": _bounded_text(item.get("code"), "ruff code", 128),
                "message": _bounded_text(
                    item.get("message"), "ruff message", MAX_DIAGNOSTIC_TEXT_BYTES
                ),
                "fixable": item.get("fix") is not None,
            }
        )
    return diagnostics, truncated


def parse_mypy_diagnostics(stdout: str) -> tuple[list[dict[str, object]], bool]:
    diagnostics: list[dict[str, object]] = []
    malformed = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Success: no issues found"):
            continue
        match = _MYpy_LINE.match(raw_line)
        if match is None:
            malformed = True
            continue
        code = match.group("message")
        code_value: str | None = None
        code_match = re.search(r"\s+\[([^\]]+)\]$", code)
        if code_match:
            code_value = code_match.group(1)
            code = code[: code_match.start()].rstrip()
        diagnostics.append(
            {
                "path": _public_path(match.group("path")),
                "line": int(match.group("line")),
                "column": int(match.group("column")),
                "severity": match.group("severity"),
                "code": code_value,
                "message": _bounded_text(
                    code, "mypy message", MAX_DIAGNOSTIC_TEXT_BYTES
                ),
            }
        )
    if malformed and not diagnostics:
        raise DiagnosticsParseError("mypy diagnostics were not parseable")
    if malformed:
        raise DiagnosticsParseError("mypy diagnostics contained an invalid line")
    return diagnostics[:MAX_DIAGNOSTICS], len(diagnostics) > MAX_DIAGNOSTICS


def adapt_ruff_result(result: dict[str, object]) -> dict[str, object]:
    return _adapt_diagnostics_result(result, parse_ruff_diagnostics, "ruff")


def adapt_mypy_result(result: dict[str, object]) -> dict[str, object]:
    return _adapt_diagnostics_result(result, parse_mypy_diagnostics, "mypy")


def adapt_pytest_result(result: dict[str, object]) -> dict[str, object]:
    adapted = dict(result)
    stdout = result.get("stdout", "")
    if not isinstance(stdout, str):
        stdout = ""
    marker_line = _marked_line(stdout, DEBUG_MARKER)
    adapted["failures"] = []
    adapted["failures_truncated"] = False
    adapted["frames_truncated"] = False
    adapted["locals_truncated"] = False
    if marker_line is None:
        if result.get("status") == "failed" and result.get("exit_code") not in {
            None,
            127,
        }:
            adapted["failure_inspection_error"] = (
                "pytest failure collector output was unavailable"
            )
        return adapted
    adapted["stdout"] = _remove_marked_line(stdout, DEBUG_MARKER)
    try:
        payload = _parse_payload(marker_line, "pytest failure collector")
        failures = payload.get("failures") if isinstance(payload, dict) else None
        if not isinstance(failures, list):
            raise DiagnosticsParseError("pytest failures must be an array")
        adapted["failures"] = _sanitize_failures(failures[:MAX_FAILURES])
        adapted["failures_truncated"] = (
            bool(payload.get("failures_truncated", False))
            or len(failures) > MAX_FAILURES
        )
        adapted["frames_truncated"] = bool(payload.get("frames_truncated", False))
        adapted["locals_truncated"] = bool(payload.get("locals_truncated", False))
    except DiagnosticsParseError:
        adapted["failure_inspection_error"] = (
            "pytest failure collector output was malformed"
        )
    return adapted


def adapt_coverage_result(result: dict[str, object]) -> dict[str, object]:
    adapted = dict(result)
    stdout = result.get("stdout", "")
    if not isinstance(stdout, str):
        stdout = ""
    marker_line = _marked_line(stdout, _COVERAGE_MARKER)
    if marker_line is None:
        adapted["tests"] = {"exit_code": result.get("exit_code")}
        adapted["coverage"] = None
        adapted["coverage_parser_error"] = "coverage summary output was unavailable"
        return adapted
    adapted["stdout"] = _remove_marked_line(stdout, _COVERAGE_MARKER)
    try:
        payload = _parse_payload(marker_line, "coverage")
        coverage = payload.get("coverage")
        if not isinstance(coverage, dict) or "percent" not in coverage:
            raise DiagnosticsParseError("coverage summary is invalid")
        adapted["tests"] = {"exit_code": payload.get("tests_exit_code")}
        adapted["coverage"] = _sanitize_coverage(coverage)
        if payload.get("error"):
            adapted["coverage_parser_error"] = "coverage report unavailable"
    except DiagnosticsParseError:
        adapted["tests"] = {"exit_code": result.get("exit_code")}
        adapted["coverage"] = None
        adapted["coverage_parser_error"] = "coverage summary output was malformed"
    return adapted


def capability_result(result: dict[str, object]) -> dict[str, object]:
    """Classify a missing image executable/package without exposing internals."""

    adapted = dict(result)
    if result.get("status") == "failed" and result.get("exit_code") == 127:
        adapted["status"] = "capability_unavailable"
        adapted["capability_error"] = (
            "the selected execution image does not provide the requested capability"
        )
    return adapted


def _adapt_diagnostics_result(
    result: dict[str, object], parser: Any, tool: str
) -> dict[str, object]:
    adapted = capability_result(result)
    stdout = result.get("stdout", "")
    if not isinstance(stdout, str):
        stdout = ""
    try:
        diagnostics, truncated = parser(stdout)
        adapted["diagnostics"] = diagnostics
        adapted["diagnostics_truncated"] = truncated
    except DiagnosticsParseError:
        adapted["diagnostics"] = []
        adapted["diagnostics_truncated"] = False
        adapted["diagnostics_parser_error"] = f"{tool} diagnostics output was malformed"
    return adapted


def _parse_json(text: str, tool: str) -> Any:
    if len(text.encode("utf-8")) > 10 * 1024 * 1024:
        raise DiagnosticsParseError(f"{tool} diagnostics exceed the parser budget")
    try:
        return json.loads(text or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise DiagnosticsParseError(f"{tool} diagnostics are not valid JSON") from exc


def _parse_payload(line: str, tool: str) -> dict[str, Any]:
    marker, _, payload = line.partition(":")
    if not marker or not payload:
        raise DiagnosticsParseError(f"{tool} payload is empty")
    value = _parse_json(payload, tool)
    if not isinstance(value, dict):
        raise DiagnosticsParseError(f"{tool} payload must be an object")
    return value


def _marked_line(text: str, marker: str) -> str | None:
    for line in reversed(text.splitlines()):
        if line.startswith(marker):
            return line
    return None


def _remove_marked_line(text: str, marker: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith(marker))


def _public_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DiagnosticsParseError("diagnostic path is invalid")
    path = PurePosixPath(value)
    if value.startswith("/workspace/"):
        value = value[len("/workspace/") :]
        path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise DiagnosticsParseError("diagnostic path is not workspace-relative")
    return path.as_posix() or "."


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise DiagnosticsParseError(f"{label} is invalid")
    return value


def _bounded_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DiagnosticsParseError(f"{label} is invalid")
    encoded = value.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _sanitize_failures(failures: list[object]) -> list[dict[str, object]]:
    sanitized: list[dict[str, object]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        exception = failure.get("exception")
        if not isinstance(exception, dict):
            exception = {}
        frames: list[dict[str, object]] = []
        raw_frames = failure.get("frames")
        if isinstance(raw_frames, list):
            for frame in raw_frames[:MAX_FRAMES]:
                if not isinstance(frame, dict):
                    continue
                try:
                    path = _public_path(frame.get("path"))
                except DiagnosticsParseError:
                    path = "<external>"
                frames.append(
                    {
                        "path": path,
                        "line": (
                            frame.get("line") if type(frame.get("line")) is int else 0
                        ),
                        "function": _bounded_text(
                            frame.get("function", "<unknown>"), "frame function", 128
                        ),
                        "source": _bounded_text(
                            frame.get("source", ""), "frame source", 512
                        ),
                        "locals": _sanitize_locals(frame.get("locals")),
                    }
                )
        sanitized.append(
            {
                "node_id": _bounded_text(
                    failure.get("node_id", "<unknown>"), "failure node id", 1024
                ),
                "exception": {
                    "type": _bounded_text(
                        exception.get("type", "Exception"), "exception type", 128
                    ),
                    "message": _bounded_text(
                        exception.get("message", ""),
                        "exception message",
                        MAX_DIAGNOSTIC_TEXT_BYTES,
                    ),
                },
                "frames": frames,
            }
        )
    return sanitized


def _sanitize_locals(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, object]] = []
    for local in value[:MAX_LOCALS]:
        if not isinstance(local, dict):
            continue
        redacted = bool(local.get("redacted", False))
        rendered = (
            "<redacted>"
            if redacted
            else _bounded_text(local.get("repr", ""), "local repr", 512)
        )
        sanitized.append(
            {
                "name": _bounded_text(local.get("name", ""), "local name", 128),
                "type": _bounded_text(local.get("type", "object"), "local type", 128),
                "repr": rendered,
                "truncated": bool(local.get("truncated", False)),
                "redacted": redacted,
            }
        )
    return sanitized


def _coverage_number(value: object, *, integer: bool) -> float | int:
    if not isinstance(value, (int, float, str)):
        raise TypeError("coverage total is not numeric")
    return int(value) if integer else float(value)


def _coverage_totals(
    value: dict[str, object], error_message: str
) -> dict[str, object]:
    try:
        return {
            "percent": _coverage_number(value.get("percent", 0.0), integer=False),
            "covered": _coverage_number(value.get("covered", 0), integer=True),
            "missing": _coverage_number(value.get("missing", 0), integer=True),
        }
    except (TypeError, ValueError) as exc:
        raise DiagnosticsParseError(error_message) from exc


def _sanitize_coverage(value: dict[str, object]) -> dict[str, object]:
    summary = _coverage_totals(value, "coverage totals are invalid")
    branches = value.get("branches")
    if isinstance(branches, dict):
        summary["branches"] = _coverage_totals(
            branches, "coverage branch totals are invalid"
        )
    if value.get("fail_under_failed"):
        summary["fail_under_failed"] = True
    return summary
