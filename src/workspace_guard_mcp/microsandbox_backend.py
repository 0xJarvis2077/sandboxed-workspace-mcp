"""Optional Microsandbox execution backend adapter."""

from __future__ import annotations

import asyncio
import errno
import importlib
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from types import ModuleType
from typing import Any, Protocol, TypeVar, cast

from .execution_backend import ExecutionHandle, ExecutionRequest, OutputCallback
from .execution_identity import local_execution_user

_OCI_DIGEST_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-fA-F]{64}\Z")
_LOCAL_IMAGE_TAG = re.compile(
    r"local/[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*:"
    r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z"
)
_MEMORY = re.compile(r"([1-9][0-9]*)([kKmMgG])?(?:[bB])?\Z")
_MIB = 1024 * 1024
_MAX_MICROSANDBOX_CPUS = 255
_MAX_MICROSANDBOX_MEMORY_MIB = (1 << 32) - 1
_T = TypeVar("_T")


class MicrosandboxExecutionError(RuntimeError):
    """Raised when a request cannot be safely executed by Microsandbox."""


class _SdkExecHandle(Protocol):
    def __aiter__(self) -> AsyncIterator[object]: ...


class _MicrosandboxSdk(Protocol):
    def bind_volume(
        self,
        path: str,
        *,
        readonly: bool,
        noexec: bool,
        nosuid: bool,
        nodev: bool,
    ) -> object: ...

    def network_none(self) -> object: ...

    def rlimit_nproc(self, limit: int) -> object: ...

    def rlimit_fsize(self, limit: int) -> object: ...

    def stdin_null(self) -> object: ...

    async def create_sandbox(
        self,
        name: str,
        *,
        image: str,
        cpus: int,
        memory: int,
        pull_policy: str,
        security: str,
        network: object,
        volumes: Mapping[str, object],
    ) -> object: ...

    async def exec_stream(
        self,
        sandbox: object,
        cmd: str,
        args: list[str],
        *,
        cwd: str,
        user: str,
        env: Mapping[str, str],
        timeout: float | None,
        stdin: object,
        tty: bool,
        rlimits: list[object],
    ) -> _SdkExecHandle: ...

    async def wait_exec(self, handle: object) -> int: ...

    async def kill_exec(self, handle: object) -> None: ...

    async def stop_sandbox(self, sandbox: object, timeout: float) -> None: ...

    async def kill_sandbox(self, sandbox: object, timeout: float) -> None: ...

    async def remove_sandbox(self, name: str) -> None: ...


class _LoadedMicrosandboxSdk:
    """Narrow dynamic boundary around the optional third-party SDK."""

    def __init__(self, module: ModuleType) -> None:
        self._module: Any = module

    def bind_volume(
        self,
        path: str,
        *,
        readonly: bool,
        noexec: bool,
        nosuid: bool,
        nodev: bool,
    ) -> object:
        mount = self._module.Volume.bind(
            path,
            readonly=readonly,
            noexec=noexec,
            nosuid=nosuid,
            nodev=nodev,
        )
        return cast(
            object,
            replace(
                mount,
                stat_virtualization="off",
                host_permissions="private",
            ),
        )

    def network_none(self) -> object:
        return cast(object, self._module.Network.none())

    def rlimit_nproc(self, limit: int) -> object:
        return cast(object, self._module.Rlimit.nproc(limit))

    def rlimit_fsize(self, limit: int) -> object:
        return cast(object, self._module.Rlimit.fsize(limit))

    def stdin_null(self) -> object:
        return cast(object, self._module.Stdin.null())

    async def create_sandbox(
        self,
        name: str,
        *,
        image: str,
        cpus: int,
        memory: int,
        pull_policy: str,
        security: str,
        network: object,
        volumes: Mapping[str, object],
    ) -> object:
        return cast(
            object,
            await self._module.Sandbox.create(
                name,
                image=image,
                cpus=cpus,
                memory=memory,
                pull_policy=pull_policy,
                security=security,
                network=network,
                volumes=dict(volumes),
            ),
        )

    async def exec_stream(
        self,
        sandbox: object,
        cmd: str,
        args: list[str],
        *,
        cwd: str,
        user: str,
        env: Mapping[str, str],
        timeout: float | None,
        stdin: object,
        tty: bool,
        rlimits: list[object],
    ) -> _SdkExecHandle:
        dynamic_sandbox: Any = sandbox
        return cast(
            _SdkExecHandle,
            await dynamic_sandbox.exec_stream(
                cmd,
                args,
                cwd=cwd,
                user=user,
                env=dict(env),
                timeout=timeout,
                stdin=stdin,
                tty=tty,
                rlimits=rlimits,
            ),
        )

    async def wait_exec(self, handle: object) -> int:
        dynamic_handle: Any = handle
        code, _success = await dynamic_handle.wait()
        return cast(int, code)

    async def kill_exec(self, handle: object) -> None:
        dynamic_handle: Any = handle
        await dynamic_handle.kill()

    async def stop_sandbox(self, sandbox: object, timeout: float) -> None:
        dynamic_sandbox: Any = sandbox
        await dynamic_sandbox.stop(timeout=timeout)

    async def kill_sandbox(self, sandbox: object, timeout: float) -> None:
        dynamic_sandbox: Any = sandbox
        await dynamic_sandbox.kill(timeout=timeout)

    async def remove_sandbox(self, name: str) -> None:
        await self._module.Sandbox.remove(name)


