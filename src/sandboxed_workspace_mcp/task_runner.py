"""Container-only execution backend and bounded synchronous task runner."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

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
                # Aggregate accounting is explicitly best effort. A later pass may
                # observe entries that were concurrently renamed or removed.
                pressure = 0.0
            else:
                pressure = usage.pressure
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - started
            self._stop.wait(_next_workspace_scan_delay(elapsed, pressure))

    def _limit_exceeded(self) -> bool:
        return self._measure_usage().exceeded

    def _measure_usage(self) -> _WorkspaceUsage:
        total = 0
        largest_file = 0
        pending = [self.request.snapshot_path]
        while pending and not self._stop.is_set():
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if self._stop.is_set():
                        return _WorkspaceUsage(False, 0.0)
                    metadata = entry.stat(follow_symlinks=False)
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        largest_file = max(largest_file, metadata.st_size)
                        total += metadata.st_size
                        pressure = _workspace_pressure(
                            largest_file,
                            total,
                            self.request.initial_workspace_bytes,
                            self.request.limits.max_workspace_file_bytes,
                            self.request.limits.max_workspace_growth_bytes,
                        )
                        if pressure > 1.0:
                            return _WorkspaceUsage(True, pressure)
        if self._stop.is_set():
            return _WorkspaceUsage(False, 0.0)
        return _WorkspaceUsage(
            False,
            _workspace_pressure(
                largest_file,
                total,
                self.request.initial_workspace_bytes,
                self.request.limits.max_workspace_file_bytes,
                self.request.limits.max_workspace_growth_bytes,
            ),
        )


@dataclass(frozen=True, slots=True)
class _WorkspaceUsage:
    exceeded: bool
    pressure: float


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
    """Stable synchronous task result returned through MCP."""

    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_ms: int

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
    """Thread-safe bounded capture shared by stdout and stderr."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._size = 0
        self._truncated = False
        self._lock = threading.Lock()

    def stdout(self, data: bytes) -> None:
        self._append(self._stdout, data)

    def stderr(self, data: bytes) -> None:
        self._append(self._stderr, data)

    def _append(self, target: bytearray, data: bytes) -> None:
        with self._lock:
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
) -> TaskRunResult:
    """Run one container with bounded output, timeout, and explicit failures."""

    started = request.started_at or time.monotonic()
    deadline = request.deadline or started + request.limits.timeout_seconds
    capture = BoundedOutput(request.limits.max_output_bytes)
    if time.monotonic() >= deadline:
        return TaskRunResult(
            status="timed_out",
            exit_code=None,
            stdout="",
            stderr="task timeout expired before container start",
            truncated=False,
            timed_out=True,
            duration_ms=_duration_ms(started),
        )
    try:
        handle = backend.start(request, capture.stdout, capture.stderr)
    except Exception as exc:
        return TaskRunResult(
            status="start_failed",
            exit_code=None,
            stdout="",
            stderr=str(exc),
            truncated=False,
            timed_out=False,
            duration_ms=_duration_ms(started),
        )

    exit_code: int | None = None
    timed_out = False
    output_overflow = False
    cancelled = False
    workspace_limit_exceeded = False
    workspace_monitor = WorkspaceGrowthMonitor(request, handle)
    workspace_monitor.start()
    try:
        while True:
            if workspace_monitor.exceeded.is_set():
                workspace_limit_exceeded = True
                break
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                handle.stop()
                break
            if capture.truncated:
                output_overflow = True
                handle.stop()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                handle.stop()
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
                handle.stop()
    except BaseException:
        handle.stop()
        raise
    finally:
        workspace_monitor.stop_and_join()
        handle.close()

    if capture.truncated:
        output_overflow = True
    if cancellation_event is not None and cancellation_event.is_set():
        cancelled = True
    stdout, stderr = capture.text()
    if workspace_monitor.exceeded.is_set():
        workspace_limit_exceeded = True
    if workspace_limit_exceeded:
        status = "workspace_limit_exceeded"
    elif cancelled:
        status = "cancelled"
    elif output_overflow:
        status = "output_limit_exceeded"
    elif timed_out:
        status = "timed_out"
    elif exit_code == 0:
        status = "succeeded"
    else:
        status = "failed"
    return TaskRunResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        truncated=capture.truncated,
        timed_out=timed_out,
        duration_ms=_duration_ms(started),
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
