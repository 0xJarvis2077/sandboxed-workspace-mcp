from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_guard_mcp.container_backend import (
    CliContainerBackend,
    TaskExecutionError,
    build_container_argv,
)
from workspace_guard_mcp.execution_backend import ExecutionRequest
from workspace_guard_mcp.task_config import TaskDefinition, TaskLimits

PINNED_IMAGE = "example.invalid/workspace-guard-mcp@sha256:" + "b" * 64


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


class TimeoutProcess(FakeProcess):
    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(["docker"], 0.0 if timeout is None else timeout)


class ContainerBackendTests(unittest.TestCase):
    def test_runtime_validation_rejects_non_container_runtime(self) -> None:
        with self.assertRaisesRegex(TaskExecutionError, "docker or podman"):
            CliContainerBackend("other")

    def test_container_argv_has_every_required_isolation_and_no_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            task = TaskDefinition(
                "test", "run", PINNED_IMAGE, ("python", "-m", "unittest")
            )
            request = ExecutionRequest(
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
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
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
            request = ExecutionRequest(
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
            unsafe = ExecutionRequest(
                "workspace-guard-mcp-command",
                Path(directory),
                task,
                TaskLimits(),
                workdir="/tmp",
            )
            with self.assertRaisesRegex(TaskExecutionError, "inside /workspace"):
                build_container_argv("/usr/bin/docker", unsafe)

    def test_writable_workspace_and_artifact_mount_compile_expected_policy(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as artifact_dir,
        ):
            request = ExecutionRequest(
                "workspace-guard-mcp-build",
                Path(workspace_dir),
                TaskDefinition(
                    "build",
                    "run",
                    PINNED_IMAGE,
                    ("python",),
                    workspace_access="writable",
                ),
                TaskLimits(
                    max_workspace_file_bytes=5,
                    max_workspace_growth_bytes=5,
                    allow_best_effort_disk_limit=True,
                ),
                artifact_path=Path(artifact_dir),
            )
            argv = build_container_argv("/usr/bin/docker", request)

        rendered = " ".join(argv)
        workspace_mount = next(
            argv[index + 1]
            for index, item in enumerate(argv)
            if item == "--mount" and "destination=/workspace" in argv[index + 1]
        )
        self.assertNotIn("readonly", workspace_mount)
        self.assertIn("--ulimit", argv)
        self.assertIn("fsize=5:5", argv)
        self.assertIn("destination=/artifacts", rendered)
        self.assertIn("WORKSPACEGUARD_ARTIFACT_DIR=/artifacts", argv)

    def test_missing_runtime_fails_without_host_fallback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "workspace_guard_mcp.container_backend.shutil.which",
                return_value=None,
            ),
        ):
            backend = CliContainerBackend("docker")
            request = ExecutionRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            with self.assertRaisesRegex(TaskExecutionError, "fallback is disabled"):
                backend.start(request, lambda data: None, lambda data: None)

    def test_cli_backend_uses_sanitized_environment_and_tracked_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ExecutionRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            process = FakeProcess(running=True)
            with (
                patch(
                    "workspace_guard_mcp.container_backend.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "workspace_guard_mcp.container_backend.subprocess.Popen",
                    return_value=process,
                ) as popen,
                patch(
                    "workspace_guard_mcp.container_backend.subprocess.run",
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
        self.assertTrue(call.kwargs["close_fds"])
        self.assertTrue(call.kwargs["start_new_session"])
        self.assertEqual(stdout, [b"container stdout"])
        self.assertEqual(stderr, [b"container stderr"])
        self.assertTrue(process.terminated)
        self.assertEqual(stop.call_args.args[0][-1], "workspace-guard-mcp-test-token")

    def test_tracked_stop_falls_back_from_stop_to_kill_then_local_terminate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ExecutionRequest(
                "workspace-guard-mcp-stop-fallback",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            process = FakeProcess(running=True)
            stop_failed: subprocess.CompletedProcess[bytes]
            stop_failed = subprocess.CompletedProcess([], 1)
            kill_succeeded: subprocess.CompletedProcess[bytes]
            kill_succeeded = subprocess.CompletedProcess([], 0)
            with (
                patch(
                    "workspace_guard_mcp.container_backend.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "workspace_guard_mcp.container_backend.subprocess.Popen",
                    return_value=process,
                ),
                patch(
                    "workspace_guard_mcp.container_backend.subprocess.run",
                    side_effect=[stop_failed, kill_succeeded],
                ) as runtime_command,
            ):
                handle = CliContainerBackend("docker").start(
                    request, lambda data: None, lambda data: None
                )
                handle.stop()
                handle.close()

        self.assertEqual(runtime_command.call_count, 2)
        self.assertEqual(runtime_command.call_args_list[0].args[0][1], "stop")
        self.assertEqual(runtime_command.call_args_list[1].args[0][1], "kill")
        self.assertTrue(process.terminated)

    def test_cli_handle_translates_subprocess_wait_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ExecutionRequest(
                "workspace-guard-mcp-timeout",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            process = TimeoutProcess(running=True)
            with (
                patch(
                    "workspace_guard_mcp.container_backend.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "workspace_guard_mcp.container_backend.subprocess.Popen",
                    return_value=process,
                ),
            ):
                handle = CliContainerBackend("docker").start(
                    request, lambda data: None, lambda data: None
                )
                with self.assertRaises(TimeoutError):
                    handle.wait(timeout=0.1)
                handle.close()

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
                request = ExecutionRequest(
                    "workspace-guard-mcp-live-logs",
                    Path(directory),
                    TaskDefinition("dev", "service", PINNED_IMAGE, ("python",)),
                    TaskLimits(),
                )
                with (
                    patch(
                        "workspace_guard_mcp.container_backend.shutil.which",
                        return_value="/usr/bin/docker",
                    ),
                    patch(
                        "workspace_guard_mcp.container_backend.subprocess.Popen",
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
            request = ExecutionRequest(
                "workspace-guard-mcp-test-token",
                Path(directory),
                TaskDefinition("test", "run", PINNED_IMAGE, ("python",)),
                TaskLimits(),
            )
            with (
                patch(
                    "workspace_guard_mcp.container_backend.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "workspace_guard_mcp.container_backend.subprocess.Popen",
                    side_effect=OSError("denied"),
                ),
                self.assertRaisesRegex(TaskExecutionError, "failed to start"),
            ):
                CliContainerBackend("docker").start(
                    request, lambda data: None, lambda data: None
                )


if __name__ == "__main__":
    unittest.main()
