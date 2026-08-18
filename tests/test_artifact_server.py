from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from _mcp_assertions import require_resource_contents
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.server import create_server
from workspace_guard_mcp.task_config import TaskConfiguration, TaskLimits
from workspace_guard_mcp.task_manager import TaskManager


class ArtifactServerTests(unittest.TestCase):
    def test_binary_artifact_resource_round_trips_as_opaque_blob(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as staging_dir,
        ):
            workspace = Path(root)
            settings = Settings.create(workspace, allow_writes=False)
            configuration = TaskConfiguration(
                source=workspace / "tasks.json",
                runtime="docker",
                limits=TaskLimits(),
                tasks=MappingProxyType({}),
            )
            manager = TaskManager(settings, configuration)
            payload = b"\x00\xff\x10artifact"
            staging = Path(staging_dir)
            (staging / "image.bin").write_bytes(payload)
            record = manager.artifact_store.collect(
                "execution-1", staging, configuration.limits
            )[0]
            server = create_server(settings, manager)

            async def exercise() -> None:
                contents = require_resource_contents(
                    await server.read_resource(
                        f"workspaceguard://artifact/{record.artifact_id}"
                    )
                )
                self.assertEqual(len(contents), 1)
                blob = contents[0]
                self.assertEqual(blob.mime_type, "application/octet-stream")
                self.assertEqual(blob.content, payload)

            asyncio.run(exercise())

    def test_oversized_artifact_resource_is_rejected_boundedly(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as staging_dir,
        ):
            workspace = Path(root)
            settings = Settings.create(workspace, allow_writes=False)
            configuration = TaskConfiguration(
                source=workspace / "tasks.json",
                runtime="docker",
                limits=TaskLimits(),
                tasks=MappingProxyType({}),
            )
            manager = TaskManager(settings, configuration)
            staging = Path(staging_dir)
            (staging / "large.bin").write_bytes(b"12345")
            record = manager.artifact_store.collect(
                "execution-1", staging, configuration.limits
            )[0]
            server = create_server(settings, manager)

            async def exercise() -> None:
                with patch(
                    "workspace_guard_mcp.artifact_store.MAX_ARTIFACT_RESOURCE_BYTES",
                    4,
                ):
                    with self.assertRaisesRegex(
                        ResourceError,
                        "artifact too large for direct resource delivery",
                    ):
                        await server.read_resource(
                            f"workspaceguard://artifact/{record.artifact_id}"
                        )

            asyncio.run(exercise())

    def test_invalid_or_missing_artifact_resource_is_generic_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            settings = Settings.create(workspace, allow_writes=False)
            configuration = TaskConfiguration(
                source=workspace / "tasks.json",
                runtime="docker",
                limits=TaskLimits(),
                tasks=MappingProxyType({}),
            )
            manager = TaskManager(settings, configuration)
            server = create_server(settings, manager)

            async def exercise() -> None:
                for uri in (
                    "workspaceguard://artifact/not-valid",
                    "workspaceguard://artifact/" + "A" * 32,
                ):
                    with self.subTest(uri=uri):
                        with self.assertRaisesRegex(
                            ResourceNotFoundError, "Unknown resource"
                        ):
                            await server.read_resource(uri)

            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
