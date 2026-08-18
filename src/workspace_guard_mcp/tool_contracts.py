"""Typed MCP tool contracts, annotations, and presentation adapters.

This module is the single metadata owner for the public MCP tool surface.  The
server owns registration, authorization, conditional capability exposure, and
dispatch; this module owns descriptions, output models, annotations, and the
small adapters that turn existing safe-domain results into public structured
results.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, RootModel

from .result_cache import ResultCache
from .result_presentation import externalize_tool_payload


class PublicResultModel(BaseModel):
    """Base for stable public result objects with no undeclared fields."""

    model_config = ConfigDict(extra="forbid")


class ProjectInfoResult(PublicResultModel):
    workspace_root: str
    exists: bool
    writable: bool
    mode: Literal["read-only", "read-write"]
    blocked_patterns: int
    max_scan_entries: int
    max_search_bytes: int
    max_concurrent_searches: int
    trash_enabled: bool
    trash_purge_enabled: bool
    max_trash_items: int
    max_trash_bytes: int


class DirectoryListResult(PublicResultModel):
    path: str
    entries: list[str]
    diagnostics: list[str]
    truncated: bool


class TextPresentationResult(PublicResultModel):
    source_truncated: bool = False
    text_available_bytes: int = 0
    text_inline_bytes: int = 0
    text_inline_truncated: bool = False
    text_resource_uri: str | None = None


class ContentPresentationResult(PublicResultModel):
    source_truncated: bool = False
    content_available_bytes: int = 0
    content_inline_bytes: int = 0
    content_inline_truncated: bool = False
    content_resource_uri: str | None = None


class TreeResult(TextPresentationResult):
    path: str
    max_depth: int
    text: str
    truncated: bool


class FileContentResult(ContentPresentationResult):
    path: str
    content: str
    start_line: int
    end_line: int | None
    truncated: bool


class VersionedFileResult(ContentPresentationResult):
    path: str
    content: str
    sha256: str
    size: int
    mtime_ns: int


class DirectoryMutationResult(PublicResultModel):
    path: str
    ready: bool


class WriteFileResult(PublicResultModel):
    path: str
    written: bool
    characters: int
    bytes: int
    overwrite: bool


class ReplaceTextResult(PublicResultModel):
    path: str
    replacements: int


class AppendFileResult(PublicResultModel):
    path: str
    appended: bool
    characters: int
    bytes: int


class SearchMatch(PublicResultModel):
    path: str
    line: int
    text: str


class SearchResult(PublicResultModel):
    matches: list[SearchMatch]
    truncated: bool
    stop_reason: (
        Literal[
            "result_limit",
            "byte_budget",
            "time_budget",
            "scan_budget",
            "output_limit",
        ]
        | None
    ) = None
    diagnostics: list[str]


class GitStatusEntry(PublicResultModel):
    path: str
    index_status: str
    worktree_status: str


class GitStatusResult(TextPresentationResult):
    text: str
    clean: bool
    entries: list[GitStatusEntry]


class GitTextResult(TextPresentationResult):
    text: str
    truncated: bool


class GitRevisionFileResult(ContentPresentationResult):
    path: str
    commit: str
    blob: str
    content: str
    sha256: str
    size: int
    mode: str


class GitInitResult(PublicResultModel):
    status: Literal["initialized", "already_initialized"]
    repository: str
    initial_branch: str


class GitBaselineResult(PublicResultModel):
    status: Literal["created"]
    commit: str
    branch: str
    files: int
    bytes: int


class GitBranchResult(PublicResultModel):
    branches: list[str]
    current: str | None


class GitRevParseResult(PublicResultModel):
    query: str
    value: str


class GitFilesResult(PublicResultModel):
    files: list[str]


class TrashItemResult(PublicResultModel):
    trash_id: str
    original_path: str
    sha256: str
    size: int
    mtime_ns: int
    mode: int
    trashed_at: int


class TrashListResult(PublicResultModel):
    items: list[TrashItemResult]
    total: int
    offset: int
    limit: int
    has_more: bool
    diagnostics: list[str] | None = None


class RestoreResult(PublicResultModel):
    status: Literal["restored"]
    restored: bool
    trash_id: str
    original_path: str
    restored_path: str
    restored_to_original: bool
    sha256: str
    size: int


class PurgeResult(PublicResultModel):
    status: Literal["purged"]
    purged: bool
    trash_id: str
    original_path: str
    sha256: str
    size: int
    cleanup_pending: bool


class PublicErrorInfo(PublicResultModel):
    code: str
    message: str
    details: dict[str, str | int | float | bool | None] | None = None


class PublicErrorResult(PublicResultModel):
    error: PublicErrorInfo


class TrashItemOutput(RootModel[TrashItemResult | PublicErrorResult]):
    pass


class TrashListOutput(RootModel[TrashListResult | PublicErrorResult]):
    pass


class RestoreOutput(RootModel[RestoreResult | PublicErrorResult]):
    pass


class PurgeOutput(RootModel[PurgeResult | PublicErrorResult]):
    pass


class ExecutionLimitsResult(PublicResultModel):
    timeout_seconds: float
    max_output_bytes: int
    max_snapshot_files: int
    max_snapshot_bytes: int
    memory: str
    cpus: str
    pids: int
    max_concurrent_tasks: int
    max_workspace_file_bytes: int
    max_workspace_growth_bytes: int
    allow_best_effort_disk_limit: bool


class TaskMetadataResult(PublicResultModel):
    name: str
    mode: str
    workspace_access: str
    limits: ExecutionLimitsResult


class TaskListResult(PublicResultModel):
    tasks: list[TaskMetadataResult]


class ExecutionProfileResult(PublicResultModel):
    name: str
    tools: list[str]
    workspace_access: str
    default: bool
    limits: ExecutionLimitsResult


class ExecutionProfileListResult(PublicResultModel):
    default_profile: str | None
    profiles: list[ExecutionProfileResult]


class CommandExecutionResult(PublicResultModel):
    execution_id: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    source_truncated: bool = False
    stdout_available_bytes: int = 0
    stdout_inline_bytes: int = 0
    stdout_inline_truncated: bool = False
    stdout_resource_uri: str | None = None
    stderr_available_bytes: int = 0
    stderr_inline_bytes: int = 0
    stderr_inline_truncated: bool = False
    stderr_resource_uri: str | None = None
    timed_out: bool
    duration_ms: int
    capability_error: str | None = None


class PytestLocalResult(PublicResultModel):
    name: str
    type: str
    repr: str
    truncated: bool
    redacted: bool


class PytestFrameResult(PublicResultModel):
    path: str
    line: int
    function: str
    source: str
    locals: list[PytestLocalResult]


class PytestExceptionResult(PublicResultModel):
    type: str
    message: str


class PytestFailureResult(PublicResultModel):
    node_id: str
    exception: PytestExceptionResult
    frames: list[PytestFrameResult]


class PytestResult(CommandExecutionResult):
    failures: list[PytestFailureResult]
    failures_truncated: bool
    frames_truncated: bool
    locals_truncated: bool
    failure_inspection_error: str | None = None


class RuffDiagnosticResult(PublicResultModel):
    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    code: str
    message: str
    fixable: bool


class RuffResult(CommandExecutionResult):
    diagnostics: list[RuffDiagnosticResult]
    diagnostics_truncated: bool
    diagnostics_parser_error: str | None = None


class MypyDiagnosticResult(PublicResultModel):
    path: str
    line: int
    column: int
    severity: str
    code: str | None
    message: str


class MypyResult(CommandExecutionResult):
    diagnostics: list[MypyDiagnosticResult]
    diagnostics_truncated: bool
    diagnostics_parser_error: str | None = None


class CoverageBranchResult(PublicResultModel):
    percent: float
    covered: int
    missing: int


class CoverageTotalsResult(PublicResultModel):
    percent: float
    covered: int
    missing: int
    branches: CoverageBranchResult | None = None
    fail_under_failed: bool | None = None


class CoverageTestResult(PublicResultModel):
    exit_code: int | None


class CoverageResult(CommandExecutionResult):
    tests: CoverageTestResult
    coverage: CoverageTotalsResult | None
    coverage_parser_error: str | None = None


class TaskStartResult(PublicResultModel):
    task_id: str
    execution_id: str
    name: str
    status: Literal["running"]


class TaskStatusResult(PublicResultModel):
    task_id: str
    execution_id: str
    name: str
    status: str
    exit_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int


class TaskLogsResult(PublicResultModel):
    cursor: int
    next_cursor: int
    stdout: str
    stderr: str
    truncated: bool
    source_truncated: bool = False
    stdout_available_bytes: int = 0
    stdout_inline_bytes: int = 0
    stdout_inline_truncated: bool = False
    stdout_resource_uri: str | None = None
    stderr_available_bytes: int = 0
    stderr_inline_bytes: int = 0
    stderr_inline_truncated: bool = False
    stderr_resource_uri: str | None = None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ToolContract:
    """One public tool's stable metadata and structured result contract."""

    name: str
    description: str
    annotations: ToolAnnotations
    output_model: type[BaseModel]

    @property
    def output_schema(self) -> dict[str, object]:
        schema = self.output_model.model_json_schema(by_alias=True)
        # MCP protocol versions before 2026-07-28 require an object at the
        # output-schema root. RootModel unions such as trash success/error
        # results otherwise begin with ``anyOf`` even though every arm is an
        # object. Keeping the explicit object constraint is valid in newer
        # JSON Schema versions too.
        if "type" not in schema:
            schema = {"type": "object", **schema}
        return schema


