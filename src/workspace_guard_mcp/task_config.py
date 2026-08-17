"""Load and freeze the trusted container-task configuration."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

MAX_TASK_CONFIG_BYTES = 1024 * 1024
_TASK_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_IMAGE_DIGEST = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:|sha256:)[0-9a-fA-F]{64}\Z"
)
_MEMORY = re.compile(r"[1-9][0-9]*(?:[kKmMgG](?:[bB])?)?\Z")
_CPUS = re.compile(r"[0-9]+(?:\.[0-9]{1,3})?\Z")
EXECUTION_PROFILE_TOOLS = frozenset(
    {
        "python_version",
        "run_pytest",
        "run_python_script",
        "run_ruff",
        "run_mypy",
        "run_pytest_coverage",
        "run_command",
        "start_command",
    }
)
ARBITRARY_COMMAND_TOOLS = frozenset({"run_command", "start_command"})


class TaskConfigurationError(ValueError):
    """Raised when a trusted task file is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class TaskLimits:
    """Resource and snapshot limits shared by every configured task."""

    timeout_seconds: float = 120.0
    max_output_bytes: int = 200_000
    max_snapshot_files: int = 20_000
    max_snapshot_bytes: int = 256 * 1024 * 1024
    memory: str = "1g"
    cpus: str = "2"
    pids: int = 128
    max_concurrent_tasks: int = 1
    max_workspace_file_bytes: int = 16 * 1024 * 1024
    max_workspace_growth_bytes: int = 256 * 1024 * 1024
    allow_best_effort_disk_limit: bool = False


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    """One operator-authorized command with no caller-controlled arguments."""

    name: str
    mode: str
    image: str
    argv: tuple[str, ...]
    workspace_access: str = "read-only"


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """Operator-authorized execution tools sharing one pinned image."""

    name: str
    image: str
    tools: frozenset[str]
    workspace_access: str = "read-only"
    allow_arbitrary_commands: bool = False


@dataclass(frozen=True, slots=True)
class TaskConfiguration:
    """Immutable task configuration loaded exactly once at process startup."""

    source: Path
    runtime: str
    limits: TaskLimits
    tasks: Mapping[str, TaskDefinition]
    profiles: Mapping[str, ExecutionProfile] = field(
        default_factory=lambda: MappingProxyType({})
    )
    default_profile: str | None = None


