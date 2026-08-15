"""Compile structured Python debugging requests into fixed container argv."""

from __future__ import annotations

from pathlib import PurePosixPath

from .command_execution import ExecutionPathError, WorkspaceExecutionPathValidator
from .config import Settings

MAX_PYTEST_TARGETS = 32
MAX_PYTEST_TARGET_BYTES = 1024
MAX_PYTEST_KEYWORD_BYTES = 512


class PythonExecutionError(ValueError):
    """Raised when structured Python execution input violates its contract."""


class PythonCommandCompiler:
    """Validate paths/options and generate argv that callers cannot extend."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.paths = WorkspaceExecutionPathValidator(settings)

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
        argv = ["python", "-m", "pytest", "-o", "cache_dir=/tmp/cache/pytest"]
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
        try:
            entry = self.paths.entry(value)
        except ExecutionPathError as exc:
            raise PythonExecutionError(str(exc)) from exc
        return entry.relative, entry.is_directory
