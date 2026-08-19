"""Task authorization, concurrency, lifecycle, and bounded service logs."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .artifact import ArtifactRecord, ArtifactStaging
from .artifact_store import (
    ARTIFACT_URI_PREFIX,
    MAX_ARTIFACT_RESOURCE_BYTES,
    ArtifactCollectionError,
    ArtifactLimitExceeded,
    ArtifactPolicyViolation,
    EphemeralArtifactStore,
)
from .command_execution import CommandCompiler
from .config import Settings
from .diagnostics import (
    adapt_coverage_result,
    adapt_mypy_result,
    adapt_pytest_result,
    adapt_ruff_result,
    capability_result,
)
from .execution import (
    ExecutionKind,
    ExecutionMode,
    ExecutionReason,
    ExecutionRecord,
    ExecutionResources,
    ExecutionState,
    legacy_execution_status,
)
from .execution_backend import (
    ExecutionBackend,
    ExecutionHandle,
    ExecutionRequest,
    OutputCallback,
)
from .execution_store import (
    ExecutionConflictError,
    ExecutionStore,
    ExecutionStoreError,
    InMemoryExecutionStore,
    UnknownExecutionError,
    reconcile_unfinished_executions,
)
from .pytest_debug_plugin import (
    DEBUG_PLUGIN_FILENAME,
    build_pytest_debug_plugin_source,
)
from .python_execution import PythonCommandCompiler
from .task_config import (
    ARBITRARY_COMMAND_TOOLS,
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
)
from .task_runner import (
    ArtifactGrowthMonitor,
    CliContainerBackend,
    WorkspaceGrowthMonitor,
    measure_workspace_usage,
    run_execution,
)
from .task_snapshot import SnapshotBuilder, WorkspaceSnapshot

_MAX_LOG_RESPONSE_BYTES = 64 * 1024
_MAX_RETAINED_SERVICES = 128


class TaskManagerError(ValueError):
    """Raised when an MCP task request violates the configured contract."""


@dataclass(slots=True)
class _LogChunk:
    start: int
    end: int
    stream: str
    data: bytes


class TaskLogBuffer:
    """A thread-safe byte-bounded ring with lifetime runtime byte counters."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("log capacity must be a positive integer")
        self.capacity = capacity
        self._chunks: deque[_LogChunk] = deque()
        self._base_cursor = 0
        self._next_cursor = 0
        self._size = 0
        self._dropped = False
        self._runtime_stdout_bytes = 0
        self._runtime_stderr_bytes = 0
        self._lock = threading.Lock()

    def append_stdout(self, data: bytes) -> None:
        self._append("stdout", data, runtime=True)

    def append_stderr(self, data: bytes) -> None:
        self._append("stderr", data, runtime=True)

    def append_diagnostic_stderr(self, data: bytes) -> None:
        self._append("stderr", data, runtime=False)

    def _append(self, stream: str, data: bytes, *, runtime: bool) -> None:
        if not data:
            return
        with self._lock:
            if runtime:
                if stream == "stdout":
                    self._runtime_stdout_bytes += len(data)
                else:
                    self._runtime_stderr_bytes += len(data)
            original_start = self._next_cursor
            self._next_cursor += len(data)
            if len(data) >= self.capacity:
                kept = data[-self.capacity :]
                start = self._next_cursor - len(kept)
                self._chunks.clear()
                self._chunks.append(_LogChunk(start, self._next_cursor, stream, kept))
                self._base_cursor = start
                self._size = len(kept)
                self._dropped = True
                return

            self._chunks.append(
                _LogChunk(original_start, self._next_cursor, stream, data)
            )
            self._size += len(data)
            self._trim_locked()

    def _trim_locked(self) -> None:
        while self._size > self.capacity:
            overflow = self._size - self.capacity
            first = self._chunks[0]
            if len(first.data) <= overflow:
                self._chunks.popleft()
                self._size -= len(first.data)
                self._base_cursor = first.end
            else:
                kept = first.data[overflow:]
                self._chunks[0] = _LogChunk(
                    first.start + overflow, first.end, first.stream, kept
                )
                self._size -= overflow
                self._base_cursor = first.start + overflow
            self._dropped = True

    def read(self, cursor: int) -> dict[str, object]:
        if type(cursor) is not int or cursor < 0:
            raise TaskManagerError("log cursor must be a non-negative integer")
        with self._lock:
            if cursor > self._next_cursor:
                raise TaskManagerError("log cursor is beyond the current log end")
            effective = max(cursor, self._base_cursor)
            was_truncated = cursor < self._base_cursor
            remaining = _MAX_LOG_RESPONSE_BYTES
            stdout = bytearray()
            stderr = bytearray()
            next_cursor = effective
            for chunk in self._chunks:
                if chunk.end <= effective:
                    continue
                offset = max(0, effective - chunk.start)
                available = chunk.data[offset:]
                accepted = available[:remaining]
                if chunk.stream == "stdout":
                    stdout.extend(accepted)
                else:
                    stderr.extend(accepted)
                next_cursor = chunk.start + offset + len(accepted)
                remaining -= len(accepted)
                if remaining == 0:
                    break
            stdout_text = _decode_bounded(bytes(stdout), len(stdout))
            stderr_text = _decode_bounded(bytes(stderr), len(stderr))
            return {
                "cursor": effective,
                "next_cursor": next_cursor,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "truncated": was_truncated,
                "has_more": next_cursor < self._next_cursor,
            }

    @property
    def dropped(self) -> bool:
        with self._lock:
            return self._dropped

    @property
    def runtime_stdout_bytes(self) -> int:
        with self._lock:
            return self._runtime_stdout_bytes

    @property
    def runtime_stderr_bytes(self) -> int:
        with self._lock:
            return self._runtime_stderr_bytes


@dataclass(slots=True)
class _ServiceSession:
    execution_id: str
    task: TaskDefinition
    handle: ExecutionHandle
    snapshot: WorkspaceSnapshot
    artifact_staging: ArtifactStaging
    logs: TaskLogBuffer
    deadline: float
    created_monotonic: float
    initial_workspace_bytes: int
    workspace_monitor: WorkspaceGrowthMonitor
    artifact_monitor: ArtifactGrowthMonitor
    owner_scope: str | None
    done: threading.Event = field(default_factory=threading.Event)
    capacity_released: bool = False


