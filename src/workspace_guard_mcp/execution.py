"""Canonical execution domain model and lifecycle transitions."""

from __future__ import annotations

import sys
from collections.abc import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - compatibility for the supported Python 3.10 runtime
    from enum import Enum

    class StrEnum(str, Enum):
        """Python 3.10 compatibility shim for enum.StrEnum."""


class ExecutionState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"


class ExecutionReason(StrEnum):
    USER_CANCELLED = "user_cancelled"
    CLIENT_CANCELLED = "client_cancelled"
    SERVER_SHUTDOWN = "server_shutdown"
    TIMEOUT = "timeout"
    WORKSPACE_LIMIT_EXCEEDED = "workspace_limit_exceeded"
    ARTIFACT_LIMIT_EXCEEDED = "artifact_limit_exceeded"
    ARTIFACT_POLICY_VIOLATION = "artifact_policy_violation"
    ARTIFACT_COLLECTION_FAILED = "artifact_collection_failed"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    RUNTIME_START_FAILED = "runtime_start_failed"
    RUNTIME_MONITOR_FAILED = "runtime_monitor_failed"
    CLEANUP_FAILED = "cleanup_failed"
    SERVER_RESTARTED = "server_restarted"


class ExecutionKind(StrEnum):
    TASK = "task"
    PROFILE = "profile"


class ExecutionMode(StrEnum):
    RUN = "run"
    SERVICE = "service"


class ExecutionEventType(StrEnum):
    CREATED = "created"
    STATE_TRANSITION = "state_transition"


TERMINAL_EXECUTION_STATES = frozenset(
    {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.TIMED_OUT,
        ExecutionState.CRASHED,
    }
)
_CANCELLATION_REASONS = frozenset(
    {
        ExecutionReason.USER_CANCELLED,
        ExecutionReason.CLIENT_CANCELLED,
        ExecutionReason.SERVER_SHUTDOWN,
    }
)

_ALLOWED_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.STARTING: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.CANCELLING,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        }
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLING,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        }
    ),
    ExecutionState.CANCELLING: frozenset(
        {
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        }
    ),
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
    ExecutionState.TIMED_OUT: frozenset(),
    ExecutionState.CRASHED: frozenset(),
}


class ExecutionTransitionError(ValueError):
    """Raised when a canonical execution state transition is not permitted."""


