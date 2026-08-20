"""Hardened Docker/Podman CLI execution backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import PurePosixPath

from .execution_backend import (
    ExecutionHandle,
    ExecutionRequest,
    OutputCallback,
)
from .execution_identity import local_execution_user


class TaskExecutionError(RuntimeError):
    """Raised when an authorized task cannot be safely executed."""


class CliContainerBackend:
    """Production Docker/Podman CLI backend."""

    def __init__(self, runtime: str) -> None:
        if runtime not in {"docker", "podman"}:
            raise TaskExecutionError("container runtime must be docker or podman")
        self.runtime = runtime
        self.executable = shutil.which(runtime)

    def start(
        self,
        request: ExecutionRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ExecutionHandle:
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
            container_name=request.runtime_name,
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


def build_container_argv(executable: str, request: ExecutionRequest) -> list[str]:
    """Render the complete hardened runtime argv without a shell."""

    snapshot = request.workspace_path.resolve(strict=True)
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
        request.runtime_name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        local_execution_user(),
        "--memory",
        limits.memory,
        "--cpus",
        limits.cpus,
        "--pids-limit",
        str(limits.pids),
        "--workdir",
        _validated_container_workdir(request.workdir),
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
