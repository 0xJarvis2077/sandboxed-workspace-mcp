from __future__ import annotations

import asyncio
import contextlib
import http.client
import io
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from sandboxed_workspace_mcp.cli import _transport_security, main, parse_runtime
from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.server import create_server

from _mcp_assertions import require_call_tool_result, require_structured_content


class CliTests(unittest.TestCase):
    def test_invalid_http_parameters_and_allowed_hosts_are_rejected(self) -> None:
        invalid_arguments = (
            ["--port", "0"],
            ["--host", "bad host"],
            ["--path", "mcp"],
            ["--path", "/mcp?token=secret"],
            ["--allowed-host", "https://mcp.example.test"],
            ["--allowed-host", "mcp.example.test:8443"],
        )
        for arguments in invalid_arguments:
            with (
                self.subTest(arguments=arguments),
                tempfile.TemporaryDirectory() as root,
            ):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    parse_runtime(["--root", root, *arguments], {})

        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(["--root", root, "--allowed-host", "[::1]"], {})
        self.assertEqual(runtime.allowed_hosts, ("::1",))

    def test_oauth_scopes_must_cover_all_enabled_tools(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            root = base_path / "workspace"
            root.mkdir()
            task_config = base_path / "tasks.json"
            task_config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "runtime": "docker",
                        "tasks": {
                            "test": {
                                "mode": "run",
                                "image": "example.invalid/test@sha256:" + "a" * 64,
                                "argv": ["python", "-m", "unittest"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            environment = {
                "SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER": "https://idp.example.test",
                "SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE": "https://mcp.example.test",
                "MCP_PUBLIC_HOST": "https://mcp.example.test",
            }
            arguments = [
                "--root",
                str(root),
                "--transport",
                "streamable-http",
                "--allow-network",
                "--oauth",
                "--allow-git-writes",
                "--allow-trash",
                "--allow-trash-purge",
                "--task-config",
                str(task_config),
            ]
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_runtime(arguments, environment)

    def test_module_entrypoint_exits_with_cli_result(self) -> None:
        with patch("sandboxed_workspace_mcp.cli.main", return_value=0) as cli_main:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module(
                    "sandboxed_workspace_mcp.__main__", run_name="__main__"
                )

        self.assertEqual(raised.exception.code, 0)
        cli_main.assert_called_once_with()

    def test_environment_config_and_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(
                [
                    "--read-only",
                    "--max-output-size",
                    "1234",
                    "--block-path",
                    "private/**",
                ],
                {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "SANDBOXED_WORKSPACE_MCP_BLOCKED_PATHS": "*.token",
                    "SANDBOXED_WORKSPACE_MCP_MAX_SCAN_ENTRIES": "4321",
                },
            )

        self.assertEqual(runtime.transport, "stdio")
        self.assertFalse(runtime.settings.allow_writes)
        self.assertEqual(runtime.settings.max_output_size, 1234)
        self.assertEqual(runtime.settings.max_scan_entries, 4321)
        self.assertIn("*.token", runtime.settings.blocked_patterns)
        self.assertIn("private/**", runtime.settings.blocked_patterns)

    def test_git_write_flag_and_environment_require_writable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(
                ["--allow-git-writes"],
                {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "SANDBOXED_WORKSPACE_MCP_MAX_GIT_BASELINE_FILES": "12",
                    "SANDBOXED_WORKSPACE_MCP_MAX_GIT_BASELINE_BYTES": "3456",
                },
            )
            self.assertTrue(runtime.settings.allow_git_writes)
            self.assertEqual(runtime.settings.max_git_baseline_files, 12)
            self.assertEqual(runtime.settings.max_git_baseline_bytes, 3456)

            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_runtime(
                    ["--read-only", "--allow-git-writes"],
                    {"SANDBOXED_WORKSPACE_MCP_ROOT": root},
                )

    def test_root_is_required(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_runtime([], {})

    def test_module_entrypoint_reports_version(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root / "src")
        result = subprocess.run(
            [sys.executable, "-m", "sandboxed_workspace_mcp", "--version"],
            cwd=project_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("sandboxed-workspace-mcp 0.2.0", result.stdout)

    def test_invalid_environment_values_are_reported_as_cli_errors(self) -> None:
        invalid_values = (
            ("SANDBOXED_WORKSPACE_MCP_TRANSPORT", "invalid"),
            ("SANDBOXED_WORKSPACE_MCP_READ_ONLY", "maybe"),
            ("SANDBOXED_WORKSPACE_MCP_BLOCKED_PATHS", "valid,,empty"),
        )
        for name, value in invalid_values:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                environment = {"SANDBOXED_WORKSPACE_MCP_ROOT": root, name: value}
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    parse_runtime([], environment)

    def test_non_loopback_http_requires_explicit_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_runtime(
                    [
                        "--root",
                        root,
                        "--transport",
                        "streamable-http",
                        "--host",
                        "0.0.0.0",
                    ],
                    {},
                )

            runtime = parse_runtime(
                [
                    "--root",
                    root,
                    "--transport",
                    "streamable-http",
                    "--host",
                    "192.0.2.1",
                    "--allow-network",
                    "--allow-unauthenticated-http",
                ],
                {},
            )

        self.assertTrue(runtime.allow_network)

    def test_wildcard_bind_requires_explicit_allowed_hosts(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                parse_runtime(
                    [
                        "--root",
                        root,
                        "--transport",
                        "streamable-http",
                        "--host",
                        "0.0.0.0",
                        "--allow-network",
                    ],
                    {},
                )

            runtime = parse_runtime(
                [
                    "--root",
                    root,
                    "--transport",
                    "streamable-http",
                    "--host",
                    "0.0.0.0",
                    "--allow-network",
                    "--allowed-host",
                    "mcp.example.test",
                    "--allow-unauthenticated-http",
                ],
                {},
            )

        self.assertEqual(runtime.allowed_hosts, ("mcp.example.test",))
        security = _transport_security(runtime.host, runtime.allowed_hosts)
        self.assertIn("mcp.example.test:*", security.allowed_hosts)
        self.assertNotIn("0.0.0.0:*", security.allowed_hosts)

    def test_public_host_environment_adds_transport_allowlist(self) -> None:
        public_host = "tunnel.example.test"
        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(
                [
                    "--allowed-host",
                    public_host,
                    "--allow-unauthenticated-http",
                ],
                {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "SANDBOXED_WORKSPACE_MCP_TRANSPORT": "streamable-http",
                    "MCP_PUBLIC_HOST": f"https://{public_host}/",
                },
            )

        self.assertEqual(runtime.allowed_hosts, (public_host,))
        security = _transport_security(runtime.host, runtime.allowed_hosts)
        self.assertIn(f"{public_host}:*", security.allowed_hosts)
        self.assertIn(f"https://{public_host}:*", security.allowed_origins)

    def test_public_host_can_authorize_wildcard_bind(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(
                [
                    "--host",
                    "0.0.0.0",
                    "--allow-network",
                    "--allow-unauthenticated-http",
                ],
                {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "SANDBOXED_WORKSPACE_MCP_TRANSPORT": "streamable-http",
                    "MCP_PUBLIC_HOST": "https://mcp.example.test:8443",
                },
            )

        self.assertEqual(runtime.allowed_hosts, ("mcp.example.test",))

    def test_invalid_public_host_is_rejected_for_http_only(self) -> None:
        invalid_origins = (
            "mcp.example.test",
            "ftp://mcp.example.test",
            "https://user@mcp.example.test",
            "https://mcp.example.test/mcp",
            "https://mcp.example.test?token=secret",
            "https://mcp.example.test:99999",
        )
        for origin in invalid_origins:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as root:
                environment = {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "SANDBOXED_WORKSPACE_MCP_TRANSPORT": "streamable-http",
                    "MCP_PUBLIC_HOST": origin,
                }
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    parse_runtime([], environment)

        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(
                [],
                {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "MCP_PUBLIC_HOST": "not-an-http-origin",
                },
            )
        self.assertEqual(runtime.transport, "stdio")

    def test_oauth_http_configuration_is_complete_and_origin_bound(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            environment = {
                "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                "SANDBOXED_WORKSPACE_MCP_TRANSPORT": "streamable-http",
                "MCP_PUBLIC_HOST": "https://MCP.example.test:443/",
                "SANDBOXED_WORKSPACE_MCP_OAUTH_ENABLED": "true",
                "SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER": "https://idp.example.test/tenant/",
                "SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE": "https://mcp.example.test",
            }
            runtime = parse_runtime([], environment)

            self.assertIsNotNone(runtime.oauth)
            assert runtime.oauth is not None
            self.assertEqual(runtime.public_origin, "https://mcp.example.test")
            self.assertEqual(runtime.oauth.audience, runtime.public_origin)
            self.assertEqual(runtime.oauth.issuer, "https://idp.example.test/tenant/")

            for name, value in (
                ("SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER", ""),
                (
                    "SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE",
                    "https://other.example.test",
                ),
            ):
                invalid = dict(environment)
                invalid[name] = value
                with (
                    self.subTest(name=name),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    parse_runtime([], invalid)

    def test_public_http_requires_oauth_unless_escape_hatch_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            environment = {
                "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                "SANDBOXED_WORKSPACE_MCP_TRANSPORT": "streamable-http",
                "MCP_PUBLIC_HOST": "https://mcp.example.test",
            }
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_runtime([], environment)

            runtime = parse_runtime(["--allow-unauthenticated-http"], environment)
            self.assertTrue(runtime.allow_unauthenticated_http)
            self.assertIsNone(runtime.oauth)

    def test_transport_security_accepts_only_expected_local_hosts(self) -> None:
        security = _transport_security("127.0.0.1")

        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertIn("127.0.0.1:*", security.allowed_hosts)
        self.assertIn("localhost:*", security.allowed_hosts)
        self.assertNotIn("0.0.0.0:*", security.allowed_hosts)

        ipv6_security = _transport_security("::1")
        self.assertIn("[::1]:*", ipv6_security.allowed_hosts)

    def test_main_dispatches_stdio_without_writing_to_stdout(self) -> None:
        fake_server = Mock()
        with (
            tempfile.TemporaryDirectory() as root,
            patch(
                "sandboxed_workspace_mcp.cli.create_server", return_value=fake_server
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            result = main(["--root", root])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")
        fake_server.run.assert_called_once_with(transport="stdio")

    def test_main_configures_http_transport_and_warns_for_network_bind(self) -> None:
        fake_server = Mock()
        with (
            tempfile.TemporaryDirectory() as root,
            patch(
                "sandboxed_workspace_mcp.cli.create_server", return_value=fake_server
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = main(
                [
                    "--root",
                    root,
                    "--transport",
                    "streamable-http",
                    "--host",
                    "192.0.2.1",
                    "--allow-network",
                    "--allow-unauthenticated-http",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn("SECURITY WARNING", stderr.getvalue())
        call = fake_server.run.call_args
        self.assertEqual(call.kwargs["transport"], "streamable-http")
        self.assertTrue(
            call.kwargs["transport_security"].enable_dns_rebinding_protection
        )


class ServerTests(unittest.TestCase):
    def test_all_expected_tools_are_registered_with_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root))
            tools = asyncio.run(server.list_tools())

        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(
            set(by_name),
            {
                "append_file",
                "create_directory",
                "git_diff",
                "git_branch",
                "git_log",
                "git_ls_files",
                "git_read_file_at_revision",
                "git_rev_parse",
                "git_show",
                "git_status",
                "workspace_diff",
                "list_directory",
                "project_info",
                "read_file",
                "read_file_versioned",
                "replace_text",
                "run_shell",
                "search_text",
                "tree",
                "write_file",
            },
        )
        read_annotations = by_name["read_file"].annotations
        write_annotations = by_name["write_file"].annotations
        assert read_annotations is not None
        assert write_annotations is not None
        self.assertTrue(read_annotations.read_only_hint)
        self.assertFalse(write_annotations.read_only_hint)
        self.assertTrue(write_annotations.destructive_hint)

    def test_read_only_server_does_not_register_mutating_tools(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root, allow_writes=False))
            tools = asyncio.run(server.list_tools())

        names = {tool.name for tool in tools}
        self.assertTrue(
            {
                "git_diff",
                "workspace_diff",
                "git_branch",
                "git_log",
                "git_ls_files",
                "git_read_file_at_revision",
                "git_rev_parse",
                "git_show",
                "git_status",
                "list_directory",
                "project_info",
                "read_file",
                "run_shell",
                "search_text",
                "tree",
            }.issubset(names)
        )
        self.assertTrue(
            names.isdisjoint(
                {
                    "append_file",
                    "create_directory",
                    "replace_text",
                    "write_file",
                }
            )
        )

    def test_registered_tools_reach_the_application_service(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root))

            async def exercise_tools() -> None:
                initial_calls: tuple[tuple[str, dict[str, str]], ...] = (
                    ("project_info", {}),
                    ("create_directory", {"path": "src"}),
                    ("write_file", {"path": "src/file.txt", "content": "one\n"}),
                )
                for name, arguments in initial_calls:
                    with self.subTest(tool=name):
                        result = require_call_tool_result(
                            await server.call_tool(name, arguments)
                        )
                        self.assertFalse(result.is_error)

                versioned = require_call_tool_result(
                    await server.call_tool(
                        "read_file_versioned", {"path": "src/file.txt"}
                    )
                )
                sha256 = require_structured_content(versioned)["sha256"]
                appended = require_call_tool_result(
                    await server.call_tool(
                        "append_file",
                        {
                            "path": "src/file.txt",
                            "content": "two\n",
                            "expected_sha256": sha256,
                        },
                    )
                )
                self.assertFalse(appended.is_error)

                versioned = require_call_tool_result(
                    await server.call_tool(
                        "read_file_versioned", {"path": "src/file.txt"}
                    )
                )
                sha256 = require_structured_content(versioned)["sha256"]
                replaced = require_call_tool_result(
                    await server.call_tool(
                        "replace_text",
                        {
                            "path": "src/file.txt",
                            "old_text": "one",
                            "new_text": "first",
                            "expected_sha256": sha256,
                        },
                    )
                )
                self.assertFalse(replaced.is_error)

                final_calls: tuple[tuple[str, dict[str, str | int]], ...] = (
                    ("read_file", {"path": "src/file.txt"}),
                    ("list_directory", {"path": "src"}),
                    ("tree", {"path": ".", "max_depth": 2}),
                    ("search_text", {"text": "first", "path": "."}),
                    ("run_shell", {"command": "pwd"}),
                )
                for name, final_arguments in final_calls:
                    with self.subTest(tool=name):
                        result = require_call_tool_result(
                            await server.call_tool(name, final_arguments)
                        )
                        self.assertFalse(result.is_error)

            asyncio.run(exercise_tools())


class StdioIntegrationTests(unittest.TestCase):
    def test_source_entrypoint_completes_an_mcp_handshake(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            tools, result, git_result, error_output = asyncio.run(
                self._exercise_stdio_server(project_root, root)
            )

        tool_names = {tool.name for tool in tools.tools}
        self.assertIn("read_file", tool_names)
        self.assertNotIn("write_file", tool_names)
        self.assertFalse(result.is_error)
        self.assertTrue(git_result.is_error)
        self.assertIn("not a git repository", git_result.content[0].text)
        self.assertIn("Mode: read-only", result.content[0].text)
        self.assertEqual(error_output, "")

    def test_task_config_stdio_discovers_only_narrow_task_tools(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as base:
            base_path = Path(base)
            root = base_path / "workspace"
            root.mkdir()
            task_config = base_path / "tasks.json"
            task_config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "runtime": "docker",
                        "tasks": {
                            "test": {
                                "mode": "run",
                                "image": (
                                    "example.invalid/sandboxed-workspace-mcp@sha256:"
                                    + "d" * 64
                                ),
                                "argv": ["python", "-m", "unittest"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            async def list_enabled_tools():
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        str(project_root / "server.py"),
                        "--root",
                        str(root),
                        "--read-only",
                        "--task-config",
                        str(task_config),
                    ],
                    cwd=project_root,
                )
                async with (
                    stdio_client(parameters) as (read_stream, write_stream),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    return await session.list_tools()

            tools = asyncio.run(list_enabled_tools())

        names = {tool.name for tool in tools.tools}
        self.assertIn("run_task", names)
        self.assertIn("list_tasks", names)
        self.assertNotIn("run_pytest", names)
        self.assertNotIn("run_python_script", names)
        self.assertNotIn("write_file", names)
        run_schema = next(tool for tool in tools.tools if tool.name == "run_task")
        self.assertFalse(run_schema.input_schema["additionalProperties"])

    @staticmethod
    async def _exercise_stdio_server(project_root: Path, root: str):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(project_root / "server.py"), "--root", root, "--read-only"],
            cwd=project_root,
        )
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
            async with (
                stdio_client(parameters, errlog=errors) as (
                    read_stream,
                    write_stream,
                ),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("project_info")
                git_result = await session.call_tool("git_status")
            errors.seek(0)
            return tools, result, git_result, errors.read()


class StreamableHttpIntegrationTests(unittest.TestCase):
    def test_http_handshake_and_host_origin_rejection(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            port = self._unused_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(project_root / "server.py"),
                    "--root",
                    root,
                    "--read-only",
                    "--transport",
                    "streamable-http",
                    "--port",
                    str(port),
                ],
                cwd=project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self._wait_for_port(process, port)
                tools = asyncio.run(self._exercise_http(f"http://127.0.0.1:{port}/mcp"))
                self.assertIn("read_file", {tool.name for tool in tools.tools})

                bad_host_status = self._raw_post_status(
                    port,
                    host="attacker.invalid",
                    origin=f"http://127.0.0.1:{port}",
                )
                bad_origin_status = self._raw_post_status(
                    port,
                    host=f"127.0.0.1:{port}",
                    origin="https://attacker.invalid",
                )
                self.assertGreaterEqual(bad_host_status, 400)
                self.assertGreaterEqual(bad_origin_status, 400)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stderr is not None:
                    process.stderr.close()

    @staticmethod
    async def _exercise_http(url: str):
        async with (
            streamable_http_client(url) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            return await session.list_tools()

    @staticmethod
    def _unused_port() -> int:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                return listener.getsockname()[1]
        except PermissionError as exc:
            raise unittest.SkipTest(
                f"sandbox does not permit loopback sockets: {exc}"
            ) from exc

    @staticmethod
    def _wait_for_port(process: subprocess.Popen[str], port: int) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                error = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"HTTP server exited early: {error}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("HTTP server did not start within 8 seconds")

    @staticmethod
    def _raw_post_status(port: int, *, host: str, origin: str) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request(
                "POST",
                "/mcp",
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Host": host,
                    "Origin": origin,
                },
            )
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
