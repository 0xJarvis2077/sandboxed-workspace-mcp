from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workspace_guard_mcp.access_policy import DEFAULT_BLOCKED_PATTERNS, AccessPolicy
from workspace_guard_mcp.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_root_is_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.create(Path(directory) / ".")

        self.assertTrue(settings.root.is_absolute())
        self.assertEqual(settings.root, Path(directory).resolve())

    def test_missing_or_non_directory_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "file.txt"
            file_path.write_text("content", encoding="utf-8")

            with self.assertRaises(ConfigurationError):
                Settings.create(root / "missing")
            with self.assertRaises(ConfigurationError):
                Settings.create(file_path)

    def test_non_positive_limits_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigurationError, "max_output_size"):
                Settings.create(directory, max_output_size=0)
            with self.assertRaisesRegex(ConfigurationError, "git_timeout"):
                Settings.create(directory, git_timeout=0)

    def test_ignored_directories_must_be_base_names(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ConfigurationError, "base names"),
        ):
            Settings.create(directory, ignored_dirs={"cache/nested"})

    def test_default_and_additional_blocked_patterns_are_combined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.create(
                directory,
                blocked_patterns=("secrets/**", "*.credential"),
                max_scan_entries=123,
            )

        self.assertTrue(
            set(DEFAULT_BLOCKED_PATTERNS).issubset(settings.blocked_patterns)
        )
        self.assertIn("secrets/**", settings.blocked_patterns)
        self.assertIn("*.credential", settings.blocked_patterns)
        self.assertEqual(settings.max_scan_entries, 123)

    def test_unsafe_or_ambiguous_blocked_patterns_are_rejected(self) -> None:
        invalid_patterns = (
            "",
            " surrounded ",
            "bad\x00name",
            "/absolute",
            "../outside",
            "nested/../outside",
            "C:\\secrets",
            "~/.ssh",
            "nested//secret",
            "nested/",
            "secret[0]",
            "secret:name",
            "***",
        )
        with tempfile.TemporaryDirectory() as directory:
            for pattern in invalid_patterns:
                with (
                    self.subTest(pattern=pattern),
                    self.assertRaises(ConfigurationError),
                ):
                    Settings.create(directory, blocked_patterns=(pattern,))

    def test_blocked_glob_semantics_and_git_pathspecs_are_deterministic(self) -> None:
        policy = AccessPolicy(
            (
                ".env.*",
                ".env.example",
                "secrets/**",
                "private/*.txt",
                "token-?.json",
            )
        )

        self.assertIsNone(policy.blocking_pattern("."))
        self.assertTrue(policy.is_blocked("secrets"))
        self.assertTrue(policy.is_blocked("secrets/nested/value.txt"))
        self.assertTrue(policy.is_blocked("private/value.txt"))
        self.assertFalse(policy.is_blocked("private/nested/value.txt"))
        self.assertTrue(policy.is_blocked("token-a.json"))
        self.assertTrue(policy.is_blocked(".env.example"))
        self.assertFalse(policy.is_blocked(".env.sample"))
        self.assertTrue(policy.is_blocked(".env.sample/nested-secret"))
        pathspecs = policy.git_exclude_pathspecs()
        self.assertIn(":(glob,exclude)secrets/**", pathspecs)
        self.assertEqual(len(pathspecs), len(set(pathspecs)))


if __name__ == "__main__":
    unittest.main()