def load_task_config(
    path: str | os.PathLike[str],
    *,
    workspace_root: Path,
    max_bytes: int = MAX_TASK_CONFIG_BYTES,
) -> TaskConfiguration:
    """Safely load, validate, and freeze one trusted JSON task file."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise TaskConfigurationError("task config path must be absolute")
    if max_bytes <= 0:
        raise TaskConfigurationError("task config size limit must be positive")

    try:
        preliminary = candidate.lstat()
    except OSError as exc:
        raise TaskConfigurationError(
            f"cannot inspect task config: {candidate}: {exc}"
        ) from exc
    if stat.S_ISLNK(preliminary.st_mode):
        raise TaskConfigurationError("task config must not be a symbolic link")
    if not stat.S_ISREG(preliminary.st_mode):
        raise TaskConfigurationError("task config must be a regular file")

    try:
        resolved = candidate.resolve(strict=True)
        root = workspace_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TaskConfigurationError(
            f"cannot resolve task config safely: {exc}"
        ) from exc
    if resolved == root or root in resolved.parents:
        raise TaskConfigurationError(
            "task config must be outside the configured workspace root"
        )

    descriptor = _open_config_descriptor(candidate)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TaskConfigurationError(
                "task config is not a regular file after opening"
            )
        if (opened.st_dev, opened.st_ino) != (
            preliminary.st_dev,
            preliminary.st_ino,
        ):
            raise TaskConfigurationError("task config changed while it was opened")
        if opened.st_size > max_bytes:
            raise TaskConfigurationError(
                f"task config exceeds the {max_bytes}-byte size limit"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(payload) > max_bytes:
        raise TaskConfigurationError(
            f"task config exceeds the {max_bytes}-byte size limit"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskConfigurationError("task config must be UTF-8 JSON") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, TaskConfigurationError) as exc:
        if isinstance(exc, TaskConfigurationError):
            raise
        raise TaskConfigurationError(f"invalid task config JSON: {exc.msg}") from exc

    return _validate_config(raw, resolved)


def _open_config_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise TaskConfigurationError(
            f"cannot open task config safely: {path}: {exc}"
        ) from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskConfigurationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _validate_config(raw: Any, source: Path) -> TaskConfiguration:
    value = _object(raw, "task config")
    _known_fields(
        value,
        {"version", "runtime", "limits", "tasks", "profiles", "default_profile"},
        "task config",
    )
    _required_fields(value, {"version", "runtime"}, "task config")
    if type(value["version"]) is not int or value["version"] != 1:
        raise TaskConfigurationError("task config version must be integer 1")
    runtime = value["runtime"]
    if runtime not in {"docker", "podman"}:
        raise TaskConfigurationError("task runtime must be 'docker' or 'podman'")

    raw_limits = _object(value.get("limits", {}), "limits")
    limits = _validate_limits(raw_limits)
    task_values = _object(value.get("tasks", {}), "tasks")
    profile_values = _object(value.get("profiles", {}), "profiles")
    if not task_values and not profile_values:
        raise TaskConfigurationError(
            "task config must define at least one task or execution profile"
        )
    tasks: dict[str, TaskDefinition] = {}
    for name, task_value in task_values.items():
        if not isinstance(name, str) or _TASK_NAME.fullmatch(name) is None:
            raise TaskConfigurationError(
                f"invalid task name {name!r}; use 1-64 letters, digits, '_' or '-'"
            )
        tasks[name] = _validate_task(name, task_value)
    profiles: dict[str, ExecutionProfile] = {}
    for name, profile_value in profile_values.items():
        if not isinstance(name, str) or _TASK_NAME.fullmatch(name) is None:
            raise TaskConfigurationError(
                f"invalid profile name {name!r}; use 1-64 letters, digits, '_' or '-'"
            )
        profiles[name] = _validate_execution_profile(name, profile_value)
    default_profile = value.get("default_profile")
    if default_profile is not None:
        if (
            not isinstance(default_profile, str)
            or _TASK_NAME.fullmatch(default_profile) is None
        ):
            raise TaskConfigurationError(
                "default_profile must be a valid execution profile name"
            )
        if default_profile not in profiles:
            raise TaskConfigurationError(
                f"default_profile must name an existing profile: {default_profile}"
            )
    if any(task.workspace_access == "writable" for task in tasks.values()) or any(
        profile.workspace_access == "writable" for profile in profiles.values()
    ):
        required_writable_limits = {
            "max_workspace_file_bytes",
            "max_workspace_growth_bytes",
            "allow_best_effort_disk_limit",
        }
        missing_limits = sorted(required_writable_limits.difference(raw_limits))
        if missing_limits:
            raise TaskConfigurationError(
                "writable tasks require explicit limit field(s): "
                + ", ".join(missing_limits)
            )
        if not limits.allow_best_effort_disk_limit:
            raise TaskConfigurationError(
                "writable tasks require allow_best_effort_disk_limit=true"
            )
    return TaskConfiguration(
        source=source,
        runtime=runtime,
        limits=limits,
        tasks=MappingProxyType(tasks),
        profiles=MappingProxyType(profiles),
        default_profile=default_profile,
    )


def _validate_limits(raw: Any) -> TaskLimits:
    values = _object(raw, "limits")
    known = {
        "timeout_seconds",
        "max_output_bytes",
        "max_snapshot_files",
        "max_snapshot_bytes",
        "memory",
        "cpus",
        "pids",
        "max_concurrent_tasks",
        "max_workspace_file_bytes",
        "max_workspace_growth_bytes",
        "allow_best_effort_disk_limit",
    }
    _known_fields(values, known, "limits")

    defaults = TaskLimits()
    timeout = _number_in_range(
        values.get("timeout_seconds", defaults.timeout_seconds),
        "timeout_seconds",
        minimum=0.1,
        maximum=86_400,
    )
    max_output = _integer_in_range(
        values.get("max_output_bytes", defaults.max_output_bytes),
        "max_output_bytes",
        minimum=1_024,
        maximum=100_000_000,
    )
    max_files = _integer_in_range(
        values.get("max_snapshot_files", defaults.max_snapshot_files),
        "max_snapshot_files",
        minimum=1,
        maximum=1_000_000,
    )
    max_snapshot = _integer_in_range(
        values.get("max_snapshot_bytes", defaults.max_snapshot_bytes),
        "max_snapshot_bytes",
        minimum=1,
        maximum=100 * 1024 * 1024 * 1024,
    )
    pids = _integer_in_range(
        values.get("pids", defaults.pids), "pids", minimum=1, maximum=32_768
    )
    concurrent = _integer_in_range(
        values.get("max_concurrent_tasks", defaults.max_concurrent_tasks),
        "max_concurrent_tasks",
        minimum=1,
        maximum=64,
    )
    max_workspace_file = _integer_in_range(
        values.get("max_workspace_file_bytes", defaults.max_workspace_file_bytes),
        "max_workspace_file_bytes",
        minimum=1_024,
        maximum=10 * 1024 * 1024 * 1024,
    )
    max_workspace_growth = _integer_in_range(
        values.get("max_workspace_growth_bytes", defaults.max_workspace_growth_bytes),
        "max_workspace_growth_bytes",
        minimum=1_024,
        maximum=100 * 1024 * 1024 * 1024,
    )
    allow_best_effort = values.get(
        "allow_best_effort_disk_limit", defaults.allow_best_effort_disk_limit
    )
    if type(allow_best_effort) is not bool:
        raise TaskConfigurationError("allow_best_effort_disk_limit must be a boolean")

    memory = values.get("memory", defaults.memory)
    if not isinstance(memory, str) or _MEMORY.fullmatch(memory) is None:
        raise TaskConfigurationError(
            "memory must be a positive integer with an optional k, m, or g suffix"
        )
    cpus = values.get("cpus", defaults.cpus)
    if not isinstance(cpus, str) or _CPUS.fullmatch(cpus) is None:
        raise TaskConfigurationError("cpus must be a decimal string")
    try:
        cpu_value = Decimal(cpus)
    except InvalidOperation as exc:
        raise TaskConfigurationError("cpus must be a decimal string") from exc
    if not cpu_value.is_finite() or cpu_value <= 0 or cpu_value > 256:
        raise TaskConfigurationError("cpus must be greater than 0 and at most 256")

    return TaskLimits(
        timeout_seconds=timeout,
        max_output_bytes=max_output,
        max_snapshot_files=max_files,
        max_snapshot_bytes=max_snapshot,
        memory=memory,
        cpus=cpus,
        pids=pids,
        max_concurrent_tasks=concurrent,
        max_workspace_file_bytes=max_workspace_file,
        max_workspace_growth_bytes=max_workspace_growth,
        allow_best_effort_disk_limit=allow_best_effort,
    )


def _validate_task(name: str, raw: Any) -> TaskDefinition:
    value = _object(raw, f"task {name!r}")
    _known_fields(
        value,
        {"mode", "image", "argv", "workspace_access"},
        f"task {name!r}",
    )
    _required_fields(value, {"mode", "image", "argv"}, f"task {name!r}")
    mode = value["mode"]
    if mode not in {"run", "service"}:
        raise TaskConfigurationError(f"task {name!r} mode must be 'run' or 'service'")
    image = value["image"]
    if not isinstance(image, str) or _IMAGE_DIGEST.fullmatch(image) is None:
        raise TaskConfigurationError(
            f"task {name!r} image must be a repository@sha256 digest or "
            "full local sha256 image ID"
        )
    argv_value = value["argv"]
    if not isinstance(argv_value, list) or not argv_value:
        raise TaskConfigurationError(f"task {name!r} argv must be a non-empty array")
    if len(argv_value) > 256:
        raise TaskConfigurationError(f"task {name!r} argv has too many elements")
    argv: list[str] = []
    for index, argument in enumerate(argv_value):
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > 16_384
        ):
            raise TaskConfigurationError(
                f"task {name!r} argv[{index}] must be a non-empty bounded string"
            )
        argv.append(argument)
    workspace_access = value.get("workspace_access", "read-only")
    if workspace_access not in {"read-only", "writable"}:
        raise TaskConfigurationError(
            f"task {name!r} workspace_access must be 'read-only' or 'writable'"
        )
    return TaskDefinition(
        name=name,
        mode=mode,
        image=image,
        argv=tuple(argv),
        workspace_access=workspace_access,
    )


def _validate_execution_profile(name: str, raw: Any) -> ExecutionProfile:
    value = _object(raw, f"profile {name!r}")
    _known_fields(
        value,
        {"image", "tools", "workspace_access", "allow_arbitrary_commands"},
        f"profile {name!r}",
    )
    _required_fields(value, {"image", "tools"}, f"profile {name!r}")
    image = value["image"]
    if not isinstance(image, str) or _IMAGE_DIGEST.fullmatch(image) is None:
        raise TaskConfigurationError(
            f"profile {name!r} image must be a repository@sha256 digest or "
            "full local sha256 image ID"
        )
    raw_tools = value["tools"]
    if not isinstance(raw_tools, list) or not raw_tools:
        raise TaskConfigurationError(
            f"profile {name!r} tools must be a non-empty array"
        )
    if len(raw_tools) > len(EXECUTION_PROFILE_TOOLS):
        raise TaskConfigurationError(f"profile {name!r} has too many tools")
    tools: list[str] = []
    for tool in raw_tools:
        if not isinstance(tool, str) or tool not in EXECUTION_PROFILE_TOOLS:
            raise TaskConfigurationError(
                f"profile {name!r} contains an unsupported execution tool: {tool!r}"
            )
        if tool in tools:
            raise TaskConfigurationError(
                f"profile {name!r} contains duplicate tool {tool!r}"
            )
        tools.append(tool)
    workspace_access = value.get("workspace_access", "read-only")
    if workspace_access not in {"read-only", "writable"}:
        raise TaskConfigurationError(
            f"profile {name!r} workspace_access must be 'read-only' or 'writable'"
        )
    allow_arbitrary_commands = value.get("allow_arbitrary_commands", False)
    if type(allow_arbitrary_commands) is not bool:
        raise TaskConfigurationError(
            f"profile {name!r} allow_arbitrary_commands must be a boolean"
        )
    if ARBITRARY_COMMAND_TOOLS.intersection(tools) and not allow_arbitrary_commands:
        raise TaskConfigurationError(
            f"profile {name!r} must explicitly set allow_arbitrary_commands=true "
            "to authorize run_command or start_command"
        )
    return ExecutionProfile(
        name=name,
        image=image,
        tools=frozenset(tools),
        workspace_access=workspace_access,
        allow_arbitrary_commands=allow_arbitrary_commands,
    )


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskConfigurationError(f"{location} must be a JSON object")
    return value


def _known_fields(value: Mapping[str, Any], known: set[str], location: str) -> None:
    unknown = sorted(set(value).difference(known))
    if unknown:
        raise TaskConfigurationError(
            f"unknown field(s) in {location}: {', '.join(unknown)}"
        )


def _required_fields(
    value: Mapping[str, Any], required: set[str], location: str
) -> None:
    missing = sorted(required.difference(value))
    if missing:
        raise TaskConfigurationError(
            f"missing field(s) in {location}: {', '.join(missing)}"
        )


def _integer_in_range(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise TaskConfigurationError(
            f"{name} must be an integer between {minimum} and {maximum}"
        )
    return value


def _number_in_range(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise TaskConfigurationError(f"{name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise TaskConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return result
