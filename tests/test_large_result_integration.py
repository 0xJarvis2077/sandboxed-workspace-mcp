from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from _mcp_assertions import (
    require_call_tool_result,
    require_resource_contents,
    require_structured_content,
)

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.server import create_server
from workspace_guard_mcp.task_config import (
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager

PINNED_IMAGE = "example.invalid/project@sha256:" + "d" * 64


class _ImmediateHandle:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _LargeOutputBackend:
    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.stderr = stderr

    def start(self, request, on_stdout, on_stderr):
        on_stdout(self.stdout)
        on_stderr(self.stderr)
        return _ImmediateHandle()


class LargeResultIntegrationTests(unittest.TestCase):
    def test_large_workspace_diff_externalizes_bounded_safe_diff(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            path = workspace / "large.txt"
            path.write_text("before\n" * 5000, encoding="utf-8")
            server = create_server(
                Settings.create(root, allow_writes=True, allow_git_writes=True)
            )

            async def exercise() -> None:
                await server.call_tool("git_init", {})
                await server.call_tool("git_create_baseline", {})
                path.write_text("after\n" * 5000, encoding="utf-8")

                result = require_call_tool_result(
                    await server.call_tool("workspace_diff", {})
                )
                structured = require_structured_content(result)
                self.assertTrue(structured["text_inline_truncated"])
                self.assertFalse(structured["source_truncated"])
                uri = structured["text_resource_uri"]
                self.assertIsInstance(uri, str)

                contents = require_resource_contents(await server.read_resource(uri))
                self.assertTrue(contents)
                full_diff = contents[0].content
                assert isinstance(full_diff, str)
                self.assertIn("-before", full_diff)
                self.assertIn("+after", full_diff)
                self.assertGreater(
                    len(full_diff.encode("utf-8")),
                    len(structured["text"].encode("utf-8")),
                )

            asyncio.run(exercise())

    def test_large_task_stdout_externalizes_without_changing_status(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            workspace = base / "workspace"
            workspace.mkdir()
            settings = Settings.create(workspace)
            limits = TaskLimits(timeout_seconds=2, max_output_bytes=100_000)
            task = TaskDefinition("large", "run", PINNED_IMAGE, ("python", "-V"))
            configuration = TaskConfiguration(
                source=base / "trusted-tasks.json",
                runtime="docker",
                limits=limits,
                tasks=MappingProxyType({"large": task}),
            )
            stdout = ("safe stdout line\n" * 2500).encode()
            manager = TaskManager(
                settings,
                configuration,
                backend=_LargeOutputBackend(stdout),  # type: ignore[arg-type]
            )
            server = create_server(settings, task_manager=manager)

            async def exercise() -> None:
                result = require_call_tool_result(
                    await server.call_tool("run_task", {"name": "large"})
                )
                structured = require_structured_content(result)
                self.assertEqual(structured["status"], "succeeded")
                self.assertEqual(structured["exit_code"], 0)
                self.assertFalse(structured["source_truncated"])
                self.assertTrue(structured["stdout_inline_truncated"])
                self.assertFalse(structured["stderr_inline_truncated"])
                uri = structured["stdout_resource_uri"]
                self.assertIsInstance(uri, str)

                contents = require_resource_contents(await server.read_resource(uri))
                self.assertTrue(contents)
                self.assertEqual(contents[0].content, stdout.decode())

            try:
                asyncio.run(exercise())
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
