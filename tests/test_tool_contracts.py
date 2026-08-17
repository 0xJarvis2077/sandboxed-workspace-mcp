from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from sandboxed_workspace_mcp import server as server_module
from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.tool_contracts import TOOL_CONTRACTS


class ToolContractTests(unittest.TestCase):
    def test_registered_tools_have_complete_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(
                Settings.create(
                    root,
                    allow_git_writes=True,
                    allow_trash=True,
                    allow_trash_purge=True,
                )
            )
            tools = asyncio.run(server.list_tools())

        self.assertTrue(tools)
        self.assertTrue({tool.name for tool in tools}.issubset(TOOL_CONTRACTS))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIsNotNone(tool.output_schema)
                self.assertEqual(tool.output_schema.get("type"), "object")
                self.assertIsNotNone(tool.annotations)
                annotations = tool.annotations.model_dump(
                    by_alias=True, exclude_none=False
                )
                for name in (
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint",
                ):
                    self.assertIsInstance(annotations[name], bool)

    def test_registry_names_are_unique_and_contracts_are_meaningful(self) -> None:
        self.assertEqual(len(TOOL_CONTRACTS), len(set(TOOL_CONTRACTS)))
        expectations = {
            "read_file_versioned": {"content", "sha256"},
            "trash_file": {"trash_id", "sha256"},
            "run_pytest": {"status", "failures"},
            "run_ruff": {"status", "diagnostics"},
            "run_pytest_coverage": {"tests", "coverage"},
        }
        for name, required_names in expectations.items():
            with self.subTest(tool=name):
                encoded = json.dumps(TOOL_CONTRACTS[name].output_schema)
                for field in required_names:
                    self.assertIn(f'"{field}"', encoded)

    def test_every_server_tool_decorator_has_exactly_one_registry_contract(
        self,
    ) -> None:
        source = Path(server_module.__file__).read_text(encoding="utf-8")
        module = ast.parse(source)
        decorated_names = {
            node.name
            for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }
        self.assertEqual(decorated_names, set(TOOL_CONTRACTS))

    def test_annotation_policy_records_non_obvious_lifecycle_decisions(self) -> None:
        def hints(
            name: str,
        ) -> tuple[bool | None, bool | None, bool | None, bool | None]:
            annotations = TOOL_CONTRACTS[name].annotations
            return (
                annotations.read_only_hint,
                annotations.destructive_hint,
                annotations.idempotent_hint,
                annotations.open_world_hint,
            )

        self.assertEqual(hints("run_pytest"), (False, True, False, False))
        self.assertEqual(hints("run_command"), (False, True, False, False))
        self.assertEqual(hints("start_command"), (False, True, False, False))
        self.assertEqual(hints("stop_task"), (False, True, True, False))
        self.assertEqual(hints("trash_file"), (False, True, False, False))
        self.assertEqual(hints("restore_trashed_file"), (False, True, False, False))
        self.assertEqual(hints("python_version"), (True, False, True, False))

    def test_core_boundary_preserves_text_and_adds_structured_content(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            (workspace / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            server = create_server(Settings.create(root))

            async def exercise() -> None:
                info = await server.call_tool("project_info", {})
                read = await server.call_tool("read_file", {"path": "sample.txt"})
                versioned = await server.call_tool(
                    "read_file_versioned", {"path": "sample.txt"}
                )
                search = await server.call_tool(
                    "search_text", {"text": "beta", "path": "."}
                )
                written = await server.call_tool(
                    "write_file",
                    {"path": "created.txt", "content": "hello"},
                )

                self.assertIn("Allowed project root:", info.content[0].text)
                self.assertEqual(info.structured_content["workspace_root"], ".")
                self.assertNotIn(root, json.dumps(info.structured_content))
                self.assertEqual(read.content[0].text, "alpha\nbeta\n")
                self.assertEqual(read.structured_content["content"], "alpha\nbeta\n")
                self.assertEqual(versioned.structured_content["path"], "sample.txt")
                self.assertEqual(len(versioned.structured_content["sha256"]), 64)
                self.assertEqual(search.structured_content["matches"][0]["line"], 2)
                self.assertEqual(
                    search.structured_content["matches"][0]["path"], "sample.txt"
                )
                self.assertTrue(written.structured_content["written"])
                self.assertEqual(written.structured_content["bytes"], 5)

            asyncio.run(exercise())

    def test_trash_and_git_boundary_results_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            (workspace / "recover.txt").write_text("recover me", encoding="utf-8")
            server = create_server(
                Settings.create(
                    root,
                    allow_git_writes=True,
                    allow_trash=True,
                )
            )

            async def exercise() -> None:
                versioned = await server.call_tool(
                    "read_file_versioned", {"path": "recover.txt"}
                )
                trashed = await server.call_tool(
                    "trash_file",
                    {
                        "path": "recover.txt",
                        "expected_sha256": versioned.structured_content["sha256"],
                    },
                )
                self.assertEqual(
                    trashed.structured_content["original_path"], "recover.txt"
                )
                restored = await server.call_tool(
                    "restore_trashed_file",
                    {
                        "trash_id": trashed.structured_content["trash_id"],
                        "expected_sha256": trashed.structured_content["sha256"],
                    },
                )
                self.assertTrue(restored.structured_content["restored"])

                initialized = await server.call_tool("git_init", {})
                self.assertIn(
                    initialized.structured_content["status"],
                    {"initialized", "already_initialized"},
                )
                status = await server.call_tool("git_status", {"style": "porcelain"})
                self.assertIn("clean", status.structured_content)
                self.assertIsInstance(status.structured_content["entries"], list)

            asyncio.run(exercise())

    def test_read_only_registration_remains_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root, allow_writes=False))
            names = {tool.name for tool in asyncio.run(server.list_tools())}

        self.assertNotIn("write_file", names)
        self.assertNotIn("create_directory", names)
        self.assertIn("read_file", names)
        self.assertIn("read_file_versioned", names)


if __name__ == "__main__":
    unittest.main()
