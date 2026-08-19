from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from _mcp_assertions import require_call_tool_result, require_structured_content

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.execution import (
    ExecutionKind,
    ExecutionMode,
    ExecutionReason,
    ExecutionRecord,
    ExecutionState,
)
from workspace_guard_mcp.execution_backend import ExecutionRequest as ContainerRequest
from workspace_guard_mcp.execution_store import (
    InMemoryExecutionStore,
    SqliteExecutionStore,
)
from workspace_guard_mcp.server import create_server
from workspace_guard_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
    load_task_config,
)
from workspace_guard_mcp.task_manager import (
    TaskLogBuffer,
    TaskManager,
    TaskManagerError,
)
from workspace_guard_mcp.task_runner import (
    BoundedOutput,
    CliContainerBackend,
    TaskExecutionError,
    WorkspaceGrowthMonitor,
    _next_workspace_scan_delay,
    _WorkspaceUsage,
    build_container_argv,
    measure_workspace_usage,
)
from workspace_guard_mcp.task_runner import (
    run_execution as run_container_task,
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


class FakeBackend:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        blocking: bool = False,
        start_error: Exception | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.blocking = blocking
        self.start_error = start_error
        self.requests: list[ContainerRequest] = []
        self.handles: list[ImmediateHandle | BlockingHandle] = []
        self.started = threading.Event()

    def start(self, request, on_stdout, on_stderr):
        if self.start_error is not None:
            raise self.start_error
        self.requests.append(request)
        on_stdout(self.stdout)
        on_stderr(self.stderr)
        handle: ImmediateHandle | BlockingHandle
        if self.blocking:
            handle = BlockingHandle()
        else:
            handle = ImmediateHandle(self.exit_code)
        self.handles.append(handle)
        self.started.set()
        return handle


class FakeProcess:
    def __init__(self, *, return_code: int = 0, running: bool = False) -> None:
        self.stdout = io.BytesIO(b"container stdout")
        self.stderr = io.BytesIO(b"container stderr")
        self.returncode = return_code
        self.running = running
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        self.running = False
        return self.returncode

    def poll(self) -> int | None:
        return None if self.running else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.running = False


def configuration(
    base: Path,
    *,
    limits: TaskLimits | None = None,
) -> TaskConfiguration:
    tasks = {
        "test": TaskDefinition(
            "test", "run", PINNED_IMAGE, ("python", "-m", "unittest")
        ),
        "dev": TaskDefinition(
            "dev", "service", PINNED_IMAGE, ("python", "-m", "example_app")
        ),
    }
    return TaskConfiguration(
        source=base / "trusted-tasks.json",
        runtime="docker",
        limits=limits or TaskLimits(timeout_seconds=2, max_output_bytes=4096),
        tasks=MappingProxyType(tasks),
    )


def profile_configuration(
    base: Path,
    *,
    tools: frozenset[str] = frozenset(
        {"python_version", "run_pytest", "run_python_script"}
    ),
    limits: TaskLimits | None = None,
    workspace_access: str = "read-only",
) -> TaskConfiguration:
    profiles = {
        "debug": ExecutionProfile(
            "debug",
            PINNED_IMAGE,
            tools,
            workspace_access=workspace_access,
            allow_arbitrary_commands=bool(
                {"run_command", "start_command"}.intersection(tools)
            ),
        )
    }
    return TaskConfiguration(
        source=base / "trusted-profiles.json",
        runtime="docker",
        limits=limits or TaskLimits(timeout_seconds=2, max_output_bytes=4096),
        tasks=MappingProxyType({}),
        profiles=MappingProxyType(profiles),
    )


class ContainerRunnerTests(unittest.TestCase):
    def test_bounded_output_counts_observed_bytes_beyond_retention(self) -> None:
        capture = BoundedOutput(8)
        capture.stdout(b"12345")
        capture.stderr(b"abcdef")
        capture.diagnostic_stderr(b"internal")

        stdout, stderr = capture.text()
        self.assertLessEqual(len(stdout.encode()) + len(stderr.encode()), 8)
        self.assertTrue(capture.truncated)
        self.assertEqual(capture.observed_stdout_bytes, 5)
        self.assertEqual(capture.observed_stderr_bytes, 6)

        threads = [
            threading.Thread(target=capture.stdout, args=(b"xy",)) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(capture.observed_stdout_bytes, 21)

        exact = BoundedOutput(4)
        exact.stdout(b"1234")
        self.assertFalse(exact.truncated)
        self.assertEqual(exact.observed_stdout_bytes, 4)
        self.assertEqual(exact.observed_stderr_bytes, 0)
        exact.stderr(b"x")
        self.assertTrue(exact.truncated)
        self.assertEqual(exact.observed_stderr_bytes, 1)

        large = BoundedOutput(3)
        large.stderr(b"12345")
        self.assertEqual(large.observed_stderr_bytes, 5)
        self.assertEqual(large.text()[1], "123")

    def test_workspace_measurement_reports_growth_and_shrink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            (snapshot / "base.bin").write_bytes(b"1234")
            limits = TaskLimits(
                max_workspace_file_bytes=1024,
                max_workspace_growth_bytes=1024,
            )
            grown = measure_workspace_usage(
                snapshot,
                initial_workspace_bytes=2,
                limits=limits,
            )
            shrunk = measure_workspace_usage(
                snapshot,
                initial_workspace_bytes=8,
                limits=limits,
            )
        self.assertEqual(grown.total_bytes, 4)
        self.assertEqual(grown.growth_bytes, 2)
        self.assertEqual(shrunk.growth_bytes, 0)

    def test_workspace_enforcement_measurement_stops_at_first_exceeded_limit(
        self,
    ) -> None:
        first = MagicMock()
        first.stat.return_value.st_size = 11
        first.is_symlink.return_value = False
        first.is_dir.return_value = False
        first.is_file.return_value = True
        second = MagicMock()
        second.stat.side_effect = AssertionError("scan continued after limit exceeded")
        scanner = MagicMock()
        scanner.__enter__.return_value = iter((first, second))
        limits = TaskLimits(
            max_workspace_file_bytes=10,
            max_workspace_growth_bytes=1024,
        )

        with patch(
            "workspace_guard_mcp.task_runner.os.scandir",
            return_value=scanner,
        ):
            usage = measure_workspace_usage(
                Path("/snapshot"),
                initial_workspace_bytes=0,
                limits=limits,
                stop_on_exceeded=True,
            )

        self.assertTrue(usage.exceeded)
        self.assertEqual(usage.total_bytes, 11)
        second.stat.assert_not_called()

    def test_container_argv_has_every_required_isolation_and_no_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            task = TaskDefinition(
                "test", "run", PINNED_IMAGE, ("python", "-m", "unittest")
            )
            request = ContainerRequest(
                "workspace-guard-mcp-test-token", snapshot, task, TaskLimits()
            )
            argv = build_container_argv("/usr/bin/docker", request)

        rendered = " ".join(argv)
        container_env = [
            argv[index + 1] for index, item in enumerate(argv) if item == "--env"
        ]
        self.assertEqual(argv[0:2], ["/usr/bin/docker", "run"])
        self.assertEqual(
            container_env,
            [
                "HOME=/tmp/home",
                "TMPDIR=/tmp",
                "XDG_CACHE_HOME=/tmp/cache",
                "RUFF_CACHE_DIR=/tmp/cache/ruff",
                "MYPY_CACHE_DIR=/tmp/cache/mypy",
                "COVERAGE_FILE=/tmp/.coverage",
                "PYTHONDONTWRITEBYTECODE=1",
                "PYTHONPYCACHEPREFIX=/tmp/cache/python",
                "PIP_NO_CACHE_DIR=1",
                "npm_config_cache=/tmp/npm-cache",
                "CI=1",
            ],
        )
        for expected in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            "--memory",
            "--cpus",
            "--tmpfs",
            "destination=/workspace",
            "readonly",
            "HOME=/tmp/home",
            "TMPDIR=/tmp",
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONPYCACHEPREFIX=/tmp/cache/python",
            "CI=1",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("shell", argv)
        self.assertNotIn("-c", argv[: argv.index(PINNED_IMAGE)])
        self.assertNotIn("PYTEST_ADDOPTS", rendered)
        self.assertEqual(argv[-3:], ["python", "-m", "unittest"])

    def test_command_workdir_and_argv_cannot_become_runtime_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            task = TaskDefinition(
                "coding-run_command",
                "run",
                PINNED_IMAGE,
                ("tool", "--network=host", "-c", "echo unsafe"),
            )
            request = ContainerRequest(
                "workspace-guard-mcp-command",
                snapshot,
                task,
                TaskLimits(),
                workdir="/workspace/src",
            )
            argv = build_container_argv("/usr/bin/docker", request)

        image_index = argv.index(PINNED_IMAGE)
        self.assertEqual(argv[argv.index("--workdir") + 1], "/workspace/src")
        self.assertEqual(
            argv[image_index + 1 :],
            ["tool", "--network=host", "-c", "echo unsafe"],
        )
        self.assertNotIn("--network=host", argv[:image_index])
        self.assertNotIn("sh", argv[:image_index])

        with tempfile.TemporaryDirectory() as directory:
            unsafe = ContainerRequest(
                "workspace-guard-mcp-command",
                Path(directory),
                task,
                TaskLimits(),
                workdir="/tmp",
            )
            with self.assertRaisesRegex(TaskExecutionError, "inside /workspace"):
                build_container_argv("/usr/bin/docker", unsafe)

    def test_writable_mount_has_fsize_ulimit_and_growth_monitor_stops_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            task = TaskDefinition(
                "build",
                "run",
                PINNED_IMAGE,
                ("python",),
                workspace_access="writable",
            )
            limits = TaskLimits(
                timeout_seconds=2,
                max_output_bytes=1024,
                max_workspace_file_bytes=5,
                max_workspace_growth_bytes=5,
                allow_best_effort_disk_limit=True,
            )
            request = ContainerRequest(
                "workspace-guard-mcp-growth", snapshot, task, limits
            )
            argv = build_container_argv("/usr/bin/docker", request)
            mount = argv[argv.index("--mount") + 1]
            self.assertNotIn("readonly", mount)
            self.assertIn("--ulimit", argv)
            self.assertIn("fsize=5:5", argv)

            backend = FakeBackend(blocking=True)
            results = []
            worker = threading.Thread(
                target=lambda: results.append(run_container_task(backend, request))
            )
            worker.start()
            self.assertTrue(backend.started.wait(timeout=2))
            (snapshot / "large.bin").write_bytes(b"123456")
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].status, "workspace_limit_exceeded")
        self.assertTrue(backend.handles[0].stopped)

    def test_workspace_growth_monitor_cancellation_keeps_usage_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            (snapshot / "first.txt").write_bytes(b"1234")
            (snapshot / "second.txt").write_bytes(b"5678")
            request = ContainerRequest(
                "workspace-guard-mcp-growth-cancel",
                snapshot,
                TaskDefinition(
                    "build",
                    "run",
                    PINNED_IMAGE,
                    ("python",),
                    workspace_access="writable",
                ),
                TaskLimits(
                    max_workspace_file_bytes=1024,
                    max_workspace_growth_bytes=1024,
                ),
            )
            monitor = WorkspaceGrowthMonitor(request, ImmediateHandle())
            real_scandir = os.scandir

            class StoppingEntries:
                def __init__(self, path: Path) -> None:
                    self._entries = list(real_scandir(path))

                def __enter__(self):
                    return iter(self)

                def __exit__(self, exc_type, exc, tb) -> None:
                    return None

                def __iter__(self):
                    yield self._entries[0]
                    monitor._stop.set()
                    yield self._entries[1]

            with patch(
                "workspace_guard_mcp.task_runner.os.scandir",
                side_effect=lambda path: StoppingEntries(Path(path)),
            ):
                result = monitor._measure_usage()

        self.assertIsInstance(result, _WorkspaceUsage)
        self.assertFalse(result.exceeded)
        self.assertEqual(result.pressure, 0.0)

    def test_workspace_growth_monitor_ignores_transient_scan_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            request = ContainerRequest(
                "workspace-guard-mcp-growth-oserror",
                snapshot,
                TaskDefinition(
                    "build",
                    "run",
                    PINNED_IMAGE,
                    ("python",),
                    workspace_access="writable",
                ),
                TaskLimits(
                    max_workspace_file_bytes=1024,
                    max_workspace_growth_bytes=1024,
                ),
            )
            handle = ImmediateHandle()
            monitor = WorkspaceGrowthMonitor(request, handle)
            monitor._started = True

            with (
                patch.object(monitor, "_measure_usage", side_effect=OSError("gone")),
                patch.object(
                    monitor._stop,
                    "wait",
                    side_effect=lambda timeout: monitor._stop.set(),
                ),
            ):
                monitor._run()

        self.assertFalse(monitor.exceeded.is_set())
        self.assertFalse(handle.stopped)

    def test_workspace_growth_monitor_delay_targets_a_bounded_duty_cycle(self) -> None:
        self.assertEqual(_next_workspace_scan_delay(0.01, 0.0), 0.25)
        self.assertAlmostEqual(_next_workspace_scan_delay(0.1, 0.0), 0.9)
        self.assertEqual(_next_workspace_scan_delay(0.5, 0.0), 2.0)
        self.assertEqual(_next_workspace_scan_delay(10.0, 0.8), 0.1)

    def test_read_only_task_does_not_start_growth_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ContainerRequest(
                "workspace-guard-mcp-read-only",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            monitor = WorkspaceGrowthMonitor(request, ImmediateHandle())
            with patch.object(monitor._thread, "start") as start:
                monitor.start()

        start.assert_not_called()

    def test_missing_runtime_fails_without_host_fallback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("workspace_guard_mcp.task_runner.shutil.which", return_value=None),
        ):
            backend = CliContainerBackend("docker")
            request = ContainerRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            with self.assertRaisesRegex(TaskExecutionError, "fallback is disabled"):
                backend.start(request, lambda data: None, lambda data: None)

    def test_cli_backend_uses_sanitized_environment_and_tracked_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ContainerRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            process = FakeProcess(running=True)
            with (
                patch(
                    "workspace_guard_mcp.task_runner.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "workspace_guard_mcp.task_runner.subprocess.Popen",
                    return_value=process,
                ) as popen,
                patch(
                    "workspace_guard_mcp.task_runner.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as stop,
            ):
                backend = CliContainerBackend("docker")
                stdout: list[bytes] = []
                stderr: list[bytes] = []
                handle = backend.start(request, stdout.append, stderr.append)
                handle.stop()
                handle.close()

        call = popen.call_args
        self.assertNotIn("shell", call.kwargs)
        self.assertNotIn("HOME", call.kwargs["env"])
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(stdout, [b"container stdout"])
        self.assertEqual(stderr, [b"container stderr"])
        self.assertTrue(process.terminated)
        self.assertEqual(stop.call_args.args[0][-1], "workspace-guard-mcp-test-token")

    def test_cli_backend_streams_flushed_pipe_output_before_eof(self) -> None:
        child_script = (
            "import sys\n"
            "sys.stdout.buffer.write(b'booted\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "sys.stderr.buffer.write(b'booted\\n')\n"
            "sys.stderr.buffer.flush()\n"
            "sys.stdin.buffer.read(1)\n"
        )
        real_popen = subprocess.Popen
        process: subprocess.Popen[bytes] | None = None
        handle = None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_received = threading.Event()
        stderr_received = threading.Event()

        def launch_pipe_process(*args, **kwargs):
            nonlocal process
            process = real_popen(
                [sys.executable, "-c", child_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            return process

        def on_stdout(data: bytes) -> None:
            stdout_chunks.append(data)
            stdout_received.set()

        def on_stderr(data: bytes) -> None:
            stderr_chunks.append(data)
            stderr_received.set()

        try:
            with tempfile.TemporaryDirectory() as directory:
                request = ContainerRequest(
                    "workspace-guard-mcp-live-logs",
                    Path(directory),
                    TaskDefinition("dev", "service", PINNED_IMAGE, ("python",)),
                    TaskLimits(),
                )
                with (
                    patch(
                        "workspace_guard_mcp.task_runner.shutil.which",
                        return_value="/usr/bin/docker",
                    ),
                    patch(
                        "workspace_guard_mcp.task_runner.subprocess.Popen",
                        side_effect=launch_pipe_process,
                    ),
                ):
                    handle = CliContainerBackend("docker").start(
                        request, on_stdout, on_stderr
                    )

                self.assertTrue(stdout_received.wait(timeout=2))
                self.assertTrue(stderr_received.wait(timeout=2))
                self.assertEqual(b"".join(stdout_chunks), b"booted\n")
                self.assertEqual(b"".join(stderr_chunks), b"booted\n")
                assert process is not None
                self.assertIsNone(process.poll(), "pipe writers must still be open")
        finally:
            if process is not None and process.stdin is not None:
                process.stdin.close()
            if handle is not None:
                try:
                    handle.wait(timeout=2)
                except TimeoutError:
                    assert process is not None
                    process.kill()
                    process.wait(timeout=2)
                finally:
                    handle.close()
            elif process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def test_cli_backend_start_error_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ContainerRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            with (
                patch(
                    "workspace_guard_mcp.task_runner.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "workspace_guard_mcp.task_runner.subprocess.Popen",
                    side_effect=OSError("denied"),
                ),
                self.assertRaisesRegex(TaskExecutionError, "failed to start"),
            ):
                CliContainerBackend("docker").start(
                    request, lambda data: None, lambda data: None
                )

    def test_sync_results_separate_streams_and_report_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ContainerRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(timeout_seconds=1, max_output_bytes=1024),
            )
            failed = run_container_task(
                FakeBackend(stdout=b"out", stderr=b"traceback", exit_code=3),
                request,
            )
            start_failed = run_container_task(
                FakeBackend(start_error=TaskExecutionError("runtime unavailable")),
                request,
            )

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.exit_code, 3)
        self.assertEqual(failed.stdout, "out")
        self.assertEqual(failed.stderr, "traceback")
        self.assertEqual(start_failed.status, "start_failed")
        self.assertIn("runtime unavailable", start_failed.stderr)

    def test_monitor_start_failure_cleans_up_started_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(blocking=True)
            request = ContainerRequest(
                "workspace-guard-mcp-monitor-start-failure",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(timeout_seconds=1, max_output_bytes=1024),
            )
            with patch.object(
                WorkspaceGrowthMonitor,
                "start",
                side_effect=RuntimeError("thread unavailable"),
            ):
                result = run_container_task(backend, request)

        self.assertEqual(result.status, "start_failed")
        self.assertIn("workspace monitor start failure", result.stderr)
        self.assertTrue(backend.handles[0].stopped)
        self.assertTrue(backend.handles[0].closed)

    def test_cleanup_failure_does_not_mask_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(exit_code=0)
            request = ContainerRequest(
                "workspace-guard-mcp-cleanup-failure",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(timeout_seconds=1, max_output_bytes=1024),
            )
            with patch.object(
                ImmediateHandle,
                "close",
                side_effect=RuntimeError("pipe cleanup failed"),
            ):
                result = run_container_task(backend, request)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("container cleanup failure", result.stderr)
        self.assertIn("pipe cleanup failed", result.stderr)

    def test_timeout_and_output_overflow_stop_the_tracked_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = TaskDefinition("test", "run", PINNED_IMAGE, ("python",))
            timeout_backend = FakeBackend(blocking=True)
            timeout = run_container_task(
                timeout_backend,
                ContainerRequest(
                    "workspace-guard-mcp-timeout",
                    Path(directory),
                    task,
                    TaskLimits(timeout_seconds=0.1, max_output_bytes=1024),
                ),
            )
            overflow_backend = FakeBackend(stdout=b"x" * 1025, blocking=True)
            overflow = run_container_task(
                overflow_backend,
                ContainerRequest(
                    "workspace-guard-mcp-overflow",
                    Path(directory),
                    task,
                    TaskLimits(timeout_seconds=1, max_output_bytes=1024),
                ),
            )
            cancellation = threading.Event()
            cancellation.set()
            cancelled_backend = FakeBackend(blocking=True)
            cancelled = run_container_task(
                cancelled_backend,
                ContainerRequest(
                    "workspace-guard-mcp-cancelled",
                    Path(directory),
                    task,
                    TaskLimits(timeout_seconds=1, max_output_bytes=1024),
                ),
                cancellation,
            )

        self.assertEqual(timeout.status, "timed_out")
        self.assertTrue(timeout.timed_out)
        self.assertTrue(timeout_backend.handles[0].stopped)
        self.assertEqual(overflow.status, "output_limit_exceeded")
        self.assertTrue(overflow.truncated)
        self.assertEqual(overflow.stdout_bytes, 1025)
        self.assertEqual(overflow.stderr_bytes, 0)
        self.assertTrue(overflow_backend.handles[0].stopped)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(cancelled_backend.handles[0].stopped)


class TaskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        (self.root / "test_module.py").write_text("value = 1\n", encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_user.py").write_text(
            "def test_login():\n    assert True\n", encoding="utf-8"
        )
        (self.root / "debug.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "notes.txt").write_text("not Python\n", encoding="utf-8")
        self.settings = Settings.create(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_task_uses_snapshot_and_public_list_hides_secrets(self) -> None:
        backend = FakeBackend(stdout=b"ok\n")
        manager = TaskManager(self.settings, configuration(self.base), backend=backend)

        public = manager.list_tasks()
        result = manager.run_task("test")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout"], "ok\n")
        self.assertNotIn(PINNED_IMAGE, repr(public))
        self.assertNotIn(str(self.base), repr(public))
        request = backend.requests[0]
        self.assertNotEqual(request.workspace_path, self.root)
        self.assertFalse(request.workspace_path.exists())
        self.assertEqual(
            (self.root / "test_module.py").read_text(encoding="utf-8"),
            "value = 1\n",
        )

    def test_final_accounting_failure_preserves_success(self) -> None:
        limits = TaskLimits(
            timeout_seconds=2,
            max_output_bytes=4096,
            max_workspace_file_bytes=1024 * 1024,
            max_workspace_growth_bytes=1024 * 1024,
            allow_best_effort_disk_limit=True,
        )
        configured = TaskConfiguration(
            source=self.base / "writable.json",
            runtime="docker",
            limits=limits,
            tasks=MappingProxyType(
                {
                    "write": TaskDefinition(
                        "write",
                        "run",
                        PINNED_IMAGE,
                        ("python",),
                        workspace_access="writable",
                    )
                }
            ),
        )
        manager = TaskManager(self.settings, configured, backend=FakeBackend())
        baseline = _WorkspaceUsage(123, 10, 0, False, 0.0)
        with patch(
            "workspace_guard_mcp.task_manager.measure_workspace_usage",
            side_effect=[baseline, OSError("final scan unavailable")],
        ):
            result = manager.run_task("write")

        self.assertEqual(result["status"], "succeeded")
        resources = result["resources"]
        assert isinstance(resources, dict)
        self.assertEqual(resources["workspace_initial_bytes"], 123)
        self.assertIsNone(resources["workspace_final_bytes"])
        self.assertIsNone(resources["workspace_growth_bytes"])

    def test_snapshot_initializer_bytes_are_baseline_not_runtime_growth(self) -> None:
        limits = TaskLimits(
            timeout_seconds=2,
            max_output_bytes=4096,
            max_workspace_file_bytes=1024 * 1024,
            max_workspace_growth_bytes=1024 * 1024,
            allow_best_effort_disk_limit=True,
        )
        backend = FakeBackend()
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_pytest"}),
                limits=limits,
                workspace_access="writable",
            ),
            backend=backend,
        )
        source_bytes = sum(
            path.stat().st_size for path in self.root.rglob("*") if path.is_file()
        )
        initializer_size = 137

        def write_initializer(
            path: Path,
            *,
            show_locals: bool,
            output_limit: int,
        ) -> None:
            del show_locals, output_limit
            (path / "server-initializer.bin").write_bytes(b"x" * initializer_size)

        with patch(
            "workspace_guard_mcp.task_manager._write_debug_plugin",
            side_effect=write_initializer,
        ):
            result = manager.run_pytest("debug")

        expected_baseline = source_bytes + initializer_size
        self.assertEqual(backend.requests[0].initial_workspace_bytes, expected_baseline)
        resources = result["resources"]
        assert isinstance(resources, dict)
        self.assertEqual(resources["workspace_initial_bytes"], expected_baseline)
        self.assertEqual(resources["workspace_final_bytes"], expected_baseline)
        self.assertEqual(resources["workspace_growth_bytes"], 0)

    def test_prestart_failure_preserves_known_workspace_baseline(self) -> None:
        configured = configuration(self.base)
        store = InMemoryExecutionStore()
        manager = TaskManager(
            self.settings,
            configured,
            backend=FakeBackend(start_error=OSError("runtime denied")),
            execution_store=store,
        )
        source_bytes = sum(
            path.stat().st_size for path in self.root.rglob("*") if path.is_file()
        )
        execution_id = "prestart-accounting-test"

        with (
            patch(
                "workspace_guard_mcp.task_manager.secrets.token_urlsafe",
                return_value=execution_id,
            ),
            self.assertRaisesRegex(TaskManagerError, "failed to start"),
        ):
            manager.start_task("dev")

        record = store.get(execution_id)
        self.assertEqual(record.state, ExecutionState.CRASHED)
        assert record.resources is not None
        self.assertEqual(record.resources.workspace_initial_bytes, source_bytes)
        self.assertIsNone(record.resources.workspace_final_bytes)
        self.assertIsNone(record.resources.workspace_growth_bytes)

    def test_only_predefined_names_and_matching_modes_are_allowed(self) -> None:
        manager = TaskManager(
            self.settings, configuration(self.base), backend=FakeBackend()
        )
        with self.assertRaisesRegex(TaskManagerError, "unknown task name"):
            manager.run_task("python -c 'bad'")
        with self.assertRaisesRegex(TaskManagerError, "required mode"):
            manager.run_task("dev")
        with self.assertRaisesRegex(TaskManagerError, "required mode"):
            manager.start_task("test")

    def test_structured_profiles_resolve_default_unique_and_ambiguous_candidates(
        self,
    ) -> None:
        profiles = {
            "safe": ExecutionProfile("safe", PINNED_IMAGE, frozenset({"run_pytest"})),
            "coding": ExecutionProfile(
                "coding",
                PINNED_IMAGE,
                frozenset({"run_pytest", "run_command"}),
                allow_arbitrary_commands=True,
            ),
        }
        configured = TaskConfiguration(
            source=self.base / "profiles.json",
            runtime="docker",
            limits=TaskLimits(timeout_seconds=2, max_output_bytes=4096),
            tasks=MappingProxyType({}),
            profiles=MappingProxyType(profiles),
            default_profile="coding",
        )
        manager = TaskManager(self.settings, configured, backend=FakeBackend())
        self.assertEqual(manager.resolve_execution_profile("run_pytest").name, "coding")
        self.assertEqual(
            manager.resolve_execution_profile("run_pytest", "safe").name, "safe"
        )
        with self.assertRaisesRegex(TaskManagerError, "does not authorize"):
            manager.resolve_execution_profile("run_command", "safe")
        with self.assertRaisesRegex(TaskManagerError, "unknown execution profile"):
            manager.resolve_execution_profile("run_pytest", "missing")
        with self.assertRaisesRegex(TaskManagerError, "profile is required"):
            manager.resolve_execution_profile("run_command")
        listed = manager.list_execution_profiles()
        self.assertEqual(listed["default_profile"], "coding")
        profile_entries = listed["profiles"]
        assert isinstance(profile_entries, list)
        by_name: dict[str, dict[object, object]] = {}
        for profile in profile_entries:
            assert isinstance(profile, dict)
            name = profile.get("name")
            assert isinstance(name, str)
            by_name[name] = profile
        self.assertTrue(by_name["coding"]["default"])
        self.assertFalse(by_name["safe"]["default"])

        ambiguous = TaskConfiguration(
            source=self.base / "ambiguous.json",
            runtime="docker",
            limits=TaskLimits(timeout_seconds=2, max_output_bytes=4096),
            tasks=MappingProxyType({}),
            profiles=MappingProxyType(
                {
                    "one": ExecutionProfile(
                        "one", PINNED_IMAGE, frozenset({"run_pytest"})
                    ),
                    "two": ExecutionProfile(
                        "two", PINNED_IMAGE, frozenset({"run_pytest"})
                    ),
                }
            ),
        )
        with self.assertRaisesRegex(TaskManagerError, "ambiguous"):
            TaskManager(
                self.settings, ambiguous, backend=FakeBackend()
            ).resolve_execution_profile("run_pytest")

    def test_derived_profile_is_consumed_as_effective_authorization(self) -> None:
        config_path = self.base / "derived-profiles.json"
        config_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "runtime": "docker",
                    "default_profile": "coding",
                    "profiles": {
                        "coding": {
                            "extends": "safe",
                            "tools_add": ["run_command"],
                            "allow_arbitrary_commands": True,
                        },
                        "safe": {
                            "image": PINNED_IMAGE,
                            "tools": ["run_pytest"],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        configured = load_task_config(config_path, workspace_root=self.root)
        backend = FakeBackend(stdout=b"Python 3.13\n")
        manager = TaskManager(self.settings, configured, backend=backend)

        resolved = manager.resolve_execution_profile("run_pytest")
        self.assertEqual(resolved.name, "coding")
        self.assertEqual(resolved.tools, {"run_pytest", "run_command"})
        result = manager.run_command("coding", "python", ["--version"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(backend.requests[0].task.image, PINNED_IMAGE)
        self.assertEqual(backend.requests[0].task.workspace_access, "read-only")

        listed = manager.list_execution_profiles()
        entries = listed["profiles"]
        assert isinstance(entries, list)
        coding = next(entry for entry in entries if entry["name"] == "coding")
        self.assertEqual(coding["tools"], ["run_command", "run_pytest"])
        self.assertNotIn("extends", coding)
        self.assertNotIn("tools_add", coding)

        with self.assertRaisesRegex(TaskManagerError, "does not authorize"):
            manager.resolve_execution_profile("run_command", "safe")

    def test_structured_analysis_tools_compile_and_adapt_results(self) -> None:
        (self.root / "src").mkdir()
        tools = frozenset(
            {
                "run_ruff",
                "run_mypy",
                "run_pytest_coverage",
            }
        )
        ruff_backend = FakeBackend(
            stdout=b'[{"filename":"src/example.py","location":{"row":1,"column":1},"code":"F401","message":"unused"}]',
            exit_code=1,
        )
        manager = TaskManager(
            self.settings,
            profile_configuration(self.base, tools=tools),
            backend=ruff_backend,
        )
        ruff = manager.run_ruff(paths=["src"])
        self.assertEqual(ruff["diagnostics"][0]["code"], "F401")  # type: ignore[index]
        self.assertEqual(
            ruff_backend.requests[0].task.argv,
            ("ruff", "check", "--output-format=json", "--", "src"),
        )

        mypy_backend = FakeBackend(
            stdout=b"src/example.py:2:4: error: bad type [assignment]\n", exit_code=1
        )
        mypy_manager = TaskManager(
            self.settings,
            profile_configuration(self.base, tools=tools),
            backend=mypy_backend,
        )
        mypy = mypy_manager.run_mypy(paths=["src"], strict=True)
        self.assertEqual(mypy["diagnostics"][0]["code"], "assignment")  # type: ignore[index]
        self.assertIn("--strict", mypy_backend.requests[0].task.argv)

        coverage_backend = FakeBackend(stdout=b"SWMCPCOVERAGE:bad\n", exit_code=0)
        coverage_manager = TaskManager(
            self.settings,
            profile_configuration(self.base, tools=tools),
            backend=coverage_backend,
        )
        coverage = coverage_manager.run_pytest_coverage(
            targets=["tests"], branch=True, fail_under=80
        )
        self.assertIn("coverage_parser_error", coverage)
        self.assertEqual(coverage_backend.requests[0].task.argv[0:2], ("python", "-c"))

        with self.assertRaises(ValueError):
            manager.run_ruff(paths=["../outside"])

    def test_structured_python_profile_compiles_only_server_generated_argv(
        self,
    ) -> None:
        backend = FakeBackend(stdout=b"ok\n")
        manager = TaskManager(
            self.settings,
            profile_configuration(self.base),
            backend=backend,
        )

        version = manager.python_version("debug")
        pytest_result = manager.run_pytest(
            "debug",
            targets=["tests/test_user.py::test_login"],
            keyword="login and not slow",
            verbosity=2,
            exit_first=True,
            no_capture=True,
            traceback="short",
        )
        quiet_result = manager.run_pytest(
            "debug", targets=["tests"], quiet=True, traceback="long"
        )
        script = manager.run_python_script("debug", "debug.py")

        self.assertEqual(version["status"], "succeeded")
        self.assertEqual(pytest_result["status"], "succeeded")
        self.assertEqual(quiet_result["status"], "succeeded")
        self.assertEqual(script["status"], "succeeded")
        self.assertEqual(backend.requests[0].task.argv, ("python", "--version"))
        self.assertEqual(
            backend.requests[1].task.argv,
            (
                "python",
                "-m",
                "pytest",
                "-p",
                "workspace_guard_mcp_debug_plugin",
                "-o",
                "cache_dir=/tmp/cache/pytest",
                "-vv",
                "-x",
                "-s",
                "--tb=short",
                "-k",
                "login and not slow",
                "--",
                "tests/test_user.py::test_login",
            ),
        )
        self.assertEqual(
            backend.requests[2].task.argv,
            (
                "python",
                "-m",
                "pytest",
                "-p",
                "workspace_guard_mcp_debug_plugin",
                "-o",
                "cache_dir=/tmp/cache/pytest",
                "-q",
                "--tb=long",
                "--",
                "tests",
            ),
        )
        self.assertEqual(backend.requests[3].task.argv, ("python", "--", "debug.py"))
        self.assertGreater(
            backend.requests[1].initial_workspace_bytes,
            backend.requests[0].initial_workspace_bytes,
        )
        self.assertTrue(
            all(not request.workspace_path.exists() for request in backend.requests)
        )

        public = manager.list_execution_profiles()
        self.assertNotIn(PINNED_IMAGE, repr(public))
        self.assertNotIn(str(self.base), repr(public))
        self.assertNotIn("argv", repr(public))

    def test_run_command_uses_profile_image_caller_argv_and_validated_cwd(self) -> None:
        (self.root / "src").mkdir()
        backend = FakeBackend(stdout=b"checked\n")
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_command"}),
            ),
            backend=backend,
        )

        result = manager.run_command(
            "debug",
            "ruff",
            ["check", "--fix", "."],
            "src",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stdout"], "checked\n")
        request = backend.requests[0]
        self.assertEqual(request.task.image, PINNED_IMAGE)
        self.assertEqual(request.task.mode, "run")
        self.assertEqual(request.task.argv, ("ruff", "check", "--fix", "."))
        self.assertEqual(request.workdir, "/workspace/src")
        self.assertFalse(request.workspace_path.exists())

        public = manager.list_execution_profiles()
        self.assertNotIn(PINNED_IMAGE, repr(public))
        self.assertNotIn("argv", repr(public))
        with self.assertRaisesRegex(TaskManagerError, "does not authorize"):
            TaskManager(
                self.settings,
                profile_configuration(
                    self.base,
                    tools=frozenset({"python_version"}),
                ),
                backend=FakeBackend(),
            ).run_command("debug", "python", cwd="../outside")

    def test_start_command_reuses_service_logs_capacity_stop_and_shutdown(self) -> None:
        backend = FakeBackend(stdout=b"ready\n", stderr=b"warning\n", blocking=True)
        limits = TaskLimits(
            timeout_seconds=3,
            max_output_bytes=1024,
            max_concurrent_tasks=1,
        )
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_command", "start_command"}),
                limits=limits,
            ),
            backend=backend,
        )

        started = manager.start_command(
            "debug",
            "uvicorn",
            ["app:app", "--log-level", "debug"],
        )
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        request = backend.requests[0]

        self.assertNotEqual(task_id, request.runtime_name)
        self.assertEqual(request.task.mode, "service")
        self.assertEqual(
            request.task.argv,
            ("uvicorn", "app:app", "--log-level", "debug"),
        )
        self.assertEqual(request.workdir, "/workspace")
        self.assertNotIn("uvicorn", repr(manager.task_status(task_id)))
        self.assertEqual(manager.task_logs(task_id)["stdout"], "ready\n")
        self.assertEqual(manager.task_logs(task_id)["stderr"], "warning\n")
        with self.assertRaisesRegex(TaskManagerError, "concurrent"):
            manager.run_command("debug", "ruff", ["check", "."])

        stopped = manager.stop_task(task_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(backend.handles[0].stopped)
        self.assertFalse(request.workspace_path.exists())

        cancelled_backend = FakeBackend(blocking=True)
        cancelled_manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"start_command"}),
            ),
            backend=cancelled_backend,
        )
        cancellation = threading.Event()
        cancellation.set()
        with self.assertRaisesRegex(TaskManagerError, "failed to start"):
            cancelled_manager.start_command(
                "debug",
                "uvicorn",
                ["app:app"],
                cancellation_event=cancellation,
            )
        self.assertEqual(cancelled_backend.requests, [])
        self.assertTrue(cancelled_manager._capacity.acquire(blocking=False))
        cancelled_manager._capacity.release()

        shutdown_backend = FakeBackend(blocking=True)
        shutdown_manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"start_command"}),
            ),
            backend=shutdown_backend,
        )
        shutdown_manager.start_command("debug", "python", ["-m", "http.server"])
        shutdown_manager.shutdown()
        self.assertTrue(shutdown_backend.handles[0].stopped)
        self.assertFalse(shutdown_backend.requests[0].workspace_path.exists())

    def test_python_profile_rejects_unauthorized_options_and_unsafe_paths(self) -> None:
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base, tools=frozenset({"run_pytest", "run_python_script"})
            ),
            backend=FakeBackend(),
        )
        with self.assertRaisesRegex(TaskManagerError, "does not authorize"):
            manager.python_version("debug")
        with self.assertRaisesRegex(ValueError, "verbosity"):
            manager.run_pytest("debug", verbosity=3)
        with self.assertRaisesRegex(ValueError, "traceback"):
            manager.run_pytest("debug", traceback="native")
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            manager.run_pytest("debug", quiet=True, verbosity=1)
        with self.assertRaisesRegex(ValueError, "keyword exceeds"):
            manager.run_pytest("debug", keyword="x" * 513)
        with self.assertRaisesRegex(ValueError, "at most 32"):
            manager.run_pytest("debug", targets=["tests"] * 33)
        with self.assertRaisesRegex(ValueError, "bounded string"):
            manager.run_pytest("debug", targets=["x" * 1025])

        (self.root / ".env.py").write_text("SECRET = 1\n", encoding="utf-8")
        outside = self.base / "outside.py"
        outside.write_text("print('outside')\n", encoding="utf-8")
        link_path = self.root / "linked.py"
        try:
            link_path.symlink_to(self.root / "debug.py")
        except (OSError, NotImplementedError):
            link: Path | None = None
        else:
            link = link_path
        invalid_paths = [
            "../outside.py",
            str(outside),
            ".env.py",
            "notes.txt",
            "-c",
        ]
        if link is not None:
            invalid_paths.append("linked.py")
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                manager.run_python_script("debug", path)

        with self.assertRaises(ValueError):
            manager.run_pytest("debug", targets=["../outside.py::test_bad"])
        ignored = self.root / ".venv"
        ignored.mkdir()
        (ignored / "test_hidden.py").write_text(
            "def test_hidden(): pass\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "omitted"):
            manager.run_pytest("debug", targets=[".venv/test_hidden.py"])

    def test_profile_timeout_and_cancellation_cleanup_snapshot(self) -> None:
        backend = FakeBackend(blocking=True)
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                limits=TaskLimits(timeout_seconds=0.1, max_output_bytes=1024),
            ),
            backend=backend,
        )
        result = manager.python_version("debug")
        self.assertEqual(result["status"], "timed_out")
        self.assertFalse(backend.requests[0].workspace_path.exists())

        failed_backend = FakeBackend(exit_code=2)
        failed_manager = TaskManager(
            self.settings,
            profile_configuration(self.base),
            backend=failed_backend,
        )
        failed = failed_manager.python_version("debug")
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed_backend.requests[0].workspace_path.exists())

        cancelled_backend = FakeBackend(blocking=True)
        cancelled_manager = TaskManager(
            self.settings,
            profile_configuration(self.base),
            backend=cancelled_backend,
        )
        cancellation = threading.Event()
        cancelled_results: list[dict[str, object]] = []
        worker = threading.Thread(
            target=lambda: cancelled_results.append(
                cancelled_manager.python_version(
                    "debug", cancellation_event=cancellation
                )
            )
        )
        worker.start()
        self.assertTrue(cancelled_backend.started.wait(timeout=2))
        cancellation.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(cancelled_results[0]["status"], "cancelled")
        self.assertFalse(cancelled_backend.requests[0].workspace_path.exists())

        shutdown_backend = FakeBackend(blocking=True)
        shutdown_manager = TaskManager(
            self.settings,
            profile_configuration(self.base),
            backend=shutdown_backend,
        )
        shutdown_results: list[dict[str, object]] = []
        shutdown_worker = threading.Thread(
            target=lambda: shutdown_results.append(
                shutdown_manager.python_version("debug")
            )
        )
        shutdown_worker.start()
        self.assertTrue(shutdown_backend.started.wait(timeout=2))
        shutdown_manager.shutdown()
        shutdown_worker.join(timeout=2)
        self.assertFalse(shutdown_worker.is_alive())
        self.assertEqual(shutdown_results[0]["status"], "cancelled")
        self.assertFalse(shutdown_backend.requests[0].workspace_path.exists())

    def test_concurrency_limit_rejects_a_second_task(self) -> None:
        backend = FakeBackend(blocking=True)
        limits = TaskLimits(
            timeout_seconds=3,
            max_output_bytes=1024,
            max_concurrent_tasks=1,
        )
        manager = TaskManager(
            self.settings,
            configuration(self.base, limits=limits),
            backend=backend,
        )
        result: list[dict[str, object]] = []
        worker = threading.Thread(
            target=lambda: result.append(manager.run_task("test"))
        )
        worker.start()
        self.assertTrue(backend.started.wait(timeout=2))
        with self.assertRaisesRegex(TaskManagerError, "concurrent"):
            manager.run_task("test")
        assert isinstance(backend.handles[0], BlockingHandle)
        backend.handles[0].finish()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "succeeded")

    def test_service_logs_status_stop_and_external_ids(self) -> None:
        backend = FakeBackend(stdout=b"ready\n", stderr=b"warning\n", blocking=True)
        manager = TaskManager(self.settings, configuration(self.base), backend=backend)
        started = manager.start_task("dev")
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        workspace_path = backend.requests[0].workspace_path

        logs = manager.task_logs(task_id)
        self.assertEqual(logs["stdout"], "ready\n")
        self.assertEqual(logs["stderr"], "warning\n")
        self.assertEqual(manager.task_status(task_id)["status"], "running")
        with self.assertRaisesRegex(TaskManagerError, "unknown task_id"):
            manager.stop_task("external-container-name")
        with self.assertRaisesRegex(TaskManagerError, "cursor"):
            manager.task_logs(task_id, 100_000)

        stopped = manager.stop_task(task_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(backend.handles[0].stopped)
        self.assertFalse(workspace_path.exists())

    def test_service_timeout_and_shutdown_stop_only_tracked_handles(self) -> None:
        timeout_backend = FakeBackend(blocking=True)
        timeout_manager = TaskManager(
            self.settings,
            configuration(
                self.base,
                limits=TaskLimits(timeout_seconds=0.1, max_output_bytes=1024),
            ),
            backend=timeout_backend,
        )
        started = timeout_manager.start_task("dev")
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = timeout_manager.task_status(task_id)
            if status["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "timed_out")
        self.assertTrue(timeout_backend.handles[0].stopped)

        shutdown_backend = FakeBackend(blocking=True)
        shutdown_manager = TaskManager(
            self.settings, configuration(self.base), backend=shutdown_backend
        )
        shutdown_manager.start_task("dev")
        shutdown_manager.shutdown()
        self.assertEqual(len(shutdown_backend.handles), 1)
        self.assertTrue(shutdown_backend.handles[0].stopped)
        with self.assertRaisesRegex(TaskManagerError, "shutting down"):
            shutdown_manager.run_task("test")

    def test_shutdown_cancels_snapshot_races_without_start_or_capacity_leak(
        self,
    ) -> None:
        for operation, name in (("run_task", "test"), ("start_task", "dev")):
            with self.subTest(operation=operation):
                backend = FakeBackend()
                manager = TaskManager(
                    self.settings, configuration(self.base), backend=backend
                )
                entered = threading.Event()
                resume = threading.Event()
                snapshot_paths: list[Path] = []
                errors: list[BaseException] = []
                original = manager._create_snapshot

                def pausing_snapshot(
                    *,
                    _original=original,
                    _paths=snapshot_paths,
                    _entered=entered,
                    _resume=resume,
                    **kwargs,
                ):
                    snapshot = _original(**kwargs)
                    _paths.append(snapshot.path)
                    _entered.set()
                    self.assertTrue(_resume.wait(timeout=3))
                    return snapshot

                def invoke(
                    _manager=manager,
                    _operation=operation,
                    _name=name,
                    _errors=errors,
                ) -> None:
                    try:
                        getattr(_manager, _operation)(_name)
                    except BaseException as exc:
                        _errors.append(exc)

                with patch.object(
                    manager, "_create_snapshot", side_effect=pausing_snapshot
                ):
                    starter = threading.Thread(target=invoke)
                    starter.start()
                    self.assertTrue(entered.wait(timeout=2))
                    shutdown = threading.Thread(target=manager.shutdown)
                    shutdown.start()
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        with manager._lock:
                            if manager._shutdown:
                                break
                        time.sleep(0.01)
                    resume.set()
                    starter.join(timeout=3)
                    shutdown.join(timeout=3)

                self.assertFalse(starter.is_alive())
                self.assertFalse(shutdown.is_alive())
                self.assertEqual(backend.requests, [])
                self.assertTrue(snapshot_paths)
                self.assertFalse(snapshot_paths[0].exists())
                self.assertTrue(manager._capacity.acquire(blocking=False))
                manager._capacity.release()
                manager.shutdown()
                if operation == "start_task":
                    self.assertTrue(errors)

    def test_shutdown_stops_a_running_synchronous_task(self) -> None:
        backend = FakeBackend(blocking=True)
        manager = TaskManager(self.settings, configuration(self.base), backend=backend)
        results: list[dict[str, object]] = []
        worker = threading.Thread(
            target=lambda: results.append(manager.run_task("test"))
        )
        worker.start()
        self.assertTrue(backend.started.wait(timeout=2))

        manager.shutdown()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertTrue(backend.handles[0].stopped)

    def test_service_natural_exit_and_start_failure_are_explicit(self) -> None:
        successful_backend = FakeBackend(exit_code=0)
        successful = TaskManager(
            self.settings,
            configuration(self.base),
            backend=successful_backend,
        )
        started = successful.start_task("dev")
        task_id = started["task_id"]
        assert isinstance(task_id, str)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = successful.task_status(task_id)
            if status["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(successful.stop_task(task_id)["status"], "succeeded")

        failing_backend = FakeBackend(start_error=OSError("runtime denied"))
        failing = TaskManager(
            self.settings,
            configuration(self.base),
            backend=failing_backend,
        )
        with self.assertRaisesRegex(TaskManagerError, "failed to start"):
            failing.start_task("dev")
        failing_backend.start_error = None
        self.assertEqual(failing.run_task("test")["status"], "succeeded")
        with self.assertRaisesRegex(TaskManagerError, "manager-issued"):
            successful.task_status("")

    def test_monitor_start_failure_rolls_back_record_snapshot_and_capacity(
        self,
    ) -> None:
        backend = FakeBackend(blocking=True)
        manager = TaskManager(
            self.settings,
            configuration(self.base),
            backend=backend,
        )
        with (
            patch(
                "workspace_guard_mcp.task_manager.WorkspaceGrowthMonitor.start",
                side_effect=RuntimeError("thread unavailable"),
            ),
            self.assertRaisesRegex(TaskManagerError, "thread unavailable"),
        ):
            manager.start_task("dev")

        self.assertTrue(backend.handles[0].stopped)
        self.assertTrue(backend.handles[0].closed)
        self.assertFalse(backend.requests[0].workspace_path.exists())
        self.assertEqual(manager._sessions, {})
        self.assertTrue(manager._capacity.acquire(blocking=False))
        manager._capacity.release()

    def test_ring_buffer_cursors_are_bounded_and_validated(self) -> None:
        logs = TaskLogBuffer(8)
        logs.append_stdout(b"12345")
        logs.append_stderr(b"abcdef")

        result = logs.read(0)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(
            len(str(result["stdout"]).encode()) + len(str(result["stderr"]).encode()),
            8,
        )
        self.assertEqual(logs.runtime_stdout_bytes, 5)
        self.assertEqual(logs.runtime_stderr_bytes, 6)
        logs.append_diagnostic_stderr(b"internal")
        self.assertEqual(logs.runtime_stderr_bytes, 6)
        with self.assertRaisesRegex(TaskManagerError, "non-negative"):
            logs.read(-1)
        with self.assertRaisesRegex(TaskManagerError, "non-negative"):
            logs.read(True)

        replacement = TaskLogBuffer(4)
        replacement.append_stdout(b"0123456789")
        self.assertEqual(replacement.read(0)["stdout"], "6789")

    def test_ring_buffer_rejects_non_positive_capacity(self) -> None:
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity):
                with self.assertRaisesRegex(
                    ValueError, "log capacity must be a positive integer"
                ):
                    TaskLogBuffer(capacity)  # type: ignore[arg-type]

    def test_task_manager_reconciles_generic_execution_store(self) -> None:
        store = InMemoryExecutionStore()
        store.create(
            ExecutionRecord(
                execution_id="old-running",
                kind=ExecutionKind.TASK,
                name="old",
                tool="run_task",
                mode=ExecutionMode.RUN,
                state=ExecutionState.STARTING,
                created_at=1.0,
                updated_at=1.0,
            )
        )
        store.transition(
            "old-running",
            {ExecutionState.STARTING},
            ExecutionState.RUNNING,
            updated_at=2.0,
        )

        TaskManager(
            self.settings,
            configuration(self.base),
            backend=FakeBackend(),
            execution_store=store,
        )

        reconciled = store.get("old-running")
        self.assertEqual(reconciled.state, ExecutionState.CRASHED)
        self.assertEqual(reconciled.reason, ExecutionReason.SERVER_RESTARTED)
        self.assertIsNotNone(reconciled.finished_at)

    def test_authorized_sync_executions_have_unique_ids_and_canonical_records(
        self,
    ) -> None:
        task_store = InMemoryExecutionStore()
        task_manager = TaskManager(
            self.settings,
            configuration(self.base),
            backend=FakeBackend(),
            execution_store=task_store,
        )
        task_result = task_manager.run_task("test")
        task_execution_id = task_result["execution_id"]
        assert isinstance(task_execution_id, str)
        self.assertEqual(
            task_store.get(task_execution_id).state,
            ExecutionState.SUCCEEDED,
        )
        task_status = task_manager.execution_status(task_execution_id)
        self.assertEqual(task_status["kind"], "task")
        self.assertEqual(task_status["mode"], "run")
        self.assertEqual(task_status["tool"], "run_task")
        self.assertEqual(task_status["state"], "succeeded")
        self.assertEqual(task_result["resources"], task_status["resources"])
        resources = task_status["resources"]
        assert isinstance(resources, dict)
        self.assertIsNone(resources["cpu_time_ms"])
        self.assertIsNone(resources["peak_memory_bytes"])
        self.assertEqual(resources["output_bytes"], 0)
        task_events = task_manager.execution_events(task_execution_id)["events"]
        assert isinstance(task_events, list)
        self.assertEqual(
            [event["event_type"] for event in task_events],
            ["created", "state_transition", "state_transition"],
        )

        tools = frozenset(
            {
                "python_version",
                "run_pytest",
                "run_python_script",
                "run_ruff",
                "run_mypy",
                "run_pytest_coverage",
                "run_command",
            }
        )
        profile_store = InMemoryExecutionStore()
        profile_manager = TaskManager(
            self.settings,
            profile_configuration(self.base, tools=tools),
            backend=FakeBackend(stdout=b"[]"),
            execution_store=profile_store,
        )
        results = [
            profile_manager.python_version("debug"),
            profile_manager.run_pytest("debug", targets=["tests"]),
            profile_manager.run_python_script("debug", "debug.py"),
            profile_manager.run_ruff("debug", paths=["."]),
            profile_manager.run_mypy("debug", paths=["."]),
            profile_manager.run_pytest_coverage("debug", targets=["tests"]),
            profile_manager.run_command("debug", "python", ["--version"]),
        ]
        execution_ids = {result["execution_id"] for result in results}
        self.assertEqual(len(execution_ids), len(results))
        for execution_id in execution_ids:
            assert isinstance(execution_id, str)
            self.assertEqual(
                profile_store.get(execution_id).state,
                ExecutionState.SUCCEEDED,
            )
            self.assertEqual(
                profile_manager.execution_status(execution_id)["kind"],
                "profile",
            )
            profile_events = profile_manager.execution_events(execution_id)["events"]
            assert isinstance(profile_events, list)
            self.assertEqual(len(profile_events), 3)

    def test_service_id_is_execution_id_and_stop_is_canonical_cancellation(
        self,
    ) -> None:
        store = InMemoryExecutionStore()
        manager = TaskManager(
            self.settings,
            configuration(self.base),
            backend=FakeBackend(stdout=b"ready", stderr=b"warn", blocking=True),
            execution_store=store,
        )
        started = manager.start_task("dev")
        self.assertEqual(started["task_id"], started["execution_id"])
        execution_id = started["execution_id"]
        assert isinstance(execution_id, str)
        self.assertEqual(store.get(execution_id).state, ExecutionState.RUNNING)
        self.assertIsNone(manager.task_status(execution_id)["resources"])
        self.assertIsNone(manager.execution_status(execution_id)["resources"])

        stopped = manager.stop_task(execution_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["execution_id"], execution_id)
        record = store.get(execution_id)
        self.assertEqual(record.state, ExecutionState.CANCELLED)
        self.assertEqual(record.reason, ExecutionReason.USER_CANCELLED)
        status = manager.execution_status(execution_id)
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(stopped["resources"], status["resources"])
        service_resources = status["resources"]
        assert isinstance(service_resources, dict)
        self.assertEqual(service_resources["stdout_bytes"], 5)
        self.assertEqual(service_resources["stderr_bytes"], 4)
        self.assertEqual(service_resources["output_bytes"], 9)
        events = manager.execution_events(execution_id)["events"]
        assert isinstance(events, list)
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "created",
                "state_transition",
                "cancellation_requested",
                "state_transition",
                "state_transition",
            ],
        )
        self.assertEqual(
            [(event["from_state"], event["to_state"]) for event in events],
            [
                (None, "starting"),
                ("starting", "running"),
                ("running", "running"),
                ("running", "cancelling"),
                ("cancelling", "cancelled"),
            ],
        )
        self.assertEqual(events[2]["reason"], "user_cancelled")
        self.assertEqual(events[-1]["reason"], "user_cancelled")
        repeated = manager.stop_task(execution_id)
        self.assertEqual(repeated["status"], "stopped")
        self.assertEqual(manager.execution_events(execution_id)["events"], events)

    def test_failed_and_crashed_execution_semantics_are_distinct(self) -> None:
        failed_store = InMemoryExecutionStore()
        failed_manager = TaskManager(
            self.settings,
            configuration(self.base),
            backend=FakeBackend(exit_code=2),
            execution_store=failed_store,
        )
        failed = failed_manager.run_task("test")
        failed_id = failed["execution_id"]
        assert isinstance(failed_id, str)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed_store.get(failed_id).state, ExecutionState.FAILED)

        crashed_store = InMemoryExecutionStore()
        crashed_manager = TaskManager(
            self.settings,
            configuration(self.base),
            backend=FakeBackend(start_error=OSError("runtime denied")),
            execution_store=crashed_store,
        )
        crashed = crashed_manager.run_task("test")
        crashed_id = crashed["execution_id"]
        assert isinstance(crashed_id, str)
        self.assertEqual(crashed["status"], "start_failed")
        crashed_record = crashed_store.get(crashed_id)
        self.assertEqual(crashed_record.state, ExecutionState.CRASHED)
        self.assertEqual(
            crashed_record.reason,
            ExecutionReason.RUNTIME_START_FAILED,
        )

    def test_crash_runtime_details_never_cross_durable_error_boundary(self) -> None:
        marker = "TOPSECRET-RUNTIME-STDERR"
        host_path = "/Users/operator/private/runtime.sock"
        db_path = self.base / "execution-audit.sqlite3"
        store = SqliteExecutionStore(db_path)
        manager = TaskManager(
            self.settings,
            configuration(self.base),
            backend=FakeBackend(start_error=OSError(f"{marker} failed at {host_path}")),
            execution_store=store,
        )

        result = manager.run_task("test")
        execution_id = result["execution_id"]
        stderr = result["stderr"]
        assert isinstance(execution_id, str)
        assert isinstance(stderr, str)
        self.assertIn(marker, stderr)
        self.assertIn(host_path, stderr)

        status = manager.execution_status(execution_id)
        self.assertEqual(status["reason"], "runtime_start_failed")
        self.assertEqual(status["error_summary"], "execution runtime failed to start")
        self.assertNotIn(marker, str(status))
        self.assertNotIn(host_path, str(status))

        events = manager.execution_events(execution_id)["events"]
        assert isinstance(events, list)
        last_event = events[-1]
        assert isinstance(last_event, dict)
        self.assertNotIn(marker, str(events))
        self.assertNotIn(host_path, str(events))
        self.assertEqual(
            last_event["error_summary"], "execution runtime failed to start"
        )

        reopened = SqliteExecutionStore(db_path)
        persisted = reopened.get(execution_id)
        self.assertEqual(persisted.error_summary, "execution runtime failed to start")
        self.assertNotIn(marker, str(persisted))
        self.assertNotIn(host_path, str(persisted))
        self.assertEqual(persisted.resources.stderr_bytes, 0)  # type: ignore[union-attr]

    def test_server_conditionally_registers_narrow_task_schemas(self) -> None:
        default_server = create_server(self.settings)
        manager = TaskManager(
            self.settings, configuration(self.base), backend=FakeBackend()
        )
        task_server = create_server(self.settings, task_manager=manager)
        default_tools = asyncio.run(default_server.list_tools())
        task_tools = asyncio.run(task_server.list_tools())

        default_names = {tool.name for tool in default_tools}
        by_name = {tool.name: tool for tool in task_tools}
        task_names = {
            "list_tasks",
            "run_task",
            "start_task",
            "task_status",
            "task_logs",
            "stop_task",
            "execution_status",
            "execution_events",
        }
        self.assertTrue(default_names.isdisjoint(task_names))
        self.assertTrue(task_names.issubset(by_name))
        self.assertEqual(set(by_name["run_task"].input_schema["properties"]), {"name"})
        self.assertEqual(
            set(by_name["start_task"].input_schema["properties"]), {"name"}
        )
        self.assertEqual(
            set(by_name["stop_task"].input_schema["properties"]), {"task_id"}
        )
        self.assertEqual(
            set(by_name["execution_status"].input_schema["properties"]),
            {"execution_id"},
        )
        self.assertEqual(
            set(by_name["execution_events"].input_schema["properties"]),
            {"execution_id", "cursor", "limit"},
        )
        self.assertFalse(
            by_name["execution_status"].input_schema["additionalProperties"]
        )
        self.assertFalse(
            by_name["execution_events"].input_schema["additionalProperties"]
        )

        async def exercise_task_tools() -> None:
            listed = require_call_tool_result(
                await task_server.call_tool("list_tasks", {})
            )
            run = require_call_tool_result(
                await task_server.call_tool("run_task", {"name": "test"})
            )
            self.assertFalse(listed.is_error)
            self.assertFalse(run.is_error)
            with self.assertRaisesRegex(ValueError, "unexpected argument"):
                await task_server.call_tool(
                    "run_task", {"name": "test", "argv": ["sh"]}
                )

        asyncio.run(exercise_task_tools())

    def test_server_registers_profiles_only_when_explicitly_configured(self) -> None:
        default_server = create_server(self.settings)
        manager = TaskManager(
            self.settings,
            profile_configuration(self.base),
            backend=FakeBackend(),
        )
        profile_server = create_server(self.settings, task_manager=manager)
        default_names = {tool.name for tool in asyncio.run(default_server.list_tools())}
        by_name = {tool.name: tool for tool in asyncio.run(profile_server.list_tools())}
        dynamic = {
            "list_execution_profiles",
            "python_version",
            "run_pytest",
            "run_python_script",
            "execution_status",
            "execution_events",
        }
        self.assertTrue(default_names.isdisjoint(dynamic))
        self.assertTrue(dynamic.issubset(by_name))
        self.assertNotIn("run_task", by_name)
        self.assertFalse(by_name["run_pytest"].input_schema["additionalProperties"])
        python_version_annotations = by_name["python_version"].annotations
        assert python_version_annotations is not None
        self.assertTrue(python_version_annotations.read_only_hint)

        async def exercise() -> None:
            result = require_call_tool_result(
                await profile_server.call_tool(
                    "run_pytest",
                    {
                        "profile": "debug",
                        "targets": ["tests/test_user.py::test_login"],
                        "verbosity": 2,
                    },
                )
            )
            self.assertFalse(result.is_error)
            with self.assertRaisesRegex(ValueError, "unexpected argument"):
                await profile_server.call_tool(
                    "run_pytest",
                    {"profile": "debug", "options": ["--collect-only"]},
                )

        asyncio.run(exercise())

    def test_server_registers_generic_commands_only_for_authorized_profiles(
        self,
    ) -> None:
        backend = FakeBackend()
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_command", "start_command"}),
            ),
            backend=backend,
        )
        command_server = create_server(self.settings, task_manager=manager)
        by_name = {tool.name: tool for tool in asyncio.run(command_server.list_tools())}
        command_tools = {
            "run_command",
            "start_command",
            "task_status",
            "task_logs",
            "stop_task",
            "execution_status",
            "execution_events",
        }
        self.assertTrue(command_tools.issubset(by_name))
        self.assertNotIn("run_task", by_name)
        for name in ("run_command", "start_command"):
            self.assertEqual(
                set(by_name[name].input_schema["properties"]),
                {"profile", "program", "args", "cwd"},
            )
            self.assertFalse(by_name[name].input_schema["additionalProperties"])

        async def exercise() -> None:
            run = require_call_tool_result(
                await command_server.call_tool(
                    "run_command",
                    {
                        "profile": "debug",
                        "program": "ruff",
                        "args": ["check", "."],
                    },
                )
            )
            started = require_call_tool_result(
                await command_server.call_tool(
                    "start_command",
                    {
                        "profile": "debug",
                        "program": "uvicorn",
                        "args": ["app:app"],
                    },
                )
            )
            self.assertFalse(run.is_error)
            self.assertFalse(started.is_error)
            with self.assertRaisesRegex(ValueError, "unexpected argument"):
                await command_server.call_tool(
                    "run_command",
                    {
                        "profile": "debug",
                        "program": "python",
                        "env": {"TOKEN": "secret"},
                    },
                )

        asyncio.run(exercise())
        self.assertEqual(len(backend.requests), 2)

    def test_server_registers_structured_analysis_tools_with_optional_profiles(
        self,
    ) -> None:
        backend = FakeBackend(stdout=b"[]")
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset(
                    {
                        "python_version",
                        "run_pytest",
                        "run_python_script",
                        "run_ruff",
                        "run_mypy",
                        "run_pytest_coverage",
                    }
                ),
            ),
            backend=backend,
        )
        server = create_server(self.settings, task_manager=manager)
        by_name = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        for name in (
            "run_ruff",
            "run_mypy",
            "run_pytest_coverage",
        ):
            self.assertIn(name, by_name)
            self.assertNotIn("profile", by_name[name].input_schema.get("required", []))
            self.assertFalse(by_name[name].input_schema["additionalProperties"])
        self.assertIn("show_locals", by_name["run_pytest"].input_schema["properties"])
        self.assertIn("max_failures", by_name["run_pytest"].input_schema["properties"])

        async def exercise() -> None:
            ruff = require_call_tool_result(await server.call_tool("run_ruff", {}))
            mypy = require_call_tool_result(await server.call_tool("run_mypy", {}))
            self.assertFalse(ruff.is_error)
            self.assertFalse(mypy.is_error)
            with self.assertRaisesRegex(ValueError, "unexpected argument"):
                await server.call_tool("run_ruff", {"argv": ["--fix"]})

        asyncio.run(exercise())

    def test_server_structured_analysis_failures_remain_safe_and_schema_stable(
        self,
    ) -> None:
        failure_payload = {
            "failures": [
                {
                    "node_id": "tests/test_user.py::test_failure",
                    "exception": {"type": "ValueError", "message": "bad value"},
                    "frames": [
                        {
                            "path": "/Users/host/private/test_user.py",
                            "line": 4,
                            "function": "test_failure",
                            "source": "assert token",
                            "locals": [
                                {
                                    "name": "api_token",
                                    "type": "str",
                                    "repr": "raw-secret",
                                    "redacted": True,
                                    "truncated": False,
                                }
                            ],
                        }
                    ],
                }
            ],
            "failures_truncated": False,
            "frames_truncated": False,
            "locals_truncated": False,
        }
        backend = FakeBackend(
            stdout=(
                "trace\nSWMCP_FAILURES:" + json.dumps(failure_payload) + "\n"
            ).encode(),
            exit_code=1,
        )
        manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_pytest"}),
            ),
            backend=backend,
        )
        server = create_server(self.settings, task_manager=manager)

        async def exercise_pytest() -> None:
            result = require_call_tool_result(await server.call_tool("run_pytest", {}))
            self.assertFalse(result.is_error)
            structured = require_structured_content(result)
            self.assertEqual(structured["status"], "failed")
            frame = structured["failures"][0]["frames"][0]
            self.assertEqual(frame["path"], "<external>")
            self.assertEqual(frame["locals"][0]["repr"], "<redacted>")
            self.assertNotIn("raw-secret", repr(structured))
            self.assertNotIn("/Users/host", repr(structured))

        asyncio.run(exercise_pytest())

        malformed_backend = FakeBackend(stdout=b"not json", exit_code=1)
        malformed_manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_ruff"}),
            ),
            backend=malformed_backend,
        )
        malformed_server = create_server(self.settings, task_manager=malformed_manager)

        async def exercise_ruff() -> None:
            result = require_call_tool_result(
                await malformed_server.call_tool("run_ruff", {})
            )
            self.assertFalse(result.is_error)
            structured = require_structured_content(result)
            self.assertEqual(structured["diagnostics"], [])
            self.assertIn("diagnostics_parser_error", structured)

        asyncio.run(exercise_ruff())

        unavailable_backend = FakeBackend(exit_code=127)
        unavailable_manager = TaskManager(
            self.settings,
            profile_configuration(
                self.base,
                tools=frozenset({"run_python_script"}),
            ),
            backend=unavailable_backend,
        )
        unavailable_server = create_server(
            self.settings, task_manager=unavailable_manager
        )

        async def exercise_unavailable() -> None:
            result = require_call_tool_result(
                await unavailable_server.call_tool(
                    "run_python_script", {"path": "tests/test_user.py"}
                )
            )
            self.assertFalse(result.is_error)
            structured = require_structured_content(result)
            self.assertEqual(structured["status"], "capability_unavailable")
            self.assertIn("capability_error", structured)

        asyncio.run(exercise_unavailable())


if __name__ == "__main__":
    unittest.main()
