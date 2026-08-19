from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import time
import unittest
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.container_backend import CliContainerBackend
from workspace_guard_mcp.microsandbox_backend import (
    MicrosandboxBackend,
    MicrosandboxExecutionError,
)
from workspace_guard_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager, TaskManagerError

PINNED_IMAGE = "example.invalid/workspace-guard-mcp@sha256:" + "c" * 64


class ControlledEvent:
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


class ControlledExecHandle:
    def __init__(
        self,
        events: list[ControlledEvent] | None = None,
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
            await self._release_event().wait()
        if self.killed and not self._killed_exit_emitted:
            self._killed_exit_emitted = True
            return ControlledEvent("exited", code=-9)
        raise StopAsyncIteration

    async def wait(self) -> tuple[int, bool]:
        if self.blocking and not self.killed:
            await self._release_event().wait()
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


ExecEffect = Callable[[Mapping[str, object], str, list[str], str], None]


class ControlledSdk:
    def __init__(
        self,
        handles: list[ControlledExecHandle],
        *,
        effects: list[ExecEffect] | None = None,
    ) -> None:
        self.handles = list(handles)
        self.effects = list(effects or [])
        self.volumes: dict[str, object] = {}
        self.exec_calls: list[dict[str, object]] = []
        self.exec_started = threading.Event()
        self.remove_calls: list[str] = []
        self.stop_calls = 0

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
        return ("nproc", limit)

    def rlimit_fsize(self, limit: int) -> object:
        return ("fsize", limit)

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
        self.volumes = dict(volumes)
        return {"name": name}

    async def exec_stream(
        self,
        sandbox: object,
        cmd: str,
        args: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout: float | None,
        stdin: object,
        tty: bool,
        rlimits: list[object],
    ) -> ControlledExecHandle:
        self.exec_calls.append(
            {
                "cmd": cmd,
                "args": list(args),
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout,
            }
        )
        self.exec_started.set()
        if self.effects:
            self.effects.pop(0)(self.volumes, cmd, list(args), cwd)
        if not self.handles:
            raise AssertionError("no controlled Microsandbox exec handle remains")
        return self.handles.pop(0)

    async def wait_exec(self, handle: object) -> int:
        assert isinstance(handle, ControlledExecHandle)
        code, _success = await handle.wait()
        return code

    async def kill_exec(self, handle: object) -> None:
        assert isinstance(handle, ControlledExecHandle)
        await handle.kill()

    async def stop_sandbox(self, sandbox: object, timeout: float) -> None:
        self.stop_calls += 1

    async def kill_sandbox(self, sandbox: object, timeout: float) -> None:
        self.stop_calls += 1

    async def remove_sandbox(self, name: str) -> None:
        self.remove_calls.append(name)


class PausedCreateSdk(ControlledSdk):
    def __init__(self, handle: ControlledExecHandle) -> None:
        super().__init__([handle])
        self.create_started = threading.Event()
        self.release_create = threading.Event()

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
        self.create_started.set()
        while not self.release_create.is_set():
            await asyncio.sleep(0.01)
        return await super().create_sandbox(
            name,
            image=image,
            cpus=cpus,
            memory=memory,
            pull_policy=pull_policy,
            security=security,
            network=network,
            volumes=volumes,
        )


class CleanupFailingPausedCreateSdk(PausedCreateSdk):
    def __init__(self, handle: ControlledExecHandle) -> None:
        super().__init__(handle)
        self.cleanup_attempts: list[str] = []

    async def kill_exec(self, handle: object) -> None:
        self.cleanup_attempts.append("kill_exec")
        raise RuntimeError("kill exec denied")

    async def stop_sandbox(self, sandbox: object, timeout: float) -> None:
        self.cleanup_attempts.append("stop_sandbox")
        raise RuntimeError("stop sandbox denied")

    async def kill_sandbox(self, sandbox: object, timeout: float) -> None:
        self.cleanup_attempts.append("kill_sandbox")
        raise RuntimeError("kill sandbox denied")

    async def remove_sandbox(self, name: str) -> None:
        self.cleanup_attempts.append("remove_sandbox")
        raise RuntimeError("remove sandbox denied")


class MicrosandboxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        (self.root / "module.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "subdir").mkdir()
        self.settings = Settings.create(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _configuration(
        self,
        *,
        runtime: str = "microsandbox",
        run_access: str = "read-only",
        limits: TaskLimits | None = None,
        profiles: bool = False,
    ) -> TaskConfiguration:
        profile_map = {}
        if profiles:
            profile_map["debug"] = ExecutionProfile(
                "debug",
                PINNED_IMAGE,
                frozenset({"python_version", "run_command", "start_command"}),
                allow_arbitrary_commands=True,
            )
        return TaskConfiguration(
            source=self.base / "tasks.json",
            runtime=runtime,
            limits=limits or TaskLimits(timeout_seconds=1, max_output_bytes=4096),
            tasks=MappingProxyType(
                {
                    "run": TaskDefinition(
                        "run",
                        "run",
                        PINNED_IMAGE,
                        ("python", "-V"),
                        workspace_access=run_access,
                    ),
                    "service": TaskDefinition(
                        "service",
                        "service",
                        PINNED_IMAGE,
                        ("python", "-m", "service"),
                    ),
                }
            ),
            profiles=MappingProxyType(profile_map),
            default_profile="debug" if profiles else None,
        )

    def _manager(
        self,
        sdk: ControlledSdk,
        *,
        runtime: str = "microsandbox",
        run_access: str = "read-only",
        limits: TaskLimits | None = None,
        profiles: bool = False,
    ) -> TaskManager:
        return TaskManager(
            self.settings,
            self._configuration(
                runtime=runtime,
                run_access=run_access,
                limits=limits,
                profiles=profiles,
            ),
            backend=MicrosandboxBackend(_sdk=sdk),
        )

    def test_default_backend_selection_and_dependency_injection(self) -> None:
        sdk = ControlledSdk([ControlledExecHandle()])
        with patch(
            "workspace_guard_mcp.microsandbox_backend._load_microsandbox_sdk",
            return_value=sdk,
        ):
            manager = TaskManager(self.settings, self._configuration())
        self.assertIsInstance(manager.backend, MicrosandboxBackend)
        manager.shutdown()

        for runtime in ("docker", "podman"):
            with self.subTest(runtime=runtime):
                manager = TaskManager(
                    self.settings, self._configuration(runtime=runtime)
                )
                self.assertIsInstance(manager.backend, CliContainerBackend)
                assert isinstance(manager.backend, CliContainerBackend)
                self.assertEqual(manager.backend.runtime, runtime)
                manager.shutdown()

        injected = MicrosandboxBackend(_sdk=ControlledSdk([ControlledExecHandle()]))
        with patch(
            "workspace_guard_mcp.task_manager.MicrosandboxBackend",
            side_effect=AssertionError("production backend must not be constructed"),
        ):
            manager = TaskManager(
                self.settings,
                self._configuration(),
                backend=injected,
            )
        self.assertIs(manager.backend, injected)
        manager.shutdown()

    def test_missing_optional_dependency_is_task_manager_error(self) -> None:
        with (
            patch(
                "workspace_guard_mcp.task_manager.MicrosandboxBackend",
                side_effect=MicrosandboxExecutionError("SDK unavailable"),
            ),
            self.assertRaisesRegex(
                TaskManagerError,
                r"Install workspace-guard-mcp\[microsandbox\]",
            ),
        ):
            TaskManager(self.settings, self._configuration())

    def test_sync_task_profile_command_artifact_and_audit_parity(self) -> None:
        artifact_bytes = b"artifact-body\n"

        def write_artifact(
            volumes: Mapping[str, object],
            cmd: str,
            args: list[str],
            cwd: str,
        ) -> None:
            artifact_mount = volumes["/artifacts"]
            assert isinstance(artifact_mount, dict)
            Path(str(artifact_mount["path"]), "result.txt").write_bytes(artifact_bytes)

        handles = [
            ControlledExecHandle(
                [
                    ControlledEvent("stdout", data=b"task-ok\n"),
                    ControlledEvent("exited", code=0),
                ]
            ),
            ControlledExecHandle(
                [
                    ControlledEvent("stdout", data=b"Python 3.13.7\n"),
                    ControlledEvent("exited", code=0),
                ]
            ),
            ControlledExecHandle([ControlledEvent("exited", code=0)]),
        ]
        sdk = ControlledSdk(handles, effects=[write_artifact])
        manager = self._manager(sdk, profiles=True)

        result = manager.run_task("run")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "task-ok\n")
        resources = result["resources"]
        assert isinstance(resources, dict)
        self.assertIsNone(resources["cpu_time_ms"])
        self.assertIsNone(resources["peak_memory_bytes"])
        artifacts = result["artifacts"]
        assert isinstance(artifacts, list)
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        assert isinstance(artifact, dict)
        self.assertEqual(artifact["name"], "result.txt")
        self.assertEqual(artifact["media_type"], "text/plain")
        self.assertEqual(artifact["size_bytes"], len(artifact_bytes))
        self.assertEqual(artifact["sha256"], hashlib.sha256(artifact_bytes).hexdigest())
        self.assertTrue(artifact["content_available"])
        self.assertIsNotNone(artifact["resource_uri"])

        execution_id = result["execution_id"]
        assert isinstance(execution_id, str)
        status = manager.execution_status(execution_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertNotIn("microsandbox", repr(status).lower())
        events = manager.execution_events(execution_id)["events"]
        self.assertIn("created", repr(events))
        self.assertIn("running", repr(events))
        self.assertIn("succeeded", repr(events))
        self.assertNotIn("sandbox_id", repr(events))

        version = manager.python_version("debug")
        self.assertEqual(version["status"], "succeeded")
        self.assertEqual(version["stdout"], "Python 3.13.7\n")

        command = manager.run_command(
            "debug",
            "python",
            ["-c", "print('ok')"],
            "subdir",
        )
        self.assertEqual(command["status"], "succeeded")
        self.assertEqual(sdk.exec_calls[-1]["cmd"], "python")
        self.assertEqual(sdk.exec_calls[-1]["args"], ["-c", "print('ok')"])
        self.assertEqual(sdk.exec_calls[-1]["cwd"], "/workspace/subdir")
        manager.shutdown()

    def test_nonzero_exit_and_artifact_limit_keep_canonical_semantics(self) -> None:
        failed_handle = ControlledExecHandle(
            [ControlledEvent("exited", code=7)], exit_code=7
        )
        manager = self._manager(ControlledSdk([failed_handle]))
        failed = manager.run_task("run")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 7)
        failed_record = manager.execution_status(str(failed["execution_id"]))
        self.assertEqual(failed_record["state"], "failed")
        self.assertIsNone(failed_record["reason"])
        manager.shutdown()

        limits = TaskLimits(
            timeout_seconds=1,
            max_output_bytes=4096,
            max_artifact_bytes=4,
            max_total_artifact_bytes=8,
        )

        def write_oversized_artifact(
            volumes: Mapping[str, object],
            cmd: str,
            args: list[str],
            cwd: str,
        ) -> None:
            artifact_mount = volumes["/artifacts"]
            assert isinstance(artifact_mount, dict)
            Path(str(artifact_mount["path"]), "large.bin").write_bytes(b"12345")

        sdk = ControlledSdk(
            [ControlledExecHandle([ControlledEvent("exited", code=0)])],
            effects=[write_oversized_artifact],
        )
        manager = self._manager(sdk, limits=limits)
        rejected = manager.run_task("run")
        rejected_record = manager.execution_status(str(rejected["execution_id"]))
        self.assertEqual(rejected_record["state"], "failed")
        self.assertEqual(rejected_record["reason"], "artifact_limit_exceeded")
        self.assertEqual(rejected["artifacts"], [])
        self.assertEqual(
            manager.execution_artifacts(str(rejected["execution_id"]))["artifacts"],
            [],
        )
        manager.shutdown()

    def test_writable_workspace_monitor_and_accounting(self) -> None:
        limits = TaskLimits(
            timeout_seconds=1,
            max_output_bytes=4096,
            max_workspace_file_bytes=1024,
            max_workspace_growth_bytes=1,
            allow_best_effort_disk_limit=True,
        )

        def grow_workspace(
            volumes: Mapping[str, object],
            cmd: str,
            args: list[str],
            cwd: str,
        ) -> None:
            workspace_mount = volumes["/workspace"]
            assert isinstance(workspace_mount, dict)
            generated = Path(str(workspace_mount["path"]), "generated.bin")
            generated.write_bytes(b"x" * 32)

        handle = ControlledExecHandle(blocking=True)
        sdk = ControlledSdk([handle], effects=[grow_workspace])
        manager = self._manager(
            sdk,
            run_access="writable",
            limits=limits,
        )
        result = manager.run_task("run")
        self.assertEqual(result["status"], "workspace_limit_exceeded")
        self.assertTrue(handle.killed)
        resources = result["resources"]
        assert isinstance(resources, dict)
        self.assertGreater(
            resources["workspace_final_bytes"],
            resources["workspace_initial_bytes"],
        )
        self.assertGreater(resources["workspace_growth_bytes"], 0)
        manager.shutdown()

    def test_output_limit_cancellation_and_service_lifecycle(self) -> None:
        overflow_limits = TaskLimits(timeout_seconds=1, max_output_bytes=8)
        overflow_handle = ControlledExecHandle(
            [ControlledEvent("stdout", data=b"0123456789")], blocking=True
        )
        manager = self._manager(
            ControlledSdk([overflow_handle]),
            limits=overflow_limits,
        )
        overflow = manager.run_task("run")
        self.assertEqual(overflow["status"], "output_limit_exceeded")
        self.assertTrue(overflow_handle.killed)
        manager.shutdown()

        cancellation = threading.Event()
        cancel_handle = ControlledExecHandle(blocking=True)
        cancel_sdk = ControlledSdk([cancel_handle])
        manager = self._manager(cancel_sdk)
        result_box: list[dict[str, object]] = []
        thread = threading.Thread(
            target=lambda: result_box.append(
                manager.run_task("run", cancellation_event=cancellation)
            )
        )
        thread.start()
        self.assertTrue(cancel_sdk.exec_started.wait(timeout=1))
        cancellation.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0]["status"], "cancelled")
        self.assertTrue(cancel_handle.killed)
        manager.shutdown()

        service_handle = ControlledExecHandle(
            [
                ControlledEvent("stdout", data=b"live-out\n"),
                ControlledEvent("stderr", data=b"live-err\n"),
            ],
            blocking=True,
        )
        manager = self._manager(ControlledSdk([service_handle]))
        started = manager.start_task("service")
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        self.assertEqual(task_id, started["execution_id"])
        logs: dict[str, object] = {"stdout": "", "stderr": ""}
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            logs = manager.task_logs(task_id)
            if logs["stdout"] and logs["stderr"]:
                break
            time.sleep(0.01)
        stdout = logs["stdout"]
        stderr = logs["stderr"]
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)
        self.assertIn("live-out", stdout)
        self.assertIn("live-err", stderr)
        self.assertEqual(manager.task_status(task_id)["status"], "running")
        stopped = manager.stop_task(task_id)
        self.assertEqual(stopped["status"], "stopped")
        record = manager.execution_status(task_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["reason"], "user_cancelled")
        manager.shutdown()

    def test_service_timeout_and_shutdown_keep_canonical_reasons(self) -> None:
        timeout_handle = ControlledExecHandle(blocking=True)
        timeout_limits = TaskLimits(timeout_seconds=0.1, max_output_bytes=4096)
        manager = self._manager(
            ControlledSdk([timeout_handle]),
            limits=timeout_limits,
        )
        started = manager.start_task("service")
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        deadline = time.monotonic() + 2
        status = manager.task_status(task_id)
        while status["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            status = manager.task_status(task_id)
        self.assertEqual(status["status"], "timed_out")
        record = manager.execution_status(task_id)
        self.assertEqual(record["state"], "timed_out")
        self.assertEqual(record["reason"], "timeout")
        manager.shutdown()

        shutdown_handle = ControlledExecHandle(blocking=True)
        sdk = ControlledSdk([shutdown_handle])
        manager = self._manager(sdk)
        started = manager.start_task("service")
        shutdown_id = started["task_id"]
        assert isinstance(shutdown_id, str)
        manager.shutdown()
        record = manager.execution_status(shutdown_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["reason"], "server_shutdown")
        self.assertTrue(shutdown_handle.killed)
        self.assertTrue(sdk.remove_calls)

    def test_service_natural_exit_finalizes_artifact_and_resources(self) -> None:
        artifact_bytes = b"service-artifact\n"

        def write_service_artifact(
            volumes: Mapping[str, object],
            cmd: str,
            args: list[str],
            cwd: str,
        ) -> None:
            artifact_mount = volumes["/artifacts"]
            assert isinstance(artifact_mount, dict)
            Path(str(artifact_mount["path"]), "service.txt").write_bytes(artifact_bytes)

        sdk = ControlledSdk(
            [
                ControlledExecHandle(
                    [
                        ControlledEvent("stdout", data=b"ready\n"),
                        ControlledEvent("exited", code=0),
                    ]
                )
            ],
            effects=[write_service_artifact],
        )
        manager = self._manager(sdk)
        started = manager.start_task("service")
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        deadline = time.monotonic() + 2
        record = manager.execution_status(task_id)
        while record["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            record = manager.execution_status(task_id)

        self.assertEqual(record["state"], "succeeded")
        self.assertIsNotNone(record["resources"])
        artifacts = manager.execution_artifacts(task_id)["artifacts"]
        assert isinstance(artifacts, list)
        self.assertEqual(len(artifacts), 1)
        artifact = artifacts[0]
        assert isinstance(artifact, dict)
        self.assertEqual(artifact["name"], "service.txt")
        self.assertEqual(artifact["sha256"], hashlib.sha256(artifact_bytes).hexdigest())
        self.assertTrue(sdk.remove_calls)
        manager.shutdown()

    def test_shutdown_during_create_keeps_starting_lease_owned(self) -> None:
        handle = ControlledExecHandle(blocking=True)
        sdk = PausedCreateSdk(handle)
        manager = self._manager(sdk)
        errors: list[BaseException] = []

        def start_service() -> None:
            try:
                manager.start_task("service")
            except BaseException as exc:
                errors.append(exc)

        starter = threading.Thread(target=start_service)
        starter.start()
        self.assertTrue(sdk.create_started.wait(timeout=1))
        with manager._lock:
            execution_id = next(iter(manager._starting))

        shutdown = threading.Thread(target=manager.shutdown)
        shutdown.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with manager._lock:
                if manager._shutdown:
                    break
            time.sleep(0.01)
        sdk.release_create.set()
        starter.join(timeout=2)
        shutdown.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertFalse(shutdown.is_alive())
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], TaskManagerError)
        self.assertIn("shutting down", str(errors[0]))
        record = manager.execution_status(execution_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["reason"], "server_shutdown")
        self.assertTrue(handle.killed)
        self.assertTrue(sdk.remove_calls)

    def test_shutdown_during_create_reports_cleanup_failure(self) -> None:
        handle = ControlledExecHandle(blocking=True)
        sdk = CleanupFailingPausedCreateSdk(handle)
        manager = self._manager(sdk)
        errors: list[BaseException] = []

        def start_service() -> None:
            try:
                manager.start_task("service")
            except BaseException as exc:
                errors.append(exc)

        starter = threading.Thread(target=start_service)
        starter.start()
        self.assertTrue(sdk.create_started.wait(timeout=1))
        with manager._lock:
            execution_id = next(iter(manager._starting))

        shutdown = threading.Thread(target=manager.shutdown)
        shutdown.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with manager._lock:
                if manager._shutdown:
                    break
            time.sleep(0.01)
        sdk.release_create.set()
        starter.join(timeout=5)
        shutdown.join(timeout=5)

        self.assertFalse(starter.is_alive())
        self.assertFalse(shutdown.is_alive())
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], TaskManagerError)
        error_text = str(errors[0])
        self.assertIn("shutting down", error_text)
        self.assertIn("runtime cleanup failure", error_text)
        self.assertIn("stop failed", error_text)
        self.assertIn("close failed", error_text)
        self.assertIn("kill_exec", sdk.cleanup_attempts)
        self.assertIn("stop_sandbox", sdk.cleanup_attempts)
        self.assertIn("kill_sandbox", sdk.cleanup_attempts)
        self.assertIn("remove_sandbox", sdk.cleanup_attempts)
        record = manager.execution_status(execution_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["reason"], "server_shutdown")
        with manager._lock:
            self.assertNotIn(execution_id, manager._starting)


if __name__ == "__main__":
    unittest.main()
