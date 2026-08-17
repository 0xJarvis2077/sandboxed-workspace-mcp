from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from sandboxed_workspace_mcp import resources
from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskLimits,
)
from sandboxed_workspace_mcp.task_manager import TaskManager
from sandboxed_workspace_mcp.tool_contracts import TOOL_CONTRACTS


class _UnusedBackend:
    """Placeholder backend for discovery-only task-manager tests."""


class SelfDescriptionResourceTests(unittest.TestCase):
    @staticmethod
    async def _read_text(server, uri: str) -> str:
        contents = await server.read_resource(uri)
        self_content = contents[0].content
        if not isinstance(self_content, str):
            raise AssertionError(f"expected text resource content for {uri}")
        return self_content

    @staticmethod
    def _profile_manager(
        settings: Settings,
        base: Path,
        *,
        tools: frozenset[str],
        source: Path | None = None,
        image: str = "example.invalid/project@sha256:" + "d" * 64,
    ) -> TaskManager:
        profile = ExecutionProfile(
            "debug",
            image,
            tools,
            allow_arbitrary_commands=bool(
                {"run_command", "start_command"}.intersection(tools)
            ),
        )
        configuration = TaskConfiguration(
            source=source or base / "profiles.json",
            runtime="docker",
            limits=TaskLimits(),
            tasks=MappingProxyType({}),
            profiles=MappingProxyType({"debug": profile}),
        )
        return TaskManager(
            settings,
            configuration,
            backend=_UnusedBackend(),  # type: ignore[arg-type]
        )

    def test_schema_summary_is_bounded_and_deterministic(self) -> None:
        schema = {
            "type": "object",
            "required": ["zeta", "alpha"],
            "properties": {"zeta": {"type": "str"}, "alpha": {"type": "str"}},
            "$defs": {
                "nested": {
                    "type": "object",
                    "properties": {"gamma": {"type": "integer"}},
                }
            },
        }

        summary = resources.summarize_schema(schema)

        self.assertEqual(summary["type"], "object")
        self.assertEqual(summary["required"], ["alpha", "zeta"])
        self.assertEqual(summary["properties"], ["alpha", "gamma", "zeta"])
        self.assertNotIn("$defs", summary)

    def test_instructions_and_workflows_cover_safe_operating_semantics(self) -> None:
        tool_names = {
            "read_file_versioned",
            "replace_text",
            "write_file",
            "trash_file",
            "list_trashed_files",
            "restore_trashed_file",
            "restore_trashed_file_to",
            "run_pytest",
            "run_ruff",
            "run_mypy",
            "run_pytest_coverage",
            "run_command",
            "start_command",
            "git_status",
            "git_diff",
            "workspace_diff",
        }

        instructions = resources.build_instructions(tool_names)
        self.assertIn("read_file_versioned", instructions)
        self.assertIn("expected_sha256", instructions)
        self.assertIn("trash_file", instructions)
        self.assertIn("run_pytest", instructions)
        self.assertIn("workspace_diff", instructions)
        self.assertLess(
            instructions.index("run_pytest"), instructions.index("run_command")
        )

        edit = resources.get_workflow("edit-file", tool_names)
        debug = resources.get_workflow("debug-python", tool_names)
        recover = resources.get_workflow("recover-file", tool_names)
        review = resources.get_workflow("review-changes", tool_names)
        self.assertIn("replace_text", edit)
        self.assertIn("expected_sha256", edit)
        self.assertIn("structured", debug)
        self.assertIn("list_trashed_files", recover)
        self.assertIn("restore_trashed_file_to", recover)
        self.assertIn("workspace_diff", review)
        self.assertNotIn("interactive debugger", debug.lower())

    def test_resource_list_and_template_are_discoverable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root, allow_writes=False))

            async def exercise() -> None:
                listed = await server.list_resources()
                templates = await server.list_resource_templates()
                uris = {str(resource.uri) for resource in listed}
                self.assertEqual(
                    uris,
                    {
                        "internal://instructions",
                        "internal://tool-catalog",
                        "internal://workflows/edit-file",
                        "internal://workflows/debug-python",
                        "internal://workflows/recover-file",
                        "internal://workflows/review-changes",
                    },
                )
                self.assertEqual(
                    {template.uri_template for template in templates},
                    {"internal://tool-info/{name}"},
                )

            asyncio.run(exercise())

    def test_catalog_exactly_matches_current_list_tools_and_is_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(
                Settings.create(
                    root,
                    allow_writes=True,
                    allow_trash=True,
                    allow_git_writes=True,
                )
            )

            async def exercise() -> None:
                tools = await server.list_tools()
                content = await self._read_text(server, "internal://tool-catalog")
                catalog = json.loads(content)
                catalog_names = [entry["name"] for entry in catalog["tools"]]
                tool_names = {tool.name for tool in tools}
                self.assertEqual(set(catalog_names), tool_names)
                self.assertEqual(catalog_names, sorted(catalog_names))
                self.assertNotIn("$defs", content)

            asyncio.run(exercise())

    def test_tool_info_reuses_registered_tool_and_registry_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            root = base_path / "workspace"
            root.mkdir()
            settings = Settings.create(root, allow_trash=True)
            manager = self._profile_manager(
                settings,
                base_path,
                tools=frozenset({"run_pytest", "run_command"}),
            )
            server = create_server(settings, task_manager=manager)

            async def exercise() -> None:
                by_name = {tool.name: tool for tool in await server.list_tools()}
                for name in (
                    "read_file_versioned",
                    "run_pytest",
                    "run_command",
                    "trash_file",
                ):
                    with self.subTest(tool=name):
                        content = await self._read_text(
                            server, f"internal://tool-info/{name}"
                        )
                        info = json.loads(content)
                        contract = TOOL_CONTRACTS[name]
                        self.assertEqual(info["name"], name)
                        self.assertEqual(info["description"], contract.description)
                        self.assertEqual(
                            info["input_schema"], by_name[name].input_schema
                        )
                        self.assertEqual(info["output_schema"], contract.output_schema)
                        expected_annotations = contract.annotations.model_dump(
                            by_alias=True, exclude_none=True
                        )
                        self.assertEqual(
                            info["annotations"],
                            {
                                key: expected_annotations[key]
                                for key in (
                                    "readOnlyHint",
                                    "destructiveHint",
                                    "idempotentHint",
                                    "openWorldHint",
                                )
                            },
                        )

            try:
                asyncio.run(exercise())
            finally:
                manager.shutdown()

    def test_unregistered_contract_is_invisible_to_catalog_and_tool_info(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            root = base_path / "workspace"
            root.mkdir()
            settings = Settings.create(root)
            manager = self._profile_manager(
                settings,
                base_path,
                tools=frozenset({"run_pytest"}),
            )
            server = create_server(settings, task_manager=manager)

            async def exercise() -> None:
                names = {tool.name for tool in await server.list_tools()}
                self.assertIn("run_pytest", names)
                self.assertNotIn("run_mypy", names)
                catalog = json.loads(
                    await self._read_text(server, "internal://tool-catalog")
                )
                catalog_names = {entry["name"] for entry in catalog["tools"]}
                self.assertEqual(catalog_names, names)
                with self.assertRaises(ResourceNotFoundError):
                    await server.read_resource("internal://tool-info/run_mypy")

            try:
                asyncio.run(exercise())
            finally:
                manager.shutdown()

    def test_unknown_and_traversal_tool_info_uris_fail_as_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root))

            async def exercise() -> None:
                for uri in (
                    "internal://tool-info/not-a-tool",
                    "internal://tool-info/../secret",
                    "internal://tool-info/%2e%2e",
                    "internal://tool-info/foo/bar",
                    "internal://workflows/not-real",
                ):
                    with self.subTest(uri=uri):
                        with self.assertRaises(ResourceNotFoundError):
                            await server.read_resource(uri)

            asyncio.run(exercise())

    def test_private_execution_configuration_is_not_leaked(self) -> None:
        forbidden = (
            "/Users/host/private",
            "secret-token-value",
            "sha256:image-private-digest",
            "/tmp/private-snapshot",
        )
        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            root = base_path / "workspace"
            root.mkdir()
            settings = Settings.create(root)
            manager = self._profile_manager(
                settings,
                base_path,
                tools=frozenset({"run_pytest"}),
                source=Path("/Users/host/private/secret-token-value.json"),
                image="sha256:image-private-digest",
            )
            server = create_server(settings, task_manager=manager)

            async def exercise() -> None:
                uris = [
                    "internal://instructions",
                    "internal://tool-catalog",
                    "internal://workflows/edit-file",
                    "internal://workflows/debug-python",
                    "internal://workflows/recover-file",
                    "internal://workflows/review-changes",
                    "internal://tool-info/run_pytest",
                ]
                combined = "\n".join(
                    [await self._read_text(server, uri) for uri in uris]
                )
                for secret in forbidden:
                    self.assertNotIn(secret, combined)

            try:
                asyncio.run(exercise())
            finally:
                manager.shutdown()

    def test_resource_reads_use_declared_mime_types(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root))

            async def exercise() -> None:
                instructions = await server.read_resource("internal://instructions")
                catalog = await server.read_resource("internal://tool-catalog")
                tool_info = await server.read_resource(
                    "internal://tool-info/read_file_versioned"
                )
                self.assertEqual(instructions[0].mime_type, resources.MARKDOWN_MIME)
                self.assertEqual(catalog[0].mime_type, resources.JSON_MIME)
                self.assertEqual(tool_info[0].mime_type, resources.JSON_MIME)

            asyncio.run(exercise())

    def test_stdio_mcp_boundary_lists_and_reads_resources(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:

            async def exercise() -> None:
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        str(project_root / "server.py"),
                        "--root",
                        root,
                        "--read-only",
                    ],
                    cwd=project_root,
                )
                async with (
                    stdio_client(parameters) as (read_stream, write_stream),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    listed = await session.list_resources()
                    templates = await session.list_resource_templates()
                    catalog = await session.read_resource("internal://tool-catalog")
                    info = await session.read_resource(
                        "internal://tool-info/read_file_versioned"
                    )

                uris = {str(resource.uri) for resource in listed.resources}
                self.assertIn("internal://instructions", uris)
                self.assertIn("internal://tool-catalog", uris)
                template_uris = {
                    template.uri_template for template in templates.resource_templates
                }
                self.assertEqual(
                    template_uris,
                    {"internal://tool-info/{name}"},
                )
                self.assertEqual(len(catalog.contents), 1)
                self.assertEqual(len(info.contents), 1)
                self.assertIn('"tools"', catalog.contents[0].text)
                self.assertIn('"read_file_versioned"', info.contents[0].text)

            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
