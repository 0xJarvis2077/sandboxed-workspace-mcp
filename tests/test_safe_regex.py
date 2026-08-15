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

    def test_character_class_case_sensitivity(self) -> None:
        self.assertTrue(SafeRegex(r"^[a-z]$").search("m"))
        self.assertFalse(SafeRegex(r"^[a-z]$").search("M"))
        self.assertTrue(SafeRegex(r"^[a-z]$", ignore_case=True).search("M"))

    def test_negated_character_class(self) -> None:
        character_class = SafeRegex(r"^[^a-c]$")

        self.assertFalse(character_class.search("b"))
        self.assertTrue(character_class.search("d"))

    def test_character_class_dash_positions_and_escapes(self) -> None:
        self.assertTrue(SafeRegex(r"^[-a]$").search("-"))
        self.assertTrue(SafeRegex(r"^[a-]$").search("-"))
        self.assertTrue(SafeRegex(r"^[\-]$").search("-"))
        self.assertTrue(SafeRegex(r"^[\]]$").search("]"))

    def test_character_class_ranges_are_merged_without_changing_matches(self) -> None:
        character_class = SafeRegex(r"^[a-ca-cb-dd-f]$")

        for character in "abcdef":
            with self.subTest(character=character):
                self.assertTrue(character_class.search(character))
        self.assertFalse(character_class.search("g"))

    def test_ignore_case_character_class_folds_multi_character_literals(self) -> None:
        character_class = SafeRegex(r"^[ßẞ]$", ignore_case=True)

        self.assertTrue(character_class.search("ß"))
        self.assertTrue(character_class.search("ẞ"))
        self.assertFalse(character_class.search("s"))

    def test_ignore_case_multi_character_range_endpoint_is_unmatchable(self) -> None:
        character_class = SafeRegex(r"^[ß-ß]$", ignore_case=True)

        self.assertFalse(character_class.search("ß"))
        self.assertFalse(character_class.search("ẞ"))

    def test_reversed_character_class_range_still_fails(self) -> None:
        with self.assertRaises(SafeRegexError):
            SafeRegex(r"[z-a]", ignore_case=True)

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