def _annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool = False,
) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


READ_ONLY_LOCAL = _annotations(read_only=True, destructive=False, idempotent=True)
ADDITIVE_IDEMPOTENT = _annotations(read_only=False, destructive=False, idempotent=True)
ADDITIVE_MUTATION = _annotations(read_only=False, destructive=False, idempotent=False)
DESTRUCTIVE_MUTATION = _annotations(read_only=False, destructive=True, idempotent=False)
DISPOSABLE_EXECUTION = _annotations(read_only=False, destructive=True, idempotent=False)
LIFECYCLE_STOP = _annotations(read_only=False, destructive=True, idempotent=True)


def _contract(
    name: str,
    description: str,
    annotations: ToolAnnotations,
    output_model: type[BaseModel],
) -> ToolContract:
    return ToolContract(name, description, annotations, output_model)


_CONTRACTS = (
    _contract(
        "project_info",
        "Show the workspace root, access mode, and current writability.",
        READ_ONLY_LOCAL,
        ProjectInfoResult,
    ),
    _contract(
        "list_directory",
        "List entries inside a workspace directory.",
        READ_ONLY_LOCAL,
        DirectoryListResult,
    ),
    _contract(
        "tree",
        "Show a bounded recursive tree while skipping dependencies and caches.",
        READ_ONLY_LOCAL,
        TreeResult,
    ),
    _contract(
        "create_directory",
        "Create a directory and its parents inside the workspace.",
        ADDITIVE_IDEMPOTENT,
        DirectoryMutationResult,
    ),
    _contract(
        "read_file",
        "Read a bounded UTF-8 text file or line range inside the workspace.",
        READ_ONLY_LOCAL,
        FileContentResult,
    ),
    _contract(
        "read_file_versioned",
        "Read text and SHA-256; use this before modifying an existing file.",
        READ_ONLY_LOCAL,
        VersionedFileResult,
    ),
    _contract(
        "write_file",
        "Create text; overwrite requires SHA-256 from read_file_versioned.",
        DESTRUCTIVE_MUTATION,
        WriteFileResult,
    ),
    _contract(
        "replace_text",
        "Replace once using SHA-256 from read_file_versioned.",
        DESTRUCTIVE_MUTATION,
        ReplaceTextResult,
    ),
    _contract(
        "append_file",
        "Append text; existing files require read_file_versioned SHA-256.",
        ADDITIVE_MUTATION,
        AppendFileResult,
    ),
    _contract(
        "trash_file",
        "Move one version-checked regular file into the protected recycle bin.",
        DESTRUCTIVE_MUTATION,
        TrashItemOutput,
    ),
    _contract(
        "list_trashed_files",
        "List bounded recycle-bin metadata without exposing payload contents.",
        READ_ONLY_LOCAL,
        TrashListOutput,
    ),
    _contract(
        "restore_trashed_file",
        "Restore one item to its original path without overwriting it.",
        DESTRUCTIVE_MUTATION,
        RestoreOutput,
    ),
    _contract(
        "restore_trashed_file_to",
        "Restore or recover one trashed recycle-bin file to an alternate path.",
        DESTRUCTIVE_MUTATION,
        RestoreOutput,
    ),
    _contract(
        "purge_trashed_file",
        "Permanently delete one verified trash item; this cannot be undone.",
        DESTRUCTIVE_MUTATION,
        PurgeOutput,
    ),
    _contract(
        "search_text",
        "Search project text files without following directory symlinks.",
        READ_ONLY_LOCAL,
        SearchResult,
    ),
    _contract(
        "git_status",
        "Show bounded Git status in the selected stable allowlisted form.",
        READ_ONLY_LOCAL,
        GitStatusResult,
    ),
    _contract(
        "git_read_file_at_revision",
        "Read one policy-approved UTF-8 regular file from a safe Git revision.",
        READ_ONLY_LOCAL,
        GitRevisionFileResult,
    ),
    _contract(
        "git_init",
        "Initialize the configured workspace root as a Git main repository.",
        ADDITIVE_IDEMPOTENT,
        GitInitResult,
    ),
    _contract(
        "git_create_baseline",
        "Create the server-owned first baseline commit, never a general commit.",
        ADDITIVE_MUTATION,
        GitBaselineResult,
    ),
    _contract(
        "git_diff",
        "Show a bounded Git diff with external drivers and textconv disabled.",
        READ_ONLY_LOCAL,
        GitTextResult,
    ),
    _contract(
        "workspace_diff",
        "Show final tracked changes and safe untracked text files.",
        READ_ONLY_LOCAL,
        GitTextResult,
    ),
    _contract(
        "git_log",
        "Show up to 50 recent one-line commits.",
        READ_ONLY_LOCAL,
        GitTextResult,
    ),
    _contract(
        "git_show",
        "Show one safe commit and an optional literal, policy-checked path.",
        READ_ONLY_LOCAL,
        GitTextResult,
    ),
    _contract(
        "git_branch",
        "List local branches or show only the current branch name.",
        READ_ONLY_LOCAL,
        GitBranchResult,
    ),
    _contract(
        "git_rev_parse",
        "Resolve HEAD or return the configured Git workspace top level.",
        READ_ONLY_LOCAL,
        GitRevParseResult,
    ),
    _contract(
        "git_ls_files",
        "List tracked files after applying blocked-path exclusions.",
        READ_ONLY_LOCAL,
        GitFilesResult,
    ),
    _contract(
        "run_shell",
        "Run the documented read-only command grammar, never a real shell.",
        READ_ONLY_LOCAL,
        GitTextResult,
    ),
    _contract(
        "list_tasks",
        "List operator-authorized task names, modes, and resource limits.",
        READ_ONLY_LOCAL,
        TaskListResult,
    ),
    _contract(
        "run_task",
        "Run one configured run-mode task in a disposable container snapshot.",
        DISPOSABLE_EXECUTION,
        CommandExecutionResult,
    ),
    _contract(
        "start_task",
        "Start one configured service-mode task for diagnostics and logs.",
        DISPOSABLE_EXECUTION,
        TaskStartResult,
    ),
    _contract(
        "task_status",
        "Inspect one service task created by this server instance.",
        READ_ONLY_LOCAL,
        TaskStatusResult,
    ),
    _contract(
        "task_logs",
        "Read bounded service stdout/stderr from an absolute byte cursor.",
        READ_ONLY_LOCAL,
        TaskLogsResult,
    ),
    _contract(
        "stop_task",
        "Stop one service task created and tracked by this server instance.",
        LIFECYCLE_STOP,
        TaskStatusResult,
    ),
    _contract(
        "list_execution_profiles",
        "List enabled profile names, tools, access modes, and public limits.",
        READ_ONLY_LOCAL,
        ExecutionProfileListResult,
    ),
    _contract(
        "python_version",
        "Read Python version inside an authorized pinned container image.",
        READ_ONLY_LOCAL,
        CommandExecutionResult,
    ),
    _contract(
        "run_pytest",
        "Run structured targeted pytest in an authorized container profile.",
        DISPOSABLE_EXECUTION,
        PytestResult,
    ),
    _contract(
        "run_python_script",
        "Execute one policy-checked workspace .py file without arguments.",
        DISPOSABLE_EXECUTION,
        CommandExecutionResult,
    ),
    _contract(
        "run_ruff",
        "Run server-compiled Ruff checks with structured diagnostics.",
        DISPOSABLE_EXECUTION,
        RuffResult,
    ),
    _contract(
        "run_mypy",
        "Run server-compiled mypy checks with structured diagnostics.",
        DISPOSABLE_EXECUTION,
        MypyResult,
    ),
    _contract(
        "run_pytest_coverage",
        "Run pytest and coverage in one disposable execution.",
        DISPOSABLE_EXECUTION,
        CoverageResult,
    ),
    _contract(
        "run_command",
        "Run caller argv in an explicitly authorized container profile.",
        DISPOSABLE_EXECUTION,
        CommandExecutionResult,
    ),
    _contract(
        "start_command",
        "Start caller argv for bounded diagnostics and log observation.",
        DISPOSABLE_EXECUTION,
        TaskStartResult,
    ),
)

