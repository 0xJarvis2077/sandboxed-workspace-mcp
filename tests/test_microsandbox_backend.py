from __future__ import annotations

import asyncio
import errno
import os
import tempfile
import threading
import time
import unittest
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType
from unittest.mock import patch

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.execution import ExecutionReason, ExecutionState
from workspace_guard_mcp.execution_backend import ExecutionBackend, ExecutionRequest
from workspace_guard_mcp.microsandbox_backend import (
    MicrosandboxBackend,
    MicrosandboxExecutionError,
    _LoadedMicrosandboxSdk,
    _parse_microsandbox_cpus,
    _parse_microsandbox_memory_mib,
)
from workspace_guard_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager
from workspace_guard_mcp.task_runner import run_execution

PINNED_IMAGE = "example.invalid/workspace-guard-mcp@sha256:" + "a" * 64
LOCAL_IMAGE_ID = "sha256:" + "b" * 64


class FakeEvent:
    def __init__(
        self,
        event_type: str,
        *,
        data: bytes | None = None,
        code: int | None = None,
    ) -> None:
        self.event_type = event_type
        self.data = data
        self.code = code


class FakeExecHandle:
    def __init__(
        self,
        events: list[FakeEvent] | None = None,
        *,
        exit_code: int = 0,
        blocking: bool = False,
    ) -> None:
        self.events = list(events or [])
        self.exit_code = exit_code
        self.blocking = blocking
        self.killed = False
        self.kill_calls = 0
        self._release: asyncio.Event | None = None
        self._killed_exit_emitted = False

    def __aiter__(self) -> AsyncIterator[object]:
        return self

    async def __anext__(self) -> object:
        if self.events:
            return self.events.pop(0)
        if self.blocking and not self.killed:
            release = self._release_event()
            await release.wait()
        if self.killed and not self._killed_exit_emitted:
            self._killed_exit_emitted = True
            return FakeEvent("exited", code=-9)
        raise StopAsyncIteration

    async def wait(self) -> tuple[int, bool]:
        if self.blocking and not self.killed:
            release = self._release_event()
            await release.wait()
        code = -9 if self.killed else self.exit_code
        return code, code == 0

    async def kill(self) -> None:
        self.kill_calls += 1
        self.killed = True
        if self._release is not None:
            self._release.set()

    def _release_event(self) -> asyncio.Event:
        if self._release is None:
            self._release = asyncio.Event()
        return self._release


class FakeSdk:
    def __init__(
        self,
        *,
        exec_handle: FakeExecHandle | None = None,
        create_error: Exception | None = None,
        exec_error: Exception | None = None,
    ) -> None:
        self.exec_handle = exec_handle or FakeExecHandle([FakeEvent("exited", code=0)])
        self.create_error = create_error
        self.exec_error = exec_error
        self.create_calls: list[dict[str, object]] = []
        self.exec_calls: list[dict[str, object]] = []
        self.stop_calls = 0
        self.kill_sandbox_calls = 0
        self.remove_calls: list[str] = []
        self.sandbox = object()

    def bind_volume(
        self,
        path: str,
        *,
        readonly: bool,
        noexec: bool,
        nosuid: bool,
        nodev: bool,
    ) -> object:
        return {
            "path": path,
            "readonly": readonly,
            "noexec": noexec,
            "nosuid": nosuid,
            "nodev": nodev,
        }

    def network_none(self) -> object:
        return {"policy": "none"}

    def rlimit_nproc(self, limit: int) -> object:
        return ("nproc", limit, limit)

    def rlimit_fsize(self, limit: int) -> object:
        return ("fsize", limit, limit)

    def stdin_null(self) -> object:
        return {"stdin": "null"}

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
        self.create_calls.append(
            {
                "name": name,
                "image": image,
                "cpus": cpus,
                "memory": memory,
                "pull_policy": pull_policy,
                "security": security,
                "network": network,
                "volumes": dict(volumes),
            }
        )
        if self.create_error is not None:
            raise self.create_error
        return self.sandbox

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
    ) -> FakeExecHandle:
        self.exec_calls.append(
            {
                "sandbox": sandbox,
                "cmd": cmd,
                "args": list(args),
                "cwd": cwd,
                "user": user,
                "env": dict(env),
                "timeout": timeout,
                "stdin": stdin,
                "tty": tty,
                "rlimits": list(rlimits),
            }
        )
        if self.exec_error is not None:
            raise self.exec_error
        return self.exec_handle

    async def wait_exec(self, handle: object) -> int:
        assert isinstance(handle, FakeExecHandle)
        code, _success = await handle.wait()
        return code

    async def kill_exec(self, handle: object) -> None:
        assert isinstance(handle, FakeExecHandle)
        await handle.kill()

    async def stop_sandbox(self, sandbox: object, timeout: float) -> None:
        self.stop_calls += 1

    async def kill_sandbox(self, sandbox: object, timeout: float) -> None:
        self.kill_sandbox_calls += 1

    async def remove_sandbox(self, name: str) -> None:
        self.remove_calls.append(name)


