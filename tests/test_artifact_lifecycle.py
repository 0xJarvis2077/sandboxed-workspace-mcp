from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import MappingProxyType

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.execution import ExecutionReason, ExecutionState
from workspace_guard_mcp.task_config import (
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager, TaskManagerError
from workspace_guard_mcp.task_runner import (
    ArtifactGrowthMonitor,
    ContainerRequest,
    build_container_argv,
)

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
        self.requests: list[ContainerRequest] = []
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


class ArtifactContainerContractTests(unittest.TestCase):
    def test_readonly_workspace_gets_independent_writable_artifact_mount(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            workspace = Path(workspace_dir)
            artifacts = Path(artifact_dir)
            request = ContainerRequest(
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
            request = ContainerRequest(
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
        self.assertEqual(listed["artifacts"], artifacts)
        self.assertEqual(
            manager.execution_artifacts(
                str(result["execution_id"]), owner_scope="owner-b"
            )["artifacts"],
            [],
        )

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

    def test_runtime_artifact_monitor_stops_on_policy_violation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            artifacts = Path(artifact_dir)
            (artifacts / "nested").mkdir()
            handle = BlockingHandle()
            request = ContainerRequest(
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
            request = ContainerRequest(
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
