from __future__ import annotations

import asyncio
import errno
import importlib.metadata
import json
import os
import platform
import re
import shutil
import tempfile
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.execution import ExecutionReason, ExecutionState
from workspace_guard_mcp.execution_backend import (
    ExecutionBackend,
    ExecutionHandle,
    ExecutionRequest,
    OutputCallback,
)
from workspace_guard_mcp.microsandbox_backend import MicrosandboxBackend
from workspace_guard_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager
from workspace_guard_mcp.task_runner import TaskRunResult, run_execution

RUN_REAL = os.environ.get("WORKSPACE_GUARD_MCP_RUN_MICROSANDBOX_SECURITY_TESTS") == "1"
IMAGE_ENV = "WORKSPACE_GUARD_MCP_MICROSANDBOX_TEST_IMAGE"
IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-fA-F]{64}\Z")
FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "microsandbox_security"
SUPPORTED_PLATFORMS = {
    ("Linux", "x86_64"),
    ("Linux", "aarch64"),
    ("Linux", "arm64"),
    ("Darwin", "arm64"),
    ("Darwin", "aarch64"),
}


class _CapturingBackend:
    def __init__(self, backend: ExecutionBackend) -> None:
        self.backend = backend
        self.requests: list[ExecutionRequest] = []

    def start(
        self,
        request: ExecutionRequest,
        on_stdout: OutputCallback,
        on_stderr: OutputCallback,
    ) -> ExecutionHandle:
        self.requests.append(request)
        return self.backend.start(request, on_stdout, on_stderr)


def _json_output(text: str) -> dict[str, object]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("probe produced no JSON output")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise AssertionError("probe output must be a JSON object")
    return value


def _sandbox_exists(name: str) -> bool:
    microsandbox = importlib.import_module("microsandbox")
    not_found_error = getattr(microsandbox, "SandboxNotFoundError", None)
    if not isinstance(not_found_error, type) or not issubclass(
        not_found_error, BaseException
    ):
        raise AssertionError(
            "microsandbox==0.6.8 must expose SandboxNotFoundError for cleanup evidence"
        )

    async def inspect() -> bool:
        try:
            await microsandbox.Sandbox.get(name)
        except not_found_error:
            return False
        return True

    return asyncio.run(inspect())


