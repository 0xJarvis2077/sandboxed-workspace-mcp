"""Pure builders for agent-facing self-description MCP resources."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from mcp.types import Tool

from .tool_contracts import get_tool_contract

MARKDOWN_MIME = "text/markdown; charset=utf-8"
JSON_MIME = "application/json"
TOOL_INFO_URI_TEMPLATE = "internal://tool-info/{name}"

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCHEMA_MAX_DEPTH = 5
_SCHEMA_MAX_FIELDS = 128


def valid_tool_name(name: str) -> bool:
    """Accept only the bounded literal identifier shape used by public tools."""

    return bool(_TOOL_NAME.fullmatch(name))


def _collect_property_names(
    value: object,
    *,
    depth: int = 0,
    names: set[str] | None = None,
) -> set[str]:
    names = set() if names is None else names
    if depth > _SCHEMA_MAX_DEPTH or len(names) >= _SCHEMA_MAX_FIELDS:
        return names
    if isinstance(value, Mapping):
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            for name in sorted(str(item) for item in properties)[:_SCHEMA_MAX_FIELDS]:
                names.add(name)
                if len(names) >= _SCHEMA_MAX_FIELDS:
                    return names
        for key in ("anyOf", "oneOf", "allOf", "$defs"):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                children = [nested[item] for item in sorted(nested, key=str)]
            elif isinstance(nested, list):
                children = nested
            else:
                continue
            for child in children:
                _collect_property_names(child, depth=depth + 1, names=names)
                if len(names) >= _SCHEMA_MAX_FIELDS:
                    return names
    return names


def summarize_schema(schema: Mapping[str, Any]) -> dict[str, object]:
    """Return a small deterministic capability summary instead of full schema noise."""

    required = schema.get("required", [])
    required_names = (
        sorted(str(item) for item in required) if isinstance(required, list) else []
    )
    return {
        "type": schema.get("type", "object"),
        "required": required_names[:_SCHEMA_MAX_FIELDS],
        "properties": sorted(_collect_property_names(schema))[:_SCHEMA_MAX_FIELDS],
    }


def _annotation_summary(name: str) -> dict[str, bool]:
    values = get_tool_contract(name).annotations.model_dump(
        by_alias=True, exclude_none=False
    )
    keys = (
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    )
    return {key: bool(values[key]) for key in keys}


def build_tool_catalog(tools: Iterable[Tool]) -> dict[str, object]:
    """Build the current public capability catalog from registered MCP tools."""

    entries: list[dict[str, object]] = []
    for tool in sorted(tools, key=lambda item: item.name):
        contract = get_tool_contract(tool.name)
        entries.append(
            {
                "name": contract.name,
                "description": contract.description,
                "annotations": _annotation_summary(tool.name),
                "input": summarize_schema(tool.input_schema),
                "output": summarize_schema(contract.output_schema),
            }
        )
    return {"tools": entries}


def build_tool_info(tool: Tool) -> dict[str, object]:
    """Build one full public tool contract after visibility has been checked."""

    contract = get_tool_contract(tool.name)
    return {
        "name": contract.name,
        "description": contract.description,
        "annotations": _annotation_summary(tool.name),
        "input_schema": tool.input_schema,
        "output_schema": contract.output_schema,
    }


def _ordered_available(tool_names: set[str], preferred: Iterable[str]) -> list[str]:
    return [name for name in preferred if name in tool_names]


def build_instructions(tool_names: Iterable[str]) -> str:
    """Build bounded operating guidance that mentions only currently public tools."""

    names = set(tool_names)
    lines = [
        "# Workspace operating instructions",
        "",
        (
            "Use the server's structured, capability-bounded tools as the primary "
            "interface. Tool annotations are behavior hints; authorization and safety "
            "are enforced by server policy, version checks, and isolated execution "
            "boundaries."
        ),
        "",
        "## File changes",
        "",
        (
            "For an existing file, start with `read_file_versioned` and preserve its "
            "SHA-256 as `expected_sha256`. Re-read before resolving an ambiguous or "
            "stale edit."
        ),
    ]
    mutations = _ordered_available(
        names, ("replace_text", "write_file", "append_file", "create_directory")
    )
    if mutations:
        lines.append(
            "Prefer the narrowest available mutation. For small edits, use "
            "`replace_text` when it is available instead of rewriting the whole file. "
            "Never overwrite an existing file without the version guard."
        )
    else:
        lines.append(
            "This server currently exposes no workspace mutation tools; treat the "
            "workspace as read-only."
        )

    if "trash_file" in names:
        lines.extend(
            [
                "",
                "## Delete and recover",
                "",
                (
                    "Use `read_file_versioned` before `trash_file` and pass the "
                    "current `expected_sha256`. Prefer recoverable trash over "
                    "irreversible deletion. Use `list_trashed_files` to obtain the "
                    "current trash ID and SHA before restoring. Recovery never "
                    "overwrites an occupied "
                    "destination."
                ),
            ]
        )

    structured = _ordered_available(
        names,
        (
            "run_pytest",
            "run_ruff",
            "run_mypy",
            "run_pytest_coverage",
            "run_python_script",
        ),
    )
    lines.extend(["", "## Python and execution", ""])
    if structured:
        rendered = ", ".join(f"`{name}`" for name in structured)
        lines.append(
            f"Prefer structured execution tools before generic execution: {rendered}. "
            "Start with a focused target, inspect structured failures or diagnostics, "
            "make the smallest general fix, then broaden verification."
        )
    else:
        lines.append(
            "No structured Python execution tool is currently public. Do not infer or "
            "attempt hidden execution capabilities."
        )
    generic = _ordered_available(names, ("run_command", "start_command"))
    if generic:
        rendered = ", ".join(f"`{name}`" for name in generic)
        lines.append(
            f"Use {rendered} only when the available structured tools cannot express "
            "a project-specific diagnostic. Generic execution is not the default path."
        )

    lines.extend(
        [
            "",
            "## Review changes",
            "",
            (
                "Inspect `git_status` and `workspace_diff` after edits. Use `git_diff` "
                "when a native tracked diff is useful. Do not revert, overwrite, or "
                "format unrelated user changes."
            ),
            "",
            "## Large results",
            "",
            (
                "Large human-readable text may be returned as a bounded inline preview "
                "plus an ephemeral `workspaceguard://result/...` resource URI. "
                "Follow that URI only when the complete bounded result is necessary. "
                "The resource contains only content already admitted by the server's "
                "existing safety and output bounds; it never exposes discarded raw "
                "output."
            ),
            "",
            "## Security",
            "",
            (
                "Execution, when exposed, occurs through configured sandbox or "
                "snapshot boundaries. Workspace mutation has separate policy checks. "
                "Do not attempt to bypass structured tools, discover unavailable "
                "capabilities, "
                "or treat tool annotations as permission grants."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _edit_file_workflow(names: set[str]) -> str:
    if "replace_text" not in names and "write_file" not in names:
        return (
            "# Edit file\n\n"
            "This server currently exposes no workspace mutation tools. Locate and "
            "read the file, but do not infer a hidden write path.\n"
        )
    preferred = (
        "`replace_text`" if "replace_text" in names else "the narrowest write tool"
    )
    return (
        "# Edit file\n\n"
        "1. Locate the file with the available read/search tools.\n"
        "2. Read the existing file with `read_file_versioned`.\n"
        "3. Preserve the returned SHA-256 as `expected_sha256`.\n"
        f"4. Apply the smallest precise mutation, preferring {preferred} for a small "
        "edit.\n"
        "5. Run focused verification.\n"
        "6. Inspect `workspace_diff`.\n\n"
        "Do not rewrite a whole file for a small change and do not overwrite an "
        "existing file without a version guard.\n"
    )


def _debug_python_workflow(names: set[str]) -> str:
    steps = [
        "# Debug Python",
        "",
        (
            "1. Inspect the relevant tree, search results, and source before changing "
            "code."
        ),
    ]
    verification = _ordered_available(
        names,
        ("run_pytest", "run_ruff", "run_mypy", "run_pytest_coverage"),
    )
    if "run_pytest" in names:
        steps.extend(
            [
                (
                    "2. Run a targeted `run_pytest` first and read its structured "
                    "failures."
                ),
                "3. Make the smallest general fix and re-run the focused test.",
            ]
        )
    else:
        steps.append(
            "2. No structured pytest tool is currently public; do not assume one "
            "exists."
        )
    if verification:
        rendered = " -> ".join(f"`{name}`" for name in verification)
        steps.append(
            "4. Broaden verification through the available structured tools: "
            f"{rendered}."
        )
    generic = _ordered_available(names, ("run_command", "start_command"))
    if generic:
        rendered = ", ".join(f"`{name}`" for name in generic)
        steps.append(
            f"5. Use {rendered} only for diagnostics the structured tools cannot "
            "express."
        )
    steps.append(
        "6. If stdout/stderr is truncated inline and a result resource URI is "
        "returned, read that URI when the complete bounded output is necessary."
    )
    steps.append("7. Finish with `workspace_diff` and preserve unrelated user changes.")
    steps.extend(
        [
            "",
            "There is no interactive step/next/continue debugger in this workflow.",
        ]
    )
    return "\n".join(steps) + "\n"


def _recover_file_workflow(names: set[str]) -> str:
    required = {
        "list_trashed_files",
        "restore_trashed_file",
        "restore_trashed_file_to",
    }
    if not required.issubset(names):
        return (
            "# Recover file\n\n"
            "This server currently exposes no complete recycle-bin recovery workflow. "
            "Do not infer unavailable recovery or permanent-delete capabilities.\n"
        )
    return (
        "# Recover file\n\n"
        "1. Call `list_trashed_files`.\n"
        "2. Identify the intended `trash_id` and its current SHA-256.\n"
        "3. Use `restore_trashed_file` with that exact SHA for the original path.\n"
        "4. If the original path is occupied, use `restore_trashed_file_to` with a "
        "safe, empty alternate destination.\n\n"
        "Restore never overwrites an existing target and requires the correct SHA.\n"
    )


def _review_changes_workflow(names: set[str]) -> str:
    checks = _ordered_available(
        names, ("run_pytest", "run_ruff", "run_mypy", "run_pytest_coverage")
    )
    lines = [
        "# Review changes",
        "",
        "1. Inspect `git_status`.",
        "2. Inspect `workspace_diff` for the final workspace view.",
    ]
    if checks:
        lines.append(
            "3. Run focused and then broader checks using the currently available "
            f"structured tools: {', '.join(f'`{name}`' for name in checks)}."
        )
    lines.append(
        "4. If a diff or log is truncated inline and a result resource URI is "
        "returned, read that URI when the complete bounded result is necessary."
    )
    lines.append("5. Do not revert or reformat unrelated user changes.")
    return "\n".join(lines) + "\n"


_WORKFLOW_BUILDERS = {
    "edit-file": _edit_file_workflow,
    "debug-python": _debug_python_workflow,
    "recover-file": _recover_file_workflow,
    "review-changes": _review_changes_workflow,
}


def get_workflow(name: str, tool_names: Iterable[str]) -> str:
    """Build one known workflow without duplicating tool metadata."""

    try:
        builder = _WORKFLOW_BUILDERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown self-description workflow: {name}") from exc
    return builder(set(tool_names))
