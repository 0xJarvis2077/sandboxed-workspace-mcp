from __future__ import annotations

import unittest
from unittest.mock import patch

from sandboxed_workspace_mcp.safe_regex import SafeRegex, SafeRegexError


class SafeRegexTests(unittest.TestCase):
    def test_literal_fast_path_matches_and_respects_anchors(self) -> None:
        cases = (
            ("needle", "prefix needle suffix", True),
            ("needle", "prefix missing suffix", False),
            ("^needle", "needle suffix", True),
            ("^needle", "prefix needle", False),
            ("needle$", "prefix needle", True),
            ("needle$", "needle suffix", False),
            ("^needle$", "needle", True),
            ("^needle$", "needle suffix", False),
        )
        for pattern, text, expected in cases:
            with self.subTest(pattern=pattern, text=text):
                matcher = SafeRegex(pattern)
                self.assertIsNotNone(matcher._literal_pattern)
                self.assertEqual(matcher.search(text), expected)

    def test_literal_fast_path_supports_escaped_metacharacters(self) -> None:
        matcher = SafeRegex(r"a\+b")

        literal_pattern = matcher._literal_pattern
        assert literal_pattern is not None
        self.assertEqual(literal_pattern.needle, "a+b")
        self.assertTrue(matcher.search("prefix a+b suffix"))
        self.assertFalse(matcher.search("a++b"))

    def test_literal_fast_path_supports_ascii_ignore_case(self) -> None:
        self.assertTrue(SafeRegex("Needle", ignore_case=True).search("NEEDLE"))
        self.assertFalse(SafeRegex("Needle", ignore_case=True).search("missing"))

    def test_non_ascii_ignore_case_keeps_nfa_semantics(self) -> None:
        matcher = SafeRegex("ß", ignore_case=True)

        self.assertTrue(matcher.search("ẞ"))
        self.assertFalse(matcher.search("SS"))

    def test_general_patterns_continue_to_use_the_nfa(self) -> None:
        matcher = SafeRegex(r"ab+c")

        self.assertIsNone(matcher._literal_pattern)
        with patch.object(matcher, "_closure", wraps=matcher._closure) as closure:
            self.assertTrue(matcher.search("xxabbbcxx"))
        self.assertGreater(closure.call_count, 0)

    def test_literal_fast_path_checks_cancellation_before_matching(self) -> None:
        checks = 0

        def should_stop() -> bool:
            nonlocal checks
            checks += 1
            return True

        self.assertFalse(SafeRegex("needle").search("needle", should_stop=should_stop))
        self.assertEqual(checks, 1)

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
