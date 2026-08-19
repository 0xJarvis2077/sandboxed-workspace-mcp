from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import MappingProxyType

from workspace_guard_mcp.artifact_store import EphemeralArtifactStore
from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.container_backend import build_container_argv
from workspace_guard_mcp.execution import ExecutionReason, ExecutionState
from workspace_guard_mcp.execution_backend import ExecutionRequest
from workspace_guard_mcp.execution_store import SqliteExecutionStore
from workspace_guard_mcp.task_config import (
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager, TaskManagerError
from workspace_guard_mcp.task_runner import ArtifactGrowthMonitor

PINNED_IMAGE = "example.invalid/workspace-guard-mcp@sha256:" + "b" * 64


class ImmediateHandle:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.stopped = False
        self.closed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class BlockingHandle:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.exit_code = 0
        self.stopped = False
        self.closed = False

    def wait(self, timeout: float | None = None) -> int:
        if not self.done.wait(timeout):
            raise TimeoutError
        return self.exit_code

    def stop(self) -> None:
        self.stopped = True
        self.exit_code = -15
        self.done.set()

    def finish(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.done.set()

    def close(self) -> None:
        self.closed = True


class _LateArtifactMixin:
    def __init__(self, artifact_path: Path, files: dict[str, bytes]) -> None:
        self._artifact_path = artifact_path
        self._files = files

    def _write_late_artifacts(self) -> None:
        for name, content in self._files.items():
            (self._artifact_path / name).write_bytes(content)


class LateArtifactImmediateHandle(_LateArtifactMixin, ImmediateHandle):
    def __init__(
        self,
        artifact_path: Path,
        files: dict[str, bytes],
        exit_code: int,
        *,
        wait_error: bool = False,
    ) -> None:
        _LateArtifactMixin.__init__(self, artifact_path, files)
        ImmediateHandle.__init__(self, exit_code)
        self._wait_error = wait_error

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_error:
            raise RuntimeError("forced runtime monitor failure")
        return super().wait(timeout)

    def close(self) -> None:
        self._write_late_artifacts()
        super().close()


class LateArtifactBlockingHandle(_LateArtifactMixin, BlockingHandle):
    def __init__(self, artifact_path: Path, files: dict[str, bytes]) -> None:
        _LateArtifactMixin.__init__(self, artifact_path, files)
        BlockingHandle.__init__(self)

    def close(self) -> None:
        self._write_late_artifacts()
        super().close()


class LateArtifactBackend:
    def __init__(
        self,
        files: dict[str, bytes],
        *,
        exit_code: int = 0,
        blocking: bool = False,
        wait_error: bool = False,
    ) -> None:
        self.files = files
        self.exit_code = exit_code
        self.blocking = blocking
        self.wait_error = wait_error
        self.handles: list[
            LateArtifactImmediateHandle | LateArtifactBlockingHandle
        ] = []
        self.started = threading.Event()

    def start(self, request, on_stdout, on_stderr):
        assert request.artifact_path is not None
        if self.blocking:
            handle: LateArtifactImmediateHandle | LateArtifactBlockingHandle = (
                LateArtifactBlockingHandle(request.artifact_path, self.files)
            )
        else:
            handle = LateArtifactImmediateHandle(
                request.artifact_path,
                self.files,
                self.exit_code,
                wait_error=self.wait_error,
            )
        self.handles.append(handle)
        self.started.set()
        return handle


class ArtifactWritingBackend:
    def __init__(
        self,
        files: dict[str, bytes],
        *,
        exit_code: int = 0,
        blocking: bool = False,
    ) -> None:
        self.files = files
        self.exit_code = exit_code
        self.blocking = blocking
        self.requests: list[ExecutionRequest] = []
        self.handles: list[ImmediateHandle | BlockingHandle] = []
        self.started = threading.Event()

    def start(self, request, on_stdout, on_stderr):
        self.requests.append(request)
        assert request.artifact_path is not None
        for name, content in self.files.items():
            (request.artifact_path / name).write_bytes(content)
        handle: ImmediateHandle | BlockingHandle
        if self.blocking:
            handle = BlockingHandle()
        else:
            handle = ImmediateHandle(self.exit_code)
        self.handles.append(handle)
        self.started.set()
        return handle


def _configuration(
    base: Path,
    *,
    limits: TaskLimits | None = None,
) -> TaskConfiguration:
    tasks = {
        "run": TaskDefinition("run", "run", PINNED_IMAGE, ("python",)),
        "service": TaskDefinition("service", "service", PINNED_IMAGE, ("python",)),
        "readonly": TaskDefinition(
            "readonly",
            "run",
            PINNED_IMAGE,
            ("python",),
            workspace_access="read-only",
        ),
    }
    return TaskConfiguration(
        source=base / "tasks.json",
        runtime="docker",
        limits=limits or TaskLimits(timeout_seconds=2, max_output_bytes=4096),
        tasks=MappingProxyType(tasks),
    )


def _settings(base: Path) -> Settings:
    return Settings(root=base)


def _artifact_items(payload: dict[str, object]) -> list[dict[str, object]]:
    raw = payload["artifacts"]
    assert isinstance(raw, list)
    items: list[dict[str, object]] = []
    for item in raw:
        assert isinstance(item, dict)
        items.append(item)
    return items


class ArtifactContainerContractTests(unittest.TestCase):
    def test_readonly_workspace_gets_independent_writable_artifact_mount(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            workspace = Path(workspace_dir)
            artifacts = Path(artifact_dir)
            request = ExecutionRequest(
                "container",
                workspace,
                TaskDefinition(
                    "readonly",
                    "run",
                    PINNED_IMAGE,
                    ("python",),
                    workspace_access="read-only",
                ),
                TaskLimits(),
                artifact_path=artifacts,
            )
            argv = build_container_argv("docker", request)
        rendered = " ".join(argv)
        self.assertIn("destination=/workspace,readonly", rendered)
        self.assertIn("destination=/artifacts", rendered)
        self.assertNotIn("destination=/artifacts,readonly", rendered)
        self.assertIn("WORKSPACEGUARD_ARTIFACT_DIR=/artifacts", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--network=none", argv)
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("--security-opt=no-new-privileges", argv)

    def test_writable_workspace_and_artifact_mount_are_both_writable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            request = ExecutionRequest(
                "container",
                Path(workspace_dir),
                TaskDefinition(
                    "writable",
                    "run",
                    PINNED_IMAGE,
                    ("python",),
                    workspace_access="writable",
                ),
                TaskLimits(),
                artifact_path=Path(artifact_dir),
            )
            argv = build_container_argv("docker", request)
        rendered = " ".join(argv)
        self.assertIn("destination=/workspace", rendered)
        self.assertNotIn("destination=/workspace,readonly", rendered)
        self.assertIn("destination=/artifacts", rendered)


class ArtifactLifecycleTests(unittest.TestCase):
    def test_sync_execution_returns_metadata_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = ArtifactWritingBackend({"result.csv": b"a,b\n1,2\n"})
            manager = TaskManager(
                _settings(root),
                _configuration(root),
                backend=backend,
            )
            result = manager.run_task("run", owner_scope="owner-a")
            request = backend.requests[0]

        self.assertEqual(result["status"], "succeeded")
        artifacts = result["artifacts"]
        self.assertIsInstance(artifacts, list)
        artifact = artifacts[0]  # type: ignore[index]
        self.assertEqual(artifact["name"], "result.csv")  # type: ignore[index]
        resource_uri = str(artifact["resource_uri"])  # type: ignore[index]
        self.assertTrue(resource_uri.startswith("workspaceguard://artifact/"))
        self.assertFalse(request.artifact_path.exists())  # type: ignore[union-attr]
        listed = manager.execution_artifacts(
            str(result["execution_id"]), owner_scope="owner-a"
        )
        self.assertTrue(listed["manifest_complete"])
        self.assertEqual(listed["artifacts"], artifacts)
        wrong_owner = manager.execution_artifacts(
            str(result["execution_id"]), owner_scope="owner-b"
        )
        self.assertTrue(wrong_owner["manifest_complete"])
        wrong_owner_items = _artifact_items(wrong_owner)
        self.assertEqual(len(wrong_owner_items), 1)
        self.assertFalse(wrong_owner_items[0]["content_available"])
        self.assertIsNone(wrong_owner_items[0]["resource_uri"])

    def test_ttl_expiry_preserves_manifest_but_removes_content_availability(
        self,
    ) -> None:
        now = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_store = EphemeralArtifactStore(
                ttl_seconds=5,
                clock=lambda: now[0],
            )
            manager = TaskManager(
                _settings(root),
                _configuration(root),
                backend=ArtifactWritingBackend({"result.txt": b"ok"}),
                artifact_store=artifact_store,
            )
            result = manager.run_task("run")
            execution_id = str(result["execution_id"])
            before = manager.execution_artifacts(execution_id)
            now[0] = 16.0
            after = manager.execution_artifacts(execution_id)
        self.assertTrue(before["manifest_complete"])
        before_items = _artifact_items(before)
        after_items = _artifact_items(after)
        self.assertTrue(before_items[0]["content_available"])
        self.assertTrue(after["manifest_complete"])
        self.assertEqual(after_items[0]["name"], "result.txt")
        self.assertFalse(after_items[0]["content_available"])
        self.assertIsNone(after_items[0]["resource_uri"])

    def test_capacity_eviction_preserves_old_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_store = EphemeralArtifactStore(
                max_retained_executions=1,
                max_store_bytes=1024,
            )
            manager = TaskManager(
                _settings(root),
                _configuration(root),
                backend=ArtifactWritingBackend({"result.txt": b"ok"}),
                artifact_store=artifact_store,
            )
            first = manager.run_task("run")
            second = manager.run_task("run")
            first_manifest = manager.execution_artifacts(str(first["execution_id"]))
            second_manifest = manager.execution_artifacts(str(second["execution_id"]))
        self.assertTrue(first_manifest["manifest_complete"])
        first_items = _artifact_items(first_manifest)
        second_items = _artifact_items(second_manifest)
        self.assertFalse(first_items[0]["content_available"])
        self.assertIsNone(first_items[0]["resource_uri"])
        self.assertTrue(second_items[0]["content_available"])

    def test_sqlite_restart_preserves_manifest_but_not_ephemeral_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            root.mkdir()
            database = base / "executions.sqlite3"
            first_manager = TaskManager(
                _settings(root),
                _configuration(root),
                backend=ArtifactWritingBackend({"result.txt": b"ok"}),
                execution_store=SqliteExecutionStore(database),
            )
            result = first_manager.run_task("run")
            execution_id = str(result["execution_id"])
            before_restart = first_manager.execution_artifacts(execution_id)
            self.assertTrue(_artifact_items(before_restart)[0]["content_available"])

            restarted = TaskManager(
                _settings(root),
                _configuration(root),
                execution_store=SqliteExecutionStore(database),
            )
            after = restarted.execution_artifacts(execution_id)
        self.assertTrue(after["manifest_complete"])
        after_items = _artifact_items(after)
        self.assertEqual(after_items[0]["name"], "result.txt")
        self.assertFalse(after_items[0]["content_available"])
        self.assertIsNone(after_items[0]["resource_uri"])

    def test_failed_execution_can_retain_safe_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = ArtifactWritingBackend(
                {"junit.xml": b"<testsuite/>"},
                exit_code=2,
            )
            manager = TaskManager(
                _settings(root),
                _configuration(root),
                backend=backend,
            )
            result = manager.run_task("run")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["artifacts"]), 1)  # type: ignore[arg-type]

    def test_final_collector_rejects_oversized_artifact_without_partial_publish(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limits = TaskLimits(
                timeout_seconds=2,
                max_output_bytes=4096,
                max_artifact_bytes=4,
                max_total_artifact_bytes=8,
            )
            backend = ArtifactWritingBackend({"good.bin": b"ok", "large.bin": b"12345"})
            manager = TaskManager(
                _settings(root),
                _configuration(root, limits=limits),
                backend=backend,
            )
            result = manager.run_task("run")
            record = manager.execution_status(str(result["execution_id"]))
            artifacts = manager.execution_artifacts(str(result["execution_id"]))
        self.assertEqual(record["state"], ExecutionState.FAILED.value)
        self.assertEqual(
            record["reason"],
            ExecutionReason.ARTIFACT_LIMIT_EXCEEDED.value,
        )
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(artifacts["artifacts"], [])
        self.assertEqual(result["resources"], record["resources"])
        self.assertIsNotNone(record["resources"])
        self.assertTrue(artifacts["manifest_complete"])

    def test_timeout_truth_survives_late_oversized_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limits = TaskLimits(
                timeout_seconds=0.02,
                max_output_bytes=4096,
                max_artifact_bytes=4,
                max_total_artifact_bytes=8,
            )
            manager = TaskManager(
                _settings(root),
                _configuration(root, limits=limits),
                backend=LateArtifactBackend({"late.bin": b"12345"}, blocking=True),
            )
            result = manager.run_task("run")
            status = manager.execution_status(str(result["execution_id"]))
            manifest = manager.execution_artifacts(str(result["execution_id"]))
        self.assertEqual(status["state"], ExecutionState.TIMED_OUT.value)
        self.assertEqual(status["reason"], ExecutionReason.TIMEOUT.value)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["resources"], status["resources"])
        self.assertTrue(manifest["manifest_complete"])
        self.assertEqual(manifest["artifacts"], [])

    def test_failed_truth_survives_late_oversized_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limits = TaskLimits(max_artifact_bytes=4, max_total_artifact_bytes=8)
            manager = TaskManager(
                _settings(root),
                _configuration(root, limits=limits),
                backend=LateArtifactBackend({"late.bin": b"12345"}, exit_code=2),
            )
            result = manager.run_task("run")
            status = manager.execution_status(str(result["execution_id"]))
        self.assertEqual(status["state"], ExecutionState.FAILED.value)
        self.assertIsNone(status["reason"])
        self.assertEqual(status["exit_code"], 2)

    def test_crash_truth_survives_late_oversized_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limits = TaskLimits(max_artifact_bytes=4, max_total_artifact_bytes=8)
            manager = TaskManager(
                _settings(root),
                _configuration(root, limits=limits),
                backend=LateArtifactBackend({"late.bin": b"12345"}, wait_error=True),
            )
            result = manager.run_task("run")
            status = manager.execution_status(str(result["execution_id"]))
        self.assertEqual(status["state"], ExecutionState.CRASHED.value)
        self.assertEqual(status["reason"], ExecutionReason.RUNTIME_MONITOR_FAILED.value)
        self.assertEqual(status["error_summary"], "execution runtime monitor failed")

    def test_service_cancel_truth_survives_late_oversized_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            limits = TaskLimits(max_artifact_bytes=4, max_total_artifact_bytes=8)
            backend = LateArtifactBackend({"late.bin": b"12345"}, blocking=True)
            manager = TaskManager(
                _settings(root),
                _configuration(root, limits=limits),
                backend=backend,
            )
            started = manager.start_task("service")
            execution_id = str(started["execution_id"])
            stopped = manager.stop_task(execution_id)
            status = manager.execution_status(execution_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(status["state"], ExecutionState.CANCELLED.value)
        self.assertEqual(status["reason"], ExecutionReason.USER_CANCELLED.value)
        self.assertEqual(stopped["resources"], status["resources"])

    def test_service_artifacts_are_hidden_until_terminal_then_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = ArtifactWritingBackend(
                {"partial.json": b"{}"},
                blocking=True,
            )
            manager = TaskManager(
                _settings(root),
                _configuration(root),
                backend=backend,
            )
            started = manager.start_task("service", owner_scope="owner-a")
            execution_id = str(started["execution_id"])
            with self.assertRaisesRegex(
                TaskManagerError,
                "artifacts are available only after execution is terminal",
            ):
                manager.execution_artifacts(execution_id, owner_scope="owner-a")
            handle = backend.handles[0]
            assert isinstance(handle, BlockingHandle)
            handle.finish(0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if manager.execution_status(execution_id)["state"] == "succeeded":
                    break
                time.sleep(0.01)
            listed = manager.execution_artifacts(
                execution_id,
                owner_scope="owner-a",
            )
        self.assertEqual(len(listed["artifacts"]), 1)  # type: ignore[arg-type]
        artifact = listed["artifacts"][0]  # type: ignore[index]
        self.assertEqual(artifact["name"], "partial.json")

    def test_runtime_artifact_monitor_cannot_account_deleted_open_file(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            artifacts = Path(artifact_dir)
            ghost = artifacts / "ghost.bin"
            with ghost.open("wb", buffering=0) as handle_file:
                handle_file.write(b"12345")
                ghost.unlink()
                handle_file.write(b"67890")
                request = ExecutionRequest(
                    "container",
                    Path(workspace_dir),
                    TaskDefinition("run", "run", PINNED_IMAGE, ("python",)),
                    TaskLimits(max_artifact_bytes=4, max_total_artifact_bytes=8),
                    artifact_path=artifacts,
                )
                monitor = ArtifactGrowthMonitor(request, BlockingHandle())
                usage = monitor._measure_usage()
        self.assertFalse(usage.limit_exceeded)
        self.assertFalse(usage.policy_violation)
        self.assertEqual(usage.pressure, 0.0)

    def test_runtime_artifact_monitor_stops_on_policy_violation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            artifacts = Path(artifact_dir)
            (artifacts / "nested").mkdir()
            handle = BlockingHandle()
            request = ExecutionRequest(
                "container",
                Path(workspace_dir),
                TaskDefinition("run", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
                artifact_path=artifacts,
            )
            monitor = ArtifactGrowthMonitor(request, handle)
            monitor.start()
            self.assertTrue(handle.done.wait(1))
            monitor.stop_and_join()
        self.assertTrue(handle.stopped)
        self.assertTrue(monitor.policy_violation.is_set())

    def test_runtime_artifact_monitor_stops_on_limit(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            artifacts = Path(artifact_dir)
            (artifacts / "big.bin").write_bytes(b"12345")
            handle = BlockingHandle()
            request = ExecutionRequest(
                "container",
                Path(workspace_dir),
                TaskDefinition("run", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(
                    max_artifact_bytes=4,
                    max_total_artifact_bytes=8,
                ),
                artifact_path=artifacts,
            )
            monitor = ArtifactGrowthMonitor(request, handle)
            monitor.start()
            self.assertTrue(handle.done.wait(1))
            monitor.stop_and_join()
        self.assertTrue(handle.stopped)
        self.assertTrue(monitor.limit_exceeded.is_set())


if __name__ == "__main__":
    unittest.main()
