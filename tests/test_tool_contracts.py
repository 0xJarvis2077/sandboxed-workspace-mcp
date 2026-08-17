from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from _mcp_assertions import (
    require_call_tool_result,
    require_structured_content,
    require_text_content,
)
from mcp.types import CallToolResult

from workspace_guard_mcp import server as server_module
from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.server import create_server
from workspace_guard_mcp.tool_contracts import TOOL_CONTRACTS


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
                output_schema = tool.output_schema
                annotations_model = tool.annotations
                assert output_schema is not None
                assert annotations_model is not None
                self.assertEqual(output_schema.get("type"), "object")
                annotations = annotations_model.model_dump(
                    by_alias=True, exclude_none=False
                )
                for name in (
                    "readOnlyHint",
                    "destructiveHint",
                    "idempotentHint",
                    "openWorldHint",
                ):
                    self.assertIsInstance(annotations[name], bool)

    def test_call_tool_result_preserves_mcp_wire_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            (workspace / "sample.txt").write_text("sample", encoding="utf-8")
            server = create_server(Settings.create(root, allow_trash=True))
            result = asyncio.run(
                server.call_tool(
                    "trash_file",
                    {"path": "sample.txt", "expected_sha256": "0" * 64},
                )
            )

        self.assertIsInstance(result, CallToolResult)
        assert isinstance(result, CallToolResult)
        self.assertTrue(result.is_error)
        self.assertIsNotNone(result.structured_content)
        wire = result.model_dump(by_alias=True)
        self.assertTrue(wire["isError"])
        self.assertIsNotNone(wire["structuredContent"])
        self.assertNotIn("is_error", wire)
        self.assertNotIn("structured_content", wire)

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
                info = require_call_tool_result(
                    await server.call_tool("project_info", {})
                )
                read = require_call_tool_result(
                    await server.call_tool("read_file", {"path": "sample.txt"})
                )
                versioned = require_call_tool_result(
                    await server.call_tool(
                        "read_file_versioned", {"path": "sample.txt"}
                    )
                )
                search = require_call_tool_result(
                    await server.call_tool("search_text", {"text": "beta", "path": "."})
                )
                written = require_call_tool_result(
                    await server.call_tool(
                        "write_file",
                        {"path": "created.txt", "content": "hello"},
                    )
                )
                info_structured = require_structured_content(info)
                read_structured = require_structured_content(read)
                versioned_structured = require_structured_content(versioned)
                search_structured = require_structured_content(search)
                written_structured = require_structured_content(written)

                self.assertTrue(info.content)
                self.assertIn(
                    "Allowed project root:",
                    require_text_content(info.content[0]).text,
                )
                self.assertEqual(info_structured["workspace_root"], ".")
                self.assertNotIn(root, json.dumps(info_structured))
                self.assertTrue(read.content)
                self.assertEqual(
                    require_text_content(read.content[0]).text, "alpha\nbeta\n"
                )
                self.assertEqual(read_structured["content"], "alpha\nbeta\n")
                self.assertEqual(versioned_structured["path"], "sample.txt")
                self.assertEqual(len(versioned_structured["sha256"]), 64)
                self.assertEqual(search_structured["matches"][0]["line"], 2)
                self.assertEqual(search_structured["matches"][0]["path"], "sample.txt")
                self.assertTrue(written_structured["written"])
                self.assertEqual(written_structured["bytes"], 5)

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
                versioned = require_call_tool_result(
                    await server.call_tool(
                        "read_file_versioned", {"path": "recover.txt"}
                    )
                )
                versioned_structured = require_structured_content(versioned)
                trashed = require_call_tool_result(
                    await server.call_tool(
                        "trash_file",
                        {
                            "path": "recover.txt",
                            "expected_sha256": versioned_structured["sha256"],
                        },
                    )
                )
                trashed_structured = require_structured_content(trashed)
                self.assertEqual(trashed_structured["original_path"], "recover.txt")
                restored = require_call_tool_result(
                    await server.call_tool(
                        "restore_trashed_file",
                        {
                            "trash_id": trashed_structured["trash_id"],
                            "expected_sha256": trashed_structured["sha256"],
                        },
                    )
                )
                restored_structured = require_structured_content(restored)
                self.assertTrue(restored_structured["restored"])

                initialized = require_call_tool_result(
                    await server.call_tool("git_init", {})
                )
                initialized_structured = require_structured_content(initialized)
                self.assertIn(
                    initialized_structured["status"],
                    {"initialized", "already_initialized"},
                )
                status = require_call_tool_result(
                    await server.call_tool("git_status", {"style": "porcelain"})
                )
                status_structured = require_structured_content(status)
                self.assertIn("clean", status_structured)
                self.assertIsInstance(status_structured["entries"], list)

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
