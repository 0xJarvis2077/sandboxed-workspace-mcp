from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import jwt
from _mcp_assertions import (
    require_call_tool_result,
    require_resource_contents,
    require_structured_content,
)
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import Tool
from starlette.applications import Starlette
from starlette.types import Message

from workspace_guard_mcp.config import Settings
from workspace_guard_mcp.execution_backend import ExecutionRequest
from workspace_guard_mcp.oauth import (
    DEFAULT_OAUTH_SCOPES,
    JWTTokenVerifier,
    OAuthSettings,
)
from workspace_guard_mcp.server import create_server
from workspace_guard_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from workspace_guard_mcp.task_manager import TaskManager

ISSUER = "https://idp.example.test/tenant"
RESOURCE = "https://mcp.example.test"
JWKS_URI = "https://idp.example.test/keys"
IMAGE = "example.invalid/workspace-guard-mcp@sha256:" + "f" * 64


class _ImmediateHandle:
    def wait(self, timeout: float | None = None) -> int:
        return 0

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    def start(self, request, on_stdout, on_stderr):
        self.requests.append(request)
        on_stdout(b"ok\n")
        return _ImmediateHandle()


def _oauth() -> OAuthSettings:
    return OAuthSettings(
        issuer=ISSUER,
        audience=RESOURCE,
        public_origin=RESOURCE,
        jwks_uri=JWKS_URI,
    )


def _security_schemes(tool: Tool) -> object:
    meta = tool.meta
    assert meta is not None
    return meta["securitySchemes"]


def _rsa_key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


def _ec_key(kid: str):
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return private, jwk


def _token(
    private,
    kid: str,
    *,
    algorithm: str = "RS256",
    issuer: str = ISSUER,
    audience: str = RESOURCE,
    expires_at: int | None = None,
    not_before: int | None = None,
    scopes: str = "workspace.read",
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": "test-user",
        "client_id": "chatgpt-test-client",
        "scope": scopes,
        "iat": now,
        "exp": expires_at if expires_at is not None else now + 300,
    }
    if not_before is not None:
        claims["nbf"] = not_before
    return jwt.encode(
        claims,
        private,
        algorithm=algorithm,
        headers={"kid": kid, "typ": "at+jwt"},
    )


async def _asgi_request(
    app,
    method: str,
    path: str,
    *,
    authorization: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = [(b"host", b"mcp.example.test")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("mcp.example.test", 443),
    }
    received = False
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start["headers"]
    }
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], response_headers, body