class BlockingCreateSdk(FakeSdk):
    async def create_sandbox(self, name: str, **kwargs: object) -> object:
        self.create_calls.append({"name": name, **kwargs})
        await asyncio.Event().wait()
        return self.sandbox


class FlakyRemoveSdk(FakeSdk):
    async def remove_sandbox(self, name: str) -> None:
        self.remove_calls.append(name)
        if len(self.remove_calls) == 1:
            raise RuntimeError("transient remove failure")


class MicrosandboxBackendTests(unittest.TestCase):
    def _request(
        self,
        workspace: Path,
        *,
        image: str = PINNED_IMAGE,
        argv: tuple[str, ...] = ("python", "-V"),
        access: str = "read-only",
        limits: TaskLimits | None = None,
        artifact_path: Path | None = None,
        workdir: str = "/workspace",
        deadline: float | None = None,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            "workspace-guard-mcp-msb-test",
            workspace,
            TaskDefinition("test", "run", image, argv, workspace_access=access),
            limits or TaskLimits(),
            artifact_path=artifact_path,
            workdir=workdir,
            deadline=deadline,
        )

    def test_policy_compile_is_explicit_and_structured(self) -> None:
        events = [
            FakeEvent("started"),
            FakeEvent("stdout", data=b"out"),
            FakeEvent("stderr", data=b"err"),
            FakeEvent("exited", code=7),
        ]
        sdk = FakeSdk(exec_handle=FakeExecHandle(events, exit_code=7))
        stdout: list[bytes] = []
        stderr: list[bytes] = []
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            request = self._request(
                workspace,
                argv=("tool", "--network=host", "-c", "echo unsafe"),
                limits=TaskLimits(cpus="2", memory="1g", pids=17),
                workdir="/workspace/src",
            )
            backend: ExecutionBackend = MicrosandboxBackend(_sdk=sdk)
            handle = backend.start(request, stdout.append, stderr.append)
            self.assertEqual(handle.wait(timeout=2), 7)
            handle.close()

        create = sdk.create_calls[0]
        self.assertEqual(create["name"], "workspace-guard-mcp-msb-test")
        self.assertEqual(create["image"], PINNED_IMAGE)
        self.assertEqual(create["cpus"], 2)
        self.assertEqual(create["memory"], 1024)
        self.assertEqual(create["pull_policy"], "never")
        self.assertEqual(create["security"], "restricted")
        self.assertEqual(create["network"], {"policy": "none"})
        create_volumes = create["volumes"]
        assert isinstance(create_volumes, dict)
        workspace_mount = create_volumes["/workspace"]
        self.assertEqual(
            workspace_mount,
            {
                "path": str(workspace.resolve()),
                "readonly": True,
                "noexec": False,
                "nosuid": True,
                "nodev": True,
            },
        )

        execution = sdk.exec_calls[0]
        self.assertEqual(execution["cmd"], "tool")
        self.assertEqual(execution["args"], ["--network=host", "-c", "echo unsafe"])
        self.assertEqual(execution["cwd"], "/workspace/src")
        user = execution["user"]
        self.assertIsInstance(user, str)
        assert isinstance(user, str)
        uid_text, gid_text = user.split(":", 1)
        self.assertGreater(int(uid_text), 0)
        self.assertGreaterEqual(int(gid_text), 0)
        self.assertEqual(execution["stdin"], {"stdin": "null"})
        self.assertIs(execution["tty"], False)
        self.assertEqual(execution["rlimits"], [("nproc", 17, 17)])
        command_timeout = execution["timeout"]
        assert isinstance(command_timeout, float)
        self.assertGreater(command_timeout, 0)
        self.assertEqual(stdout, [b"out"])
        self.assertEqual(stderr, [b"err"])
        self.assertEqual(sdk.remove_calls, ["workspace-guard-mcp-msb-test"])

    def test_writable_workspace_and_artifact_compile_expected_policy(self) -> None:
        sdk = FakeSdk()
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            workspace = Path(workspace_dir)
            artifact = Path(artifact_dir)
            limits = TaskLimits(
                pids=23,
                max_workspace_file_bytes=4096,
                max_workspace_growth_bytes=8192,
                allow_best_effort_disk_limit=True,
            )
            handle = MicrosandboxBackend(_sdk=sdk).start(
                self._request(
                    workspace,
                    access="writable",
                    limits=limits,
                    artifact_path=artifact,
                ),
                lambda data: None,
                lambda data: None,
            )
            handle.wait(timeout=2)
            handle.close()

        volumes = sdk.create_calls[0]["volumes"]
        assert isinstance(volumes, dict)
        workspace_mount = volumes["/workspace"]
        artifact_mount = volumes["/artifacts"]
        assert isinstance(workspace_mount, dict)
        assert isinstance(artifact_mount, dict)
        self.assertIs(workspace_mount["readonly"], False)
        self.assertIs(artifact_mount["readonly"], False)
        self.assertIs(artifact_mount["nosuid"], True)
        self.assertIs(artifact_mount["nodev"], True)
        execution = sdk.exec_calls[0]
        self.assertEqual(
            execution["rlimits"],
            [("nproc", 23, 23), ("fsize", 4096, 4096)],
        )
        environment = execution["env"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["WORKSPACEGUARD_ARTIFACT_DIR"], "/artifacts")

    def test_server_authored_environment_does_not_leak_host_secrets(self) -> None:
        sdk = FakeSdk()
        secret_names = (
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "HTTPS_PROXY",
            "SSH_AUTH_SOCK",
        )
        host_values = {name: f"secret-{name}" for name in secret_names}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, host_values, clear=False),
        ):
            handle = MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
            handle.wait(timeout=2)
            handle.close()

        environment = sdk.exec_calls[0]["env"]
        assert isinstance(environment, dict)
        for name in secret_names:
            self.assertNotIn(name, environment)
        self.assertNotIn("PATH", environment)
        self.assertEqual(
            environment,
            {
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
            },
        )

    def test_cpu_conversion_accepts_only_exact_u8_values(self) -> None:
        for value, expected in (("1", 1), ("2.000", 2), ("255", 255)):
            with self.subTest(value=value):
                self.assertEqual(_parse_microsandbox_cpus(value), expected)
        for value in ("0", "0.5", "1.5", "2.001", "256"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    MicrosandboxExecutionError, "cannot be represented exactly"
                ),
            ):
                _parse_microsandbox_cpus(value)

    def test_memory_conversion_uses_exact_integer_mib(self) -> None:
        for value, expected in (
            ("1m", 1),
            ("512m", 512),
            ("1g", 1024),
            ("1024k", 1),
            ("1048576", 1),
            ("1GB", 1024),
        ):
            with self.subTest(value=value):
                self.assertEqual(_parse_microsandbox_memory_mib(value), expected)
        for value in ("1537k", "1000", "1k", "0"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    MicrosandboxExecutionError, "cannot be represented exactly"
                ),
            ):
                _parse_microsandbox_memory_mib(value)

    def test_unrepresentable_policy_fails_before_sdk_create(self) -> None:
        cases = (
            (TaskLimits(cpus="0.5"), PINNED_IMAGE, "/workspace", "CPU limit"),
            (TaskLimits(cpus="256"), PINNED_IMAGE, "/workspace", "CPU limit"),
            (TaskLimits(memory="1537k"), PINNED_IMAGE, "/workspace", "memory limit"),
            (TaskLimits(), LOCAL_IMAGE_ID, "/workspace", "Docker local sha256"),
            (TaskLimits(), PINNED_IMAGE, "/tmp", "inside /workspace"),
        )
        for limits, image, workdir, message in cases:
            sdk = FakeSdk()
            with (
                tempfile.TemporaryDirectory() as directory,
                self.subTest(message=message),
            ):
                with self.assertRaisesRegex(MicrosandboxExecutionError, message):
                    MicrosandboxBackend(_sdk=sdk).start(
                        self._request(
                            Path(directory),
                            image=image,
                            limits=limits,
                            workdir=workdir,
                        ),
                        lambda data: None,
                        lambda data: None,
                    )
            self.assertEqual(sdk.create_calls, [])

    def test_missing_optional_dependency_is_explicit_and_package_import_is_independent(
        self,
    ) -> None:
        import workspace_guard_mcp

        self.assertTrue(hasattr(workspace_guard_mcp, "create_server"))
        with (
            patch(
                "workspace_guard_mcp.microsandbox_backend.importlib.import_module",
                side_effect=ModuleNotFoundError(
                    "No module named 'microsandbox'", name="microsandbox"
                ),
            ),
            self.assertRaisesRegex(
                MicrosandboxExecutionError,
                "requires the 'microsandbox' optional dependency",
            ),
        ):
            MicrosandboxBackend()

    def test_loaded_sdk_facade_maps_the_official_dynamic_surface(self) -> None:
        module = ModuleType("microsandbox")
        calls: dict[str, object] = {}
        native_exec = FakeExecHandle(exit_code=5)

        class NativeSandbox:
            async def exec_stream(
                self,
                cmd: str,
                args: list[str],
                **kwargs: object,
            ) -> FakeExecHandle:
                calls["exec_stream"] = (cmd, list(args), dict(kwargs))
                return native_exec

            async def stop(self, *, timeout: float) -> None:
                calls["stop"] = timeout

            async def kill(self, *, timeout: float) -> None:
                calls["kill"] = timeout

        native_sandbox = NativeSandbox()

        class SandboxApi:
            @staticmethod
            async def create(name: str, **kwargs: object) -> NativeSandbox:
                calls["create"] = (name, dict(kwargs))
                return native_sandbox

            @staticmethod
            async def remove(name: str) -> None:
                calls["remove"] = name

        @dataclass(frozen=True)
        class NativeMount:
            path: str
            readonly: bool
            noexec: bool
            nosuid: bool
            nodev: bool
            stat_virtualization: str | None = None
            host_permissions: str | None = None

        class VolumeApi:
            @staticmethod
            def bind(path: str, **kwargs: object) -> object:
                return NativeMount(
                    path=path,
                    readonly=bool(kwargs["readonly"]),
                    noexec=bool(kwargs["noexec"]),
                    nosuid=bool(kwargs["nosuid"]),
                    nodev=bool(kwargs["nodev"]),
                )

        class NetworkApi:
            @staticmethod
            def none() -> object:
                return ("network", "none")

        class RlimitApi:
            @staticmethod
            def nproc(limit: int) -> object:
                return ("nproc", limit)

            @staticmethod
            def fsize(limit: int) -> object:
                return ("fsize", limit)

        class StdinApi:
            @staticmethod
            def null() -> object:
                return ("stdin", "null")

        module.__dict__.update(
            {
                "Sandbox": SandboxApi,
                "Volume": VolumeApi,
                "Network": NetworkApi,
                "Rlimit": RlimitApi,
                "Stdin": StdinApi,
            }
        )

        with patch(
            "workspace_guard_mcp.microsandbox_backend.importlib.import_module",
            return_value=module,
        ):
            backend = MicrosandboxBackend()
        sdk = backend._sdk  # type: ignore[attr-defined]
        self.assertIsInstance(sdk, _LoadedMicrosandboxSdk)
        self.assertEqual(
            sdk.bind_volume(
                "/host",
                readonly=True,
                noexec=False,
                nosuid=True,
                nodev=True,
            ),
            NativeMount(
                path="/host",
                readonly=True,
                noexec=False,
                nosuid=True,
                nodev=True,
                stat_virtualization="off",
                host_permissions="private",
            ),
        )
        self.assertEqual(sdk.network_none(), ("network", "none"))
        self.assertEqual(sdk.rlimit_nproc(7), ("nproc", 7))
        self.assertEqual(sdk.rlimit_fsize(11), ("fsize", 11))
        self.assertEqual(sdk.stdin_null(), ("stdin", "null"))

        async def exercise() -> None:
            sandbox = await sdk.create_sandbox(
                "owned",
                image=PINNED_IMAGE,
                cpus=2,
                memory=512,
                pull_policy="never",
                security="restricted",
                network=("network", "none"),
                volumes={"/workspace": ("volume", "/host")},
            )
            handle = await sdk.exec_stream(
                sandbox,
                "tool",
                ["arg"],
                cwd="/workspace",
                user="1000:1000",
                env={"CI": "1"},
                timeout=3.0,
                stdin=("stdin", "null"),
                tty=False,
                rlimits=[("nproc", 7)],
            )
            self.assertIs(handle, native_exec)
            self.assertEqual(await sdk.wait_exec(handle), 5)
            await sdk.kill_exec(handle)
            await sdk.stop_sandbox(sandbox, 2.0)
            await sdk.kill_sandbox(sandbox, 1.0)
            await sdk.remove_sandbox("owned")

        asyncio.run(exercise())
        self.assertTrue(native_exec.killed)
        self.assertEqual(calls["remove"], "owned")
        create_call = calls["create"]
        assert isinstance(create_call, tuple)
        self.assertEqual(create_call[0], "owned")
        create_kwargs = create_call[1]
        assert isinstance(create_kwargs, dict)
        self.assertEqual(create_kwargs["pull_policy"], "never")
        self.assertEqual(create_kwargs["security"], "restricted")
        exec_call = calls["exec_stream"]
        assert isinstance(exec_call, tuple)
        self.assertEqual(exec_call[0:2], ("tool", ["arg"]))
        exec_kwargs = exec_call[2]
        assert isinstance(exec_kwargs, dict)
        self.assertEqual(exec_kwargs["user"], "1000:1000")
        self.assertEqual(calls["stop"], 2.0)
        self.assertEqual(calls["kill"], 1.0)

    def test_create_failure_is_explicit_and_worker_is_closed(self) -> None:
        sdk = FakeSdk(create_error=RuntimeError("boot denied"))
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(MicrosandboxExecutionError, "boot denied"),
        ):
            MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
        self.assertEqual(sdk.remove_calls, [])

    def test_exec_start_failure_removes_owned_sandbox(self) -> None:
        sdk = FakeSdk(exec_error=RuntimeError("exec denied"))
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(MicrosandboxExecutionError, "exec denied"),
        ):
            MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
        self.assertGreaterEqual(sdk.stop_calls, 1)
        self.assertEqual(sdk.remove_calls, ["workspace-guard-mcp-msb-test"])

    def test_partial_startup_cleanup_retries_owned_sandbox_remove(self) -> None:
        sdk = FlakyRemoveSdk(exec_error=RuntimeError("exec denied"))
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(MicrosandboxExecutionError, "exec denied"),
        ):
            MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
        self.assertEqual(
            sdk.remove_calls,
            ["workspace-guard-mcp-msb-test", "workspace-guard-mcp-msb-test"],
        )

    def test_missing_exec_program_is_a_normal_failed_execution(self) -> None:
        sdk = FakeSdk(
            exec_handle=FakeExecHandle(
                [
                    FakeEvent(
                        "failed",
                        data=b"No such file or directory",
                        code=errno.ENOENT,
                    )
                ]
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_execution(
                MicrosandboxBackend(_sdk=sdk),
                self._request(Path(directory), argv=("missing-tool",)),
            )
        self.assertEqual(result.state, ExecutionState.FAILED)
        self.assertEqual(result.exit_code, 127)
        self.assertIn("No such file or directory", result.stderr)
        self.assertNotIn("runtime monitor failure", result.stderr)

    def test_missing_ruff_binary_is_capability_unavailable_not_runtime_crash(
        self,
    ) -> None:
        sdk = FakeSdk(
            exec_handle=FakeExecHandle(
                [
                    FakeEvent(
                        "failed",
                        data=b"No such file or directory",
                        code=errno.ENOENT,
                    )
                ]
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ExecutionProfile(
                "debug",
                PINNED_IMAGE,
                frozenset({"run_ruff"}),
            )
            configuration = TaskConfiguration(
                source=root / "profiles.json",
                runtime="microsandbox",
                limits=TaskLimits(timeout_seconds=5, max_output_bytes=4096),
                tasks=MappingProxyType({}),
                profiles=MappingProxyType({"debug": profile}),
                default_profile="debug",
            )
            manager = TaskManager(
                Settings.create(root),
                configuration,
                backend=MicrosandboxBackend(_sdk=sdk),
            )
            result = manager.run_ruff("debug", paths=["."])
            manager.shutdown()

        self.assertEqual(result["status"], "capability_unavailable")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("No such file or directory", str(result["stderr"]))
        self.assertNotIn("runtime monitor failure", str(result["stderr"]))

    def test_service_stop_interrupts_running_microsandbox_command(self) -> None:
        exec_handle = FakeExecHandle(
            [FakeEvent("stdout", data=b"READY\n")],
            blocking=True,
        )
        sdk = FakeSdk(exec_handle=exec_handle)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = ExecutionProfile(
                "debug",
                PINNED_IMAGE,
                frozenset({"start_command"}),
                workspace_access="read-only",
                allow_arbitrary_commands=True,
            )
            configuration = TaskConfiguration(
                source=root / "profiles.json",
                runtime="microsandbox",
                limits=TaskLimits(timeout_seconds=30, max_output_bytes=4096),
                tasks=MappingProxyType({}),
                profiles=MappingProxyType({"debug": profile}),
                default_profile="debug",
            )
            manager = TaskManager(
                Settings.create(root),
                configuration,
                backend=MicrosandboxBackend(_sdk=sdk),
            )
            started = manager.start_command(
                "debug",
                "python",
                ["-c", "import time; print('READY'); time.sleep(20)"],
            )
            task_id = str(started["task_id"])
            self.assertEqual(manager.task_logs(task_id)["stdout"], "READY\n")

            before = time.monotonic()
            stopped = manager.stop_task(task_id)
            elapsed = time.monotonic() - before

            self.assertLess(elapsed, 2.0)
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(exec_handle.kill_calls, 1)
            record = manager.execution_status(task_id)
            self.assertEqual(record["state"], "cancelled")
            self.assertEqual(record["reason"], "user_cancelled")
            self.assertEqual(manager.stop_task(task_id)["status"], "stopped")
            self.assertEqual(exec_handle.kill_calls, 1)
            self.assertEqual(len(sdk.remove_calls), 1)
            manager.shutdown()

    def test_wait_timeout_does_not_stop_execution_and_stop_is_idempotent(self) -> None:
        exec_handle = FakeExecHandle(blocking=True)
        sdk = FakeSdk(exec_handle=exec_handle)
        with tempfile.TemporaryDirectory() as directory:
            handle = MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
            with self.assertRaises(TimeoutError):
                handle.wait(timeout=0.01)
            self.assertFalse(exec_handle.killed)
            handle.stop()
            handle.stop()
            self.assertEqual(handle.wait(timeout=2), -9)
            handle.close()
            handle.close()
        self.assertEqual(exec_handle.kill_calls, 1)
        self.assertEqual(sdk.remove_calls, ["workspace-guard-mcp-msb-test"])

    def test_close_pending_execution_terminates_private_worker_thread(self) -> None:
        sdk = FakeSdk(exec_handle=FakeExecHandle(blocking=True))
        with tempfile.TemporaryDirectory() as directory:
            handle = MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
            worker = handle._thread  # type: ignore[attr-defined]
            loop = handle._loop  # type: ignore[attr-defined]
            self.assertTrue(worker.is_alive())
            self.assertIsNotNone(loop)
            handle.close()
            self.assertFalse(worker.is_alive())
            assert loop is not None
            self.assertTrue(loop.is_closed())

    def test_stop_can_interrupt_a_pending_wait(self) -> None:
        sdk = FakeSdk(exec_handle=FakeExecHandle(blocking=True))
        with tempfile.TemporaryDirectory() as directory:
            handle = MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)),
                lambda data: None,
                lambda data: None,
            )
            result: list[int] = []
            done = threading.Event()

            def waiter() -> None:
                result.append(handle.wait(timeout=2))
                done.set()

            thread = threading.Thread(target=waiter)
            thread.start()
            handle.stop()
            self.assertTrue(done.wait(timeout=2))
            thread.join(timeout=2)
            self.assertEqual(result, [-9])
            handle.close()

    def test_multiple_handles_do_not_share_lifecycle_state(self) -> None:
        sdk_one = FakeSdk(exec_handle=FakeExecHandle(blocking=True))
        sdk_two = FakeSdk(exec_handle=FakeExecHandle(blocking=True))
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            handle_one = MicrosandboxBackend(_sdk=sdk_one).start(
                self._request(Path(first)), lambda data: None, lambda data: None
            )
            handle_two = MicrosandboxBackend(_sdk=sdk_two).start(
                self._request(Path(second)), lambda data: None, lambda data: None
            )
            handle_one.stop()
            self.assertTrue(sdk_one.exec_handle.killed)
            self.assertFalse(sdk_two.exec_handle.killed)
            handle_one.close()
            handle_two.stop()
            handle_two.close()

    def test_callback_exception_becomes_execution_failure_not_worker_hang(self) -> None:
        sdk = FakeSdk(
            exec_handle=FakeExecHandle(
                [FakeEvent("stdout", data=b"boom"), FakeEvent("exited", code=0)]
            )
        )

        def fail_callback(data: bytes) -> None:
            raise ValueError("callback denied")

        with tempfile.TemporaryDirectory() as directory:
            handle = MicrosandboxBackend(_sdk=sdk).start(
                self._request(Path(directory)), fail_callback, lambda data: None
            )
            with self.assertRaisesRegex(MicrosandboxExecutionError, "callback denied"):
                handle.wait(timeout=2)
            handle.close()

    def test_runtime_timeout_remains_canonical_timeout(self) -> None:
        class RuntimeTimeoutExecHandle(FakeExecHandle):
            async def __anext__(self) -> object:
                raise asyncio.TimeoutError

        sdk = FakeSdk(exec_handle=RuntimeTimeoutExecHandle())
        limits = TaskLimits(timeout_seconds=0.05, max_output_bytes=1024)
        with tempfile.TemporaryDirectory() as directory:
            result = run_execution(
                MicrosandboxBackend(_sdk=sdk),
                self._request(Path(directory), limits=limits),
            )
        self.assertEqual(result.state, ExecutionState.TIMED_OUT)
        self.assertEqual(result.reason, ExecutionReason.TIMEOUT)
        self.assertNotIn("runtime monitor failure", result.stderr)

    def test_startup_deadline_is_bounded_and_generic_runner_reports_timeout(
        self,
    ) -> None:
        sdk = BlockingCreateSdk()
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(Path(directory), deadline=time.monotonic() + 0.05)
            result = run_execution(MicrosandboxBackend(_sdk=sdk), request)
        self.assertEqual(result.state, ExecutionState.TIMED_OUT)
        self.assertEqual(result.reason, ExecutionReason.TIMEOUT)
        self.assertIn("timeout expired during runtime start", result.stderr)


if __name__ == "__main__":
    unittest.main()
