from __future__ import annotations

import unittest

from workspace_guard_mcp.result_cache import ResultCache
from workspace_guard_mcp.result_presentation import (
    externalize_text,
    externalize_tool_payload,
    preview_utf8,
)
from workspace_guard_mcp.workspace import TRUNCATION_MARKER


class ResultPresentationTests(unittest.TestCase):
    def test_small_text_stays_inline_without_cache_entry(self) -> None:
        cache = ResultCache(max_item_bytes=1024, max_total_bytes=2048)

        result = externalize_text(
            "small",
            cache,
            owner_scope=None,
            inline_threshold_bytes=100,
        )

        self.assertEqual(result.text, "small")
        self.assertFalse(result.inline_truncated)
        self.assertIsNone(result.resource_uri)
        self.assertEqual(cache.entry_count, 0)

    def test_large_text_gets_utf8_preview_and_resource(self) -> None:
        cache = ResultCache(max_item_bytes=10_000, max_total_bytes=20_000)
        text = "中文🙂" * 400

        result = externalize_text(
            text,
            cache,
            owner_scope=None,
            inline_threshold_bytes=1000,
        )

        self.assertTrue(result.inline_truncated)
        self.assertLessEqual(len(result.text.encode("utf-8")), 1000)
        self.assertIsNotNone(result.resource_uri)
        assert result.resource_uri is not None
        result_id = result.resource_uri.rsplit("/", 1)[1]
        self.assertEqual(cache.get(result_id).content, text)
        self.assertEqual(result.available_bytes, len(text.encode("utf-8")))

    def test_unicode_preview_is_valid_and_byte_bounded(self) -> None:
        text = "é中文🙂" * 50

        preview = preview_utf8(text, 37)

        preview.encode("utf-8")
        self.assertLessEqual(len(preview.encode("utf-8")), 37)

    def test_too_large_cache_item_returns_preview_without_uri(self) -> None:
        cache = ResultCache(max_item_bytes=100, max_total_bytes=100)

        result = externalize_text(
            "x" * 200,
            cache,
            owner_scope=None,
            inline_threshold_bytes=50,
        )

        self.assertTrue(result.inline_truncated)
        self.assertIsNone(result.resource_uri)
        self.assertEqual(cache.entry_count, 0)

    def test_truncation_semantics_cover_all_four_combinations(self) -> None:
        cases = (
            ("small", False, False, False),
            ("x" * 30_000, False, True, True),
            ("x" * 30_000 + TRUNCATION_MARKER, True, True, True),
            ("small" + TRUNCATION_MARKER, True, False, False),
        )
        for text, source, inline, has_resource in cases:
            with self.subTest(source=source, inline=inline):
                cache = ResultCache(max_item_bytes=100_000, max_total_bytes=200_000)
                payload, _ = externalize_tool_payload(
                    "read_file",
                    {
                        "path": "sample.txt",
                        "content": text,
                        "start_line": 1,
                        "end_line": None,
                        "truncated": source,
                    },
                    cache,
                    owner_scope=None,
                )
                self.assertEqual(payload["source_truncated"], source)
                self.assertEqual(payload["content_inline_truncated"], inline)
                self.assertEqual(payload["truncated"], source or inline)
                self.assertEqual(
                    payload["content_resource_uri"] is not None,
                    has_resource,
                )

    def test_execution_preserves_machine_fields_and_externalizes_streams(self) -> None:
        cache = ResultCache(max_item_bytes=100_000, max_total_bytes=200_000)
        payload = {
            "status": "failed",
            "exit_code": 1,
            "stdout": "x" * 30_000,
            "stderr": "error",
            "truncated": True,
            "timed_out": False,
            "duration_ms": 12,
            "diagnostics": [{"path": "a.py", "code": "E1"}],
        }

        adapted, changed = externalize_tool_payload(
            "run_ruff", payload, cache, owner_scope=None
        )

        self.assertTrue(changed)
        self.assertEqual(adapted["status"], "failed")
        self.assertEqual(adapted["exit_code"], 1)
        self.assertEqual(adapted["diagnostics"], payload["diagnostics"])
        self.assertTrue(adapted["source_truncated"])
        self.assertTrue(adapted["stdout_inline_truncated"])
        self.assertFalse(adapted["stderr_inline_truncated"])
        self.assertTrue(adapted["truncated"])


if __name__ == "__main__":
    unittest.main()
