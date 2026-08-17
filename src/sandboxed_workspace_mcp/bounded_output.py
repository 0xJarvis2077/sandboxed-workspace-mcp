"""Small primitives for UTF-8 byte-bounded human-readable output."""

from __future__ import annotations

from dataclasses import dataclass

TRUNCATION_MARKER = "\n\n... OUTPUT TRUNCATED ..."


@dataclass(frozen=True, slots=True)
class BoundedText:
    """Rendered text plus authoritative byte-truncation provenance."""

    text: str
    truncated: bool


def truncate_utf8_result(text: str, max_bytes: int) -> BoundedText:
    """Bound UTF-8 text while preserving whether this operation truncated it."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return BoundedText(text=text, truncated=False)

    marker = TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        rendered = marker[:max_bytes].decode("utf-8", errors="ignore")
    else:
        prefix = encoded[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
        rendered = prefix + TRUNCATION_MARKER
    return BoundedText(text=rendered, truncated=True)