@dataclass(slots=True)
class _ExecutionLease:
    execution_id: str
    created_monotonic: float
    owner_scope: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    handle: ExecutionHandle | None = None
    finished: bool = False
    capacity_transferred: bool = False


class _CombinedCancellation:
    def __init__(self, *events: threading.Event | None) -> None:
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


class _LeaseBackend:
    """Atomically reject post-shutdown starts and track a returned handle."""

    def __init__(
        self,
        manager: TaskManager,
        lease: _ExecutionLease,
        cancellation: _CombinedCancellation | None = None,
    ) -> None:
        self.manager = manager
        self.lease = lease
        self.cancellation = cancellation

    def start(
        self,
        request: ExecutionRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ExecutionHandle:
        with self.manager._lock:
            if (
                self.manager._shutdown
                or self.lease.cancellation.is_set()
                or (self.cancellation is not None and self.cancellation.is_set())
            ):
                raise TaskManagerError("task manager is shutting down")
            handle = self.manager.backend.start(request, on_stdout, on_stderr)
            self.lease.handle = handle
            return handle


class TaskManager:
    """Expose only configured task names and manager-issued service IDs."""

    def __init__(
        self,
        settings: Settings,
        configuration: TaskConfiguration,
        *,
        backend: ExecutionBackend | None = None,
        execution_store: ExecutionStore | None = None,
        artifact_store: EphemeralArtifactStore | None = None,
    ) -> None:
        self.settings = settings
        self.configuration = configuration
        self.backend = backend or CliContainerBackend(configuration.runtime)
        self.execution_store = execution_store or InMemoryExecutionStore()
        self.artifact_store = artifact_store or EphemeralArtifactStore()
        reconcile_unfinished_executions(self.execution_store)
        self.python_commands = PythonCommandCompiler(settings)
        self.commands = CommandCompiler(settings)
        self._capacity = threading.BoundedSemaphore(
            configuration.limits.max_concurrent_tasks
        )
        self._instance_token = secrets.token_hex(8)
        self._sessions: dict[str, _ServiceSession] = {}
        self._starting: dict[str, _ExecutionLease] = {}
        self._lock = threading.RLock()
        self._shutdown = False
        self._shutdown_done = threading.Event()

    def list_tasks(self) -> dict[str, object]:
        """Return public task metadata without paths, images, or argv."""

        limits = asdict(self.configuration.limits)
        tasks = [
            {
                "name": task.name,
                "mode": task.mode,
                "workspace_access": task.workspace_access,
                "limits": dict(limits),
            }
            for task in self.configuration.tasks.values()
        ]
        return {"tasks": tasks}

    def list_execution_profiles(self) -> dict[str, object]:
        """Return profile capabilities without image, argv, or config paths."""

        limits = asdict(self.configuration.limits)
        profiles = [
            {
                "name": profile.name,
                "tools": sorted(profile.tools),
                "workspace_access": profile.workspace_access,
                "default": profile.name == self.configuration.default_profile,
                "limits": dict(limits),
            }
            for profile in self.configuration.profiles.values()
        ]
        return {
            "default_profile": self.configuration.default_profile,
            "profiles": profiles,
        }

    def resolve_execution_profile(
        self,
        tool: str,
        requested_profile: str | None = None,
    ) -> ExecutionProfile:
        """Resolve an explicit or deterministic structured-tool profile."""

        if tool in ARBITRARY_COMMAND_TOOLS and requested_profile is None:
            raise TaskManagerError(
                f"profile is required for arbitrary execution tool {tool}"
            )
        with self._lock:
            if self._shutdown:
                raise TaskManagerError("task manager is shutting down")
            if requested_profile is not None:
                profile = self.configuration.profiles.get(requested_profile)
                if profile is None:
                    raise TaskManagerError(
                        f"unknown execution profile: {requested_profile}"
                    )
                self._authorize_profile(profile, tool)
                return profile
            candidates = [
                profile
                for profile in self.configuration.profiles.values()
                if tool in profile.tools
            ]
            if self.configuration.default_profile is not None:
                default = self.configuration.profiles.get(
                    self.configuration.default_profile
                )
                if default is not None and default in candidates:
                    return default
            if len(candidates) == 1:
                return candidates[0]
            if not candidates:
                raise TaskManagerError(f"no execution profile authorizes {tool}")
            names = ", ".join(sorted(profile.name for profile in candidates))
            raise TaskManagerError(
                f"ambiguous execution profile for {tool}; choose one of: {names}"
            )

    def python_version(
        self,
        profile: str | None = None,
        *,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        resolved_profile = self.resolve_execution_profile("python_version", profile)
        return self._run_profile_command(
            resolved_profile.name,
            "python_version",
            self.python_commands.python_version(),
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
        )

    def run_pytest(
        self,
        profile: str | None = None,
        *,
        targets: list[str] | None = None,
        keyword: str | None = None,
        quiet: bool = False,
        verbosity: int = 0,
        exit_first: bool = False,
        no_capture: bool = False,
        traceback: str = "auto",
        show_locals: bool = False,
        max_failures: int | None = None,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_pytest", profile)
        argv = self.python_commands.pytest(
            targets=targets,
            keyword=keyword,
            quiet=quiet,
            verbosity=verbosity,
            exit_first=exit_first,
            no_capture=no_capture,
            traceback=traceback,
            show_locals=show_locals,
            max_failures=max_failures,
            include_failure_plugin=True,
        )
        return self._run_profile_command(
            selected.name,
            "run_pytest",
            argv,
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
            result_adapter=adapt_pytest_result,
            snapshot_initializer=lambda path: _write_debug_plugin(
                path,
                show_locals=show_locals,
                output_limit=self.configuration.limits.max_output_bytes,
            ),
        )

    def run_ruff(
        self,
        profile: str | None = None,
        *,
        paths: list[str] | None = None,
        fix: bool = False,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_ruff", profile)
        if fix and selected.workspace_access != "writable":
            raise TaskManagerError("ruff fix requires a writable execution profile")
        return self._run_profile_command(
            selected.name,
            "run_ruff",
            self.python_commands.ruff(paths=paths, fix=fix),
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
            result_adapter=adapt_ruff_result,
        )

    def run_mypy(
        self,
        profile: str | None = None,
        *,
        paths: list[str] | None = None,
        strict: bool = False,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_mypy", profile)
        return self._run_profile_command(
            selected.name,
            "run_mypy",
            self.python_commands.mypy(paths=paths, strict=strict),
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
            result_adapter=adapt_mypy_result,
        )

    def run_pytest_coverage(
        self,
        profile: str | None = None,
        *,
        targets: list[str] | None = None,
        keyword: str | None = None,
        branch: bool = False,
        fail_under: float | None = None,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_pytest_coverage", profile)
        return self._run_profile_command(
            selected.name,
            "run_pytest_coverage",
            self.python_commands.pytest_coverage(
                targets=targets,
                keyword=keyword,
                branch=branch,
                fail_under=fail_under,
            ),
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
            result_adapter=adapt_coverage_result,
        )

    def run_python_script(
        self,
        profile: str | None = None,
        path: str | None = None,
        *,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_python_script", profile)
        argv = self.python_commands.python_script(path)
        return self._run_profile_command(
            selected.name,
            "run_python_script",
            argv,
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
        )

    def run_command(
        self,
        profile: str,
        program: str,
        args: list[str] | None = None,
        cwd: str = ".",
        *,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        """Run a bounded caller argv in an explicitly authorized profile."""

        self._require_profile(profile, "run_command")
        command = self.commands.compile(program, args, cwd)
        return self._run_profile_command(
            profile,
            "run_command",
            command.argv,
            workdir=command.workdir,
            cancellation_event=cancellation_event,
            owner_scope=owner_scope,
        )

    def start_command(
        self,
        profile: str,
        program: str,
        args: list[str] | None = None,
        cwd: str = ".",
        *,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        """Start a bounded caller argv through the existing service lifecycle."""

        started = time.monotonic()
        self._require_profile(profile, "start_command")
        command = self.commands.compile(program, args, cwd)
        task, lease = self._begin_profile_start(
            profile,
            "start_command",
            command.argv,
            mode="service",
            owner_scope=owner_scope,
        )
        return self._start_service(
            task,
            lease,
            started=started,
            workdir=command.workdir,
            cancellation_event=cancellation_event,
            failure_description=f"command profile {profile!r}",
        )

    def run_task(
        self,
        name: str,
        *,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        """Run one configured run-mode task against a disposable snapshot."""

        started = time.monotonic()
        deadline = started + self.configuration.limits.timeout_seconds
        task, lease = self._begin_start(
            name, "run", "run_task", owner_scope=owner_scope
        )
        return self._run_sync_execution(
            task,
            lease,
            started=started,
            deadline=deadline,
            cancellation_event=cancellation_event,
        )

    def _run_profile_command(
        self,
        profile_name: str,
        tool: str,
        argv: tuple[str, ...],
        *,
        workdir: str = "/workspace",
        cancellation_event: threading.Event | None,
        owner_scope: str | None = None,
        result_adapter: Callable[[dict[str, object]], dict[str, object]] | None = None,
        snapshot_initializer: Callable[[Path], None] | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        deadline = started + self.configuration.limits.timeout_seconds
        task, lease = self._begin_profile_start(
            profile_name, tool, argv, owner_scope=owner_scope
        )
        result = self._run_sync_execution(
            task,
            lease,
            started=started,
            deadline=deadline,
            cancellation_event=cancellation_event,
            workdir=workdir,
            snapshot_initializer=snapshot_initializer,
        )
        result = capability_result(result)
        if result_adapter is not None:
            result = result_adapter(result)
        return result

    def _run_sync_execution(
        self,
        task: TaskDefinition,
        lease: _ExecutionLease,
        *,
        started: float,
        deadline: float,
        cancellation_event: threading.Event | None,
        workdir: str = "/workspace",
        snapshot_initializer: Callable[[Path], None] | None = None,
    ) -> dict[str, object]:
        cancellation = _CombinedCancellation(lease.cancellation, cancellation_event)
        snapshot: WorkspaceSnapshot | None = None
        artifact_staging: ArtifactStaging | None = None
        initial_workspace_bytes: int | None = None
        final_workspace_bytes: int | None = None
        try:
            try:
                snapshot = self._create_snapshot(
                    deadline=deadline,
                    cancellation_event=cancellation,  # type: ignore[arg-type]
                )
                if snapshot_initializer is not None:
                    snapshot_initializer(snapshot.path)
                initial_workspace_bytes = self._measure_workspace_baseline(snapshot)
                artifact_staging = ArtifactStaging.create()
            except Exception:
                self._finish_prestart_failure(
                    lease.execution_id,
                    lease=lease,
                    cancellation_event=cancellation_event,
                    deadline=deadline,
                    workspace_initial_bytes=initial_workspace_bytes,
                )
                raise

            assert initial_workspace_bytes is not None
            request = ExecutionRequest(
                runtime_name=self._runtime_name(),
                workspace_path=snapshot.path,
                task=task,
                limits=self.configuration.limits,
                artifact_path=artifact_staging.path,
                workdir=workdir,
                initial_workspace_bytes=initial_workspace_bytes,
                started_at=started,
                deadline=deadline,
            )

            def mark_cancelling() -> None:
                self._request_cancellation(
                    lease.execution_id,
                    self._cancellation_reason(lease, cancellation_event),
                )

            task_result = run_execution(
                _LeaseBackend(self, lease, cancellation),
                request,
                cancellation,  # type: ignore[arg-type]
                on_started=lambda: self._mark_running(lease.execution_id),
                on_cancelling=mark_cancelling,
            )
            runtime_state = task_result.state
            runtime_reason = task_result.reason
            if runtime_state is ExecutionState.CANCELLED:
                runtime_reason = self._cancellation_reason(lease, cancellation_event)
            final_workspace_bytes = self._measure_final_workspace(
                task, snapshot, initial_workspace_bytes
            )
            artifacts: list[ArtifactRecord] = []
            artifact_failure: ExecutionReason | None = None
            try:
                artifacts = self.artifact_store.collect(
                    lease.execution_id,
                    artifact_staging.path,
                    self.configuration.limits,
                    owner_scope=lease.owner_scope,
                )
            except ArtifactLimitExceeded:
                artifact_failure = ExecutionReason.ARTIFACT_LIMIT_EXCEEDED
            except ArtifactPolicyViolation:
                artifact_failure = ExecutionReason.ARTIFACT_POLICY_VIOLATION
            except ArtifactCollectionError:
                artifact_failure = ExecutionReason.ARTIFACT_COLLECTION_FAILED
            cleanup_failed = False
            try:
                artifact_staging.cleanup()
                artifact_staging = None
                snapshot.cleanup()
                snapshot = None
            except Exception:
                cleanup_failed = True
            state, reason = _resolve_terminal_outcome(
                runtime_state,
                runtime_reason,
                artifact_failure=artifact_failure,
                cleanup_failed=cleanup_failed,
            )
            resources = self._build_execution_resources(
                created_monotonic=lease.created_monotonic,
                workspace_initial_bytes=initial_workspace_bytes,
                workspace_final_bytes=final_workspace_bytes,
                stdout_bytes=task_result.stdout_bytes,
                stderr_bytes=task_result.stderr_bytes,
            )
            completed = self._complete_execution(
                lease.execution_id,
                state,
                reason=reason,
                exit_code=task_result.exit_code,
                resources=resources,
                artifact_manifest=artifacts,
            )
            result = task_result.as_dict()
            if state is not task_result.state:
                result["status"] = legacy_execution_status(state, reason)
                result["timed_out"] = state is ExecutionState.TIMED_OUT
            result["execution_id"] = lease.execution_id
            result["resources"] = (
                completed.resources.model_dump(mode="json")
                if completed.resources is not None
                else None
            )
            result["artifacts"] = [_artifact_payload(item) for item in artifacts]
            return result
        finally:
            try:
                if artifact_staging is not None:
                    artifact_staging.cleanup()
                if snapshot is not None:
                    snapshot.cleanup()
            except Exception:
                self._crash_if_unfinished(
                    lease.execution_id,
                    ExecutionReason.CLEANUP_FAILED,
                )
            finally:
                self._finish_lease(lease)

    def start_task(
        self,
        name: str,
        *,
        cancellation_event: threading.Event | None = None,
        owner_scope: str | None = None,
    ) -> dict[str, object]:
        """Start one configured service task and retain only bounded logs/state."""

        started = time.monotonic()
        task, lease = self._begin_start(
            name, "service", "start_task", owner_scope=owner_scope
        )
        return self._start_service(
            task,
            lease,
            started=started,
            cancellation_event=cancellation_event,
            failure_description=f"service task {name!r}",
        )

    def _start_service(
        self,
        task: TaskDefinition,
        lease: _ExecutionLease,
        *,
        started: float,
        cancellation_event: threading.Event | None,
        failure_description: str,
        workdir: str = "/workspace",
    ) -> dict[str, object]:
        deadline = started + self.configuration.limits.timeout_seconds
        cancellation = _CombinedCancellation(lease.cancellation, cancellation_event)
        snapshot: WorkspaceSnapshot | None = None
        artifact_staging: ArtifactStaging | None = None
        workspace_monitor: WorkspaceGrowthMonitor | None = None
        artifact_monitor: ArtifactGrowthMonitor | None = None
        session: _ServiceSession | None = None
        initial_workspace_bytes: int | None = None
        try:
            snapshot = self._create_snapshot(
                deadline=deadline,
                cancellation_event=cancellation,  # type: ignore[arg-type]
            )
            initial_workspace_bytes = self._measure_workspace_baseline(snapshot)
            artifact_staging = ArtifactStaging.create()
            logs = TaskLogBuffer(self.configuration.limits.max_output_bytes)
            request = ExecutionRequest(
                runtime_name=self._runtime_name(),
                workspace_path=snapshot.path,
                task=task,
                limits=self.configuration.limits,
                artifact_path=artifact_staging.path,
                workdir=workdir,
                initial_workspace_bytes=initial_workspace_bytes,
                started_at=started,
                deadline=deadline,
            )
            handle = _LeaseBackend(self, lease, cancellation).start(
                request, logs.append_stdout, logs.append_stderr
            )
            self._mark_running(lease.execution_id)
            workspace_monitor = WorkspaceGrowthMonitor(request, handle)
            artifact_monitor = ArtifactGrowthMonitor(request, handle)
            session = _ServiceSession(
                execution_id=lease.execution_id,
                task=task,
                handle=handle,
                snapshot=snapshot,
                artifact_staging=artifact_staging,
                logs=logs,
                deadline=deadline,
                created_monotonic=lease.created_monotonic,
                initial_workspace_bytes=initial_workspace_bytes,
                workspace_monitor=workspace_monitor,
                artifact_monitor=artifact_monitor,
                owner_scope=lease.owner_scope,
            )
            with self._lock:
                if self._shutdown:
                    self._request_cancellation(
                        lease.execution_id, ExecutionReason.SERVER_SHUTDOWN
                    )
                    raise TaskManagerError("task manager is shutting down")
                self._sessions[lease.execution_id] = session
                self._transfer_lease_locked(lease)
            workspace_monitor.start()
            artifact_monitor.start()
            monitor = threading.Thread(
                target=self._monitor_service,
                args=(session,),
                name=f"workspace-guard-mcp-service-{lease.execution_id[:8]}",
                daemon=True,
            )
            monitor.start()
            return {
                "task_id": lease.execution_id,
                "execution_id": lease.execution_id,
                "name": task.name,
                "status": "running",
            }
        except BaseException as exc:
            try:
                if session is not None and lease.capacity_transferred:
                    self._rollback_service_start(session)
                else:
                    if workspace_monitor is not None:
                        workspace_monitor.stop_and_join()
                    if artifact_monitor is not None:
                        artifact_monitor.stop_and_join()
                    if lease.handle is not None:
                        try:
                            lease.handle.stop()
                        finally:
                            lease.handle.close()
                    if artifact_staging is not None:
                        artifact_staging.cleanup()
                    if snapshot is not None:
                        snapshot.cleanup()
                    self._finish_prestart_failure(
                        lease.execution_id,
                        lease=lease,
                        cancellation_event=cancellation_event,
                        deadline=deadline,
                        workspace_initial_bytes=initial_workspace_bytes,
                    )
            finally:
                self._finish_lease(lease)
            if isinstance(exc, TaskManagerError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise TaskManagerError(
                f"failed to start {failure_description}: {exc}"
            ) from exc

    def execution_status(self, execution_id: str) -> dict[str, object]:
        record = self._execution_record(execution_id, id_label="execution_id")
        return record.model_dump(mode="json")

    def execution_events(
        self, execution_id: str, cursor: int = 0, limit: int = 50
    ) -> dict[str, object]:
        self._execution_record(execution_id, id_label="execution_id")
        try:
            page = self.execution_store.list_events(
                execution_id, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            raise TaskManagerError(str(exc)) from exc
        except UnknownExecutionError as exc:
            raise TaskManagerError(
                "unknown execution_id for this server instance"
            ) from exc
        except ExecutionStoreError as exc:
            raise TaskManagerError(f"failed to read execution events: {exc}") from exc
        return {
            "execution_id": execution_id,
            "cursor": cursor,
            "next_cursor": page.next_cursor,
            "events": [event.model_dump(mode="json") for event in page.events],
            "has_more": page.has_more,
            "history_complete": page.history_complete,
        }

    def execution_artifacts(
        self, execution_id: str, *, owner_scope: str | None = None
    ) -> dict[str, object]:
        record = self._execution_record(execution_id, id_label="execution_id")
        if not record.terminal:
            raise TaskManagerError(
                "artifacts are available only after execution is terminal"
            )
        try:
            manifest = self.execution_store.list_artifacts(execution_id)
        except UnknownExecutionError as exc:
            raise TaskManagerError(
                "unknown execution_id for this server instance"
            ) from exc
        except ExecutionStoreError as exc:
            raise TaskManagerError(f"failed to read artifact manifest: {exc}") from exc
        live_artifact_ids = {
            item.artifact_id
            for item in self.artifact_store.list_execution(
                execution_id, owner_scope=owner_scope
            )
        }
        return {
            "execution_id": execution_id,
            "manifest_complete": manifest.manifest_complete,
            "artifacts": [
                _artifact_payload(
                    item,
                    content_available=item.artifact_id in live_artifact_ids,
                )
                for item in manifest.artifacts
            ],
        }

    def task_status(self, task_id: str) -> dict[str, object]:
        record = self._service_execution_record(task_id)
        with self._lock:
            session = self._sessions.get(task_id)
            truncated = session.logs.dropped if session is not None else False
        started = record.started_at or record.created_at
        ended = record.finished_at or time.time()
        return {
            "task_id": record.execution_id,
            "execution_id": record.execution_id,
            "name": record.name,
            "status": legacy_execution_status(
                record.state, record.reason, service=True
            ),
            "exit_code": record.exit_code,
            "timed_out": record.state is ExecutionState.TIMED_OUT,
            "truncated": truncated,
            "duration_ms": max(0, int((ended - started) * 1000)),
            "resources": (
                record.resources.model_dump(mode="json")
                if record.resources is not None
                else None
            ),
        }

    def task_logs(self, task_id: str, cursor: int = 0) -> dict[str, object]:
        return self._session(task_id).logs.read(cursor)

    def stop_task(self, task_id: str) -> dict[str, object]:
        record = self._service_execution_record(task_id)
        if record.terminal:
            return self.task_status(task_id)
        session = self._session(task_id)
        self._request_cancellation(task_id, ExecutionReason.USER_CANCELLED)
        session.handle.stop()
        if not session.done.wait(timeout=10):
            raise TaskManagerError("service task did not stop within 10 seconds")
        return self.task_status(task_id)

    def shutdown(self) -> None:
        """Cancel only executions whose runtime ownership belongs to this process."""

        with self._lock:
            if self._shutdown:
                shutdown_done = self._shutdown_done
                first_shutdown = False
                leases: list[_ExecutionLease] = []
                sessions: list[_ServiceSession] = []
            else:
                first_shutdown = True
                shutdown_done = self._shutdown_done
                self._shutdown = True
                leases = list(self._starting.values())
                sessions = list(self._sessions.values())
                for lease in leases:
                    lease.cancellation.set()
        if not first_shutdown:
            shutdown_done.wait()
            return

        for lease in leases:
            self._request_cancellation(
                lease.execution_id, ExecutionReason.SERVER_SHUTDOWN
            )
            if lease.handle is not None:
                try:
                    lease.handle.stop()
                except Exception:
                    pass
        for session in sessions:
            self._request_cancellation(
                session.execution_id, ExecutionReason.SERVER_SHUTDOWN
            )
            try:
                session.handle.stop()
            except Exception:
                pass
        for lease in leases:
            lease.done.wait()
        for session in sessions:
            session.done.wait()
        shutdown_done.set()

    def _begin_start(
        self,
        name: str,
        mode: str,
        tool: str,
        *,
        owner_scope: str | None = None,
    ) -> tuple[TaskDefinition, _ExecutionLease]:
        if not isinstance(name, str):
            raise TaskManagerError("task name must be a string")
        with self._lock:
            if self._shutdown:
                raise TaskManagerError("task manager is shutting down")
            task = self.configuration.tasks.get(name)
            if task is None:
                raise TaskManagerError(f"unknown task name: {name}")
            if task.mode != mode:
                raise TaskManagerError(
                    f"task {name!r} has mode {task.mode!r}, not required mode {mode!r}"
                )
            if not self._capacity.acquire(blocking=False):
                raise TaskManagerError("maximum concurrent task limit has been reached")
            return task, self._create_execution_lease(
                kind=ExecutionKind.TASK,
                name=name,
                tool=tool,
                mode=ExecutionMode(mode),
                owner_scope=owner_scope,
            )

    def _require_profile(self, name: str, tool: str) -> ExecutionProfile:
        if not isinstance(name, str):
            raise TaskManagerError("profile name must be a string")
        with self._lock:
            if self._shutdown:
                raise TaskManagerError("task manager is shutting down")
            profile = self.configuration.profiles.get(name)
            if profile is None:
                raise TaskManagerError(f"unknown execution profile: {name}")
            self._authorize_profile(profile, tool)
            return profile

    @staticmethod
    def _authorize_profile(profile: ExecutionProfile, tool: str) -> None:
        if tool not in profile.tools:
            raise TaskManagerError(
                f"execution profile {profile.name!r} does not authorize {tool}"
            )
        if tool in ARBITRARY_COMMAND_TOOLS and not profile.allow_arbitrary_commands:
            raise TaskManagerError(
                f"execution profile {profile.name!r} does not authorize "
                "arbitrary commands"
            )

    def _begin_profile_start(
        self,
        name: str,
        tool: str,
        argv: tuple[str, ...],
        *,
        mode: str = "run",
        owner_scope: str | None = None,
    ) -> tuple[TaskDefinition, _ExecutionLease]:
        with self._lock:
            profile = self._require_profile(name, tool)
            if not self._capacity.acquire(blocking=False):
                raise TaskManagerError("maximum concurrent task limit has been reached")
            task = TaskDefinition(
                name=f"{name}-{tool}",
                mode=mode,
                image=profile.image,
                argv=argv,
                workspace_access=profile.workspace_access,
            )
            lease = self._create_execution_lease(
                kind=ExecutionKind.PROFILE,
                name=task.name,
                tool=tool,
                mode=ExecutionMode(mode),
                owner_scope=owner_scope,
            )
            return task, lease

    def _create_execution_lease(
        self,
        *,
        kind: ExecutionKind,
        name: str,
        tool: str,
        mode: ExecutionMode,
        owner_scope: str | None = None,
    ) -> _ExecutionLease:
        execution_id = secrets.token_urlsafe(24)
        created_monotonic = time.monotonic()
        now = time.time()
        try:
            record = ExecutionRecord(
                execution_id=execution_id,
                kind=kind,
                name=name,
                tool=tool,
                mode=mode,
                state=ExecutionState.STARTING,
                created_at=now,
                updated_at=now,
            )
            self.execution_store.create(record)
        except (ValueError, ExecutionStoreError) as exc:
            self._capacity.release()
            raise TaskManagerError(f"failed to create execution record: {exc}") from exc
        lease = _ExecutionLease(
            execution_id,
            created_monotonic=created_monotonic,
            owner_scope=owner_scope,
        )
        self._starting[execution_id] = lease
        return lease

    def _finish_lease(self, lease: _ExecutionLease) -> None:
        release_capacity = False
        with self._lock:
            if lease.finished:
                return
            lease.finished = True
            self._starting.pop(lease.execution_id, None)
            release_capacity = not lease.capacity_transferred
        if release_capacity:
            self._capacity.release()
        lease.done.set()

    def _transfer_lease_locked(self, lease: _ExecutionLease) -> None:
        lease.capacity_transferred = True
        lease.finished = True
        self._starting.pop(lease.execution_id, None)
        lease.done.set()

    def _execution_record(
        self, execution_id: str, *, id_label: str = "task_id"
    ) -> ExecutionRecord:
        if not isinstance(execution_id, str) or not execution_id:
            raise TaskManagerError(f"{id_label} must be a non-empty manager-issued ID")
        try:
            return self.execution_store.get(execution_id)
        except UnknownExecutionError as exc:
            raise TaskManagerError(
                f"unknown {id_label} for this server instance"
            ) from exc
        except ExecutionStoreError as exc:
            raise TaskManagerError(f"failed to read execution record: {exc}") from exc

    def _service_execution_record(self, execution_id: str) -> ExecutionRecord:
        record = self._execution_record(execution_id)
        if record.mode is not ExecutionMode.SERVICE:
            raise TaskManagerError("unknown task_id for this server instance")
        return record

    def _session(self, execution_id: str) -> _ServiceSession:
        self._service_execution_record(execution_id)
        with self._lock:
            session = self._sessions.get(execution_id)
        if session is None:
            raise TaskManagerError("service runtime session is no longer available")
        return session

    def _rollback_service_start(self, session: _ServiceSession) -> None:
        """Release runtime ownership transferred before monitor startup failed."""

        with self._lock:
            if self._sessions.get(session.execution_id) is session:
                self._sessions.pop(session.execution_id)
            release_capacity = not session.capacity_released
            session.capacity_released = True
        try:
            session.handle.stop()
        except Exception:
            pass
        try:
            session.workspace_monitor.stop_and_join()
        except Exception:
            pass
        try:
            session.artifact_monitor.stop_and_join()
        except Exception:
            pass
        final_workspace_bytes = self._measure_final_workspace(
            session.task,
            session.snapshot,
            session.initial_workspace_bytes,
        )
        try:
            session.handle.close()
        except Exception:
            pass
        try:
            session.artifact_staging.cleanup()
            session.snapshot.cleanup()
        finally:
            resources = self._build_execution_resources(
                created_monotonic=session.created_monotonic,
                workspace_initial_bytes=session.initial_workspace_bytes,
                workspace_final_bytes=final_workspace_bytes,
                stdout_bytes=session.logs.runtime_stdout_bytes,
                stderr_bytes=session.logs.runtime_stderr_bytes,
            )
            self._crash_if_unfinished(
                session.execution_id,
                ExecutionReason.RUNTIME_MONITOR_FAILED,
                resources=resources,
            )
            if release_capacity:
                self._capacity.release()
            session.done.set()

    def _mark_running(self, execution_id: str) -> None:
        try:
            self.execution_store.transition(
                execution_id,
                {ExecutionState.STARTING},
                ExecutionState.RUNNING,
                started_at=time.time(),
            )
        except ExecutionStoreError as exc:
            raise TaskManagerError(f"failed to mark execution running: {exc}") from exc

    def _request_cancellation(
        self, execution_id: str, reason: ExecutionReason
    ) -> ExecutionRecord:
        record = self._execution_record(execution_id)
        if record.terminal or record.state is ExecutionState.CANCELLING:
            return record
        if record.state not in {ExecutionState.STARTING, ExecutionState.RUNNING}:
            return record
        try:
            return self.execution_store.request_cancellation(
                execution_id,
                {record.state},
                reason,
            )
        except ExecutionConflictError:
            return self._execution_record(execution_id)
        except ExecutionStoreError as exc:
            raise TaskManagerError(f"failed to cancel execution: {exc}") from exc

    def _complete_execution(
        self,
        execution_id: str,
        state: ExecutionState,
        *,
        reason: ExecutionReason | None = None,
        exit_code: int | None = None,
        resources: ExecutionResources | None = None,
        artifact_manifest: Iterable[ArtifactRecord] = (),
    ) -> ExecutionRecord:
        record = self._execution_record(execution_id)
        if record.terminal:
            return record
        if (
            record.state is ExecutionState.CANCELLING
            and state is not ExecutionState.TIMED_OUT
        ):
            state = ExecutionState.CANCELLED
            reason = record.reason or reason
        try:
            return self.execution_store.transition(
                execution_id,
                {record.state},
                state,
                reason=reason,
                exit_code=exit_code,
                resources=resources,
                artifact_manifest=artifact_manifest,
                finished_at=time.time(),
            )
        except ExecutionConflictError as conflict:
            current = self._execution_record(execution_id)
            if current.terminal:
                return current
            if current.state is ExecutionState.CANCELLING:
                return self.execution_store.transition(
                    execution_id,
                    {ExecutionState.CANCELLING},
                    ExecutionState.CANCELLED,
                    reason=current.reason or reason,
                    exit_code=exit_code,
                    resources=resources,
                    artifact_manifest=artifact_manifest,
                    finished_at=time.time(),
                )
            raise TaskManagerError(
                "execution state changed during completion"
            ) from conflict
        except ExecutionStoreError as exc:
            raise TaskManagerError(f"failed to complete execution: {exc}") from exc

    def _build_execution_resources(
        self,
        *,
        created_monotonic: float,
        workspace_initial_bytes: int | None,
        workspace_final_bytes: int | None,
        stdout_bytes: int,
        stderr_bytes: int,
    ) -> ExecutionResources:
        growth = (
            None
            if workspace_final_bytes is None or workspace_initial_bytes is None
            else max(0, workspace_final_bytes - workspace_initial_bytes)
        )
        return ExecutionResources(
            wall_time_ms=max(0, int((time.monotonic() - created_monotonic) * 1000)),
            cpu_time_ms=None,
            peak_memory_bytes=None,
            workspace_initial_bytes=workspace_initial_bytes,
            workspace_final_bytes=workspace_final_bytes,
            workspace_growth_bytes=growth,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            output_bytes=stdout_bytes + stderr_bytes,
        )

    def _measure_workspace_baseline(self, snapshot: WorkspaceSnapshot) -> int:
        return measure_workspace_usage(
            snapshot.path,
            initial_workspace_bytes=snapshot.total_bytes,
            limits=self.configuration.limits,
        ).total_bytes

    def _measure_final_workspace(
        self,
        task: TaskDefinition,
        snapshot: WorkspaceSnapshot,
        initial_workspace_bytes: int,
    ) -> int | None:
        if task.workspace_access != "writable":
            return initial_workspace_bytes
        try:
            return measure_workspace_usage(
                snapshot.path,
                initial_workspace_bytes=initial_workspace_bytes,
                limits=self.configuration.limits,
            ).total_bytes
        except OSError:
            return None

    def _finish_prestart_failure(
        self,
        execution_id: str,
        *,
        lease: _ExecutionLease,
        cancellation_event: threading.Event | None,
        deadline: float,
        workspace_initial_bytes: int | None,
    ) -> None:
        resources = self._build_execution_resources(
            created_monotonic=lease.created_monotonic,
            workspace_initial_bytes=workspace_initial_bytes,
            workspace_final_bytes=None,
            stdout_bytes=0,
            stderr_bytes=0,
        )
        if lease.cancellation.is_set() or self._shutdown:
            self._request_cancellation(execution_id, ExecutionReason.SERVER_SHUTDOWN)
            self._complete_execution(
                execution_id,
                ExecutionState.CANCELLED,
                reason=ExecutionReason.SERVER_SHUTDOWN,
                resources=resources,
            )
            return
        if cancellation_event is not None and cancellation_event.is_set():
            self._request_cancellation(execution_id, ExecutionReason.CLIENT_CANCELLED)
            self._complete_execution(
                execution_id,
                ExecutionState.CANCELLED,
                reason=ExecutionReason.CLIENT_CANCELLED,
                resources=resources,
            )
            return
        if time.monotonic() >= deadline:
            self._complete_execution(
                execution_id,
                ExecutionState.TIMED_OUT,
                reason=ExecutionReason.TIMEOUT,
                resources=resources,
            )
            return
        self._complete_execution(
            execution_id,
            ExecutionState.CRASHED,
            reason=ExecutionReason.RUNTIME_START_FAILED,
            resources=resources,
        )

    def _cancellation_reason(
        self,
        lease: _ExecutionLease,
        cancellation_event: threading.Event | None,
    ) -> ExecutionReason:
        if lease.cancellation.is_set() or self._shutdown:
            return ExecutionReason.SERVER_SHUTDOWN
        if cancellation_event is not None and cancellation_event.is_set():
            return ExecutionReason.CLIENT_CANCELLED
        return ExecutionReason.CLIENT_CANCELLED

    def _crash_if_unfinished(
        self,
        execution_id: str,
        reason: ExecutionReason,
        *,
        resources: ExecutionResources | None = None,
    ) -> None:
        record = self._execution_record(execution_id)
        if record.terminal:
            return
        self._complete_execution(
            execution_id,
            ExecutionState.CRASHED,
            reason=reason,
            resources=resources,
        )

    def _create_snapshot(
        self,
        *,
        deadline: float,
        cancellation_event: threading.Event,
    ) -> WorkspaceSnapshot:
        return SnapshotBuilder(self.settings, self.configuration.limits).create(
            deadline=deadline, cancellation_event=cancellation_event
        )

    def _runtime_name(self) -> str:
        return f"workspace-guard-mcp-{self._instance_token}-{secrets.token_hex(8)}"

    def _monitor_service(self, session: _ServiceSession) -> None:
        exit_code: int | None = None
        state = ExecutionState.FAILED
        reason: ExecutionReason | None = None
        timed_out = False
        try:
            try:
                remaining = max(0, session.deadline - time.monotonic())
                exit_code = session.handle.wait(timeout=remaining)
            except TimeoutError:
                timed_out = True
                session.handle.stop()
                try:
                    exit_code = session.handle.wait(timeout=5)
                except TimeoutError:
                    exit_code = None
            current = self._execution_record(session.execution_id)
            if session.workspace_monitor.exceeded.is_set():
                state = ExecutionState.FAILED
                reason = ExecutionReason.WORKSPACE_LIMIT_EXCEEDED
            elif session.artifact_monitor.policy_violation.is_set():
                state = ExecutionState.FAILED
                reason = ExecutionReason.ARTIFACT_POLICY_VIOLATION
            elif session.artifact_monitor.limit_exceeded.is_set():
                state = ExecutionState.FAILED
                reason = ExecutionReason.ARTIFACT_LIMIT_EXCEEDED
            elif timed_out:
                state = ExecutionState.TIMED_OUT
                reason = ExecutionReason.TIMEOUT
            elif current.state is ExecutionState.CANCELLING:
                state = ExecutionState.CANCELLED
                reason = current.reason
            elif exit_code == 0:
                state = ExecutionState.SUCCEEDED
            else:
                state = ExecutionState.FAILED
        except Exception as exc:
            try:
                session.handle.stop()
            except Exception:
                pass
            session.logs.append_diagnostic_stderr(
                f"container monitor failure: {exc}".encode("utf-8", errors="replace")
            )
            state = ExecutionState.CRASHED
            reason = ExecutionReason.RUNTIME_MONITOR_FAILED
        finally:
            runtime_state = state
            runtime_reason = reason
            cleanup_failed = False
            for label, monitor in (
                ("workspace", session.workspace_monitor),
                ("artifact", session.artifact_monitor),
            ):
                try:
                    monitor.stop_and_join()
                except Exception as exc:
                    cleanup_failed = True
                    session.logs.append_diagnostic_stderr(
                        f"{label} monitor cleanup failure: {exc}".encode(
                            "utf-8", errors="replace"
                        )
                    )
            try:
                session.handle.close()
            except Exception as exc:
                cleanup_failed = True
                session.logs.append_diagnostic_stderr(
                    f"container cleanup failure: {exc}".encode(
                        "utf-8", errors="replace"
                    )
                )
            final_workspace_bytes = self._measure_final_workspace(
                session.task,
                session.snapshot,
                session.initial_workspace_bytes,
            )
            artifacts: list[ArtifactRecord] = []
            artifact_failure: ExecutionReason | None = None
            try:
                artifacts = self.artifact_store.collect(
                    session.execution_id,
                    session.artifact_staging.path,
                    self.configuration.limits,
                    owner_scope=session.owner_scope,
                )
            except ArtifactLimitExceeded:
                artifact_failure = ExecutionReason.ARTIFACT_LIMIT_EXCEEDED
            except ArtifactPolicyViolation:
                artifact_failure = ExecutionReason.ARTIFACT_POLICY_VIOLATION
            except ArtifactCollectionError:
                artifact_failure = ExecutionReason.ARTIFACT_COLLECTION_FAILED
            try:
                session.artifact_staging.cleanup()
                session.snapshot.cleanup()
            except Exception as exc:
                cleanup_failed = True
                session.logs.append_diagnostic_stderr(
                    f"execution temporary cleanup failure: {exc}".encode(
                        "utf-8", errors="replace"
                    )
                )
            state, reason = _resolve_terminal_outcome(
                runtime_state,
                runtime_reason,
                artifact_failure=artifact_failure,
                cleanup_failed=cleanup_failed,
            )
            resources = self._build_execution_resources(
                created_monotonic=session.created_monotonic,
                workspace_initial_bytes=session.initial_workspace_bytes,
                workspace_final_bytes=final_workspace_bytes,
                stdout_bytes=session.logs.runtime_stdout_bytes,
                stderr_bytes=session.logs.runtime_stderr_bytes,
            )
            try:
                self._complete_execution(
                    session.execution_id,
                    state,
                    reason=reason,
                    exit_code=exit_code,
                    resources=resources,
                    artifact_manifest=artifacts,
                )
            finally:
                with self._lock:
                    self._prune_sessions_locked()
                    release_capacity = not session.capacity_released
                    session.capacity_released = True
                if release_capacity:
                    self._capacity.release()
                session.done.set()

    def _prune_sessions_locked(self) -> None:
        completed = [
            execution_id
            for execution_id in self._sessions
            if self._execution_record(execution_id).terminal
        ]
        for execution_id in completed[:-_MAX_RETAINED_SERVICES]:
            self._sessions.pop(execution_id, None)


def _artifact_payload(
    record: ArtifactRecord,
    *,
    content_available: bool = True,
) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["content_available"] = content_available
    payload["resource_uri"] = (
        ARTIFACT_URI_PREFIX + record.artifact_id
        if content_available and record.size_bytes <= MAX_ARTIFACT_RESOURCE_BYTES
        else None
    )
    return payload


def _resolve_terminal_outcome(
    runtime_state: ExecutionState,
    runtime_reason: ExecutionReason | None,
    *,
    artifact_failure: ExecutionReason | None,
    cleanup_failed: bool,
) -> tuple[ExecutionState, ExecutionReason | None]:
    """Preserve a primary runtime outcome unless successful finalization fails."""

    if runtime_state is not ExecutionState.SUCCEEDED:
        return runtime_state, runtime_reason
    if cleanup_failed:
        return ExecutionState.CRASHED, ExecutionReason.CLEANUP_FAILED
    if artifact_failure is ExecutionReason.ARTIFACT_COLLECTION_FAILED:
        return ExecutionState.CRASHED, artifact_failure
    if artifact_failure is not None:
        return ExecutionState.FAILED, artifact_failure
    return runtime_state, runtime_reason


def _decode_bounded(data: bytes, limit: int) -> str:
    rendered = data.decode("utf-8", errors="replace")
    encoded = rendered.encode("utf-8")
    if len(encoded) <= limit:
        return rendered
    return encoded[:limit].decode("utf-8", errors="ignore")


def _write_debug_plugin(
    snapshot_path: Path,
    *,
    show_locals: bool,
    output_limit: int,
) -> None:
    """Materialize a server-controlled collector only inside the disposable snapshot."""

    target = snapshot_path / DEBUG_PLUGIN_FILENAME
    target.write_text(
        build_pytest_debug_plugin_source(
            show_locals=show_locals,
            output_limit=output_limit,
        ),
        encoding="utf-8",
    )