class ExecutionResources(BaseModel):
    """Immutable aggregate resource accounting for one terminal execution."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    wall_time_ms: int = Field(ge=0, strict=True)
    cpu_time_ms: int | None = Field(default=None, ge=0, strict=True)
    peak_memory_bytes: int | None = Field(default=None, ge=0, strict=True)
    workspace_initial_bytes: int | None = Field(default=None, ge=0, strict=True)
    workspace_final_bytes: int | None = Field(default=None, ge=0, strict=True)
    workspace_growth_bytes: int | None = Field(default=None, ge=0, strict=True)
    stdout_bytes: int = Field(ge=0, strict=True)
    stderr_bytes: int = Field(ge=0, strict=True)
    output_bytes: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _validate_accounting(self) -> ExecutionResources:
        if self.output_bytes != self.stdout_bytes + self.stderr_bytes:
            raise ValueError("output_bytes must equal stdout_bytes + stderr_bytes")
        if self.workspace_final_bytes is None:
            if self.workspace_growth_bytes is not None:
                raise ValueError(
                    "workspace_growth_bytes requires workspace_final_bytes"
                )
            return self
        if self.workspace_initial_bytes is None:
            raise ValueError("workspace_final_bytes requires workspace_initial_bytes")
        expected_growth = max(
            0, self.workspace_final_bytes - self.workspace_initial_bytes
        )
        if self.workspace_growth_bytes != expected_growth:
            raise ValueError(
                "workspace_growth_bytes must equal max(0, final - initial)"
            )
        return self


class ExecutionRecord(BaseModel):
    """Bounded public-safe metadata for exactly one authorized execution."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    execution_id: str = Field(min_length=1, max_length=128)
    kind: ExecutionKind
    name: str = Field(min_length=1, max_length=256)
    tool: str | None = Field(default=None, min_length=1, max_length=128)
    mode: ExecutionMode
    state: ExecutionState
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    reason: ExecutionReason | None = None
    error_summary: str | None = Field(default=None, max_length=4096)
    resources: ExecutionResources | None = None

    @field_validator(
        "execution_id",
        "name",
        "tool",
        "error_summary",
        mode="after",
    )
    @classmethod
    def _bound_utf8(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        limits = {
            "execution_id": 512,
            "name": 1024,
            "tool": 512,
            "error_summary": 16 * 1024,
        }
        field_name = info.field_name
        if field_name is None:
            raise ValueError("execution string field name is unavailable")
        limit = limits[field_name]
        if len(value.encode("utf-8")) > limit:
            raise ValueError(f"{field_name} exceeds the {limit}-byte limit")
        return value

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> ExecutionRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not be earlier than created_at")
        if self.finished_at is not None:
            lower_bound = (
                self.started_at if self.started_at is not None else self.created_at
            )
            if self.finished_at < lower_bound:
                label = "started_at" if self.started_at is not None else "created_at"
                raise ValueError(f"finished_at must not be earlier than {label}")

        if (
            self.state in {ExecutionState.RUNNING, ExecutionState.CANCELLING}
            and self.started_at is None
        ):
            raise ValueError(f"{self.state.value} execution requires started_at")
        if is_terminal_state(self.state):
            if self.finished_at is None:
                raise ValueError("terminal execution requires finished_at")
        else:
            if self.finished_at is not None:
                raise ValueError("non-terminal execution must not have finished_at")
            if self.resources is not None:
                raise ValueError("non-terminal execution must not have resources")

        if (
            self.state is ExecutionState.TIMED_OUT
            and self.reason is not ExecutionReason.TIMEOUT
        ):
            raise ValueError("timed_out execution requires timeout reason")
        if (
            self.state is ExecutionState.CANCELLED
            and self.reason not in _CANCELLATION_REASONS
        ):
            raise ValueError("cancelled execution requires a cancellation reason")
        if self.state is ExecutionState.SUCCEEDED and self.reason is not None:
            raise ValueError("succeeded execution must not have a reason")
        return self

    @property
    def terminal(self) -> bool:
        return is_terminal_state(self.state)


class ExecutionEvent(BaseModel):
    """Bounded append-only lifecycle metadata for one canonical execution."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    execution_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1, strict=True)
    timestamp: float
    event_type: ExecutionEventType
    from_state: ExecutionState | None
    to_state: ExecutionState
    reason: ExecutionReason | None = None
    error_summary: str | None = Field(default=None, max_length=4096)

    @field_validator("execution_id", "error_summary", mode="after")
    @classmethod
    def _bound_utf8(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        limits = {
            "execution_id": 512,
            "error_summary": 16 * 1024,
        }
        field_name = info.field_name
        if field_name is None:
            raise ValueError("execution event string field name is unavailable")
        limit = limits[field_name]
        if len(value.encode("utf-8")) > limit:
            raise ValueError(f"{field_name} exceeds the {limit}-byte limit")
        return value

    @model_validator(mode="after")
    def _validate_event_lifecycle(self) -> ExecutionEvent:
        if self.event_type is ExecutionEventType.CREATED:
            if self.from_state is not None:
                raise ValueError("created event requires from_state=None")
            return self
        if self.from_state is None:
            raise ValueError("state_transition event requires from_state")
        ensure_execution_transition(self.from_state, self.to_state)
        return self


def is_terminal_state(state: ExecutionState) -> bool:
    return state in TERMINAL_EXECUTION_STATES


def ensure_execution_transition(
    old_state: ExecutionState,
    new_state: ExecutionState,
) -> None:
    allowed = _ALLOWED_TRANSITIONS[old_state]
    if new_state not in allowed:
        raise ExecutionTransitionError(
            f"illegal execution transition: {old_state.value} -> {new_state.value}"
        )


def legacy_execution_status(
    state: ExecutionState,
    reason: ExecutionReason | None,
    *,
    service: bool = False,
) -> str:
    """Map canonical state/reason back to the additive legacy MCP status strings."""

    if state in {ExecutionState.STARTING, ExecutionState.RUNNING}:
        return "running"
    if state is ExecutionState.CANCELLING:
        return "stopping"
    if state is ExecutionState.SUCCEEDED:
        return "succeeded"
    if state is ExecutionState.FAILED:
        if reason is ExecutionReason.WORKSPACE_LIMIT_EXCEEDED:
            return "workspace_limit_exceeded"
        if reason is ExecutionReason.OUTPUT_LIMIT_EXCEEDED:
            return "output_limit_exceeded"
        return "failed"
    if state is ExecutionState.CANCELLED:
        return "stopped" if service else "cancelled"
    if state is ExecutionState.TIMED_OUT:
        return "timed_out"
    if state is ExecutionState.CRASHED:
        if reason is ExecutionReason.RUNTIME_START_FAILED:
            return "start_failed"
        if not service and reason is ExecutionReason.RUNTIME_MONITOR_FAILED:
            # A synchronous monitor-start failure historically surfaced as
            # start_failed. Keep that public string while canonical state is CRASHED.
            return "start_failed"
        return "failed"
    raise AssertionError(f"unhandled execution state: {state}")