if len({contract.name for contract in _CONTRACTS}) != len(_CONTRACTS):
    raise RuntimeError("duplicate MCP tool contract name")

TOOL_CONTRACTS: Mapping[str, ToolContract] = MappingProxyType(
    {contract.name: contract for contract in _CONTRACTS}
)


def get_tool_contract(name: str) -> ToolContract:
    try:
        return TOOL_CONTRACTS[name]
    except KeyError as exc:
        raise KeyError(f"missing MCP tool contract: {name}") from exc


def directory_list_payload(
    path: str, text: str, *, source_truncated: bool
) -> dict[str, object]:
    entries: list[str] = []
    diagnostics: list[str] = []
    for line in text.splitlines():
        if line == "(empty directory)":
            continue
        if line.startswith("... "):
            diagnostics.append(line)
        else:
            entries.append(line)
    return {
        "path": path,
        "entries": entries,
        "diagnostics": diagnostics,
        "truncated": source_truncated,
    }


def tree_payload(
    path: str, max_depth: int, text: str, *, source_truncated: bool
) -> dict[str, object]:
    return {
        "path": path,
        "max_depth": max_depth,
        "text": text,
        "source_truncated": source_truncated,
        "truncated": source_truncated,
    }


def file_content_payload(
    path: str,
    text: str,
    start_line: int,
    end_line: int,
    *,
    source_truncated: bool,
) -> dict[str, object]:
    return {
        "path": path,
        "content": text,
        "start_line": max(start_line, 1),
        "end_line": end_line if end_line > 0 else None,
        "source_truncated": source_truncated,
        "truncated": source_truncated,
    }


