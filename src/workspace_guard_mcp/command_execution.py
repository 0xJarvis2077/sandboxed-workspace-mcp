"""Validate generic execution commands and compile their fixed execution context."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import Settings
from .workspace import Workspace, WorkspaceError

MAX_COMMAND_PROGRAM_BYTES = 256
MAX_COMMAND_ARGS = 128
MAX_COMMAND_ARG_BYTES = 4096
MAX_COMMAND_ARGS_BYTES = 32_768
MAX_COMMAND_CWD_BYTES = 1024


class ExecutionPathError(ValueError):
    """Raised when an execution path cannot enter a disposable snapshot."""


class CommandExecutionError(ValueError):
    """Raised when a generic execution command violates its public contract."""


@dataclass(frozen=True, slots=True)
class ValidatedWorkspaceEntry:
    """One real, policy-checked workspace entry expressed relative to its root."""

    relative: str
    is_directory: bool


@dataclass(frozen=True, slots=True)
class CompiledCommand:
    """Caller argv plus a server-generated workspace workdir."""

    argv: tuple[str, ...]
    workdir: str


class WorkspaceExecutionPathValidator:
    """Own path rules shared by every snapshot-backed execution interface."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workspace = Workspace(settings)

    def entry(
        self,
        value: str,
        *,
        label: str = "execution path",
        relative_only: bool = False,
        max_bytes: int | None = None,
    ) -> ValidatedWorkspaceEntry:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ExecutionPathError(f"{label} must be a non-empty string")
        if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
            raise ExecutionPathError(f"{label} exceeds {max_bytes} UTF-8 bytes")
        if "\\" in value or value.startswith("~") or PureWindowsPath(value).drive:
            raise ExecutionPathError(
                f"{label} must use workspace-relative '/' path syntax"
            )
        supplied = PurePosixPath(value)
        if relative_only and supplied.is_absolute():
            raise ExecutionPathError(f"{label} must be workspace-relative")
        if ".." in supplied.parts:
            raise ExecutionPathError(f"{label} must not contain '..'")

        raw_path = Path(value)
        candidate = (
            raw_path if raw_path.is_absolute() else self.settings.root / raw_path
        )
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical_relative = lexical.relative_to(self.settings.root)
        except ValueError as exc:
            raise ExecutionPathError(f"{label} escapes workspace") from exc
        try:
            resolved = self.workspace.safe_path(value)
        except WorkspaceError as exc:
            raise ExecutionPathError(str(exc)) from exc
        try:
            relative = resolved.relative_to(self.settings.root)
        except ValueError as exc:  # defensive: safe_path already enforces this
            raise ExecutionPathError(f"{label} escapes workspace") from exc
        if relative != lexical_relative:
            raise ExecutionPathError(
                f"{label} must not contain symbolic links: {value}"
            )
        if any(part in self.settings.ignored_dirs for part in lexical_relative.parts):
            raise ExecutionPathError(
                f"{label} is omitted from disposable workspace snapshots"
            )

        current = self.settings.root
        for part in lexical_relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise ExecutionPathError(f"{label} does not exist: {value}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ExecutionPathError(
                    f"{label} must not contain symbolic links: {value}"
                )
        try:
            metadata = lexical.lstat()
        except OSError as exc:
            raise ExecutionPathError(f"{label} does not exist: {value}") from exc
        if stat.S_ISDIR(metadata.st_mode):
            is_directory = True
        elif stat.S_ISREG(metadata.st_mode):
            is_directory = False
        else:
            raise ExecutionPathError(
                f"{label} is not a regular file or directory: {value}"
            )
        return ValidatedWorkspaceEntry(
            relative="." if not lexical_relative.parts else lexical_relative.as_posix(),
            is_directory=is_directory,
        )


class CommandCompiler:
    """Compile bounded generic commands without shell parsing or host lookups."""

    def __init__(self, settings: Settings) -> None:
        self.paths = WorkspaceExecutionPathValidator(settings)

    def compile(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str = ".",
    ) -> CompiledCommand:
        validated_program = self._program(program)
        validated_args = self._args(args)
        try:
            directory = self.paths.entry(
                cwd,
                label="command cwd",
                relative_only=True,
                max_bytes=MAX_COMMAND_CWD_BYTES,
            )
        except ExecutionPathError as exc:
            raise CommandExecutionError(str(exc)) from exc
        if not directory.is_directory:
            raise CommandExecutionError("command cwd must be an existing directory")
        workdir = "/workspace"
        if directory.relative != ".":
            workdir += f"/{directory.relative}"
        return CompiledCommand(
            argv=(validated_program, *validated_args),
            workdir=workdir,
        )

    @staticmethod
    def _program(program: str) -> str:
        if not isinstance(program, str) or not program:
            raise CommandExecutionError("command program must be a non-empty string")
        if "\x00" in program:
            raise CommandExecutionError("command program must not contain NUL bytes")
        if len(program.encode("utf-8")) > MAX_COMMAND_PROGRAM_BYTES:
            raise CommandExecutionError(
                f"command program exceeds {MAX_COMMAND_PROGRAM_BYTES} UTF-8 bytes"
            )
        if program in {".", ".."} or "/" in program or "\\" in program:
            raise CommandExecutionError(
                "command program must be an image PATH basename"
            )
        return program

    @staticmethod
    def _args(args: list[str] | None) -> tuple[str, ...]:
        if args is None:
            return ()
        if not isinstance(args, list):
            raise CommandExecutionError("command args must be an array")
        if len(args) > MAX_COMMAND_ARGS:
            raise CommandExecutionError(
                f"command args accepts at most {MAX_COMMAND_ARGS} items"
            )
        total_bytes = 0
        validated: list[str] = []
        for index, argument in enumerate(args):
            if not isinstance(argument, str):
                raise CommandExecutionError(f"command args[{index}] must be a string")
            if "\x00" in argument:
                raise CommandExecutionError(
                    f"command args[{index}] must not contain NUL bytes"
                )
            argument_bytes = len(argument.encode("utf-8"))
            if argument_bytes > MAX_COMMAND_ARG_BYTES:
                raise CommandExecutionError(
                    f"command args[{index}] exceeds {MAX_COMMAND_ARG_BYTES} UTF-8 bytes"
                )
            total_bytes += argument_bytes
            if total_bytes > MAX_COMMAND_ARGS_BYTES:
                raise CommandExecutionError(
                    f"command args exceed {MAX_COMMAND_ARGS_BYTES} total UTF-8 bytes"
                )
            validated.append(argument)
        return tuple(validated)
