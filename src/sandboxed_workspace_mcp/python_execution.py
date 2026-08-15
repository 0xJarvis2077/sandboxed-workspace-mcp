"""Compile structured Python debugging requests into fixed container argv."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import Settings
from .workspace import Workspace, WorkspaceError

MAX_PYTEST_TARGETS = 32
MAX_PYTEST_TARGET_BYTES = 1024
MAX_PYTEST_KEYWORD_BYTES = 512


class PythonExecutionError(ValueError):
    """Raised when structured Python execution input violates its contract."""


class PythonCommandCompiler:
    """Validate paths/options and generate argv that callers cannot extend."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspace = Workspace(settings)

    @staticmethod
    def python_version() -> tuple[str, ...]:
        return ("python", "--version")

    def pytest(
        self,
        *,
        targets: list[str] | None = None,
        keyword: str | None = None,
        quiet: bool = False,
        verbosity: int = 0,
        exit_first: bool = False,
        no_capture: bool = False,
        traceback: str = "auto",
    ) -> tuple[str, ...]:
        if type(quiet) is not bool:
            raise PythonExecutionError("pytest quiet must be a boolean")
        if type(verbosity) is not int or verbosity not in {0, 1, 2}:
            raise PythonExecutionError("pytest verbosity must be 0, 1, or 2")
        if quiet and verbosity:
            raise PythonExecutionError(
                "pytest quiet and positive verbosity cannot be combined"
            )
        if type(exit_first) is not bool or type(no_capture) is not bool:
            raise PythonExecutionError(
                "pytest exit_first and no_capture must be booleans"
            )
        if traceback not in {"auto", "short", "long"}:
            raise PythonExecutionError("pytest traceback must be auto, short, or long")
        if keyword is not None:
            if not isinstance(keyword, str) or not keyword or "\x00" in keyword:
                raise PythonExecutionError(
                    "pytest keyword must be a non-empty string without NUL bytes"
                )
            if len(keyword.encode("utf-8")) > MAX_PYTEST_KEYWORD_BYTES:
                raise PythonExecutionError(
                    f"pytest keyword exceeds {MAX_PYTEST_KEYWORD_BYTES} bytes"
                )
        if targets is None:
            target_values: list[str] = []
        elif isinstance(targets, list):
            target_values = targets
        else:
            raise PythonExecutionError("pytest targets must be an array")
        if len(target_values) > MAX_PYTEST_TARGETS:
            raise PythonExecutionError(
                f"pytest accepts at most {MAX_PYTEST_TARGETS} targets"
            )

        validated_targets = [self._pytest_target(target) for target in target_values]
        argv = ["python", "-m", "pytest"]
        if quiet:
            argv.append("-q")
        if verbosity:
            argv.append("-" + "v" * verbosity)
        if exit_first:
            argv.append("-x")
        if no_capture:
            argv.append("-s")
        if traceback != "auto":
            argv.append(f"--tb={traceback}")
        if keyword is not None:
            argv.extend(["-k", keyword])
        if validated_targets:
            argv.append("--")
            argv.extend(validated_targets)
        return tuple(argv)

    def python_script(self, path: str) -> tuple[str, ...]:
        if not isinstance(path, str) or path in {"-c", "-m"}:
            raise PythonExecutionError("Python script path must name a workspace file")
        relative, is_directory = self._workspace_entry(path)
        if is_directory:
            raise PythonExecutionError("Python script path must be a regular file")
        if PurePosixPath(relative).suffix != ".py":
            raise PythonExecutionError("Python script path must end in .py")
        return ("python", "--", relative)

    def _pytest_target(self, target: str) -> str:
        if (
            not isinstance(target, str)
            or not target
            or "\x00" in target
            or len(target.encode("utf-8")) > MAX_PYTEST_TARGET_BYTES
        ):
            raise PythonExecutionError(
                "each pytest target must be a non-empty bounded string"
            )
        parts = target.split("::")
        path_text = parts[0]
        if not path_text:
            raise PythonExecutionError("pytest test node must start with a path")
        if any(not selector for selector in parts[1:]):
            raise PythonExecutionError("pytest test node selectors cannot be empty")
        relative, is_directory = self._workspace_entry(path_text)
        if len(parts) > 1 and is_directory:
            raise PythonExecutionError("pytest test nodes must start with a file")
        return "::".join([relative, *parts[1:]])

    def _workspace_entry(self, value: str) -> tuple[str, bool]:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise PythonExecutionError("execution path must be a non-empty string")
        if "\\" in value or value.startswith("~") or PureWindowsPath(value).drive:
            raise PythonExecutionError(
                "execution paths use workspace-relative '/' path syntax"
            )
        supplied = PurePosixPath(value)
        if ".." in supplied.parts:
            raise PythonExecutionError("execution paths must not contain '..'")
        raw_path = Path(value)
        candidate = (
            raw_path if raw_path.is_absolute() else self.settings.root / raw_path
        )
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical_relative = lexical.relative_to(self.settings.root)
        except ValueError as exc:
            raise PythonExecutionError("execution path escapes workspace") from exc
        try:
            resolved = self.workspace.safe_path(value)
        except WorkspaceError as exc:
            raise PythonExecutionError(str(exc)) from exc
        try:
            relative = resolved.relative_to(self.settings.root)
        except ValueError as exc:  # defensive: safe_path already enforces this
            raise PythonExecutionError("execution path escapes workspace") from exc
        if relative != lexical_relative:
            raise PythonExecutionError(
                f"execution path must not contain symbolic links: {value}"
            )
        candidate = lexical
        if any(part in self.settings.ignored_dirs for part in lexical_relative.parts):
            raise PythonExecutionError(
                "execution path is omitted from disposable workspace snapshots"
            )

        current = self.settings.root
        for part in lexical_relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise PythonExecutionError(
                    f"execution path does not exist: {value}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PythonExecutionError(
                    f"execution path must not contain symbolic links: {value}"
                )
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise PythonExecutionError(
                f"execution path does not exist: {value}"
            ) from exc
        if stat.S_ISDIR(metadata.st_mode):
            is_directory = True
        elif stat.S_ISREG(metadata.st_mode):
            is_directory = False
        else:
            raise PythonExecutionError(
                f"execution path is not a regular file or directory: {value}"
            )
        return (
            "." if not lexical_relative.parts else lexical_relative.as_posix(),
            is_directory,
        )