_SEARCH_LINE = re.compile(r"^(?P<path>.+?):(?P<line>[0-9]+): (?P<text>.*)$")


def search_payload(
    text: str, *, truncated: bool, stop_reason: str | None
) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    diagnostics: list[str] = []
    for line in text.splitlines():
        if line.startswith("... "):
            diagnostics.append(line)
            continue
        match = _SEARCH_LINE.match(line)
        if match is None:
            if line != "No matches found.":
                diagnostics.append(line)
            continue
        matches.append(
            {
                "path": match.group("path"),
                "line": int(match.group("line")),
                "text": match.group("text"),
            }
        )
    return {
        "matches": matches,
        "truncated": truncated,
        "stop_reason": stop_reason,
        "diagnostics": diagnostics,
    }


def git_text_payload(text: str, *, source_truncated: bool) -> dict[str, object]:
    return {
        "text": text,
        "source_truncated": source_truncated,
        "truncated": source_truncated,
    }


def git_branch_payload(text: str, show_current: bool) -> dict[str, object]:
    if text == "(no output)":
        branches: list[str] = []
    else:
        branches = [
            line.strip().removeprefix("* ").strip() for line in text.splitlines()
        ]
    current = branches[0] if show_current and branches else None
    return {"branches": branches, "current": current}