def _load_microsandbox_sdk() -> _MicrosandboxSdk:
    try:
        module = importlib.import_module("microsandbox")
    except ModuleNotFoundError as exc:
        if exc.name == "microsandbox":
            raise MicrosandboxExecutionError(
                "Microsandbox backend requires the 'microsandbox' optional dependency"
            ) from exc
        raise MicrosandboxExecutionError(
            f"Microsandbox SDK could not be loaded: {exc}"
        ) from exc
    except (ImportError, OSError) as exc:
        raise MicrosandboxExecutionError(
            f"Microsandbox SDK could not be loaded: {exc}"
        ) from exc
    return _LoadedMicrosandboxSdk(module)


class MicrosandboxBackend:
    """Local microVM execution backend using the Microsandbox Python SDK."""

    def __init__(self, *, _sdk: _MicrosandboxSdk | None = None) -> None:
        self._sdk = _load_microsandbox_sdk() if _sdk is None else _sdk

    def start(
        self,
        request: ExecutionRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ExecutionHandle:
        image = _validated_microsandbox_image(request.task.image)
        cpus = _parse_microsandbox_cpus(request.limits.cpus)
        memory_mib = _parse_microsandbox_memory_mib(request.limits.memory)
        workdir = _validated_microsandbox_workdir(request.workdir)
        deadline = _request_deadline(request)
        if time.monotonic() >= deadline:
            raise TimeoutError("Microsandbox startup exceeded execution deadline")

        workspace = request.workspace_path.resolve(strict=True)
        volumes: dict[str, object] = {
            "/workspace": self._sdk.bind_volume(
                str(workspace),
                readonly=request.task.workspace_access == "read-only",
                noexec=False,
                nosuid=True,
                nodev=True,
            )
        }
        if request.artifact_path is not None:
            artifact_path = request.artifact_path.resolve(strict=True)
            volumes["/artifacts"] = self._sdk.bind_volume(
                str(artifact_path),
                readonly=False,
                noexec=False,
                nosuid=True,
                nodev=True,
            )

        rlimits = [self._sdk.rlimit_nproc(request.limits.pids)]
        if request.task.workspace_access == "writable":
            rlimits.append(
                self._sdk.rlimit_fsize(request.limits.max_workspace_file_bytes)
            )

        handle = _MicrosandboxHandle(
            sdk=self._sdk,
            request=request,
            image=image,
            cpus=cpus,
            memory_mib=memory_mib,
            workdir=workdir,
            volumes=volumes,
            rlimits=rlimits,
            deadline=deadline,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        try:
            handle._start_worker()
            handle._wait_until_started()
        except BaseException:
            try:
                handle.close()
            except Exception:
                pass
            raise
        return handle


class _MicrosandboxHandle:
    def __init__(
        self,
        *,
        sdk: _MicrosandboxSdk,
        request: ExecutionRequest,
        image: str,
        cpus: int,
        memory_mib: int,
        workdir: str,
        volumes: Mapping[str, object],
        rlimits: list[object],
        deadline: float,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> None:
        self._sdk = sdk
        self._request = request
        self._image = image
        self._cpus = cpus
        self._memory_mib = memory_mib
        self._workdir = workdir
        self._volumes = dict(volumes)
        self._rlimits = list(rlimits)
        self._deadline = deadline
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr

        self._loop: asyncio.AbstractEventLoop | None = None
        self._execution_task: asyncio.Task[None] | None = None
        self._sandbox: object | None = None
        self._exec_handle: _SdkExecHandle | None = None
        self._exit_code: int | None = None
        self._startup_error: BaseException | None = None
        self._execution_error: BaseException | None = None

        self._loop_ready = threading.Event()
        self._started = threading.Event()
        self._completed = threading.Event()
        self._stop_requested = threading.Event()
        self._close_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._closed = False
        self._stop_submitted = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"{request.runtime_name}-microsandbox",
            daemon=True,
        )

    def _start_worker(self) -> None:
        self._thread.start()
        if not self._loop_ready.wait(timeout=self._startup_remaining()):
            raise TimeoutError("Microsandbox startup exceeded execution deadline")

    def _wait_until_started(self) -> None:
        if not self._started.wait(timeout=self._startup_remaining()):
            self._stop_requested.set()
            raise TimeoutError("Microsandbox startup exceeded execution deadline")
        if self._startup_error is not None:
            if isinstance(self._startup_error, TimeoutError):
                raise self._startup_error
            raise MicrosandboxExecutionError(
                f"Microsandbox runtime start failed: {self._startup_error}"
            ) from self._startup_error

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        if not self._completed.wait(timeout=timeout):
            raise TimeoutError
        if self._execution_error is not None:
            if isinstance(self._execution_error, TimeoutError):
                raise TimeoutError(
                    str(self._execution_error)
                ) from self._execution_error
            raise MicrosandboxExecutionError(
                f"Microsandbox execution failed: {self._execution_error}"
            ) from self._execution_error
        return -1 if self._exit_code is None else self._exit_code

    def stop(self) -> None:
        self._stop_requested.set()
        with self._stop_lock:
            if self._stop_submitted or self._closed:
                return
            self._stop_submitted = True
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._stop_async(), loop)
        try:
            future.result(timeout=10)
        except FutureTimeoutError as exc:
            raise MicrosandboxExecutionError(
                "Microsandbox stop did not complete"
            ) from exc
        except Exception as exc:
            raise MicrosandboxExecutionError(
                f"Microsandbox stop failed: {exc}"
            ) from exc

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        self._stop_requested.set()
        cleanup_error: Exception | None = None
        loop = self._loop
        if loop is not None and not loop.is_closed():
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._cleanup_async(), loop)
                try:
                    future.result(timeout=15)
                except FutureTimeoutError as exc:
                    cleanup_error = MicrosandboxExecutionError(
                        "Microsandbox cleanup did not complete"
                    )
                    cleanup_error.__cause__ = exc
                except Exception as exc:
                    cleanup_error = MicrosandboxExecutionError(
                        f"Microsandbox cleanup failed: {exc}"
                    )
                    cleanup_error.__cause__ = exc
                finally:
                    loop.call_soon_threadsafe(loop.stop)
            else:
                loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=5)
        if self._thread.is_alive() and cleanup_error is None:
            cleanup_error = MicrosandboxExecutionError(
                "Microsandbox event-loop worker did not terminate"
            )
        if cleanup_error is not None:
            raise cleanup_error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        if self._closed:
            loop.close()
            asyncio.set_event_loop(None)
            return
        self._execution_task = loop.create_task(self._run_execution())
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            asyncio.set_event_loop(None)

    async def _run_execution(self) -> None:
        try:
            self._sandbox = await self._await_before_deadline(
                self._sdk.create_sandbox(
                    self._request.runtime_name,
                    image=self._image,
                    cpus=self._cpus,
                    memory=self._memory_mib,
                    pull_policy="never",
                    security="restricted",
                    network=self._sdk.network_none(),
                    volumes=self._volumes,
                )
            )
            command_timeout = self._remaining()
            self._exec_handle = await self._await_before_deadline(
                self._sdk.exec_stream(
                    self._sandbox,
                    self._request.task.argv[0],
                    list(self._request.task.argv[1:]),
                    cwd=self._workdir,
                    user=local_execution_user(),
                    env=_guest_environment(self._request.artifact_path is not None),
                    timeout=command_timeout,
                    stdin=self._sdk.stdin_null(),
                    tty=False,
                    rlimits=self._rlimits,
                )
            )
            self._started.set()
            async for event in self._exec_handle:
                event_type = _event_type(event)
                if event_type == "stdout":
                    self._on_stdout(_event_data(event, "stdout"))
                elif event_type == "stderr":
                    self._on_stderr(_event_data(event, "stderr"))
                elif event_type == "exited":
                    self._exit_code = _event_code(event)
                elif event_type == "started":
                    continue
                elif event_type == "failed":
                    failed_errno = _event_code(event)
                    if failed_errno == errno.ENOENT:
                        self._on_stderr(_event_data(event, "failed"))
                        self._exit_code = 127
                        break
                    raise MicrosandboxExecutionError(
                        "Microsandbox command failed after exec stream start"
                    )
                elif event_type == "stdin_error":
                    raise MicrosandboxExecutionError(
                        "Microsandbox reported a stdin error for null stdin"
                    )
                else:
                    raise MicrosandboxExecutionError(
                        f"Microsandbox returned unsupported exec event {event_type!r}"
                    )
            if self._exit_code is None and not self._stop_requested.is_set():
                self._exit_code = await self._sdk.wait_exec(self._exec_handle)
        except asyncio.CancelledError:
            if not self._stop_requested.is_set():
                self._execution_error = MicrosandboxExecutionError(
                    "Microsandbox execution worker was cancelled unexpectedly"
                )
            raise
        except asyncio.TimeoutError as exc:
            startup_timed_out = not self._started.is_set()
            phase = "startup" if startup_timed_out else "execution"
            timeout_error = TimeoutError(
                f"Microsandbox {phase} exceeded execution deadline"
            )
            timeout_error.__cause__ = exc
            if startup_timed_out:
                self._startup_error = timeout_error
                await self._cleanup_partial_startup()
            elif not self._stop_requested.is_set():
                self._execution_error = timeout_error
                await self._stop_async_best_effort()
        except BaseException as exc:
            if not self._started.is_set():
                self._startup_error = exc
                await self._cleanup_partial_startup()
            elif not self._stop_requested.is_set():
                self._execution_error = exc
                await self._stop_async_best_effort()
        finally:
            if not self._started.is_set():
                self._started.set()
            self._completed.set()

    async def _cleanup_partial_startup(self) -> None:
        if self._sandbox is None:
            return
        await self._stop_sandbox_best_effort()
        try:
            await self._sdk.remove_sandbox(self._request.runtime_name)
        except Exception:
            return
        self._sandbox = None

    async def _stop_async(self) -> None:
        exec_error: Exception | None = None
        if self._exec_handle is not None and not self._completed.is_set():
            try:
                await self._sdk.kill_exec(self._exec_handle)
            except Exception as exc:
                exec_error = exc
        sandbox_stopped = await self._stop_sandbox_best_effort()
        if not sandbox_stopped and exec_error is not None:
            raise exec_error
        if not sandbox_stopped and self._sandbox is not None:
            raise MicrosandboxExecutionError(
                "Microsandbox sandbox could not be stopped"
            )

    async def _stop_async_best_effort(self) -> None:
        try:
            await self._stop_async()
        except Exception:
            pass

    async def _stop_sandbox_best_effort(self) -> bool:
        if self._sandbox is None:
            return True
        try:
            await self._sdk.stop_sandbox(self._sandbox, 3.0)
            return True
        except Exception:
            try:
                await self._sdk.kill_sandbox(self._sandbox, 3.0)
                return True
            except Exception:
                return False

    async def _cleanup_async(self) -> None:
        cleanup_errors: list[Exception] = []
        if self._exec_handle is not None and not self._completed.is_set():
            try:
                await self._sdk.kill_exec(self._exec_handle)
            except Exception as exc:
                cleanup_errors.append(exc)
        if self._sandbox is not None:
            if not await self._stop_sandbox_best_effort():
                cleanup_errors.append(
                    MicrosandboxExecutionError(
                        "Microsandbox sandbox could not be stopped"
                    )
                )

        task = self._execution_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                pass

        if self._sandbox is not None:
            try:
                await self._sdk.remove_sandbox(self._request.runtime_name)
            except Exception as exc:
                cleanup_errors.append(exc)
            self._sandbox = None
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _await_before_deadline(self, awaitable: Awaitable[_T]) -> _T:
        remaining = self._remaining()
        if remaining <= 0:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError
        return await asyncio.wait_for(awaitable, timeout=remaining)

    def _remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _startup_remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())


