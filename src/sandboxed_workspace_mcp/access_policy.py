"""Data-driven ignored and blocked path policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

TRASH_DIRECTORY_NAME = ".sandboxed_workspace_mcp_trash"

DEFAULT_BLOCKED_PATTERNS = (
    TRASH_DIRECTORY_NAME,
    ".git",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "id_dsa",
    "id_dsa.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_rsa",
    "id_rsa.*",
)

SAFE_ENV_EXAMPLE_NAMES = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
    }
)


class PolicyConfigurationError(ValueError):
    """Raised when a blocked-path pattern has unsafe or unclear semantics."""


class PathFilterConfigurationError(ValueError):
    """Raised when a caller-supplied narrowing glob is invalid."""


def validate_blocked_pattern(pattern: str) -> str:
    """Validate one root-relative glob using the documented limited grammar."""

    if not isinstance(pattern, str) or not pattern or not pattern.strip():
        raise PolicyConfigurationError("blocked patterns must not be empty")
    if pattern != pattern.strip():
        raise PolicyConfigurationError(
            f"blocked pattern must not have surrounding whitespace: {pattern!r}"
        )
    if "\x00" in pattern:
        raise PolicyConfigurationError("blocked patterns must not contain NUL bytes")
    if "\\" in pattern:
        raise PolicyConfigurationError(
            "blocked patterns use '/' separators; backslashes are not allowed"
        )
    if (
        PurePosixPath(pattern).is_absolute()
        or PureWindowsPath(pattern).is_absolute()
        or PureWindowsPath(pattern).drive
        or pattern.startswith("~")
    ):
        raise PolicyConfigurationError(
            f"blocked patterns must be relative to the workspace: {pattern}"
        )
    if pattern.endswith("/") or "//" in pattern:
        raise PolicyConfigurationError(
            f"blocked pattern has an empty path component: {pattern}"
        )

    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyConfigurationError(
            "blocked pattern must not contain '.', '..', or empty components: "
            f"{pattern}"
        )
    if any(character in pattern for character in "[]{}!:"):
        raise PolicyConfigurationError(
            "blocked patterns support literals, '/', '*', '?', and '**' only"
        )
    if "***" in pattern:
        raise PolicyConfigurationError(
            f"blocked pattern contains an ambiguous wildcard sequence: {pattern}"
        )
    return pattern


def _glob_regex(pattern: str) -> re.Pattern[str]:
    expression: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("/**", index) and index + 3 == len(pattern):
            expression.append("(?:/.*)?")
            index += 3
            continue
        if pattern.startswith("**/", index):
            expression.append("(?:[^/]+/)*")
            index += 3
            continue
        if pattern.startswith("**", index):
            expression.append(".*")
            index += 2
            continue
        character = pattern[index]
        if character == "*":
            expression.append("[^/]*")
        elif character == "?":
            expression.append("[^/]")
        else:
            expression.append(re.escape(character))
        index += 1
    expression.append("$")
    return re.compile("".join(expression))


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: str
    expression: re.Pattern[str]
    basename_only: bool


class AccessPolicy:
    """Classify root-relative paths without performing filesystem IO."""

    def __init__(self, blocked_patterns: tuple[str, ...]) -> None:
        self.blocked_patterns = blocked_patterns
        self._rules = tuple(
            _Rule(
                pattern=pattern,
                expression=_glob_regex(pattern),
                basename_only="/" not in pattern,
            )
            for pattern in blocked_patterns
        )

    def blocking_pattern(self, relative_path: str) -> str | None:
        """Return the first matching rule, including matches on an ancestor."""

        if relative_path in {"", "."}:
            return None
        parts = PurePosixPath(relative_path).parts
        for rule in self._rules:
            if rule.basename_only:
                for index, part in enumerate(parts):
                    if (
                        rule.pattern == ".env.*"
                        and part in SAFE_ENV_EXAMPLE_NAMES
                        and index == len(parts) - 1
                    ):
                        continue
                    if rule.expression.fullmatch(part):
                        return rule.pattern
                continue

            for length in range(1, len(parts) + 1):
                candidate = "/".join(parts[:length])
                if rule.expression.fullmatch(candidate):
                    return rule.pattern
        return None

    def is_blocked(self, relative_path: str) -> bool:
        return self.blocking_pattern(relative_path) is not None

    def git_exclude_pathspecs(self) -> tuple[str, ...]:
        """Render validated rules as Git exclusion pathspecs."""

        pathspecs: list[str] = []
        for pattern in self.blocked_patterns:
            if "/" in pattern:
                candidates = (pattern, f"{pattern}/**")
            else:
                candidates = (pattern, f"**/{pattern}")
            pathspecs.extend(f":(glob,exclude){candidate}" for candidate in candidates)
        return tuple(dict.fromkeys(pathspecs))


@dataclass(frozen=True, slots=True)
class NarrowingPathFilter:
    """Match a documented glob without ever changing traversal policy."""

    pattern: str
    expression: re.Pattern[str]
    basename_only: bool

    @classmethod
    def compile(cls, pattern: str) -> NarrowingPathFilter:
        """Validate and compile a glob used only to remove candidate files."""

        if not isinstance(pattern, str) or not pattern:
            raise PathFilterConfigurationError("rg glob cannot be empty")
        if pattern != pattern.strip():
            raise PathFilterConfigurationError(
                "rg glob must not have surrounding whitespace"
            )
        if "\x00" in pattern:
            raise PathFilterConfigurationError("rg glob must not contain NUL bytes")
        if "\\" in pattern:
            raise PathFilterConfigurationError(
                "rg glob uses '/' separators; backslashes are not allowed"
            )
        if (
            PurePosixPath(pattern).is_absolute()
            or PureWindowsPath(pattern).is_absolute()
            or PureWindowsPath(pattern).drive
            or pattern.startswith(("~", "!"))
        ):
            raise PathFilterConfigurationError(
                "rg glob must be a non-negated workspace-relative pattern"
            )
        if pattern.endswith("/") or "//" in pattern:
            raise PathFilterConfigurationError(
                "rg glob must not contain an empty path component"
            )
        parts = pattern.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise PathFilterConfigurationError(
                "rg glob must not contain '.', '..', or empty components"
            )
        if any(character in pattern for character in "[]{}") or "***" in pattern:
            raise PathFilterConfigurationError(
                "rg glob supports literals, '/', '*', '?', and '**' only"
            )
        return cls(
            pattern=pattern,
            expression=_glob_regex(pattern),
            basename_only="/" not in pattern,
        )

    def matches(self, relative_path: str) -> bool:
        candidate = PurePosixPath(relative_path)
        value = candidate.name if self.basename_only else candidate.as_posix()
        return self.expression.fullmatch(value) is not None