def git_files_payload(text: str) -> dict[str, object]:
    files = (
        [] if text == "(no output)" else [line for line in text.splitlines() if line]
    )
    return {"files": files}


_PROJECT_INFO_FIELDS: Mapping[str, tuple[str, Callable[[str], object]]] = {
    "Exists": ("exists", lambda value: value == "True"),
    "Writable": ("writable", lambda value: value == "True"),
    "Mode": ("mode", str),
    "Blocked patterns": ("blocked_patterns", int),
    "Scan entry budget": ("max_scan_entries", int),
    "Search byte budget": ("max_search_bytes", int),
    "Concurrent searches": ("max_concurrent_searches", int),
    "Trash enabled": ("trash_enabled", lambda value: value == "True"),
    "Trash purge enabled": ("trash_purge_enabled", lambda value: value == "True"),
    "Trash item limit": ("max_trash_items", int),
    "Trash byte limit": ("max_trash_bytes", int),
}


def project_info_payload(text: str) -> dict[str, object]:
    payload: dict[str, object] = {"workspace_root": "."}
    for line in text.splitlines():
        label, separator, value = line.partition(": ")
        if not separator or label == "Allowed project root":
            continue
        field = _PROJECT_INFO_FIELDS.get(label)
        if field is None:
            continue
        name, converter = field
        payload[name] = converter(value)
    return payload


