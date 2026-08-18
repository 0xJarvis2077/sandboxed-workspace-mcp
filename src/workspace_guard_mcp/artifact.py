"""Execution artifact domain model and private staging ownership."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_MAX_NAME_CHARS = 255
_MAX_NAME_BYTES = 1024
_MAX_MEDIA_TYPE_BYTES = 512


class ArtifactRecord(BaseModel):
    """Immutable public-safe metadata for one admitted execution artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    artifact_id: str = Field(min_length=32, max_length=32, pattern=_ARTIFACT_ID.pattern)
    execution_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=_MAX_NAME_CHARS)
    media_type: str = Field(min_length=3, max_length=255, pattern=_MEDIA_TYPE.pattern)
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    created_at: float

    @field_validator("execution_id", mode="after")
    @classmethod
    def _bound_execution_id(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 512:
            raise ValueError("execution_id exceeds the 512-byte limit")
        return value

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not is_safe_artifact_name(value):
            raise ValueError("artifact name must be a bounded safe basename")
        return value

    @field_validator("media_type", mode="after")
    @classmethod
    def _bound_media_type(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_MEDIA_TYPE_BYTES:
            raise ValueError("media_type exceeds the 512-byte limit")
        return value


@dataclass(slots=True)
class ArtifactStaging:
    """Private writable staging directory owned by exactly one execution."""

    path: Path
    _temporary: tempfile.TemporaryDirectory[str] = field(repr=False)
    _cleaned: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(cls) -> ArtifactStaging:
        temporary = tempfile.TemporaryDirectory(prefix="workspace-guard-mcp-artifact-")
        path = Path(temporary.name).resolve() / "artifacts"
        mode = 0o777 if getattr(os, "geteuid", lambda: 1)() == 0 else 0o700
        try:
            path.mkdir(mode=mode)
            if mode == 0o777:
                path.chmod(mode)
        except BaseException:
            temporary.cleanup()
            raise
        return cls(path=path, _temporary=temporary)

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._temporary.cleanup()
        self._cleaned = True


def is_safe_artifact_name(value: object) -> bool:
    """Return whether a workload filename is safe as public basename metadata."""

    if not isinstance(value, str) or not value or len(value) > _MAX_NAME_CHARS:
        return False
    if len(value.encode("utf-8")) > _MAX_NAME_BYTES:
        return False
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        return False
    return not any(unicodedata.category(character) == "Cc" for character in value)
