"""Task authorization, concurrency, lifecycle, and bounded service logs."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .command_execution import CommandCompiler
from .config import Settings
from .diagnostics import (
    adapt_coverage_result,
    adapt_mypy_result,
    adapt_pytest_result,
    adapt_ruff_result,
    capability_result,
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
    CliContainerBackend,
    ContainerBackend,
    ContainerHandle,
    ContainerRequest,
    WorkspaceGrowthMonitor,
    run_container_task,
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
    """A thread-safe byte-bounded ring with absolute cursors."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("log capacity must be a positive integer")
        self.capacity = capacity
        self._chunks: deque[_LogChunk] = deque()
        self._base_cursor = 0
        self._next_cursor = 0
        self._size = 0
        self._dropped = False
        self._lock = threading.Lock()

    def append_stdout(self, data: bytes) -> None:
        self._append("stdout", data)

    def append_stderr(self, data: bytes) -> None:
        self._append("stderr", data)

    def _append(self, stream: str, data: bytes) -> None:
        if not data:
            return
        with self._lock:
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


@dataclass(slots=True)
class _ServiceRecord:
    task_id: str
    task: TaskDefinition
    handle: ContainerHandle
    snapshot: WorkspaceSnapshot
    logs: TaskLogBuffer
    started: float
    deadline: float
    workspace_monitor: WorkspaceGrowthMonitor
    status: str = "running"
    exit_code: int | None = None
    timed_out: bool = False
    stop_requested: bool = False
    ended: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    capacity_released: bool = False


