from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sandboxed_workspace_mcp.bounded_output import (
    TRUNCATION_MARKER,
    truncate_utf8_result,
)
from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.server import create_server


class BoundedTextTests(unittest.TestCase):
    def test_untruncated_ascii_reports_false(self) -> None:
        result = truncate_utf8_result("hello", 5)
        self.assertEqual(result.text, "hello")
        self.assertFalse(result.truncated)

    def test_truncated_ascii_reports_true_and_is_bounded(self) -> None:
        result = truncate_utf8_result("x" * 100, 40)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text.encode("utf-8")), 40)

    def test_multibyte_utf8_is_valid_and_bounded(self) -> None:
        result = truncate_utf8_result("🙂中文" * 20, 41)
        self.assertTrue(result.truncated)
        result.text.encode("utf-8")
        self.assertLessEqual(len(result.text.encode("utf-8")), 41)

    def test_limit_smaller_than_marker_is_respected(self) -> None:
        result = truncate_utf8_result("long input", 3)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text.encode("utf-8")), 3)

    def test_exact_byte_boundary_is_not_truncated(self) -> None:
        result = truncate_utf8_result("12345", 5)
        self.assertEqual(result.text, "12345")
        self.assertFalse(result.truncated)

    def test_truncated_flag_matches_original_encoded_length(self) -> None:
        for text, limit in (("abc", 3), ("abc", 2), ("🙂", 4), ("🙂", 3)):
            with self.subTest(text=text, limit=limit):
                result = truncate_utf8_result(text, limit)
                self.assertEqual(result.truncated, len(text.encode("utf-8")) > limit)
                self.assertLessEqual(len(result.text.encode("utf-8")), limit)

    def test_natural_marker_does_not_mean_truncation(self) -> None:
        text = f"hello{TRUNCATION_MARKER}\nworld"
        result = truncate_utf8_result(text, len(text.encode("utf-8")))
        self.assertEqual(result.text, text)
        self.assertFalse(result.truncated)


class MCPTruncationProvenanceTests(unittest.TestCase):
    def test_file_marker_is_not_source_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            text = f"hello{TRUNCATION_MARKER}\nworld\n"
            Path(root, "marker.txt").write_text(text, encoding="utf-8")
            server = create_server(Settings.create(root))

            async def exercise() -> None:
                read = await server.call_tool("read_file", {"path": "marker.txt"})
                versioned = await server.call_tool(
                    "read_file_versioned", {"path": "marker.txt"}
                )
                self.assertEqual(read.content[0].text, text)
                self.assertEqual(read.structured_content["content"], text)
                self.assertFalse(read.structured_content["source_truncated"])
                self.assertFalse(read.structured_content["content_inline_truncated"])
                self.assertFalse(read.structured_content["truncated"])
                self.assertEqual(versioned.structured_content["content"], text)
                self.assertFalse(versioned.structured_content["source_truncated"])
                self.assertEqual(
                    versioned.structured_content["size"], len(text.encode())
                )
                self.assertEqual(len(versioned.structured_content["sha256"]), 64)

            asyncio.run(exercise())

    def test_real_file_bounding_reports_source_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            Path(root, "large.txt").write_text("x" * 5000, encoding="utf-8")
            server = create_server(Settings.create(root, max_output_size=1000))

            async def exercise() -> None:
                result = await server.call_tool("read_file", {"path": "large.txt"})
                self.assertTrue(result.structured_content["source_truncated"])
                self.assertTrue(result.structured_content["truncated"])
                self.assertLessEqual(
                    len(result.structured_content["content"].encode("utf-8")), 1000
                )

            asyncio.run(exercise())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_diff_and_show_marker_are_not_source_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=root, check=True
            )
            path = workspace / "marker.txt"
            path.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "marker.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            marker_line = TRUNCATION_MARKER.strip()
            path.write_text(f"before\n{marker_line}\n", encoding="utf-8")
            server = create_server(Settings.create(root))

            async def exercise() -> None:
                diff = await server.call_tool("git_diff", {})
                self.assertIn(marker_line, diff.content[0].text)
                self.assertFalse(diff.structured_content["source_truncated"])
                self.assertFalse(diff.structured_content["truncated"])

                subprocess.run(["git", "add", "marker.txt"], cwd=root, check=True)
                subprocess.run(
                    ["git", "commit", "-qm", "marker commit"], cwd=root, check=True
                )
                shown = await server.call_tool("git_show", {"commit": "HEAD"})
                self.assertIn(marker_line, shown.content[0].text)
                self.assertFalse(shown.structured_content["source_truncated"])
                self.assertFalse(shown.structured_content["truncated"])

            asyncio.run(exercise())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_workspace_diff_uses_authoritative_output_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            Path(root, "large.txt").write_text("line\n" * 500, encoding="utf-8")
            server = create_server(Settings.create(root, max_output_size=350))

            async def exercise() -> None:
                result = await server.call_tool("workspace_diff", {})
                self.assertIn("workspace_diff output truncated", result.content[0].text)
                self.assertTrue(result.structured_content["source_truncated"])
                self.assertTrue(result.structured_content["truncated"])

            asyncio.run(exercise())

    def test_run_shell_marker_content_is_not_machine_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            text = f"hello{TRUNCATION_MARKER}\nworld\n"
            Path(root, "marker.txt").write_text(text, encoding="utf-8")
            server = create_server(Settings.create(root))

            async def exercise() -> None:
                result = await server.call_tool(
                    "run_shell", {"command": "cat marker.txt"}
                )
                self.assertEqual(result.content[0].text, text)
                self.assertFalse(result.structured_content["source_truncated"])
                self.assertFalse(result.structured_content["truncated"])

            asyncio.run(exercise())

    def test_internal_carrier_does_not_replace_public_schema(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root))
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            schema = tools["read_file"].output_schema
            encoded = str(schema)
            self.assertIn("content", encoded)
            self.assertNotIn("'text':", encoded)


if __name__ == "__main__":
    unittest.main()
