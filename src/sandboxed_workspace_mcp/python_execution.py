"""Compile structured Python debugging requests into fixed container argv."""

from __future__ import annotations

from pathlib import PurePosixPath

from .analysis_execution import coverage_harness
from .command_execution import ExecutionPathError, WorkspaceExecutionPathValidator
from .config import Settings

MAX_PYTEST_TARGETS = 32
MAX_PYTEST_TARGET_BYTES = 1024
MAX_PYTEST_KEYWORD_BYTES = 512
MAX_ANALYSIS_PATHS = 32
MAX_ANALYSIS_PATH_BYTES = 1024
MAX_PYTEST_FAILURES = 20


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
        show_locals: bool = False,
        max_failures: int | None = None,
        include_failure_plugin: bool = False,
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
        if type(show_locals) is not bool:
            raise PythonExecutionError("pytest show_locals must be a boolean")
        if max_failures is not None and (
            type(max_failures) is not int
            or not 1 <= max_failures <= MAX_PYTEST_FAILURES
        ):
            raise PythonExecutionError(
                "pytest max_failures must be an integer between 1 and "
                f"{MAX_PYTEST_FAILURES}"
            )
        if exit_first and max_failures not in {None, 1}:
            raise PythonExecutionError(
                "pytest exit_first cannot be combined with max_failures other than 1"
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
        if include_failure_plugin:
            argv.extend(["-p", "sandboxed_workspace_mcp_debug_plugin"])
        argv.extend(["-o", "cache_dir=/tmp/cache/pytest"])
        if quiet:
            argv.append("-q")
        if verbosity:
            argv.append("-" + "v" * verbosity)
        if exit_first:
            argv.append("-x")
        if no_capture:
            argv.append("-s")
        if show_locals:
            argv.append("--showlocals")
        if max_failures is not None and not exit_first:
            argv.append(f"--maxfail={max_failures}")
        if traceback != "auto":
            argv.append(f"--tb={traceback}")
        if keyword is not None:
            argv.extend(["-k", keyword])
        if validated_targets:
            argv.append("--")
            argv.extend(validated_targets)
        return tuple(argv)

    def ruff(
        self,
        *,
        paths: list[str] | None = None,
        fix: bool = False,
    ) -> tuple[str, ...]:
        if type(fix) is not bool:
            raise PythonExecutionError("ruff fix must be a boolean")
        validated = self._analysis_paths(paths)
        argv = ["ruff", "check", "--output-format=json"]
        if fix:
            argv.append("--fix")
        argv.append("--")
        argv.extend(validated or ["."])
        return tuple(argv)

    def mypy(
        self,
        *,
        paths: list[str] | None = None,
        strict: bool = False,
    ) -> tuple[str, ...]:
        if type(strict) is not bool:
            raise PythonExecutionError("mypy strict must be a boolean")
        validated = self._analysis_paths(paths)
        argv = [
            "python",
            "-m",
            "mypy",
            "--no-color-output",
            "--hide-error-context",
            "--show-error-codes",
            "--show-column-numbers",
            "--no-error-summary",
        ]
        if strict:
            argv.append("--strict")
        argv.append("--")
        argv.extend(validated or ["."])
        return tuple(argv)

    def pytest_coverage(
        self,
        *,
        targets: list[str] | None = None,
        keyword: str | None = None,
        branch: bool = False,
        fail_under: float | None = None,
    ) -> tuple[str, ...]:
        if type(branch) is not bool:
            raise PythonExecutionError("coverage branch must be a boolean")
        if fail_under is not None:
            if type(fail_under) not in {int, float}:
                raise PythonExecutionError("coverage fail_under must be a number")
            if not 0 <= float(fail_under) <= 100:
                raise PythonExecutionError(
                    "coverage fail_under must be between 0 and 100"
                )
        pytest_argv = self.pytest(targets=targets, keyword=keyword)
        return (
            "python",
            "-c",
            coverage_harness(branch, fail_under),
            "--",
            *pytest_argv[3:],
        )

    def python_script(self, path: str | None) -> tuple[str, ...]:
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

    def _analysis_paths(self, paths: list[str] | None) -> list[str]:
        if paths is None:
            return []
        if not isinstance(paths, list):
            raise PythonExecutionError("analysis paths must be an array")
        if len(paths) > MAX_ANALYSIS_PATHS:
            raise PythonExecutionError(
                f"analysis accepts at most {MAX_ANALYSIS_PATHS} paths"
            )
        validated: list[str] = []
        for path in paths:
            try:
                entry = self.paths.entry(
                    path,
                    label="analysis path",
                    max_bytes=MAX_ANALYSIS_PATH_BYTES,
                )
            except ExecutionPathError as exc:
                raise PythonExecutionError(str(exc)) from exc
            validated.append(entry.relative)
        return validated

    def _workspace_entry(self, value: str) -> tuple[str, bool]:
        try:
            entry = self.paths.entry(value)
        except ExecutionPathError as exc:
            raise PythonExecutionError(str(exc)) from exc
        return entry.relative, entry.is_directory
