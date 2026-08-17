"""MCP presentation helpers for large already-public-safe text fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .result_cache import DEFAULT_INLINE_THRESHOLD_BYTES, ResultCache, ResultCacheError


@dataclass(frozen=True, slots=True)
class ExternalizedText:
    """Presentation metadata for one public-safe text field."""

    text: str
    available_bytes: int
    inline_bytes: int
    inline_truncated: bool
    resource_uri: str | None


def preview_utf8(text: str, max_bytes: int) -> str:
    """Return a valid UTF-8 string whose encoded size is at most ``max_bytes``."""

    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def externalize_text(
    text: str,
    cache: ResultCache,
    *,
    owner_scope: str | None,
    inline_threshold_bytes: int = DEFAULT_INLINE_THRESHOLD_BYTES,
) -> ExternalizedText:
    """Build an inline preview and optional cache reference for safe bounded text."""

    available_bytes = len(text.encode("utf-8"))
    if available_bytes <= inline_threshold_bytes:
        return ExternalizedText(
            text=text,
            available_bytes=available_bytes,
            inline_bytes=available_bytes,
            inline_truncated=False,
            resource_uri=None,
        )

    preview = preview_utf8(text, inline_threshold_bytes)
    resource_uri: str | None = None
    try:
        cached = cache.put_text(text, owner_scope=owner_scope)
    except ResultCacheError:
        cached = None
    if cached is not None:
        resource_uri = cached.uri
    return ExternalizedText(
        text=preview,
        available_bytes=available_bytes,
        inline_bytes=len(preview.encode("utf-8")),
        inline_truncated=True,
        resource_uri=resource_uri,
    )


def externalize_tool_payload(
    name: str,
    payload: dict[str, Any],
    cache: ResultCache,
    *,
    owner_scope: str | None,
) -> tuple[dict[str, Any], bool]:
    """Externalize only large human-readable fields on known public result shapes."""

    adapted = dict(payload)
    changed = False

    if name in {
        "tree",
        "git_diff",
        "workspace_diff",
        "git_log",
        "git_show",
        "run_shell",
    }:
        changed = _externalize_single_text(
            adapted,
            field="text",
            prefix="text",
            cache=cache,
            owner_scope=owner_scope,
            source_key="truncated",
        )
        adapted["truncated"] = bool(adapted.get("source_truncated", False)) or bool(
            adapted.get("text_inline_truncated", False)
        )
        return adapted, changed

    if name == "git_status":
        changed = _externalize_single_text(
            adapted,
            field="text",
            prefix="text",
            cache=cache,
            owner_scope=owner_scope,
            source_value=False,
        )
        return adapted, changed

    if name == "read_file":
        changed = _externalize_single_text(
            adapted,
            field="content",
            prefix="content",
            cache=cache,
            owner_scope=owner_scope,
            source_key="truncated",
        )
        adapted["truncated"] = bool(adapted.get("source_truncated", False)) or bool(
            adapted.get("content_inline_truncated", False)
        )
        return adapted, changed

    if name in {"read_file_versioned", "git_read_file_at_revision"}:
        changed = _externalize_single_text(
            adapted,
            field="content",
            prefix="content",
            cache=cache,
            owner_scope=owner_scope,
            source_value=bool(adapted.get("source_truncated", False)),
        )
        return adapted, changed

    if name in {
        "run_task",
        "python_version",
        "run_pytest",
        "run_python_script",
        "run_ruff",
        "run_mypy",
        "run_pytest_coverage",
        "run_command",
    }:
        source_truncated = bool(adapted.get("truncated", False))
        adapted["source_truncated"] = source_truncated
        stdout_changed = _externalize_single_text(
            adapted,
            field="stdout",
            prefix="stdout",
            cache=cache,
            owner_scope=owner_scope,
            add_source=False,
        )
        stderr_changed = _externalize_single_text(
            adapted,
            field="stderr",
            prefix="stderr",
            cache=cache,
            owner_scope=owner_scope,
            add_source=False,
        )
        adapted["truncated"] = (
            source_truncated
            or bool(adapted.get("stdout_inline_truncated", False))
            or bool(adapted.get("stderr_inline_truncated", False))
        )
        return adapted, stdout_changed or stderr_changed

    if name == "task_logs":
        source_truncated = bool(adapted.get("truncated", False))
        adapted["source_truncated"] = source_truncated
        stdout_changed = _externalize_single_text(
            adapted,
            field="stdout",
            prefix="stdout",
            cache=cache,
            owner_scope=owner_scope,
            add_source=False,
        )
        stderr_changed = _externalize_single_text(
            adapted,
            field="stderr",
            prefix="stderr",
            cache=cache,
            owner_scope=owner_scope,
            add_source=False,
        )
        adapted["truncated"] = (
            source_truncated
            or bool(adapted.get("stdout_inline_truncated", False))
            or bool(adapted.get("stderr_inline_truncated", False))
        )
        return adapted, stdout_changed or stderr_changed

    return adapted, False


def _externalize_single_text(
    payload: dict[str, Any],
    *,
    field: str,
    prefix: str,
    cache: ResultCache,
    owner_scope: str | None,
    source_key: str | None = None,
    source_value: bool | None = None,
    add_source: bool = True,
) -> bool:
    value = payload.get(field)
    if not isinstance(value, str):
        return False
    result = externalize_text(value, cache, owner_scope=owner_scope)
    payload[field] = result.text
    payload[f"{prefix}_available_bytes"] = result.available_bytes
    payload[f"{prefix}_inline_bytes"] = result.inline_bytes
    payload[f"{prefix}_inline_truncated"] = result.inline_truncated
    payload[f"{prefix}_resource_uri"] = result.resource_uri
    if add_source:
        if source_key is not None:
            source_value = bool(payload.get(source_key, False))
        payload["source_truncated"] = bool(source_value)
    return result.inline_truncated