def _request_deadline(request: ExecutionRequest) -> float:
    if request.deadline is not None:
        return request.deadline
    started = request.started_at if request.started_at is not None else time.monotonic()
    return started + request.limits.timeout_seconds


def _parse_microsandbox_cpus(value: str) -> int:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MicrosandboxExecutionError(
            "configured CPU limit cannot be represented exactly by Microsandbox"
        ) from exc
    integral = parsed.to_integral_value()
    if (
        not parsed.is_finite()
        or parsed != integral
        or integral <= 0
        or integral > _MAX_MICROSANDBOX_CPUS
    ):
        raise MicrosandboxExecutionError(
            "configured CPU limit cannot be represented exactly by Microsandbox"
        )
    return int(integral)


def _parse_microsandbox_memory_mib(value: str) -> int:
    match = _MEMORY.fullmatch(value)
    if match is None:
        raise MicrosandboxExecutionError(
            "configured memory limit cannot be represented exactly by Microsandbox"
        )
    amount = int(match.group(1))
    suffix = (match.group(2) or "").lower()
    factor = {"": 1, "k": 1024, "m": _MIB, "g": 1024 * _MIB}[suffix]
    bytes_value = amount * factor
    if bytes_value % _MIB != 0:
        raise MicrosandboxExecutionError(
            "configured memory limit cannot be represented exactly by Microsandbox"
        )
    memory_mib = bytes_value // _MIB
    if memory_mib <= 0 or memory_mib > _MAX_MICROSANDBOX_MEMORY_MIB:
        raise MicrosandboxExecutionError(
            "configured memory limit cannot be represented exactly by Microsandbox"
        )
    return memory_mib