def directory_mutation_payload(text: str) -> dict[str, object]:
    prefix = "Directory ready: "
    path = text[len(prefix) :] if text.startswith(prefix) else text
    return {"path": path, "ready": True}


def write_file_payload(text: str, path: str, overwrite: bool) -> dict[str, object]:
    values = {"characters": 0, "bytes": 0}
    for line in text.splitlines():
        if line.startswith("Characters: "):
            values["characters"] = int(line.partition(": ")[2])
        elif line.startswith("Bytes: "):
            values["bytes"] = int(line.partition(": ")[2])
        elif line.startswith("Written successfully: "):
            path = line.partition(": ")[2]
    return {
        "path": path,
        "written": True,
        "characters": values["characters"],
        "bytes": values["bytes"],
        "overwrite": overwrite,
    }


def replace_text_payload(text: str, path: str) -> dict[str, object]:
    replacements = 1
    for line in text.splitlines():
        if line.startswith("Updated successfully: "):
            path = line.partition(": ")[2]
        elif line.startswith("Replacements: "):
            replacements = int(line.partition(": ")[2])
    return {"path": path, "replacements": replacements}


def append_file_payload(text: str, path: str, content: str) -> dict[str, object]:
    prefix = "Appended successfully: "
    if text.startswith(prefix):
        path = text[len(prefix) :]
    return {
        "path": path,
        "appended": True,
        "characters": len(content),
        "bytes": len(content.encode("utf-8")),
    }


def _text_content(result: CallToolResult) -> str:
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    return ""


def _structured_mapping(result: CallToolResult) -> Mapping[str, object] | None:
    value = result.structured_content
    return value if isinstance(value, Mapping) else None


def _git_status_from_text(text: str) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    if text != "(no output)":
        for line in text.splitlines():
            if line.startswith("##") or len(line) < 3:
                continue
            entries.append(
                {
                    "path": line[3:],
                    "index_status": line[0],
                    "worktree_status": line[1],
                }
            )
    return {"text": text, "clean": not entries, "entries": entries}


def _validated_tool_int(value: object, *, field: str) -> int:
    """Mirror MCP tool-layer integer coercions at this adapter boundary."""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"tool argument {field} must be an integer")
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"tool argument {field} must be an integer") from exc
    raise ValueError(f"tool argument {field} must be an integer")


