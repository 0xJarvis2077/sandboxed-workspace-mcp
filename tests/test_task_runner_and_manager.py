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
from unittest.mock import patch

from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from sandboxed_workspace_mcp.task_manager import (
    TaskLogBuffer,
    TaskManager,
    TaskManagerError,
)
from sandboxed_workspace_mcp.task_runner import (
    CliContainerBackend,
    ContainerRequest,
    TaskExecutionError,
    WorkspaceGrowthMonitor,
    _next_workspace_scan_delay,
    _WorkspaceUsage,
    build_container_argv,
    run_container_task,
)

PINNED_IMAGE = "example.invalid/sandboxed-workspace-mcp@sha256:" + "b" * 64


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
) -> TaskConfiguration:
    profiles = {
        "debug": ExecutionProfile(
            "debug",
            PINNED_IMAGE,
            tools,
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
    def test_container_argv_has_every_required_isolation_and_no_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            task = TaskDefinition(
                "test", "run", PINNED_IMAGE, ("python", "-m", "unittest")
            )
            request = ContainerRequest(
                "sandboxed-workspace-mcp-test-token", snapshot, task, TaskLimits()
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
                "sandboxed-workspace-mcp-command",
                snapshot,
                task,
                TaskLimits(),
                container_workdir="/workspace/src",
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
                "sandboxed-workspace-mcp-command",
                Path(directory),
                task,
                TaskLimits(),
                container_workdir="/tmp",
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
                "sandboxed-workspace-mcp-growth", snapshot, task, limits
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
                "sandboxed-workspace-mcp-growth-cancel",
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
                "sandboxed_workspace_mcp.task_runner.os.scandir",
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
                "sandboxed-workspace-mcp-growth-oserror",
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
                "sandboxed-workspace-mcp-read-only",
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
            patch(
                "sandboxed_workspace_mcp.task_runner.shutil.which", return_value=None
            ),
        ):
            backend = CliContainerBackend("docker")
            request = ContainerRequest(
                "sandboxed-workspace-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            with self.assertRaisesRegex(TaskExecutionError, "fallback is disabled"):
                backend.start(request, lambda data: None, lambda data: None)

    def test_cli_backend_uses_sanitized_environment_and_tracked_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ContainerRequest(
                "sandboxed-workspace-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            process = FakeProcess(running=True)
            with (
                patch(
                    "sandboxed_workspace_mcp.task_runner.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "sandboxed_workspace_mcp.task_runner.subprocess.Popen",
                    return_value=process,
                ) as popen,
                patch(
                    "sandboxed_workspace_mcp.task_runner.subprocess.run",
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
        self.assertEqual(
            stop.call_args.args[0][-1], "sandboxed-workspace-mcp-test-token"
        )

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
                    "sandboxed-workspace-mcp-live-logs",
                    Path(directory),
                    TaskDefinition("dev", "service", PINNED_IMAGE, ("python",)),
                    TaskLimits(),
                )
                with (
                    patch(
                        "sandboxed_workspace_mcp.task_runner.shutil.which",
                        return_value="/usr/bin/docker",
                    ),
                    patch(
                        "sandboxed_workspace_mcp.task_runner.subprocess.Popen",
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
                "sandboxed-workspace-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            with (
                patch(
                    "sandboxed_workspace_mcp.task_runner.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "sandboxed_workspace_mcp.task_runner.subprocess.Popen",
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
                "sandboxed-workspace-mcp-test-token",
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

    def test_timeout_and_output_overflow_stop_the_tracked_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = TaskDefinition("test", "run", PINNED_IMAGE, ("python",))
            timeout_backend = FakeBackend(blocking=True)
            timeout = run_container_task(
                timeout_backend,
                ContainerRequest(
                    "sandboxed-workspace-mcp-timeout",
                    Path(directory),
                    task,
                    TaskLimits(timeout_seconds=0.1, max_output_bytes=1024),
                ),
            )
            overflow_backend = FakeBackend(stdout=b"x" * 1025, blocking=True)
            overflow = run_container_task(
                overflow_backend,
                ContainerRequest(
                    "sandboxed-workspace-mcp-overflow",
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
                    "sandboxed-workspace-mcp-cancelled",
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
        self.assertNotEqual(request.snapshot_path, self.root)
        self.assertFalse(request.snapshot_path.exists())
        self.assertEqual(
            (self.root / "test_module.py").read_text(encoding="utf-8"),
            "value = 1\n",
        )

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
        by_name = {profile["name"]: profile for profile in listed["profiles"]}  # type: ignore[index]
        self.assertTrue(by_name["coding"]["default"])  # type: ignore[index]
        self.assertFalse(by_name["safe"]["default"])  # type: ignore[index]

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
                "sandboxed_workspace_mcp_debug_plugin",
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
                "sandboxed_workspace_mcp_debug_plugin",
                "-o",
                "cache_dir=/tmp/cache/pytest",
                "-q",
                "--tb=long",
                "--",
                "tests",
            ),
        )
        self.assertEqual(backend.requests[3].task.argv, ("python", "--", "debug.py"))
        self.assertTrue(
            all(not request.snapshot_path.exists() for request in backend.requests)
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
        self.assertEqual(request.container_workdir, "/workspace/src")
        self.assertFalse(request.snapshot_path.exists())

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

        self.assertNotEqual(task_id, request.container_name)
        self.assertEqual(request.task.mode, "service")
        self.assertEqual(
            request.task.argv,
            ("uvicorn", "app:app", "--log-level", "debug"),
        )
        self.assertEqual(request.container_workdir, "/workspace")
        self.assertNotIn("uvicorn", repr(manager.task_status(task_id)))
        self.assertEqual(manager.task_logs(task_id)["stdout"], "ready\n")
        self.assertEqual(manager.task_logs(task_id)["stderr"], "warning\n")
        with self.assertRaisesRegex(TaskManagerError, "concurrent"):
            manager.run_command("debug", "ruff", ["check", "."])

        stopped = manager.stop_task(task_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(backend.handles[0].stopped)
        self.assertFalse(request.snapshot_path.exists())

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
        self.assertFalse(shutdown_backend.requests[0].snapshot_path.exists())

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
        link = self.root / "linked.py"
        try:
            link.symlink_to(self.root / "debug.py")
        except (OSError, NotImplementedError):
            link = None
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
        self.assertFalse(backend.requests[0].snapshot_path.exists())

        failed_backend = FakeBackend(exit_code=2)
        failed_manager = TaskManager(
            self.settings,
            profile_configuration(self.base),
            backend=failed_backend,
        )
        failed = failed_manager.python_version("debug")
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed_backend.requests[0].snapshot_path.exists())

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
        self.assertFalse(cancelled_backend.requests[0].snapshot_path.exists())

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
        self.assertFalse(shutdown_backend.requests[0].snapshot_path.exists())

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
        snapshot_path = backend.requests[0].snapshot_path

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
        self.assertFalse(snapshot_path.exists())

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
                "sandboxed_workspace_mcp.task_manager.WorkspaceGrowthMonitor.start",
                side_effect=RuntimeError("thread unavailable"),
            ),
            self.assertRaisesRegex(TaskManagerError, "thread unavailable"),
        ):
            manager.start_task("dev")

        self.assertTrue(backend.handles[0].stopped)
        self.assertTrue(backend.handles[0].closed)
        self.assertFalse(backend.requests[0].snapshot_path.exists())
        self.assertEqual(manager._records, {})
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

        async def exercise_task_tools() -> None:
            listed = await task_server.call_tool("list_tasks", {})
            run = await task_server.call_tool("run_task", {"name": "test"})
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
        }
        self.assertTrue(default_names.isdisjoint(dynamic))
        self.assertTrue(dynamic.issubset(by_name))
        self.assertNotIn("run_task", by_name)
        self.assertFalse(by_name["run_pytest"].input_schema["additionalProperties"])
        self.assertTrue(by_name["python_version"].annotations.read_only_hint)

        async def exercise() -> None:
            result = await profile_server.call_tool(
                "run_pytest",
                {
                    "profile": "debug",
                    "targets": ["tests/test_user.py::test_login"],
                    "verbosity": 2,
                },
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
            run = await command_server.call_tool(
                "run_command",
                {
                    "profile": "debug",
                    "program": "ruff",
                    "args": ["check", "."],
                },
            )
            started = await command_server.call_tool(
                "start_command",
                {
                    "profile": "debug",
                    "program": "uvicorn",
                    "args": ["app:app"],
                },
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
            ruff = await server.call_tool("run_ruff", {})
            mypy = await server.call_tool("run_mypy", {})
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
            result = await server.call_tool("run_pytest", {})
            self.assertFalse(result.is_error)
            structured = result.structured_content
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
            result = await malformed_server.call_tool("run_ruff", {})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["diagnostics"], [])
            self.assertIn("diagnostics_parser_error", result.structured_content)

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
            result = await unavailable_server.call_tool(
                "run_python_script", {"path": "tests/test_user.py"}
            )
            self.assertFalse(result.is_error)
            self.assertEqual(
                result.structured_content["status"], "capability_unavailable"
            )
            self.assertIn("capability_error", result.structured_content)

        asyncio.run(exercise_unavailable())


if __name__ == "__main__":
    unittest.main()
