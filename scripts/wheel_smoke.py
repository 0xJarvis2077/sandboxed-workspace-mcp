"""Install a built wheel into an isolated environment and enumerate MCP tools."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)

    with tempfile.TemporaryDirectory() as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            cwd=directory,
            check=True,
        )
        executable = environment / (
            "Scripts/sandboxed-workspace-mcp.exe"
            if os.name == "nt"
            else "bin/sandboxed-workspace-mcp"
        )
        version = subprocess.run(
            [str(executable), "--version"],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
        )
        if version.stdout.strip() != "sandboxed-workspace-mcp 0.2.0":
            raise RuntimeError(f"unexpected CLI version: {version.stdout!r}")
        code = """
import asyncio
import json
import tempfile
from pathlib import Path

import sandboxed_workspace_mcp
from sandboxed_workspace_mcp import Settings, create_server
from sandboxed_workspace_mcp.oauth import OAuthSettings
from sandboxed_workspace_mcp import (
    python_execution,
    safe_regex,
    task_config,
    task_manager,
    task_runner,
    task_snapshot,
)
from sandboxed_workspace_mcp.task_config import load_task_config
from sandboxed_workspace_mcp.task_manager import TaskManager

with tempfile.TemporaryDirectory() as root:
    writable = create_server(Settings.create(root, allow_writes=True))
    writable_names = {tool.name for tool in asyncio.run(writable.list_tools())}
    assert {"read_file_versioned", "write_file"}.issubset(writable_names)
    read_only = create_server(Settings.create(root, allow_writes=False))
    read_only_names = {tool.name for tool in asyncio.run(read_only.list_tools())}
    assert "read_file" in read_only_names
    assert "write_file" not in read_only_names
    oauth = OAuthSettings(
        issuer="https://idp.example.test/tenant",
        audience="https://mcp.example.test",
        public_origin="https://mcp.example.test",
        jwks_uri="https://idp.example.test/jwks",
    )
    oauth_server = create_server(Settings.create(root), oauth=oauth)
    oauth_tools = {tool.name: tool for tool in asyncio.run(oauth_server.list_tools())}
    assert oauth_tools["read_file"].meta["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["workspace.read"]}
    ]
    assert oauth_tools["write_file"].meta["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["workspace.write"]}
    ]
with tempfile.TemporaryDirectory() as base:
    base_path = Path(base)
    workspace = base_path / "workspace"
    workspace.mkdir()
    task_path = base_path / "tasks.json"
    task_path.write_text(json.dumps({
        "version": 1,
        "runtime": "docker",
        "tasks": {
            "test": {
                "mode": "run",
                "image": "example.invalid/sandboxed-workspace-mcp@sha256:" + "c" * 64,
                "argv": ["python", "-m", "unittest"],
            }
        },
    }), encoding="utf-8")
    settings = Settings.create(workspace, allow_writes=False)
    loaded_tasks = load_task_config(task_path, workspace_root=settings.root)
    manager = TaskManager(settings, loaded_tasks)
    task_server = create_server(settings, task_manager=manager, oauth=oauth)
    task_tools = {tool.name: tool for tool in asyncio.run(task_server.list_tools())}
    task_names = set(task_tools)
    assert {
        "list_tasks", "run_task", "start_task", "task_status", "task_logs", "stop_task"
    }.issubset(task_names)
    assert task_tools["list_tasks"].meta["securitySchemes"][0]["scopes"] == [
        "tasks.read"
    ]
    assert task_tools["run_task"].meta["securitySchemes"][0]["scopes"] == [
        "tasks.run"
    ]
    manager.shutdown()
    task_path.write_text(json.dumps({
        "version": 1,
        "runtime": "docker",
        "profiles": {
            "debug": {
                "image": "example.invalid/sandboxed-workspace-mcp@sha256:" + "d" * 64,
                "tools": ["python_version", "run_pytest", "run_python_script"],
            }
        },
    }), encoding="utf-8")
    loaded_profiles = load_task_config(task_path, workspace_root=settings.root)
    profile_manager = TaskManager(settings, loaded_profiles)
    profile_server = create_server(
        settings, task_manager=profile_manager, oauth=oauth
    )
    profile_tools = {
        tool.name: tool for tool in asyncio.run(profile_server.list_tools())
    }
    assert {
        "list_execution_profiles",
        "python_version",
        "run_pytest",
        "run_python_script",
    }.issubset(profile_tools)
    assert "run_task" not in profile_tools
    assert profile_tools["run_pytest"].input_schema["additionalProperties"] is False
    assert profile_tools["python_version"].meta["securitySchemes"][0]["scopes"] == [
        "tasks.run"
    ]
    profile_manager.shutdown()
package_path = Path(sandboxed_workspace_mcp.__file__).resolve()
assert "site-packages" in package_path.parts, package_path
for module in (
    python_execution,
    safe_regex,
    task_config,
    task_manager,
    task_runner,
    task_snapshot,
):
    assert "site-packages" in Path(module.__file__).resolve().parts, module.__file__
print(f"wheel import: {package_path}")
print(f"writable tools: {','.join(sorted(writable_names))}")
print(f"read-only tools: {','.join(sorted(read_only_names))}")
print(f"task tools: {','.join(sorted(task_names))}")
"""
        subprocess.run([str(python), "-c", code], cwd=directory, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