@pytest.mark.microsandbox_security
@unittest.skipUnless(
    RUN_REAL,
    "real Microsandbox security tests require "
    "WORKSPACE_GUARD_MCP_RUN_MICROSANDBOX_SECURITY_TESTS=1",
)
class MicrosandboxSecurityIntegrationTests(unittest.TestCase):
    """Adversarial but bounded enforcement probes against a real microVM runtime."""

    image: str = ""
    platform_label: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        image = os.environ.get(IMAGE_ENV)
        if image is None:
            raise unittest.SkipTest(f"{IMAGE_ENV} must name a pre-cached OCI digest")
        if IMAGE_PATTERN.fullmatch(image) is None:
            raise AssertionError(f"{IMAGE_ENV} must be repository@sha256:<64hex>")

        current_platform = (platform.system(), platform.machine().lower())
        if current_platform not in SUPPORTED_PLATFORMS:
            raise unittest.SkipTest(
                "real Microsandbox security tests are not validated by this suite on "
                f"{current_platform[0]} {current_platform[1]}"
            )
        try:
            installed = importlib.metadata.version("microsandbox")
        except importlib.metadata.PackageNotFoundError as exc:
            raise unittest.SkipTest(
                "microsandbox==0.6.8 must be installed by the operator before the test"
            ) from exc
        if installed != "0.6.8":
            raise AssertionError(
                f"security evidence must use microsandbox==0.6.8, found {installed}"
            )
        cls.image = image
        cls.platform_label = f"{current_platform[0]} {current_platform[1]}"
        print(
            "Microsandbox security evidence context: "
            f"sdk={installed} host={cls.platform_label} image={image}"
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="workspace-guard-msb-security-"
        )
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.probes = self.root / "probes"
        shutil.copytree(FIXTURE_SOURCE, self.probes)
        (self.root / "original.txt").write_text("original\n", encoding="utf-8")
        self.settings = Settings.create(self.root)
        self.managers: list[TaskManager] = []

    def tearDown(self) -> None:
        for manager in reversed(self.managers):
            manager.shutdown()
        self.temporary.cleanup()

    def _limits(self, **overrides: object) -> TaskLimits:
        values: dict[str, object] = {
            "timeout_seconds": 8.0,
            "max_output_bytes": 64 * 1024,
            "max_snapshot_files": 1_000,
            "max_snapshot_bytes": 64 * 1024 * 1024,
            "memory": "256m",
            "cpus": "1",
            "pids": 64,
            "max_concurrent_tasks": 2,
            "max_workspace_file_bytes": 64 * 1024,
            "max_workspace_growth_bytes": 512 * 1024,
            "max_artifacts_per_execution": 16,
            "max_artifact_bytes": 256 * 1024,
            "max_total_artifact_bytes": 512 * 1024,
            "allow_best_effort_disk_limit": True,
        }
        values.update(overrides)
        return TaskLimits(**values)  # type: ignore[arg-type]

    def _task(
        self,
        name: str,
        probe: str,
        *,
        mode: str = "run",
        access: str = "read-only",
        args: tuple[str, ...] = (),
    ) -> TaskDefinition:
        return TaskDefinition(
            name,
            mode,
            self.image,
            ("python", f"/workspace/probes/{probe}", *args),
            workspace_access=access,
        )

    def _manager(
        self,
        tasks: Mapping[str, TaskDefinition],
        *,
        limits: TaskLimits | None = None,
        capture: bool = False,
    ) -> tuple[TaskManager, _CapturingBackend | None]:
        backend: ExecutionBackend = MicrosandboxBackend()
        capturing: _CapturingBackend | None = None
        if capture:
            capturing = _CapturingBackend(backend)
            backend = capturing
        configuration = TaskConfiguration(
            source=self.base / "tasks.json",
            runtime="microsandbox",
            limits=limits or self._limits(),
            tasks=MappingProxyType(dict(tasks)),
        )
        manager = TaskManager(self.settings, configuration, backend=backend)
        self.managers.append(manager)
        return manager, capturing

    def _profile_manager(
        self,
        *,
        limits: TaskLimits | None = None,
        capture: bool = False,
    ) -> tuple[TaskManager, _CapturingBackend | None]:
        backend: ExecutionBackend = MicrosandboxBackend()
        capturing: _CapturingBackend | None = None
        if capture:
            capturing = _CapturingBackend(backend)
            backend = capturing
        profile = ExecutionProfile(
            "microsandbox-coding",
            self.image,
            frozenset({"start_command"}),
            workspace_access="read-only",
            allow_arbitrary_commands=True,
        )
        configuration = TaskConfiguration(
            source=self.base / "profiles.json",
            runtime="microsandbox",
            limits=limits or self._limits(timeout_seconds=30.0),
            tasks=MappingProxyType({}),
            profiles=MappingProxyType({profile.name: profile}),
            default_profile=profile.name,
        )
        manager = TaskManager(self.settings, configuration, backend=backend)
        self.managers.append(manager)
        return manager, capturing

    def _direct_workspace(self, probe: str) -> Path:
        workspace = self.base / f"direct-{probe.removesuffix('.py')}"
        workspace.mkdir()
        shutil.copy2(FIXTURE_SOURCE / probe, workspace / probe)
        return workspace

    def _run_direct(
        self,
        probe: str,
        *,
        limits: TaskLimits | None = None,
        access: str = "read-only",
        artifact_path: Path | None = None,
        args: tuple[str, ...] = (),
        runtime_name: str | None = None,
    ) -> tuple[TaskRunResult, ExecutionRequest]:
        workspace = self._direct_workspace(probe)
        if probe == "readonly_probe.py":
            (workspace / "original.txt").write_text("original\n", encoding="utf-8")
        task = TaskDefinition(
            "probe",
            "run",
            self.image,
            ("python", f"/workspace/{probe}", *args),
            workspace_access=access,
        )
        selected_limits = limits or self._limits()
        started = time.monotonic()
        request = ExecutionRequest(
            runtime_name or f"workspace-guard-round75-{time.monotonic_ns()}",
            workspace,
            task,
            selected_limits,
            artifact_path=artifact_path,
            initial_workspace_bytes=sum(
                path.stat().st_size for path in workspace.rglob("*") if path.is_file()
            ),
            started_at=started,
            deadline=started + selected_limits.timeout_seconds,
        )
        result = run_execution(MicrosandboxBackend(), request)
        return result, request

    def test_network_none_blocks_dns_and_outbound_connections(self) -> None:
        result, _request = self._run_direct("network_probe.py")
        self.assertEqual(result.state, ExecutionState.SUCCEEDED, result.stderr)
        data = _json_output(result.stdout)
        self.assertIs(data["nameserver_present"], True)
        self.assertIs(data["dns_success"], False)
        self.assertIs(data["external_tcp_success"], False)

    def test_read_only_workspace_rejects_mutation_and_preserves_host_truth(
        self,
    ) -> None:
        result, request = self._run_direct("readonly_probe.py")
        self.assertEqual(result.state, ExecutionState.SUCCEEDED, result.stderr)
        data = _json_output(result.stdout)
        for operation in (
            "create",
            "overwrite",
            "truncate",
            "rename",
            "unlink",
            "mkdir",
            "chmod",
        ):
            outcome = data[operation]
            assert isinstance(outcome, dict)
            self.assertIs(outcome["success"], False, operation)

        self.assertEqual(
            (request.workspace_path / "original.txt").read_text(encoding="utf-8"),
            "original\n",
        )
        self.assertFalse((request.workspace_path / "created.txt").exists())
        self.assertFalse((request.workspace_path / "renamed.txt").exists())
        self.assertFalse((request.workspace_path / "created-dir").exists())

    def test_artifact_mount_is_writable_without_workspace_traversal(self) -> None:
        task = self._task("separation", "artifact_separation_probe.py")
        manager, _capture = self._manager({"separation": task})
        result = manager.run_task("separation")
        self.assertEqual(result["status"], "succeeded", result)
        data = _json_output(str(result["stdout"]))
        self.assertIs(data["artifact_write"], True)
        self.assertIs(data["traversal_write"], False)
        self.assertIs(data["workspace_target_exists"], False)
        self.assertFalse((self.root / "artifact-traversal.txt").exists())
        artifacts = result["artifacts"]
        assert isinstance(artifacts, list)
        self.assertEqual([artifact["name"] for artifact in artifacts], ["allowed.txt"])

    def test_artifact_special_files_fail_closed_at_canonical_policy(self) -> None:
        task = self._task("abuse", "artifact_abuse_probe.py")
        manager, _capture = self._manager({"abuse": task})
        result = manager.run_task("abuse")
        record = manager.execution_status(str(result["execution_id"]))
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["reason"], "artifact_policy_violation")
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(
            manager.execution_artifacts(str(result["execution_id"]))["artifacts"],
            [],
        )
        self.assertFalse((self.root / "artifact-traversal.txt").exists())

    def test_nproc_creates_an_observable_process_creation_ceiling(self) -> None:
        limits = self._limits(pids=8)
        result, _request = self._run_direct("process_limit_probe.py", limits=limits)
        self.assertEqual(result.state, ExecutionState.SUCCEEDED, result.stderr)
        data = _json_output(result.stdout)
        attempted = data["attempted"]
        started = data["started"]
        self.assertIsInstance(attempted, int)
        self.assertIsInstance(started, int)
        assert isinstance(attempted, int)
        assert isinstance(started, int)
        self.assertEqual(attempted, 20)
        self.assertLess(started, limits.pids, data)
        self.assertEqual(data["failure_errno_name"], "EAGAIN")
        self.assertIn(data["failure_type"], {"BlockingIOError", "OSError"})

    def test_guest_memory_allocation_cannot_sustain_twice_the_vm_memory(self) -> None:
        limits = self._limits(memory="256m", timeout_seconds=10.0)
        result, _request = self._run_direct("memory_probe.py", limits=limits)
        self.assertNotEqual(
            result.reason,
            ExecutionReason.RUNTIME_START_FAILED,
            result.stderr,
        )
        if result.state is ExecutionState.SUCCEEDED:
            data = _json_output(result.stdout)
            allocated = data["allocated_mib"]
            target = data["target_mib"]
            self.assertIsInstance(allocated, int)
            self.assertIsInstance(target, int)
            assert isinstance(allocated, int)
            assert isinstance(target, int)
            self.assertEqual(target, 512)
            self.assertLess(allocated, target)
            self.assertIn(data["failure_type"], {"MemoryError", "OSError"})
        else:
            self.assertEqual(result.state, ExecutionState.FAILED, result.as_dict())
            self.assertIsNone(result.reason, result.as_dict())
            self.assertIsNotNone(result.exit_code, result.as_dict())
            self.assertNotEqual(result.exit_code, 0, result.as_dict())

    def test_cpu_exposure_is_bounded_to_one_vcpu(self) -> None:
        result, _request = self._run_direct(
            "inspect_probe.py",
            limits=self._limits(cpus="1"),
        )
        self.assertEqual(result.state, ExecutionState.SUCCEEDED, result.stderr)
        data = _json_output(result.stdout)
        cpu_count = data["cpu_count"]
        affinity_count = data["affinity_count"]
        self.assertIsInstance(cpu_count, int, data)
        assert isinstance(cpu_count, int)
        self.assertLessEqual(cpu_count, 1)
        if affinity_count is not None:
            self.assertIsInstance(affinity_count, int, data)
            assert isinstance(affinity_count, int)
            self.assertLessEqual(affinity_count, 1)

    def test_restricted_profile_and_mount_flags_are_observable_in_guest(self) -> None:
        artifact = self.base / "inspect-artifacts"
        artifact.mkdir()
        result, _request = self._run_direct(
            "inspect_probe.py",
            artifact_path=artifact,
        )
        self.assertEqual(result.state, ExecutionState.SUCCEEDED, result.stderr)
        data = _json_output(result.stdout)
        self.assertEqual(data["no_new_privs"], "1")
        self.assertIs(data["cap_sys_admin"], False)
        for key in ("workspace_mount_options", "artifact_mount_options"):
            options = data[key]
            self.assertIsInstance(options, list, data)
            assert isinstance(options, list)
            self.assertIn("nosuid", options)
            self.assertIn("nodev", options)

    def test_fsize_rlimit_caps_a_single_writable_workspace_file(self) -> None:
        file_limit = 64 * 1024
        limits = self._limits(max_workspace_file_bytes=file_limit)
        result, request = self._run_direct(
            "fsize_probe.py",
            limits=limits,
            access="writable",
        )
        self.assertNotEqual(
            result.reason,
            ExecutionReason.RUNTIME_START_FAILED,
            result.stderr,
        )
        generated = request.workspace_path / "fsize.bin"
        self.assertTrue(generated.exists(), result.as_dict())
        self.assertLessEqual(generated.stat().st_size, file_limit)
        if result.state is ExecutionState.SUCCEEDED:
            data = _json_output(result.stdout)
            self.assertEqual(data["target_bytes"], 96 * 1024)
            size_bytes = data["size_bytes"]
            bytes_written = data["bytes_written"]
            self.assertIsInstance(size_bytes, int, data)
            self.assertIsInstance(bytes_written, int, data)
            assert isinstance(size_bytes, int)
            assert isinstance(bytes_written, int)
            self.assertLessEqual(size_bytes, file_limit)
            self.assertLessEqual(bytes_written, file_limit)
            self.assertEqual(size_bytes, bytes_written)
            if data["failure_errno"] is not None:
                self.assertEqual(data["failure_errno"], errno.EFBIG)
                self.assertIn(data["failure_type"], {"OSError", "BlockingIOError"})
        else:
            self.assertEqual(result.state, ExecutionState.FAILED, result.as_dict())
            self.assertIsNone(result.reason, result.as_dict())
            self.assertIsNotNone(result.exit_code, result.as_dict())
            self.assertNotEqual(result.exit_code, 0, result.as_dict())

    def test_aggregate_workspace_growth_monitor_stops_real_writable_execution(
        self,
    ) -> None:
        limits = self._limits(
            timeout_seconds=6.0,
            max_workspace_file_bytes=64 * 1024,
            max_workspace_growth_bytes=32 * 1024,
        )
        task = self._task("growth", "growth_probe.py", access="writable")
        manager, _capture = self._manager({"growth": task}, limits=limits)
        result = manager.run_task("growth")
        self.assertEqual(result["status"], "workspace_limit_exceeded", result)
        record = manager.execution_status(str(result["execution_id"]))
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["reason"], "workspace_limit_exceeded")

    def test_host_environment_and_public_results_do_not_leak_secrets(self) -> None:
        sentinel = f"round75-{time.monotonic_ns()}"
        task = self._task("inspect", "inspect_probe.py")
        manager, _capture = self._manager({"inspect": task})
        host_env = {
            "WORKSPACE_GUARD_SECRET_SENTINEL": sentinel,
            "AWS_SECRET_ACCESS_KEY": sentinel,
            "OPENAI_API_KEY": sentinel,
            "HTTP_PROXY": sentinel,
            "HTTPS_PROXY": sentinel,
            "SSH_AUTH_SOCK": sentinel,
        }
        with patch.dict(os.environ, host_env, clear=False):
            result = manager.run_task("inspect")
        self.assertEqual(result["status"], "succeeded", result)
        data = _json_output(str(result["stdout"]))
        selected_env = data["selected_env"]
        assert isinstance(selected_env, dict)
        self.assertTrue(all(value is None for value in selected_env.values()))

        execution_id = str(result["execution_id"])
        public_views = (
            result,
            manager.execution_status(execution_id),
            manager.execution_events(execution_id),
            manager.execution_artifacts(execution_id),
        )
        for view in public_views:
            self.assertNotIn(sentinel, repr(view))
            self.assertNotIn("workspace-guard-msb-security-", repr(view))

    def test_cross_execution_artifacts_and_private_tmp_do_not_persist(self) -> None:
        tasks = {
            "write": self._task("write", "isolation_probe.py", args=("write",)),
            "inspect": self._task("inspect", "isolation_probe.py", args=("inspect",)),
        }
        manager, _capture = self._manager(tasks)
        first = manager.run_task("write")
        self.assertEqual(first["status"], "succeeded", first)
        second = manager.run_task("inspect")
        self.assertEqual(second["status"], "succeeded", second)
        data = _json_output(str(second["stdout"]))
        self.assertIs(data["private_sentinel_visible"], False)
        self.assertEqual(data["artifact_names"], [])

    def test_uncached_digest_fails_closed_without_pull_claim(self) -> None:
        missing_image = "example.invalid/workspace-guard-missing@sha256:" + "0" * 64
        workspace = self._direct_workspace("inspect_probe.py")
        limits = self._limits(timeout_seconds=5.0)
        task = TaskDefinition(
            "missing-image",
            "run",
            missing_image,
            ("python", "/workspace/inspect_probe.py"),
        )
        started = time.monotonic()
        request = ExecutionRequest(
            f"workspace-guard-round75-missing-{time.monotonic_ns()}",
            workspace,
            task,
            limits,
            started_at=started,
            deadline=started + limits.timeout_seconds,
        )
        result = run_execution(MicrosandboxBackend(), request)
        self.assertEqual(result.state, ExecutionState.CRASHED, result.as_dict())
        self.assertEqual(result.reason, ExecutionReason.RUNTIME_START_FAILED)
        self.assertLess(time.monotonic() - started, limits.timeout_seconds + 2.0)

    def test_manual_cancellation_stops_descendant_artifact_writer_and_cleans_sandbox(
        self,
    ) -> None:
        limits = self._limits(timeout_seconds=8.0)
        task = self._task("tree", "process_tree_probe.py", mode="service")
        manager, capture = self._manager({"tree": task}, limits=limits, capture=True)
        assert capture is not None
        started = manager.start_task("tree")
        self.assertTrue(capture.requests)
        request = capture.requests[-1]
        assert request.artifact_path is not None
        heartbeat = request.artifact_path / "heartbeat"
        self._wait_for_heartbeat(heartbeat)
        before = heartbeat.stat().st_size

        stopped = manager.stop_task(str(started["task_id"]))
        self.assertEqual(stopped["status"], "stopped", stopped)
        self._assert_heartbeat_frozen_or_removed(heartbeat, before)
        self.assertFalse(_sandbox_exists(request.runtime_name))
        record = manager.execution_status(str(started["execution_id"]))
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["reason"], "user_cancelled")

    def test_start_command_service_cancellation_interrupts_real_command(self) -> None:
        manager, capture = self._profile_manager(capture=True)
        assert capture is not None
        started = manager.start_command(
            "microsandbox-coding",
            "python",
            [
                "-c",
                "import time; print('READY', flush=True); time.sleep(20); "
                "print('SHOULD_NOT_PRINT', flush=True)",
            ],
        )
        request = capture.requests[-1]
        task_id = str(started["task_id"])
        deadline = time.monotonic() + 4.0
        logs = manager.task_logs(task_id)
        while "READY" not in str(logs["stdout"]) and time.monotonic() < deadline:
            time.sleep(0.05)
            logs = manager.task_logs(task_id)
        self.assertIn("READY", str(logs["stdout"]), logs)

        stop_started = time.monotonic()
        stopped = manager.stop_task(task_id)
        stop_elapsed = time.monotonic() - stop_started

        self.assertLess(stop_elapsed, 8.0, stopped)
        self.assertEqual(stopped["status"], "stopped", stopped)
        final_logs = manager.task_logs(task_id)
        self.assertNotIn("SHOULD_NOT_PRINT", str(final_logs["stdout"]), final_logs)
        record = manager.execution_status(task_id)
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["reason"], "user_cancelled")
        events = manager.execution_events(task_id)["events"]
        assert isinstance(events, list)
        self.assertTrue(
            any(
                isinstance(event, dict)
                and event.get("event_type") == "cancellation_requested"
                for event in events
            )
        )
        self.assertEqual(manager.stop_task(task_id)["status"], "stopped")
        self.assertFalse(_sandbox_exists(request.runtime_name))

    def test_timeout_stops_descendant_artifact_writer_and_cleans_sandbox(self) -> None:
        limits = self._limits(timeout_seconds=0.8)
        task = self._task("tree", "process_tree_probe.py", mode="service")
        manager, capture = self._manager({"tree": task}, limits=limits, capture=True)
        assert capture is not None
        started = manager.start_task("tree")
        request = capture.requests[-1]
        assert request.artifact_path is not None
        heartbeat = request.artifact_path / "heartbeat"
        self._wait_for_heartbeat(heartbeat)
        before = heartbeat.stat().st_size

        task_id = str(started["task_id"])
        deadline = time.monotonic() + 5.0
        status = manager.task_status(task_id)
        while status["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = manager.task_status(task_id)
        self.assertEqual(status["status"], "timed_out", status)
        self._assert_heartbeat_frozen_or_removed(heartbeat, before)
        self.assertFalse(_sandbox_exists(request.runtime_name))
        record = manager.execution_status(task_id)
        self.assertEqual(record["state"], "timed_out")
        self.assertEqual(record["reason"], "timeout")

    def test_successful_execution_removes_its_exact_owned_sandbox(self) -> None:
        task = self._task("inspect", "inspect_probe.py")
        manager, capture = self._manager({"inspect": task}, capture=True)
        assert capture is not None
        result = manager.run_task("inspect")
        self.assertEqual(result["status"], "succeeded", result)
        request = capture.requests[-1]
        self.assertFalse(_sandbox_exists(request.runtime_name))

    def _wait_for_heartbeat(self, path: Path) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size >= 2:
                return
            time.sleep(0.05)
        self.fail("descendant heartbeat did not become observable")

    def _assert_heartbeat_frozen_or_removed(
        self,
        path: Path,
        minimum_size: int,
    ) -> None:
        if not path.exists():
            return
        first = path.stat().st_size
        self.assertGreaterEqual(first, minimum_size)
        time.sleep(0.3)
        if not path.exists():
            return
        self.assertEqual(path.stat().st_size, first)