def _validated_microsandbox_image(value: str) -> str:
    if (
        _OCI_DIGEST_IMAGE.fullmatch(value) is None
        and _LOCAL_IMAGE_TAG.fullmatch(value) is None
    ):
        raise MicrosandboxExecutionError(
            "Microsandbox requires an OCI repository@sha256 digest or an explicit "
            "local/...:tag cache reference; Docker local sha256 image IDs are not "
            "supported"
        )
    return value


def _validated_microsandbox_workdir(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise MicrosandboxExecutionError(
            "Microsandbox workdir must be inside /workspace"
        )
    if value != "/workspace" and not value.startswith("/workspace/"):
        raise MicrosandboxExecutionError(
            "Microsandbox workdir must be inside /workspace"
        )
    path = PurePosixPath(value)
    if path.as_posix() != value or ".." in path.parts:
        raise MicrosandboxExecutionError(
            "Microsandbox workdir must be a canonical workspace path"
        )
    return value


def _guest_environment(has_artifact: bool) -> dict[str, str]:
    environment = {
        "HOME": "/tmp/home",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/tmp/cache",
        "RUFF_CACHE_DIR": "/tmp/cache/ruff",
        "MYPY_CACHE_DIR": "/tmp/cache/mypy",
        "COVERAGE_FILE": "/tmp/.coverage",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/cache/python",
        "PIP_NO_CACHE_DIR": "1",
        "npm_config_cache": "/tmp/npm-cache",
        "CI": "1",
    }
    if has_artifact:
        environment["WORKSPACEGUARD_ARTIFACT_DIR"] = "/artifacts"
    return environment


def _event_type(event: object) -> str:
    value = getattr(event, "event_type", None)
    if not isinstance(value, str):
        raise MicrosandboxExecutionError("Microsandbox exec event has no event_type")
    return value


def _event_data(event: object, stream: str) -> bytes:
    value = getattr(event, "data", None)
    if not isinstance(value, bytes):
        raise MicrosandboxExecutionError(
            f"Microsandbox {stream} event did not contain bytes"
        )
    return value


def _event_code(event: object) -> int:
    value = getattr(event, "code", None)
    if type(value) is not int:
        raise MicrosandboxExecutionError(
            "Microsandbox exited event has no integer code"
        )
    return value