def adapt_tool_call_result(
    name: str,
    arguments: Mapping[str, object],
    result: CallToolResult,
    *,
    result_cache: ResultCache | None = None,
    owner_scope: str | None = None,
) -> CallToolResult:
    """Apply the registered public output contract to one completed tool call."""

    if result.is_error:
        structured = _structured_mapping(result)
        if structured is not None and name in {
            "trash_file",
            "list_trashed_files",
            "restore_trashed_file",
            "restore_trashed_file_to",
            "purge_trashed_file",
        }:
            contract = get_tool_contract(name)
            validated = contract.output_model.model_validate(dict(structured))
            result.structured_content = validated.model_dump(mode="json", by_alias=True)
        return result

    text = _text_content(result)
    structured = _structured_mapping(result)
    carrier_tools = {
        "list_directory",
        "tree",
        "read_file",
        "search_text",
        "git_diff",
        "workspace_diff",
        "git_log",
        "git_show",
        "run_shell",
    }
    if name in carrier_tools and structured is not None:
        carrier_text = structured.get("text")
        if not isinstance(carrier_text, str):
            raise ValueError(f"tool {name} internal carrier is missing text")
        text = carrier_text
        result.content = [TextContent(type="text", text=text)]

    payload: Mapping[str, object]

    if name == "project_info":
        payload = project_info_payload(text)
    elif name == "list_directory":
        assert structured is not None
        payload = directory_list_payload(
            str(arguments.get("path", ".")),
            text,
            source_truncated=bool(structured.get("source_truncated", False)),
        )
    elif name == "tree":
        assert structured is not None
        payload = tree_payload(
            str(arguments.get("path", ".")),
            _validated_tool_int(arguments.get("max_depth", 4), field="max_depth"),
            text,
            source_truncated=bool(structured.get("source_truncated", False)),
        )
    elif name == "create_directory":
        payload = directory_mutation_payload(text)
    elif name == "read_file":
        assert structured is not None
        payload = file_content_payload(
            str(arguments.get("path", "")),
            text,
            _validated_tool_int(arguments.get("start_line", 1), field="start_line"),
            _validated_tool_int(arguments.get("end_line", 0), field="end_line"),
            source_truncated=bool(structured.get("source_truncated", False)),
        )
    elif name == "write_file":
        payload = write_file_payload(
            text,
            str(arguments.get("path", "")),
            bool(arguments.get("overwrite", False)),
        )
    elif name == "replace_text":
        payload = replace_text_payload(text, str(arguments.get("path", "")))
    elif name == "append_file":
        payload = append_file_payload(
            text,
            str(arguments.get("path", "")),
            str(arguments.get("content", "")),
        )
    elif name == "search_text":
        assert structured is not None
        stop_reason = structured.get("stop_reason")
        payload = search_payload(
            text,
            truncated=bool(structured.get("truncated", False)),
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        )
    elif name == "git_status":
        payload = _git_status_from_text(text)
    elif name in {"git_diff", "workspace_diff", "git_log", "git_show", "run_shell"}:
        assert structured is not None
        payload = git_text_payload(
            text,
            source_truncated=bool(structured.get("source_truncated", False)),
        )
    elif name == "git_branch":
        payload = git_branch_payload(text, bool(arguments.get("show_current", False)))
    elif name == "git_rev_parse":
        query = str(arguments.get("query", "HEAD"))
        value = "." if query == "--show-toplevel" else text
        payload = {"query": query, "value": value}
    elif name == "git_ls_files":
        payload = git_files_payload(text)
    elif structured is not None:
        payload = structured
    else:
        raise ValueError(f"tool {name} did not produce adaptable structured output")

    contract = get_tool_contract(name)
    validated = contract.output_model.model_validate(dict(payload))
    public_payload = validated.model_dump(mode="json", by_alias=True)
    externalized = False
    if result_cache is not None:
        public_payload, externalized = externalize_tool_payload(
            name,
            public_payload,
            result_cache,
            owner_scope=owner_scope,
        )
        validated = contract.output_model.model_validate(public_payload)
        public_payload = validated.model_dump(mode="json", by_alias=True)
    result.structured_content = public_payload
    if externalized:
        result.content = [
            TextContent(
                type="text",
                text=(
                    "Large bounded text is truncated inline; complete bounded text "
                    "is available through the result resource URI in structuredContent."
                ),
            )
        ]
    return result
