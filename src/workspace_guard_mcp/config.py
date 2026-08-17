"""Validated runtime configuration for WorkspaceGuard MCP."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .access_policy import (
    DEFAULT_BLOCKED_PATTERNS,
    TRASH_DIRECTORY_NAME,
    PolicyConfigurationError,
    validate_blocked_pattern,
)

DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        TRASH_DIRECTORY_NAME,
        "venv",
    }
)


class ConfigurationError(ValueError):
    """Raised when the server configuration is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Limits and permissions for one sandboxed workspace."""

    root: Path
    max_file_size: int = 2 * 1024 * 1024
    max_output_size: int = 200_000
    max_tree_entries: int = 1_500
    max_tree_depth: int = 5
    max_scan_entries: int = 10_000
    max_search_bytes: int = 64 * 1024 * 1024
    search_timeout_seconds: float = 10.0
    max_concurrent_searches: int = 1
    git_timeout: float = 30.0
    max_git_baseline_files: int = 10_000
    max_git_baseline_bytes: int = 256 * 1024 * 1024
    allow_writes: bool = True
    allow_git_writes: bool = False
    allow_trash: bool = False
    allow_trash_purge: bool = False
    max_trash_items: int = 200
    max_trash_bytes: int = 256 * 1024 * 1024
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS
    blocked_patterns: tuple[str, ...] = DEFAULT_BLOCKED_PATTERNS

    def __post_init__(self) -> None:
        try:
            root = Path(self.root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigurationError(
                f"workspace root does not exist: {self.root}"
            ) from exc

        if not root.is_dir():
            raise ConfigurationError(f"workspace root is not a directory: {root}")

        for name, value in (
            ("max_file_size", self.max_file_size),
            ("max_output_size", self.max_output_size),
            ("max_tree_entries", self.max_tree_entries),
            ("max_tree_depth", self.max_tree_depth),
            ("max_scan_entries", self.max_scan_entries),
            ("max_search_bytes", self.max_search_bytes),
            ("max_concurrent_searches", self.max_concurrent_searches),
        ):
            if value <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")

        if self.max_search_bytes > 1024 * 1024 * 1024:
            raise ConfigurationError("max_search_bytes must be at most 1073741824")
        if self.max_concurrent_searches > 32:
            raise ConfigurationError("max_concurrent_searches must be at most 32")
        if not 0 < self.search_timeout_seconds <= 300:
            raise ConfigurationError(
                "search_timeout_seconds must be greater than zero and at most 300"
            )
        if self.git_timeout <= 0:
            raise ConfigurationError("git_timeout must be greater than zero")
        for name, value in (
            ("max_git_baseline_files", self.max_git_baseline_files),
            ("max_git_baseline_bytes", self.max_git_baseline_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
        if self.max_git_baseline_files > 1_000_000:
            raise ConfigurationError("max_git_baseline_files must be at most 1000000")
        if self.max_git_baseline_bytes > 4 * 1024 * 1024 * 1024:
            raise ConfigurationError(
                "max_git_baseline_bytes must be at most 4294967296"
            )
        if type(self.allow_writes) is not bool:
            raise ConfigurationError("allow_writes must be a boolean")
        if type(self.allow_git_writes) is not bool:
            raise ConfigurationError("allow_git_writes must be a boolean")
        if self.allow_git_writes and not self.allow_writes:
            raise ConfigurationError("allow_git_writes requires allow_writes=True")
        if type(self.allow_trash) is not bool:
            raise ConfigurationError("allow_trash must be a boolean")
        if self.allow_trash and not self.allow_writes:
            raise ConfigurationError("allow_trash requires allow_writes=True")
        if type(self.allow_trash_purge) is not bool:
            raise ConfigurationError("allow_trash_purge must be a boolean")
        if self.allow_trash_purge and not self.allow_trash:
            raise ConfigurationError("allow_trash_purge requires allow_trash=True")
        if self.allow_trash_purge and not self.allow_writes:
            raise ConfigurationError("allow_trash_purge requires allow_writes=True")
        for name, value in (
            ("max_trash_items", self.max_trash_items),
            ("max_trash_bytes", self.max_trash_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ConfigurationError(f"{name} must be greater than zero")
        if self.max_trash_items > 10_000:
            raise ConfigurationError("max_trash_items must be at most 10000")
        if self.max_trash_bytes > 4 * 1024 * 1024 * 1024:
            raise ConfigurationError("max_trash_bytes must be at most 4294967296")

        ignored_dirs = frozenset(self.ignored_dirs)
        if any(not name or "/" in name or "\\" in name for name in ignored_dirs):
            raise ConfigurationError(
                "ignored directory names must be non-empty base names"
            )

        try:
            blocked_patterns = tuple(
                dict.fromkeys(
                    validate_blocked_pattern(pattern)
                    for pattern in self.blocked_patterns
                )
            )
        except PolicyConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc

        object.__setattr__(self, "root", root)
        object.__setattr__(self, "ignored_dirs", ignored_dirs)
        object.__setattr__(self, "blocked_patterns", blocked_patterns)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        ignored_dirs: Iterable[str] | None = None,
        max_file_size: int = 2 * 1024 * 1024,
        max_output_size: int = 200_000,
        max_tree_entries: int = 1_500,
        max_tree_depth: int = 5,
        max_scan_entries: int = 10_000,
        max_search_bytes: int = 64 * 1024 * 1024,
        search_timeout_seconds: float = 10.0,
        max_concurrent_searches: int = 1,
        git_timeout: float = 30.0,
        max_git_baseline_files: int = 10_000,
        max_git_baseline_bytes: int = 256 * 1024 * 1024,
        allow_writes: bool = True,
        allow_git_writes: bool = False,
        allow_trash: bool = False,
        allow_trash_purge: bool = False,
        max_trash_items: int = 200,
        max_trash_bytes: int = 256 * 1024 * 1024,
        blocked_patterns: Iterable[str] | None = None,
    ) -> Settings:
        """Construct settings from a path-like value and optional directory names."""

        return cls(
            root=Path(root),
            max_file_size=max_file_size,
            max_output_size=max_output_size,
            max_tree_entries=max_tree_entries,
            max_tree_depth=max_tree_depth,
            max_scan_entries=max_scan_entries,
            max_search_bytes=max_search_bytes,
            search_timeout_seconds=search_timeout_seconds,
            max_concurrent_searches=max_concurrent_searches,
            git_timeout=git_timeout,
            max_git_baseline_files=max_git_baseline_files,
            max_git_baseline_bytes=max_git_baseline_bytes,
            allow_writes=allow_writes,
            allow_git_writes=allow_git_writes,
            allow_trash=allow_trash,
            allow_trash_purge=allow_trash_purge,
            max_trash_items=max_trash_items,
            max_trash_bytes=max_trash_bytes,
            ignored_dirs=(
                DEFAULT_IGNORED_DIRS
                if ignored_dirs is None
                else frozenset(ignored_dirs)
            ),
            blocked_patterns=tuple(
                dict.fromkeys(
                    (
                        *DEFAULT_BLOCKED_PATTERNS,
                        *(blocked_patterns or ()),
                    )
                )
            ),
        )
