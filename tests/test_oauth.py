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
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from sandboxed_workspace_mcp.config import Settings
from sandboxed_workspace_mcp.oauth import (
    DEFAULT_OAUTH_SCOPES,
    JWTTokenVerifier,
    OAuthSettings,
)
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.task_config import (
    ExecutionProfile,
    TaskConfiguration,
    TaskDefinition,
    TaskLimits,
)
from sandboxed_workspace_mcp.task_manager import TaskManager
from sandboxed_workspace_mcp.task_runner import ContainerRequest

ISSUER = "https://idp.example.test/tenant"
RESOURCE = "https://mcp.example.test"
JWKS_URI = "https://idp.example.test/keys"
IMAGE = "example.invalid/sandboxed-workspace-mcp@sha256:" + "f" * 64


class _ImmediateHandle:
    def wait(self, timeout: float | None = None) -> int:
        return 0

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ContainerRequest] = []

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
    messages: list[dict[str, object]] = []

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
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
                verifier._fetch_json = AsyncMock(return_value={"keys": [jwk]})
                encoded = _token(
                    private,
                    jwk["kid"],
                    algorithm=algorithm,
                    scopes="workspace.read workspace.write",
                )

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
                verifier._fetch_json = AsyncMock(return_value={"keys": [jwk]})
                self.assertIsNone(asyncio.run(verifier.verify_token(encoded)))

    def test_unknown_kid_refreshes_jwks_once_and_discovery_is_bounded(self) -> None:
        private, wanted = _rsa_key("rotated")
        _, stale = _rsa_key("stale")
        verifier = JWTTokenVerifier(_oauth())
        verifier._fetch_json = AsyncMock(
            side_effect=({"keys": [stale]}, {"keys": [wanted]})
        )

        result = asyncio.run(
            verifier.verify_token(_token(private, "rotated", scopes="tasks.run"))
        )

        self.assertIsNotNone(result)
        self.assertEqual(verifier._fetch_json.await_count, 2)

        settings = OAuthSettings(
            issuer=ISSUER,
            audience=RESOURCE,
            public_origin=RESOURCE,
            jwks_uri=None,
        )
        discovered = JWTTokenVerifier(settings)
        discovered._fetch_json = AsyncMock(
            side_effect=(
                {"issuer": ISSUER, "jwks_uri": JWKS_URI},
                {"keys": [wanted]},
            )
        )
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
        verifier._fetch_json = AsyncMock(return_value={"keys": [jwk]})
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
            by_name["read_file"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.read"]}],
        )
        self.assertEqual(
            by_name["git_show"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.read"]}],
        )
        self.assertEqual(
            by_name["workspace_diff"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.read"]}],
        )
        self.assertEqual(
            by_name["write_file"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.write"]}],
        )
        self.assertEqual(
            by_name["run_task"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            by_name["python_version"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            by_name["run_command"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            by_name["start_command"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["tasks.run"]}],
        )
        self.assertEqual(
            by_name["list_execution_profiles"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["tasks.read"]}],
        )

        read = AccessToken(token="", client_id="client", scopes=["workspace.read"])
        write = AccessToken(token="", client_id="client", scopes=["workspace.write"])
        task = AccessToken(token="", client_id="client", scopes=["tasks.run"])

        async def exercise() -> None:
            with patch(
                "sandboxed_workspace_mcp.server.get_access_token", return_value=None
            ):
                missing = await server.call_tool("project_info", {})
                self.assertTrue(missing.is_error)
                self.assertIn("mcp/www_authenticate", missing.meta)

            with patch(
                "sandboxed_workspace_mcp.server.get_access_token", return_value=read
            ):
                allowed = await server.call_tool("read_file", {"path": "file.txt"})
                denied = await server.call_tool(
                    "write_file", {"path": "new.txt", "content": "new"}
                )
                self.assertFalse(allowed.is_error)
                self.assertTrue(denied.is_error)
                self.assertIn("insufficient_scope", repr(denied.meta))

            with patch(
                "sandboxed_workspace_mcp.server.get_access_token", return_value=write
            ):
                written = await server.call_tool(
                    "write_file", {"path": "new.txt", "content": "new"}
                )
                denied = await server.call_tool("run_task", {"name": "test"})
                self.assertFalse(written.is_error)
                self.assertTrue(denied.is_error)

            with patch(
                "sandboxed_workspace_mcp.server.get_access_token", return_value=task
            ):
                run = await server.call_tool("run_task", {"name": "test"})
                version = await server.call_tool("python_version", {"profile": "debug"})
                command = await server.call_tool(
                    "run_command",
                    {
                        "profile": "debug",
                        "program": "ruff",
                        "args": ["check", "."],
                    },
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
            by_name["git_init"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.git.write"]}],
        )
        write = AccessToken(token="", client_id="client", scopes=["workspace.write"])
        git_write = AccessToken(
            token="", client_id="client", scopes=["workspace.git.write"]
        )

        async def exercise() -> None:
            with patch(
                "sandboxed_workspace_mcp.server.get_access_token", return_value=write
            ):
                denied = await server.call_tool("git_init", {})
                self.assertTrue(denied.is_error)
                self.assertIn("insufficient_scope", repr(denied.meta))
            with patch(
                "sandboxed_workspace_mcp.server.get_access_token",
                return_value=git_write,
            ):
                allowed = await server.call_tool("git_init", {})
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
                "sandboxed_workspace_mcp.server.get_access_token", return_value=owner
            ):
                result = await server.call_tool("read_file", {"path": "large.txt"})
                uri = result.structured_content["content_resource_uri"]
                contents = await server.read_resource(uri)
                self.assertEqual(contents[0].content, large_text)

            for token in (other_subject, other_client, None):
                with patch(
                    "sandboxed_workspace_mcp.server.get_access_token",
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


if __name__ == "__main__":
    unittest.main()