class JWTVerifierTests(unittest.TestCase):
    def test_valid_rsa_and_ec_tokens_are_verified_without_retaining_token(self) -> None:
        for algorithm, factory in (("RS256", _rsa_key), ("ES256", _ec_key)):
            with self.subTest(algorithm=algorithm):
                private, jwk = factory(f"{algorithm}-key")
                verifier = JWTTokenVerifier(_oauth())
                encoded = _token(
                    private,
                    jwk["kid"],
                    algorithm=algorithm,
                    scopes="workspace.read workspace.write",
                )

                with patch.object(
                    verifier, "_fetch_json", AsyncMock(return_value={"keys": [jwk]})
                ):
                    result = asyncio.run(verifier.verify_token(encoded))

                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.token, "")
                self.assertEqual(result.scopes, ["workspace.read", "workspace.write"])
                self.assertNotIn(encoded, repr(result))

    def test_bad_signature_issuer_audience_expiry_and_nbf_are_rejected(self) -> None:
        private, jwk = _rsa_key("primary")
        wrong_private, _ = _rsa_key("wrong")
        now = int(time.time())
        candidates = {
            "signature": _token(wrong_private, "primary"),
            "issuer": _token(private, "primary", issuer="https://wrong.example"),
            "audience": _token(private, "primary", audience="https://wrong.example"),
            "expired": _token(private, "primary", expires_at=now - 1),
            "nbf": _token(private, "primary", not_before=now + 300),
        }
        for case, encoded in candidates.items():
            with self.subTest(case=case):
                verifier = JWTTokenVerifier(_oauth())
                with patch.object(
                    verifier, "_fetch_json", AsyncMock(return_value={"keys": [jwk]})
                ):
                    self.assertIsNone(asyncio.run(verifier.verify_token(encoded)))

    def test_unknown_kid_refreshes_jwks_once_and_discovery_is_bounded(self) -> None:
        private, wanted = _rsa_key("rotated")
        _, stale = _rsa_key("stale")
        verifier = JWTTokenVerifier(_oauth())
        fetch_json = AsyncMock(side_effect=({"keys": [stale]}, {"keys": [wanted]}))

        with patch.object(verifier, "_fetch_json", fetch_json):
            result = asyncio.run(
                verifier.verify_token(_token(private, "rotated", scopes="tasks.run"))
            )

        self.assertIsNotNone(result)
        self.assertEqual(fetch_json.await_count, 2)

        settings = OAuthSettings(
            issuer=ISSUER,
            audience=RESOURCE,
            public_origin=RESOURCE,
            jwks_uri=None,
        )
        discovered = JWTTokenVerifier(settings)
        discovered_fetch_json = AsyncMock(
            side_effect=(
                {"issuer": ISSUER, "jwks_uri": JWKS_URI},
                {"keys": [wanted]},
            )
        )
        with patch.object(discovered, "_fetch_json", discovered_fetch_json):
            self.assertIsNotNone(
                asyncio.run(
                    discovered.verify_token(
                        _token(private, "rotated", scopes="workspace.read")
                    )
                )
            )

    def test_issuer_trailing_slash_and_scp_string_are_preserved(self) -> None:
        issuer = "https://idp.example.test/tenant/"
        settings = OAuthSettings(
            issuer=issuer,
            audience=RESOURCE,
            public_origin=RESOURCE,
            jwks_uri=JWKS_URI,
        )
        private, jwk = _rsa_key("slash")
        verifier = JWTTokenVerifier(settings)
        now = int(time.time())
        encoded = jwt.encode(
            {
                "iss": issuer,
                "aud": RESOURCE,
                "sub": "test-user",
                "scp": "workspace.read tasks.read",
                "exp": now + 300,
            },
            private,
            algorithm="RS256",
            headers={"kid": "slash"},
        )

        with patch.object(
            verifier, "_fetch_json", AsyncMock(return_value={"keys": [jwk]})
        ):
            result = asyncio.run(verifier.verify_token(encoded))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.scopes, ["workspace.read", "tasks.read"])
        self.assertEqual(settings.issuer, issuer)


class OAuthServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        (self.root / "file.txt").write_text("content\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(self, backend: _FakeBackend) -> TaskManager:
        configuration = TaskConfiguration(
            source=self.base / "tasks.json",
            runtime="docker",
            limits=TaskLimits(timeout_seconds=2, max_output_bytes=4096),
            tasks=MappingProxyType(
                {
                    "test": TaskDefinition(
                        "test", "run", IMAGE, ("python", "-m", "unittest")
                    )
                }
            ),
            profiles=MappingProxyType(
                {
                    "debug": ExecutionProfile(
                        "debug",
                        IMAGE,
                        frozenset({"python_version", "run_command", "start_command"}),
                        allow_arbitrary_commands=True,
                    )
                }
            ),
        )
        return TaskManager(Settings.create(self.root), configuration, backend=backend)

    def test_tool_scopes_are_declared_and_rechecked_server_side(self) -> None:
        backend = _FakeBackend()
        manager = self._manager(backend)
        server = create_server(
            Settings.create(self.root), task_manager=manager, oauth=_oauth()
        )
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(
            _security_schemes(by_name["read_file"]),
            [{"type": "oauth2", "scopes": ["workspace.read"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["git_show"]),
            [{"type": "oauth2", "scopes": ["workspace.read"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["workspace_diff"]),
            [{"type": "oauth2", "scopes": ["workspace.read"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["write_file"]),
            [{"type": "oauth2", "scopes": ["workspace.write"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["run_task"]),
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["python_version"]),
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["run_command"]),
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["start_command"]),
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            _security_schemes(by_name["list_execution_profiles"]),
            [{"type": "oauth2", "scopes": ["tasks.read"]}],
        )
        for name in ("execution_status", "execution_events"):
            self.assertEqual(
                _security_schemes(by_name[name]),
                [{"type": "oauth2", "scopes": ["tasks.read"]}],
            )

        read = AccessToken(token="", client_id="client", scopes=["workspace.read"])
        write = AccessToken(token="", client_id="client", scopes=["workspace.write"])
        task = AccessToken(token="", client_id="client", scopes=["tasks.run"])

        async def exercise() -> None:
            with patch(
                "workspace_guard_mcp.server.get_access_token", return_value=None
            ):
                missing = require_call_tool_result(
                    await server.call_tool("project_info", {})
                )
                self.assertTrue(missing.is_error)
                missing_meta = missing.meta
                assert missing_meta is not None
                self.assertIn("mcp/www_authenticate", missing_meta)

            with patch(
                "workspace_guard_mcp.server.get_access_token", return_value=read
            ):
                allowed = require_call_tool_result(
                    await server.call_tool("read_file", {"path": "file.txt"})
                )
                denied = require_call_tool_result(
                    await server.call_tool(
                        "write_file", {"path": "new.txt", "content": "new"}
                    )
                )
                self.assertFalse(allowed.is_error)
                self.assertTrue(denied.is_error)
                self.assertIn("insufficient_scope", repr(denied.meta))

            with patch(
                "workspace_guard_mcp.server.get_access_token", return_value=write
            ):
                written = require_call_tool_result(
                    await server.call_tool(
                        "write_file", {"path": "new.txt", "content": "new"}
                    )
                )
                denied = require_call_tool_result(
                    await server.call_tool("run_task", {"name": "test"})
                )
                self.assertFalse(written.is_error)
                self.assertTrue(denied.is_error)

            with patch(
                "workspace_guard_mcp.server.get_access_token", return_value=task
            ):
                run = require_call_tool_result(
                    await server.call_tool("run_task", {"name": "test"})
                )
                version = require_call_tool_result(
                    await server.call_tool("python_version", {"profile": "debug"})
                )
                command = require_call_tool_result(
                    await server.call_tool(
                        "run_command",
                        {
                            "profile": "debug",
                            "program": "ruff",
                            "args": ["check", "."],
                        },
                    )
                )
                self.assertFalse(run.is_error)
                self.assertFalse(version.is_error)
                self.assertFalse(command.is_error)

        asyncio.run(exercise())
        self.assertEqual(len(backend.requests), 3)
        manager.shutdown()

    def test_git_write_scope_is_opt_in_and_not_replaced_by_workspace_write(
        self,
    ) -> None:
        self.assertNotIn("workspace.git.write", DEFAULT_OAUTH_SCOPES)
        enabled = Settings.create(self.root, allow_git_writes=True)
        with self.assertRaisesRegex(ValueError, "workspace.git.write"):
            create_server(enabled, oauth=_oauth())

        git_oauth = OAuthSettings(
            issuer=ISSUER,
            audience=RESOURCE,
            public_origin=RESOURCE,
            jwks_uri=JWKS_URI,
            scopes=(*DEFAULT_OAUTH_SCOPES, "workspace.git.write"),
        )
        server = create_server(enabled, oauth=git_oauth)
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(
            _security_schemes(by_name["git_init"]),
            [{"type": "oauth2", "scopes": ["workspace.git.write"]}],
        )
        write = AccessToken(token="", client_id="client", scopes=["workspace.write"])
        git_write = AccessToken(
            token="", client_id="client", scopes=["workspace.git.write"]
        )

        async def exercise() -> None:
            with patch(
                "workspace_guard_mcp.server.get_access_token", return_value=write
            ):
                denied = require_call_tool_result(
                    await server.call_tool("git_init", {})
                )
                self.assertTrue(denied.is_error)
                self.assertIn("insufficient_scope", repr(denied.meta))
            with patch(
                "workspace_guard_mcp.server.get_access_token",
                return_value=git_write,
            ):
                allowed = require_call_tool_result(
                    await server.call_tool("git_init", {})
                )
                self.assertFalse(allowed.is_error)

        asyncio.run(exercise())

    def test_ephemeral_results_are_scoped_to_authenticated_owner(self) -> None:
        large_text = "owner-safe\n" * 4000
        (self.root / "large.txt").write_text(large_text, encoding="utf-8")
        server = create_server(
            Settings.create(self.root, allow_writes=False), oauth=_oauth()
        )
        owner = AccessToken(
            token="",
            client_id="client-a",
            subject="user-a",
            scopes=["workspace.read"],
        )
        other_subject = AccessToken(
            token="",
            client_id="client-a",
            subject="user-b",
            scopes=["workspace.read"],
        )
        other_client = AccessToken(
            token="",
            client_id="client-b",
            subject="user-a",
            scopes=["workspace.read"],
        )

        async def exercise() -> None:
            with patch(
                "workspace_guard_mcp.server.get_access_token", return_value=owner
            ):
                result = require_call_tool_result(
                    await server.call_tool("read_file", {"path": "large.txt"})
                )
                structured = require_structured_content(result)
                uri = structured["content_resource_uri"]
                assert isinstance(uri, str)
                contents = require_resource_contents(await server.read_resource(uri))
                self.assertEqual(contents[0].content, large_text)

            for token in (other_subject, other_client, None):
                with patch(
                    "workspace_guard_mcp.server.get_access_token",
                    return_value=token,
                ):
                    with self.assertRaises(ResourceNotFoundError):
                        await server.read_resource(uri)

        asyncio.run(exercise())

    def test_resource_metadata_and_http_challenge_trigger_oauth_discovery(self) -> None:
        verifier = JWTTokenVerifier(_oauth())
        server = create_server(
            Settings.create(self.root, allow_writes=False),
            oauth=_oauth(),
            token_verifier=verifier,
        )
        app = server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
        self.assertIsInstance(app, Starlette)

        status, headers, body = asyncio.run(
            _asgi_request(app, "GET", "/.well-known/oauth-protected-resource")
        )
        self.assertEqual(status, 200)
        metadata = json.loads(body)
        self.assertEqual(metadata["resource"], RESOURCE)
        self.assertEqual(metadata["authorization_servers"], [ISSUER])
        self.assertIn("workspace.read", metadata["scopes_supported"])

        status, headers, body = asyncio.run(_asgi_request(app, "POST", "/mcp"))
        self.assertEqual(status, 401)
        challenge = headers["www-authenticate"]
        self.assertIn(_oauth().resource_metadata_url, challenge)
        self.assertIn("invalid_token", challenge)

        secret_token = "not-a-valid.jwt.token"
        status, headers, body = asyncio.run(
            _asgi_request(app, "POST", "/mcp", authorization=f"Bearer {secret_token}")
        )
        self.assertEqual(status, 401)
        self.assertNotIn(secret_token, repr((headers, body)))

    def test_streamable_http_app_forwards_current_sdk_parameters(self) -> None:
        server = create_server(Settings.create(self.root, allow_writes=False))
        parent_app = Starlette()
        with patch.object(
            MCPServer,
            "streamable_http_app",
            return_value=parent_app,
        ) as parent:
            app = server.streamable_http_app(
                streamable_http_path="/custom-mcp",
                json_response=True,
                stateless_http=True,
                event_store=None,
                retry_interval=17,
                max_request_body_size=12345,
                transport_security=None,
                host="localhost",
            )

        self.assertIs(app, parent_app)
        parent.assert_called_once_with(
            streamable_http_path="/custom-mcp",
            json_response=True,
            stateless_http=True,
            event_store=None,
            retry_interval=17,
            max_request_body_size=12345,
            transport_security=None,
            host="localhost",
        )


if __name__ == "__main__":
    unittest.main()
