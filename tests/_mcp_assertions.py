from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import CallToolResult, InputRequiredResult, TextContent


def require_call_tool_result(
    result: CallToolResult | InputRequiredResult,
) -> CallToolResult:
    assert isinstance(result, CallToolResult)
    return result


def require_resource_contents(
    result: Iterable[ReadResourceContents] | InputRequiredResult,
) -> list[ReadResourceContents]:
    assert not isinstance(result, InputRequiredResult)
    return list(result)


def require_structured_content(result: CallToolResult) -> dict[str, Any]:
    structured = result.structured_content
    assert structured is not None
    return structured


def require_text_content(content: object) -> TextContent:
    assert isinstance(content, TextContent)
    return content
