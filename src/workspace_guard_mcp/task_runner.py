"""Container-only execution backend and bounded synchronous task runner."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from .execution import (
    ExecutionReason,
    ExecutionState,
    legacy_execution_status,
)
from .task_config import TaskDefinition, TaskLimits

OutputCallback = Callable[[bytes], None]


class TaskExecutionError(RuntimeError):
    """Raised when an authorized task cannot be safely executed."""


@dataclass(frozen=True, slots=True)
class ContainerRequest:
    """A fully server-generated container invocation."""

    container_name: str
    snapshot_path: Path
    task: TaskDefinition
    limits: TaskLimits
    artifact_path: Path | None = None
    container_workdir: str = "/workspace"
    initial_workspace_bytes: int = 0
    started_at: float | None = None
    deadline: float | None = None


class ContainerHandle(Protocol):
    """A tracked container process created by a backend."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the container and return its exit code."""

    def stop(self) -> None:
        """Stop only this tracked container."""

    def close(self) -> None:
        """Release local pipe/process resources."""


class ContainerBackend(Protocol):
    """Backend boundary used by fake unit-test and production implementations."""

    def start(
        self,
        request: ContainerRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ContainerHandle:
        """Start one container without interpreting any command through a shell."""


class CliContainerBackend:
    """Production Docker/Podman CLI backend."""

    def __init__(self, runtime: str) -> None:
        if runtime not in {"docker", "podman"}:
            raise TaskExecutionError("container runtime must be docker or podman")
        self.runtime = runtime
        self.executable = shutil.which(runtime)

    def start(
        self,
        request: ContainerRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ContainerHandle:
        if self.executable is None:
            raise TaskExecutionError(
                f"container runtime {self.runtime!r} was not found; "
                "host execution fallback is disabled"
            )
        argv = build_container_argv(self.executable, request)
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_runtime_environment(),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise TaskExecutionError(
                f"failed to start container runtime {self.runtime!r}: {exc}"
            ) from exc
        return _CliContainerHandle(
            executable=self.executable,
            process=process,
            container_name=request.container_name,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )


class _CliContainerHandle:
    def __init__(
        self,
        *,
        executable: str,
        process: subprocess.Popen[bytes],
        container_name: str,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> None:
        self.executable = executable
        self.process = process
        self.container_name = container_name
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._readers = (
            threading.Thread(
                target=self._read_stream,
                args=(process.stdout, on_stdout),
                name=f"{container_name}-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=(process.stderr, on_stderr),
                name=f"{container_name}-stderr",
                daemon=True,
            ),
        )
        for reader in self._readers:
            reader.start()

    @staticmethod
    def _read_stream(
        stream: object | None,
        callback: OutputCallback,
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                # BufferedReader.read(size) may wait for size bytes or EOF.
                chunk = stream.read1(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    return
                callback(chunk)
        finally:
            stream.close()  # type: ignore[attr-defined]

    def wait(self, timeout: float | None = None) -> int:
        try:
            return_code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError from exc
        self._join_readers()
        return return_code

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped or self.process.poll() is not None:
                self._stopped = True
                return
            self._stopped = True

        stop_result: subprocess.CompletedProcess[bytes] | None = None
        try:
            stop_result = subprocess.run(
                [self.executable, "stop", "--time", "3", self.container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_runtime_environment(),
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if stop_result is None or stop_result.returncode != 0:
            try:
                subprocess.run(
                    [self.executable, "kill", self.container_name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=_runtime_environment(),
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.process.poll() is None:
            self.process.terminate()

    def close(self) -> None:
        self._join_readers()
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _join_readers(self) -> None:
        for reader in self._readers:
            reader.join(timeout=2)


def build_container_argv(executable: str, request: ContainerRequest) -> list[str]:
    """Render the complete hardened runtime argv without a shell."""

    snapshot = request.snapshot_path.resolve(strict=True)
    limits = request.limits
    mount = f"type=bind,source={snapshot},destination=/workspace"
    if request.task.workspace_access == "read-only":
        mount += ",readonly"
    artifact_mount: str | None = None
    if request.artifact_path is not None:
        artifact_path = request.artifact_path.resolve(strict=True)
        artifact_mount = f"type=bind,source={artifact_path},destination=/artifacts"
    argv = [
        executable,
        "run",
        "--rm",
        "--name",
        request.container_name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        _container_user(),
        "--memory",
        limits.memory,
        "--cpus",
        limits.cpus,
        "--pids-limit",
        str(limits.pids),
        "--workdir",
        _validated_container_workdir(request.container_workdir),
        "--mount",
        mount,
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--tmpfs",
        "/run:rw,nosuid,nodev,noexec,size=16m",
        "--env",
        "HOME=/tmp/home",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "XDG_CACHE_HOME=/tmp/cache",
        "--env",
        "RUFF_CACHE_DIR=/tmp/cache/ruff",
        "--env",
        "MYPY_CACHE_DIR=/tmp/cache/mypy",
        "--env",
        "COVERAGE_FILE=/tmp/.coverage",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONPYCACHEPREFIX=/tmp/cache/python",
        "--env",
        "PIP_NO_CACHE_DIR=1",
        "--env",
        "npm_config_cache=/tmp/npm-cache",
        "--env",
        "CI=1",
    ]
    if artifact_mount is not None:
        argv.extend(
            [
                "--mount",
                artifact_mount,
                "--env",
                "WORKSPACEGUARD_ARTIFACT_DIR=/artifacts",
            ]
        )
    if request.task.workspace_access == "writable":
        argv.extend(
            [
                "--ulimit",
                "fsize="
                f"{limits.max_workspace_file_bytes}:"
                f"{limits.max_workspace_file_bytes}",
            ]
        )
    argv.extend([request.task.image, *request.task.argv])
    return argv


class ArtifactGrowthMonitor:
    """Best-effort early enforcement for the explicit artifact staging boundary."""

    def __init__(self, request: ContainerRequest, handle: ContainerHandle) -> None:
        self.request = request
        self.handle = handle
        self.limit_exceeded = threading.Event()
        self.policy_violation = threading.Event()
        self._stop = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"{request.container_name}-artifact-monitor",
            daemon=True,
        )

    def start(self) -> None:
        if self.request.artifact_path is not None:
            self._thread.start()
            self._started = True

    def stop_and_join(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                usage = self._measure_usage()
            except OSError:
                pressure = 0.0
            else:
                if usage.policy_violation:
                    self.policy_violation.set()
                    self.handle.stop()
                    return
                if usage.limit_exceeded:
                    self.limit_exceeded.set()
                    self.handle.stop()
                    return
                pressure = usage.pressure
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - started
            self._stop.wait(_next_workspace_scan_delay(elapsed, pressure))

    def _measure_usage(self) -> _ArtifactUsage:
        if self.request.artifact_path is None:
            return _ArtifactUsage(False, False, 0.0)
        count = 0
        largest = 0
        total = 0
        with os.scandir(self.request.artifact_path) as entries:
            for entry in entries:
                if self._stop.is_set():
                    return _ArtifactUsage(False, False, 0.0)
                metadata = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    return _ArtifactUsage(False, True, 1.0)
                count += 1
                largest = max(largest, metadata.st_size)
                total += metadata.st_size
                pressure = max(
                    count / self.request.limits.max_artifacts_per_execution,
                    largest / self.request.limits.max_artifact_bytes,
                    total / self.request.limits.max_total_artifact_bytes,
                )
                if pressure > 1.0:
                    return _ArtifactUsage(True, False, pressure)
        return _ArtifactUsage(
            False,
            False,
            max(
                count / self.request.limits.max_artifacts_per_execution,
                largest / self.request.limits.max_artifact_bytes,
                total / self.request.limits.max_total_artifact_bytes,
            ),
        )


@dataclass(frozen=True, slots=True)
class _ArtifactUsage:
    limit_exceeded: bool
    policy_violation: bool
    pressure: float


@dataclass(frozen=True, slots=True)
class WorkspaceUsage:
    """One bounded snapshot measurement shared by enforcement and accounting."""

    total_bytes: int
    largest_file_bytes: int
    growth_bytes: int
    exceeded: bool
    pressure: float


_WorkspaceUsage = WorkspaceUsage


def measure_workspace_usage(
    snapshot_path: Path,
    *,
    initial_workspace_bytes: int,
    limits: TaskLimits,
    stop_event: threading.Event | None = None,
    stop_on_exceeded: bool = False,
) -> WorkspaceUsage:
    """Measure snapshot usage, optionally stopping as soon as a limit is exceeded."""

    total = 0
    largest_file = 0
    pending = [snapshot_path]
    while pending:
        if stop_event is not None and stop_event.is_set():
            break
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if stop_event is not None and stop_event.is_set():
                    break
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    largest_file = max(largest_file, metadata.st_size)
                    total += metadata.st_size
                    if stop_on_exceeded:
                        pressure = _workspace_pressure(
                            largest_file,
                            total,
                            initial_workspace_bytes,
                            limits.max_workspace_file_bytes,
                            limits.max_workspace_growth_bytes,
                        )
                        if pressure > 1.0:
                            return WorkspaceUsage(
                                total_bytes=total,
                                largest_file_bytes=largest_file,
                                growth_bytes=max(0, total - initial_workspace_bytes),
                                exceeded=True,
                                pressure=pressure,
                            )
    if stop_event is not None and stop_event.is_set():
        return WorkspaceUsage(
            total_bytes=0,
            largest_file_bytes=0,
            growth_bytes=0,
            exceeded=False,
            pressure=0.0,
        )
    growth = max(0, total - initial_workspace_bytes)
    pressure = _workspace_pressure(
        largest_file,
        total,
        initial_workspace_bytes,
        limits.max_workspace_file_bytes,
        limits.max_workspace_growth_bytes,
    )
    return WorkspaceUsage(
        total_bytes=total,
        largest_file_bytes=largest_file,
        growth_bytes=growth,
        exceeded=pressure > 1.0,
        pressure=pressure,
    )


class WorkspaceGrowthMonitor:
    """Best-effort host-side monitor for writable snapshot growth."""

    def __init__(self, request: ContainerRequest, handle: ContainerHandle) -> None:
        self.request = request
        self.handle = handle
        self.exceeded = threading.Event()
        self._stop = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"{request.container_name}-workspace-monitor",
            daemon=True,
        )

    def start(self) -> None:
        if self.request.task.workspace_access == "writable":
            self._thread.start()
            self._started = True

    def stop_and_join(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                usage = self._measure_usage()
                if usage.exceeded:
                    self.exceeded.set()
                    self.handle.stop()
                    return
            except OSError:
                # Aggregate enforcement is best effort while the runtime mutates files.
                pressure = 0.0
            else:
                pressure = usage.pressure
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - started
            self._stop.wait(_next_workspace_scan_delay(elapsed, pressure))

    def _limit_exceeded(self) -> bool:
        return self._measure_usage().exceeded

    def _measure_usage(self) -> WorkspaceUsage:
        return measure_workspace_usage(
            self.request.snapshot_path,
            initial_workspace_bytes=self.request.initial_workspace_bytes,
            limits=self.request.limits,
            stop_event=self._stop,
            stop_on_exceeded=True,
        )


def _workspace_pressure(
    largest_file: int,
    total: int,
    initial_workspace_bytes: int,
    max_workspace_file_bytes: int,
    max_workspace_growth_bytes: int,
) -> float:
    file_pressure = largest_file / max_workspace_file_bytes
    growth = max(0, total - initial_workspace_bytes)
    growth_pressure = growth / max_workspace_growth_bytes
    return max(file_pressure, growth_pressure)


def _next_workspace_scan_delay(scan_duration: float, pressure: float) -> float:
    """Return the delay that targets low scan duty cycle near safe limits."""

    if pressure >= 0.8:
        return 0.1
    return min(2.0, max(0.25, scan_duration * 9))


@dataclass(frozen=True, slots=True)
class TaskRunResult:
    """Canonical synchronous execution result with runtime observations."""

    state: ExecutionState
    reason: ExecutionReason | None
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    @property
    def status(self) -> str:
        return legacy_execution_status(self.state, self.reason)

    @property
    def timed_out(self) -> bool:
        return self.state is ExecutionState.TIMED_OUT

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
        }


class BoundedOutput:
    """Thread-safe bounded capture with lifetime runtime byte counters."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._size = 0
        self._truncated = False
        self._observed_stdout_bytes = 0
        self._observed_stderr_bytes = 0
        self._lock = threading.Lock()

    def stdout(self, data: bytes) -> None:
        self._append(self._stdout, data, stream="stdout", observed=True)

    def stderr(self, data: bytes) -> None:
        self._append(self._stderr, data, stream="stderr", observed=True)

    def diagnostic_stderr(self, data: bytes) -> None:
        """Retain server diagnostics without counting them as runtime stderr."""

        self._append(self._stderr, data, stream="stderr", observed=False)

    def _append(
        self, target: bytearray, data: bytes, *, stream: str, observed: bool
    ) -> None:
        with self._lock:
            if observed:
                if stream == "stdout":
                    self._observed_stdout_bytes += len(data)
                else:
                    self._observed_stderr_bytes += len(data)
            remaining = self.limit - self._size
            if remaining <= 0:
                if data:
                    self._truncated = True
                return
            accepted = data[:remaining]
            target.extend(accepted)
            self._size += len(accepted)
            if len(accepted) < len(data):
                self._truncated = True

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._truncated

    @property
    def observed_stdout_bytes(self) -> int:
        with self._lock:
            return self._observed_stdout_bytes

    @property
    def observed_stderr_bytes(self) -> int:
        with self._lock:
            return self._observed_stderr_bytes

    def text(self) -> tuple[str, str]:
        with self._lock:
            stdout = bytes(self._stdout)
            stderr = bytes(self._stderr)
        return _decode_bounded(stdout, len(stdout)), _decode_bounded(
            stderr, len(stderr)
        )


def run_container_task(
    backend: ContainerBackend,
    request: ContainerRequest,
    cancellation_event: threading.Event | None = None,
    *,
    on_started: Callable[[], None] | None = None,
    on_cancelling: Callable[[], None] | None = None,
) -> TaskRunResult:
    """Run one container with bounded output and canonical lifecycle semantics."""

    started = request.started_at or time.monotonic()
    deadline = request.deadline or started + request.limits.timeout_seconds
    capture = BoundedOutput(request.limits.max_output_bytes)
    if time.monotonic() >= deadline:
        return TaskRunResult(
            state=ExecutionState.TIMED_OUT,
            reason=ExecutionReason.TIMEOUT,
            exit_code=None,
            stdout="",
            stderr="task timeout expired before container start",
            truncated=False,
            duration_ms=_duration_ms(started),
        )
    handle: ContainerHandle | None = None
    try:
        handle = backend.start(request, capture.stdout, capture.stderr)
        if on_started is not None:
            on_started()
    except Exception as exc:
        if handle is not None:
            try:
                handle.stop()
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass
        if cancellation_event is not None and cancellation_event.is_set():
            if on_cancelling is not None:
                on_cancelling()
            return TaskRunResult(
                state=ExecutionState.CANCELLED,
                reason=None,
                exit_code=None,
                stdout="",
                stderr="task cancelled before container start completed",
                truncated=False,
                duration_ms=_duration_ms(started),
            )
        return TaskRunResult(
            state=ExecutionState.CRASHED,
            reason=ExecutionReason.RUNTIME_START_FAILED,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            truncated=False,
            duration_ms=_duration_ms(started),
        )

    assert handle is not None
    exit_code: int | None = None
    timed_out = False
    output_overflow = False
    cancelled = False
    workspace_limit_exceeded = False
    artifact_limit_exceeded = False
    artifact_policy_violation = False
    cleanup_failed = False
    monitor_failed = False
    workspace_monitor = WorkspaceGrowthMonitor(request, handle)
    artifact_monitor = ArtifactGrowthMonitor(request, handle)

    def record_cleanup_failure(phase: str, exc: Exception) -> None:
        nonlocal cleanup_failed
        cleanup_failed = True
        capture.diagnostic_stderr(f"{phase}: {exc}\n".encode("utf-8", errors="replace"))

    def stop_handle() -> None:
        try:
            handle.stop()
        except Exception as exc:
            record_cleanup_failure("container stop failure", exc)

    def close_handle() -> None:
        try:
            handle.close()
        except Exception as exc:
            record_cleanup_failure("container cleanup failure", exc)

    try:
        workspace_monitor.start()
        artifact_monitor.start()
    except Exception as exc:
        capture.diagnostic_stderr(
            f"workspace monitor start failure: {exc}\n".encode(
                "utf-8", errors="replace"
            )
        )
        stop_handle()
        for label, monitor in (
            ("workspace", workspace_monitor),
            ("artifact", artifact_monitor),
        ):
            try:
                monitor.stop_and_join()
            except Exception as cleanup_exc:
                record_cleanup_failure(f"{label} monitor cleanup failure", cleanup_exc)
        close_handle()
        stdout, stderr = capture.text()
        return TaskRunResult(
            state=ExecutionState.CRASHED,
            reason=ExecutionReason.RUNTIME_MONITOR_FAILED,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            truncated=capture.truncated,
            duration_ms=_duration_ms(started),
            stdout_bytes=capture.observed_stdout_bytes,
            stderr_bytes=capture.observed_stderr_bytes,
        )

    try:
        while True:
            if workspace_monitor.exceeded.is_set():
                workspace_limit_exceeded = True
                break
            if artifact_monitor.policy_violation.is_set():
                artifact_policy_violation = True
                break
            if artifact_monitor.limit_exceeded.is_set():
                artifact_limit_exceeded = True
                break
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                if on_cancelling is not None:
                    on_cancelling()
                stop_handle()
                break
            if capture.truncated:
                output_overflow = True
                stop_handle()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                stop_handle()
                break
            try:
                exit_code = handle.wait(timeout=min(remaining, 0.1))
                break
            except TimeoutError:
                continue
        if exit_code is None:
            try:
                exit_code = handle.wait(timeout=5)
            except TimeoutError:
                stop_handle()
    except Exception as exc:
        monitor_failed = True
        capture.diagnostic_stderr(
            f"container monitor failure: {exc}\n".encode("utf-8", errors="replace")
        )
        stop_handle()
    except BaseException:
        stop_handle()
        raise
    finally:
        for label, monitor in (
            ("workspace", workspace_monitor),
            ("artifact", artifact_monitor),
        ):
            try:
                monitor.stop_and_join()
            except Exception as exc:
                record_cleanup_failure(f"{label} monitor cleanup failure", exc)
        close_handle()

    if capture.truncated:
        output_overflow = True
    if cancellation_event is not None and cancellation_event.is_set():
        cancelled = True
    stdout, stderr = capture.text()
    if workspace_monitor.exceeded.is_set():
        workspace_limit_exceeded = True
    if artifact_monitor.policy_violation.is_set():
        artifact_policy_violation = True
    if artifact_monitor.limit_exceeded.is_set():
        artifact_limit_exceeded = True

    if monitor_failed:
        state = ExecutionState.CRASHED
        reason = ExecutionReason.RUNTIME_MONITOR_FAILED
    elif cleanup_failed:
        state = ExecutionState.CRASHED
        reason = ExecutionReason.CLEANUP_FAILED
    elif workspace_limit_exceeded:
        state = ExecutionState.FAILED
        reason = ExecutionReason.WORKSPACE_LIMIT_EXCEEDED
    elif artifact_policy_violation:
        state = ExecutionState.FAILED
        reason = ExecutionReason.ARTIFACT_POLICY_VIOLATION
    elif artifact_limit_exceeded:
        state = ExecutionState.FAILED
        reason = ExecutionReason.ARTIFACT_LIMIT_EXCEEDED
    elif cancelled:
        state = ExecutionState.CANCELLED
        reason = None
    elif output_overflow:
        state = ExecutionState.FAILED
        reason = ExecutionReason.OUTPUT_LIMIT_EXCEEDED
    elif timed_out:
        state = ExecutionState.TIMED_OUT
        reason = ExecutionReason.TIMEOUT
    elif exit_code == 0:
        state = ExecutionState.SUCCEEDED
        reason = None
    else:
        state = ExecutionState.FAILED
        reason = None
    return TaskRunResult(
        state=state,
        reason=reason,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated=capture.truncated,
        duration_ms=_duration_ms(started),
        stdout_bytes=capture.observed_stdout_bytes,
        stderr_bytes=capture.observed_stderr_bytes,
    )


def _container_user() -> str:
    get_uid = getattr(os, "getuid", None)
    get_gid = getattr(os, "getgid", None)
    if get_uid is not None and get_gid is not None:
        uid = get_uid()
        gid = get_gid()
        if uid > 0:
            return f"{uid}:{gid}"
    return "65532:65532"


def _validated_container_workdir(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise TaskExecutionError("container workdir must be inside /workspace")
    if value != "/workspace" and not value.startswith("/workspace/"):
        raise TaskExecutionError("container workdir must be inside /workspace")
    if PurePosixPath(value).as_posix() != value or ".." in PurePosixPath(value).parts:
        raise TaskExecutionError("container workdir must be a canonical workspace path")
    return value


def _runtime_environment() -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _decode_bounded(data: bytes, limit: int) -> str:
    rendered = data.decode("utf-8", errors="replace")
    encoded = rendered.encode("utf-8")
    if len(encoded) <= limit:
        return rendered
    return encoded[:limit].decode("utf-8", errors="ignore")