@dataclass(slots=True)
class _StartLease:
    token: str
    cancellation: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    handle: ContainerHandle | None = None
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
        lease: _StartLease,
        cancellation: _CombinedCancellation | None = None,
    ) -> None:
        self.manager = manager
        self.lease = lease
        self.cancellation = cancellation

    def start(self, request, on_stdout, on_stderr):
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
        backend: ContainerBackend | None = None,
    ) -> None:
        self.settings = settings
        self.configuration = configuration
        self.backend = backend or CliContainerBackend(configuration.runtime)
        self.python_commands = PythonCommandCompiler(settings)
        self.commands = CommandCompiler(settings)
        self._capacity = threading.BoundedSemaphore(
            configuration.limits.max_concurrent_tasks
        )
        self._instance_token = secrets.token_hex(8)
        self._records: dict[str, _ServiceRecord] = {}
        self._starting: dict[str, _StartLease] = {}
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
    ) -> dict[str, object]:
        profile = self.resolve_execution_profile("python_version", profile)
        return self._run_profile_command(
            profile.name,
            "python_version",
            self.python_commands.python_version(),
            cancellation_event=cancellation_event,
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
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_ruff", profile)
        if fix and selected.workspace_access != "writable":
            raise TaskManagerError("ruff fix requires a writable execution profile")
        return self._run_profile_command(
            selected.name,
            "run_ruff",
            self.python_commands.ruff(paths=paths, fix=fix),
            cancellation_event=cancellation_event,
            result_adapter=adapt_ruff_result,
        )

    def run_mypy(
        self,
        profile: str | None = None,
        *,
        paths: list[str] | None = None,
        strict: bool = False,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_mypy", profile)
        return self._run_profile_command(
            selected.name,
            "run_mypy",
            self.python_commands.mypy(paths=paths, strict=strict),
            cancellation_event=cancellation_event,
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
            result_adapter=adapt_coverage_result,
        )

    def run_python_script(
        self,
        profile: str | None = None,
        path: str | None = None,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, object]:
        selected = self.resolve_execution_profile("run_python_script", profile)
        argv = self.python_commands.python_script(path)
        return self._run_profile_command(
            selected.name,
            "run_python_script",
            argv,
            cancellation_event=cancellation_event,
        )

    def run_command(
        self,
        profile: str,
        program: str,
        args: list[str] | None = None,
        cwd: str = ".",
        *,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Run a bounded caller argv in an explicitly authorized profile."""

        self._require_profile(profile, "run_command")
        command = self.commands.compile(program, args, cwd)
        return self._run_profile_command(
            profile,
            "run_command",
            command.argv,
            container_workdir=command.container_workdir,
            cancellation_event=cancellation_event,
        )

    def start_command(
        self,
        profile: str,
        program: str,
        args: list[str] | None = None,
        cwd: str = ".",
        *,
        cancellation_event: threading.Event | None = None,
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
        )
        return self._start_service(
            task,
            lease,
            started=started,
            container_workdir=command.container_workdir,
            cancellation_event=cancellation_event,
            failure_description=f"command profile {profile!r}",
        )

    def run_task(
        self,
        name: str,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Run one configured run-mode task against a disposable snapshot."""

        started = time.monotonic()
        deadline = started + self.configuration.limits.timeout_seconds
        task, lease = self._begin_start(name, "run")
        cancellation = _CombinedCancellation(lease.cancellation, cancellation_event)
        snapshot: WorkspaceSnapshot | None = None
        try:
            snapshot = self._create_snapshot(
                deadline=deadline,
                cancellation_event=cancellation,  # type: ignore[arg-type]
            )
            request = ContainerRequest(
                container_name=self._container_name(),
                snapshot_path=snapshot.path,
                task=task,
                limits=self.configuration.limits,
                initial_workspace_bytes=snapshot.total_bytes,
                started_at=started,
                deadline=deadline,
            )
            return run_container_task(
                _LeaseBackend(self, lease, cancellation),
                request,
                cancellation,  # type: ignore[arg-type]
            ).as_dict()
        finally:
            try:
                if snapshot is not None:
                    snapshot.cleanup()
            finally:
                self._finish_lease(lease)

    def _run_profile_command(
        self,
        profile_name: str,
        tool: str,
        argv: tuple[str, ...],
        *,
        container_workdir: str = "/workspace",
        cancellation_event: threading.Event | None,
        result_adapter: Callable[[dict[str, object]], dict[str, object]] | None = None,
        snapshot_initializer: Callable[[Path], None] | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        deadline = started + self.configuration.limits.timeout_seconds
        task, lease = self._begin_profile_start(profile_name, tool, argv)
        cancellation = _CombinedCancellation(lease.cancellation, cancellation_event)
        snapshot: WorkspaceSnapshot | None = None
        try:
            snapshot = self._create_snapshot(
                deadline=deadline,
                cancellation_event=cancellation,  # type: ignore[arg-type]
            )
            if snapshot_initializer is not None:
                snapshot_initializer(snapshot.path)
            request = ContainerRequest(
                container_name=self._container_name(),
                snapshot_path=snapshot.path,
                task=task,
                limits=self.configuration.limits,
                container_workdir=container_workdir,
                initial_workspace_bytes=snapshot.total_bytes,
                started_at=started,
                deadline=deadline,
            )
            result = run_container_task(
                _LeaseBackend(self, lease, cancellation),
                request,
                cancellation,  # type: ignore[arg-type]
            ).as_dict()
            result = capability_result(result)
            if result_adapter is not None:
                result = result_adapter(result)
            return result
        finally:
            try:
                if snapshot is not None:
                    snapshot.cleanup()
            finally:
                self._finish_lease(lease)

    def start_task(
        self,
        name: str,
        *,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Start one configured service task and retain only bounded logs/state."""

        started = time.monotonic()
        task, lease = self._begin_start(name, "service")
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
        lease: _StartLease,
        *,
        started: float,
        cancellation_event: threading.Event | None,
        failure_description: str,
        container_workdir: str = "/workspace",
    ) -> dict[str, object]:
        deadline = started + self.configuration.limits.timeout_seconds
        cancellation = _CombinedCancellation(lease.cancellation, cancellation_event)
        snapshot: WorkspaceSnapshot | None = None
        workspace_monitor: WorkspaceGrowthMonitor | None = None
        record: _ServiceRecord | None = None
        try:
            snapshot = self._create_snapshot(
                deadline=deadline,
                cancellation_event=cancellation,  # type: ignore[arg-type]
            )
            task_id = secrets.token_urlsafe(24)
            logs = TaskLogBuffer(self.configuration.limits.max_output_bytes)
            request = ContainerRequest(
                container_name=self._container_name(),
                snapshot_path=snapshot.path,
                task=task,
                limits=self.configuration.limits,
                container_workdir=container_workdir,
                initial_workspace_bytes=snapshot.total_bytes,
                started_at=started,
                deadline=deadline,
            )
            handle = _LeaseBackend(self, lease, cancellation).start(
                request, logs.append_stdout, logs.append_stderr
            )
            workspace_monitor = WorkspaceGrowthMonitor(request, handle)
            record = _ServiceRecord(
                task_id=task_id,
                task=task,
                handle=handle,
                snapshot=snapshot,
                logs=logs,
                started=started,
                deadline=deadline,
                workspace_monitor=workspace_monitor,
            )
            with self._lock:
                if self._shutdown:
                    raise TaskManagerError("task manager is shutting down")
                self._records[task_id] = record
                self._transfer_lease_locked(lease)
            workspace_monitor.start()
            monitor = threading.Thread(
                target=self._monitor_service,
                args=(record,),
                name=f"sandboxed-workspace-mcp-service-{task_id[:8]}",
                daemon=True,
            )
            monitor.start()
            return {"task_id": task_id, "name": task.name, "status": "running"}
        except BaseException as exc:
            try:
                if record is not None and lease.capacity_transferred:
                    self._rollback_service_start(record)
                else:
                    if workspace_monitor is not None:
                        workspace_monitor.stop_and_join()
                    if lease.handle is not None:
                        try:
                            lease.handle.stop()
                        finally:
                            lease.handle.close()
                    if snapshot is not None:
                        snapshot.cleanup()
            finally:
                self._finish_lease(lease)
            if isinstance(exc, TaskManagerError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise TaskManagerError(
                f"failed to start {failure_description}: {exc}"
            ) from exc

    def task_status(self, task_id: str) -> dict[str, object]:
        record = self._record(task_id)
        with self._lock:
            ended = record.ended
            duration = (ended or time.monotonic()) - record.started
            return {
                "task_id": record.task_id,
                "name": record.task.name,
                "status": record.status,
                "exit_code": record.exit_code,
                "timed_out": record.timed_out,
                "truncated": record.logs.dropped,
                "duration_ms": max(0, int(duration * 1000)),
            }

    def task_logs(self, task_id: str, cursor: int = 0) -> dict[str, object]:
        return self._record(task_id).logs.read(cursor)

    def stop_task(self, task_id: str) -> dict[str, object]:
        record = self._record(task_id)
        with self._lock:
            if record.status not in {"running", "stopping"}:
                return self.task_status(task_id)
            record.stop_requested = True
            record.status = "stopping"
        record.handle.stop()
        if not record.done.wait(timeout=10):
            raise TaskManagerError("service task did not stop within 10 seconds")
        return self.task_status(task_id)

    def shutdown(self) -> None:
        """Best-effort stop only service containers tracked by this instance."""

        with self._lock:
            if self._shutdown:
                shutdown_done = self._shutdown_done
                first_shutdown = False
                leases: list[_StartLease] = []
                records: list[_ServiceRecord] = []
            else:
                first_shutdown = True
                shutdown_done = self._shutdown_done
                self._shutdown = True
                leases = list(self._starting.values())
                records = [
                    record
                    for record in self._records.values()
                    if record.status in {"running", "stopping"}
                ]
                for lease in leases:
                    lease.cancellation.set()
                for record in records:
                    record.stop_requested = True
                    record.status = "stopping"
        if not first_shutdown:
            shutdown_done.wait()
            return

        for lease in leases:
            if lease.handle is not None:
                try:
                    lease.handle.stop()
                except Exception:
                    pass
        for record in records:
            try:
                record.handle.stop()
            except Exception:
                pass
        for lease in leases:
            lease.done.wait()
        for record in records:
            record.done.wait()
        shutdown_done.set()

    def _begin_start(self, name: str, mode: str) -> tuple[TaskDefinition, _StartLease]:
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
            lease = _StartLease(secrets.token_urlsafe(18))
            self._starting[lease.token] = lease
            return task, lease

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
    ) -> tuple[TaskDefinition, _StartLease]:
        with self._lock:
            profile = self._require_profile(name, tool)
            if not self._capacity.acquire(blocking=False):
                raise TaskManagerError("maximum concurrent task limit has been reached")
            lease = _StartLease(secrets.token_urlsafe(18))
            self._starting[lease.token] = lease
            return (
                TaskDefinition(
                    name=f"{name}-{tool}",
                    mode=mode,
                    image=profile.image,
                    argv=argv,
                    workspace_access=profile.workspace_access,
                ),
                lease,
            )

    def _finish_lease(self, lease: _StartLease) -> None:
        release_capacity = False
        with self._lock:
            if lease.finished:
                return
            lease.finished = True
            self._starting.pop(lease.token, None)
            release_capacity = not lease.capacity_transferred
        if release_capacity:
            self._capacity.release()
        lease.done.set()

    def _transfer_lease_locked(self, lease: _StartLease) -> None:
        lease.capacity_transferred = True
        lease.finished = True
        self._starting.pop(lease.token, None)
        lease.done.set()

    def _record(self, task_id: str) -> _ServiceRecord:
        if not isinstance(task_id, str) or not task_id:
            raise TaskManagerError("task_id must be a non-empty manager-issued ID")
        with self._lock:
            record = self._records.get(task_id)
        if record is None:
            raise TaskManagerError("unknown task_id for this server instance")
        return record

    def _rollback_service_start(self, record: _ServiceRecord) -> None:
        """Release all ownership transferred before monitor startup failed."""

        with self._lock:
            if self._records.get(record.task_id) is record:
                self._records.pop(record.task_id)
            record.stop_requested = True
            record.status = "failed"
            record.ended = time.monotonic()
            release_capacity = not record.capacity_released
            record.capacity_released = True
        try:
            record.handle.stop()
        except Exception:
            pass
        record.workspace_monitor.stop_and_join()
        try:
            record.handle.close()
        except Exception:
            pass
        try:
            record.snapshot.cleanup()
        finally:
            if release_capacity:
                self._capacity.release()
            record.done.set()

    def _create_snapshot(
        self,
        *,
        deadline: float,
        cancellation_event: threading.Event,
    ) -> WorkspaceSnapshot:
        return SnapshotBuilder(self.settings, self.configuration.limits).create(
            deadline=deadline, cancellation_event=cancellation_event
        )

    def _container_name(self) -> str:
        return f"sandboxed-workspace-mcp-{self._instance_token}-{secrets.token_hex(8)}"

    def _monitor_service(self, record: _ServiceRecord) -> None:
        try:
            try:
                remaining = max(0, record.deadline - time.monotonic())
                exit_code = record.handle.wait(timeout=remaining)
            except TimeoutError:
                with self._lock:
                    record.timed_out = True
                record.handle.stop()
                try:
                    exit_code = record.handle.wait(timeout=5)
                except TimeoutError:
                    exit_code = None
            with self._lock:
                record.exit_code = exit_code
                if record.workspace_monitor.exceeded.is_set():
                    record.status = "workspace_limit_exceeded"
                elif record.timed_out:
                    record.status = "timed_out"
                elif record.stop_requested:
                    record.status = "stopped"
                elif exit_code == 0:
                    record.status = "succeeded"
                else:
                    record.status = "failed"
        except Exception as exc:
            try:
                record.handle.stop()
            except Exception:
                pass
            record.logs.append_stderr(
                f"container monitor failure: {exc}".encode("utf-8", errors="replace")
            )
            with self._lock:
                record.status = "failed"
        finally:
            record.workspace_monitor.stop_and_join()
            try:
                record.handle.close()
            except Exception as exc:
                record.logs.append_stderr(
                    f"container cleanup failure: {exc}".encode(
                        "utf-8", errors="replace"
                    )
                )
            try:
                record.snapshot.cleanup()
            except Exception as exc:
                record.logs.append_stderr(
                    f"snapshot cleanup failure: {exc}".encode("utf-8", errors="replace")
                )
            finally:
                with self._lock:
                    record.ended = time.monotonic()
                    self._prune_records_locked()
                    release_capacity = not record.capacity_released
                    record.capacity_released = True
                if release_capacity:
                    self._capacity.release()
                record.done.set()

    def _prune_records_locked(self) -> None:
        completed = [
            task_id
            for task_id, record in self._records.items()
            if record.status not in {"running", "stopping"}
        ]
        for task_id in completed[:-_MAX_RETAINED_SERVICES]:
            self._records.pop(task_id, None)


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
