"""Build the server-controlled pytest failure collector used in snapshots."""

from __future__ import annotations

DEBUG_PLUGIN_FILENAME = "sandboxed_workspace_mcp_debug_plugin.py"
DEBUG_PLUGIN_MODULE = "sandboxed_workspace_mcp_debug_plugin"
DEBUG_MARKER = "SWMCP_FAILURES:"

_PLUGIN_SOURCE = r"""\
import json
import linecache
import sys
from pathlib import Path

SHOW_LOCALS = __SHOW_LOCALS__
OUTPUT_LIMIT = __OUTPUT_LIMIT__
MARKER = "SWMCP_FAILURES:"
MAX_FAILURES = 20
MAX_FRAMES = 50
MAX_LOCALS = 100
MAX_TEXT_BYTES = 512
SECRET_NAMES = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)


def _text(value, limit=MAX_TEXT_BYTES):
    try:
        rendered = str(value)
    except BaseException as exc:
        rendered = "<text failed: " + type(exc).__name__ + ">"
    encoded = rendered.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return rendered, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _repr(value):
    try:
        rendered = repr(value)
    except BaseException as exc:
        return "<repr failed: " + type(exc).__name__ + ">", False
    encoded = rendered.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_TEXT_BYTES:
        return rendered, False
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), True


def _local(name, value):
    local_name, name_truncated = _text(name, 128)
    try:
        type_name = type(value).__name__
    except BaseException:
        type_name = "object"
    type_name, _ = _text(type_name, 128)
    lowered = local_name.casefold()
    redacted = any(secret in lowered for secret in SECRET_NAMES)
    if redacted:
        rendered, truncated = "<redacted>", False
    else:
        rendered, truncated = _repr(value)
    return {
        "name": local_name,
        "type": type_name,
        "repr": rendered,
        "truncated": bool(truncated or name_truncated),
        "redacted": redacted,
    }


def _frame(entry):
    raw_path = str(getattr(entry, "path", ""))
    workspace_prefix = "/workspace/"
    if raw_path.startswith(workspace_prefix):
        public_path = raw_path[len(workspace_prefix):]
        is_workspace = True
    else:
        public_path = "<external>"
        is_workspace = False
    try:
        line = int(entry.lineno) + 1
    except BaseException:
        line = 0
    try:
        function = str(entry.name)
    except BaseException:
        function = "<unknown>"
    function, _ = _text(function, 128)
    source = ""
    if is_workspace and line > 0:
        source, _ = _text(linecache.getline(raw_path, line).strip(), MAX_TEXT_BYTES)
    locals_value = []
    locals_truncated = False
    if SHOW_LOCALS and is_workspace:
        try:
            values = list(entry.frame.f_locals.items())
        except BaseException:
            values = []
        for name, value in values[:MAX_LOCALS]:
            locals_value.append(_local(name, value))
        locals_truncated = len(values) > MAX_LOCALS
    return {
        "path": public_path,
        "line": line,
        "function": function,
        "source": source,
        "locals": locals_value,
        "locals_truncated": locals_truncated,
        "_workspace": is_workspace,
    }


class Collector:
    def __init__(self):
        self.failures = []
        self.failures_truncated = False
        self.frames_truncated = False
        self.locals_truncated = False

    def add(self, item, when):
        if len(self.failures) >= MAX_FAILURES:
            self.failures_truncated = True
            return
        excinfo = getattr(item, "_swmcp_excinfo", None)
        if excinfo is None:
            return
        try:
            message = str(excinfo.value)
        except BaseException:
            message = "<exception message unavailable>"
        message, _ = _text(message)
        try:
            exception_type = excinfo.type.__name__
        except BaseException:
            exception_type = "Exception"
        frames = []
        try:
            traceback = list(excinfo.traceback)
        except BaseException:
            traceback = []
        workspace_frames = []
        external_frames = []
        for entry in traceback:
            rendered = _frame(entry)
            if rendered.pop("_workspace"):
                workspace_frames.append(rendered)
            else:
                external_frames.append(rendered)
        if workspace_frames:
            selected = workspace_frames
        else:
            selected = external_frames[:3]
        if len(selected) > MAX_FRAMES:
            self.frames_truncated = True
            selected = selected[:MAX_FRAMES]
        if any(frame["locals_truncated"] for frame in selected):
            self.locals_truncated = True
        for frame in selected:
            frame.pop("locals_truncated", None)
        self.failures.append({
            "node_id": _text(getattr(item, "nodeid", "<unknown>"), 1024)[0],
            "when": when,
            "exception": {"type": exception_type, "message": message},
            "frames": frames + selected,
        })

    def emit(self):
        payload = {
            "failures": self.failures,
            "failures_truncated": self.failures_truncated,
            "frames_truncated": self.frames_truncated,
            "locals_truncated": self.locals_truncated,
        }
        for failure in payload["failures"]:
            for frame in failure["frames"]:
                frame.pop("locals", None) if not SHOW_LOCALS else None
        rendered = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(rendered.encode("utf-8")) > OUTPUT_LIMIT:
            payload["locals_truncated"] = True
            for failure in payload["failures"]:
                for frame in failure["frames"]:
                    frame["locals"] = []
            rendered = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        while len(rendered.encode("utf-8")) > OUTPUT_LIMIT and payload["failures"]:
            payload["failures_truncated"] = True
            payload["failures"].pop()
            rendered = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(rendered.encode("utf-8")) > OUTPUT_LIMIT:
            payload["failures"] = []
            payload["failures_truncated"] = True
            rendered = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        print("", file=sys.stdout, flush=True)
        print(MARKER + rendered, file=sys.stdout, flush=True)


collector = Collector()


def pytest_runtest_makereport(item, call):
    if call.excinfo is not None:
        item._swmcp_excinfo = call.excinfo
    if call.excinfo is not None:
        report = getattr(call, "when", "call")
        if report in {"setup", "call", "teardown"}:
            collector.add(item, report)


def pytest_sessionfinish(session, exitstatus):
    collector.emit()
"""


def build_pytest_debug_plugin_source(*, show_locals: bool, output_limit: int) -> str:
    """Render only server-controlled constants into the injected plugin."""

    safe_limit = max(256, min(output_limit, 128 * 1024))
    return _PLUGIN_SOURCE.replace(
        "__SHOW_LOCALS__", "True" if show_locals else "False"
    ).replace("__OUTPUT_LIMIT__", str(safe_limit))
