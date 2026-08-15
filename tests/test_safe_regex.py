from __future__ import annotations

import unittest

from sandboxed_workspace_mcp.safe_regex import SafeRegex, SafeRegexError


class SafeRegexTests(unittest.TestCase):
    def test_supported_subset_matches_without_backtracking(self) -> None:
        cases = (
            (r"add|result", "a result value", True),
            (r"^a.c$", "abc", True),
            (r"^(foo|bar)[0-9]+$", "bar123", True),
            (r"colou?r", "color", True),
            (r"ab*c", "abbbc", True),
            (r"[^a-c]+", "XYZ", True),
            (r"a\+b", "a+b", True),
            (r"^needle$", "prefix needle", False),
            (r"[a-c]+", "XYZ", False),
        )
        for pattern, text, expected in cases:
            with self.subTest(pattern=pattern, text=text):
                self.assertEqual(SafeRegex(pattern).search(text), expected)

        folded = SafeRegex(r"^[A-C]+$", ignore_case=True)
        self.assertTrue(folded.search("abc"))

    def test_invalid_or_unsupported_patterns_fail_explicitly(self) -> None:
        invalid = (
            "",
            "(",
            ")",
            "()",
            "a|",
            "|a",
            "*a",
            "a**",
            "a{2}",
            "[]",
            "[z-a]",
            "[abc",
            "\\",
            r"\d+",
            r"\1",
        )
        for pattern in invalid:
            with self.subTest(pattern=pattern), self.assertRaises(SafeRegexError):
                SafeRegex(pattern)

        with self.assertRaisesRegex(SafeRegexError, "too long"):
            SafeRegex("a" * 1025)

    def test_search_honors_external_budget_checks(self) -> None:
        checks = 0

        def should_stop() -> bool:
            nonlocal checks
            checks += 1
            return True

        self.assertFalse(
            SafeRegex("missing").search("x" * 10_000, should_stop=should_stop)
        )
        self.assertEqual(checks, 1)


if __name__ == "__main__":
    unittest.main()
