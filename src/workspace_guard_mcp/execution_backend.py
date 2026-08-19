"""Backend-neutral execution request and runtime handle contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .task_config import TaskDefinition, TaskLimits

OutputCallback = Callable[[bytes], None]


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """A fully server-generated request passed to one execution backend."""

    runtime_name: str
    workspace_path: Path
    task: TaskDefinition
    limits: TaskLimits
    artifact_path: Path | None = None
    workdir: str = "/workspace"
    initial_workspace_bytes: int = 0
    started_at: float | None = None
    deadline: float | None = None


class ExecutionHandle(Protocol):
    """A tracked runtime handle created by an execution backend."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the execution and return its exit code."""

    def stop(self) -> None:
        """Stop only this tracked execution."""

    def close(self) -> None:
        """Release local runtime resources."""


class ExecutionBackend(Protocol):
    """Minimal backend boundary shared by production and test implementations."""

    def start(
        self,
        request: ExecutionRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ExecutionHandle:
        """Start one execution and return its tracked handle."""
