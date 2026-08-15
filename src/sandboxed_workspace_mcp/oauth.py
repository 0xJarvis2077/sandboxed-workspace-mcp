"""OAuth 2.1 resource-server configuration and bounded JWT verification."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from mcp.server.auth.provider import AccessToken

SUPPORTED_SCOPES = frozenset(
    {"workspace.read", "workspace.write", "tasks.read", "tasks.run"}
)
DEFAULT_OAUTH_SCOPES = (
    "workspace.read",
    "workspace.write",
    "tasks.read",
    "tasks.run",
)
_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024


class OAuthConfigurationError(ValueError):
    """Raised when OAuth resource-server settings are incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    """Validated settings for an external OAuth/OIDC provider."""

    issuer: str
    audience: str
    public_origin: str
    jwks_uri: str | None = None
    scopes: tuple[str, ...] = DEFAULT_OAUTH_SCOPES
    jwks_cache_seconds: float = 300.0
    http_timeout: float = 5.0

    def __post_init__(self) -> None:
        issuer = normalize_https_url(self.issuer, name="OAuth issuer")
        public_origin = normalize_https_origin(
            self.public_origin, name="MCP_PUBLIC_HOST"
        )
        audience = self.audience.strip()
        if audience != public_origin:
            raise OAuthConfigurationError(
                "OAuth audience must exactly match the canonical MCP_PUBLIC_HOST origin"
            )
        jwks_uri = (
            normalize_https_url(self.jwks_uri, name="OAuth JWKS URI")
            if self.jwks_uri
            else None
        )
        scopes = tuple(dict.fromkeys(self.scopes))
        if not scopes:
            raise OAuthConfigurationError("OAuth scopes must not be empty")
        invalid_scopes = sorted(set(scopes).difference(SUPPORTED_SCOPES))
        if invalid_scopes:
            raise OAuthConfigurationError(
                f"unsupported OAuth scope(s): {', '.join(invalid_scopes)}"
            )
        if not 1 <= self.jwks_cache_seconds <= 86_400:
            raise OAuthConfigurationError(
                "OAuth JWKS cache seconds must be between 1 and 86400"
            )
        if not 0.1 <= self.http_timeout <= 60:
            raise OAuthConfigurationError(
                "OAuth HTTP timeout must be between 0.1 and 60 seconds"
            )
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "public_origin", public_origin)
        object.__setattr__(self, "jwks_uri", jwks_uri)
        object.__setattr__(self, "scopes", scopes)

    @property
    def resource_metadata_url(self) -> str:
        return f"{self.public_origin}/.well-known/oauth-protected-resource"

    def protected_resource_metadata(self) -> dict[str, object]:
        """Render RFC 9728 protected resource metadata."""

        return {
            "resource": self.public_origin,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(self.scopes),
            "bearer_methods_supported": ["header"],
        }

    def challenge(
        self,
        *,
        scope: str | None = None,
        error: str = "invalid_token",
        description: str = "Authentication required",
    ) -> str:
        """Create a token-free Bearer challenge for HTTP and MCP tool errors."""

        parts = [
            f'resource_metadata="{self.resource_metadata_url}"',
            f'error="{_challenge_value(error)}"',
            f'error_description="{_challenge_value(description)}"',
        ]
        if scope:
            parts.append(f'scope="{_challenge_value(scope)}"')
        return f"Bearer {', '.join(parts)}"


class JWTTokenVerifier:
    """Verify signed JWT access tokens against cached provider JWKS."""

    def __init__(self, settings: OAuthSettings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._jwks: tuple[Mapping[str, Any], ...] = ()
        self._jwks_expires_at = 0.0
        self._discovered_jwks_uri: str | None = settings.jwks_uri

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return validated SDK access information, never token-bearing diagnostics."""

        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            algorithm = header.get("alg")
            if (
                not isinstance(kid, str)
                or not kid
                or algorithm not in _ALLOWED_ALGORITHMS
            ):
                return None

            key_data = await self._key_for(kid)
            if key_data is None:
                key_data = await self._key_for(kid, refresh=True)
            if key_data is None:
                return None
            jwk = jwt.PyJWK.from_dict(dict(key_data), algorithm=algorithm)
            claims = jwt.decode(
                token,
                key=jwk.key,
                algorithms=[algorithm],
                issuer=self.settings.issuer,
                options={
                    "require": ["exp", "iss"],
                    "verify_aud": False,
                },
            )
            if not _matches_resource(claims, self.settings.audience):
                return None
            scopes = _token_scopes(claims)
            client_id = _first_string(claims, "client_id", "azp", "sub")
            subject = claims.get("sub")
            expires_at = claims.get("exp")
            if not isinstance(expires_at, int | float):
                return None
            return AccessToken(
                token="",
                client_id=client_id or "oauth-client",
                scopes=scopes,
                expires_at=int(expires_at),
                resource=self.settings.audience,
                subject=subject if isinstance(subject, str) else None,
                claims={"iss": self.settings.issuer},
            )
        except (
            jwt.PyJWTError,
            KeyError,
            TypeError,
            ValueError,
            OAuthConfigurationError,
            OSError,
        ):
            return None

    async def _key_for(
        self, kid: str, *, refresh: bool = False
    ) -> Mapping[str, Any] | None:
        keys = await self._get_jwks(force=refresh)
        return next((key for key in keys if key.get("kid") == kid), None)

    async def _get_jwks(self, *, force: bool = False) -> tuple[Mapping[str, Any], ...]:
        now = time.monotonic()
        if not force and self._jwks and now < self._jwks_expires_at:
            return self._jwks
        async with self._lock:
            now = time.monotonic()
            if not force and self._jwks and now < self._jwks_expires_at:
                return self._jwks
            jwks_uri = self._discovered_jwks_uri or await self._discover_jwks_uri()
            payload = await self._fetch_json(jwks_uri)
            raw_keys = payload.get("keys")
            if not isinstance(raw_keys, list):
                raise OAuthConfigurationError("JWKS response has no keys array")
            keys = tuple(key for key in raw_keys if isinstance(key, dict))
            if not keys:
                raise OAuthConfigurationError("JWKS response contains no usable keys")
            self._jwks = keys
            self._jwks_expires_at = now + self.settings.jwks_cache_seconds
            return keys

    async def _discover_jwks_uri(self) -> str:
        errors: list[Exception] = []
        for url in _discovery_urls(self.settings.issuer):
            try:
                payload = await self._fetch_json(url)
                if payload.get("issuer") != self.settings.issuer:
                    raise OAuthConfigurationError(
                        "OAuth discovery issuer does not match configured issuer"
                    )
                jwks_uri = payload.get("jwks_uri")
                if not isinstance(jwks_uri, str):
                    raise OAuthConfigurationError(
                        "OAuth discovery response has no JWKS URI"
                    )
                normalized = normalize_https_url(jwks_uri, name="discovered JWKS URI")
                self._discovered_jwks_uri = normalized
                return normalized
            except (OAuthConfigurationError, OSError) as exc:
                errors.append(exc)
        raise OAuthConfigurationError(
            "OAuth discovery did not provide a usable JWKS URI"
        )

    async def _fetch_json(self, url: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._fetch_json_sync, url)

    def _fetch_json_sync(self, url: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "sandboxed-workspace-mcp/0.2",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - HTTPS is validated above
                request, timeout=self.settings.http_timeout
            ) as response:
                final_url = response.geturl()
                normalize_https_url(final_url, name="OAuth HTTP response URL")
                data = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OSError("OAuth metadata request failed") from exc
        if len(data) > _MAX_HTTP_RESPONSE_BYTES:
            raise OAuthConfigurationError("OAuth metadata response is too large")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OAuthConfigurationError(
                "OAuth metadata response is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise OAuthConfigurationError("OAuth metadata response must be an object")
        return payload


def normalize_https_origin(value: str, *, name: str) -> str:
    """Normalize an HTTPS origin without credentials, path, query, or fragment."""

    normalized = normalize_https_url(value, name=name)
    parsed = urlsplit(normalized)
    if parsed.path not in {"", "/"}:
        raise OAuthConfigurationError(f"{name} must not contain a path")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def normalize_https_url(value: str, *, name: str) -> str:
    """Validate and normalize an absolute HTTPS URL."""

    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise OAuthConfigurationError(f"invalid {name}") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise OAuthConfigurationError(
            f"{name} must be an absolute HTTPS URL without credentials, "
            "query, or fragment"
        )
    hostname = parsed.hostname.casefold()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None or port == 443 else f"{hostname}:{port}"
    # An issuer is an exact identifier. In particular, a trailing slash is
    # significant for providers that publish one, so only origins discard it.
    path = parsed.path
    return urlunsplit(("https", netloc, path, "", ""))


def _discovery_urls(issuer: str) -> tuple[str, str]:
    parsed = urlsplit(issuer)
    issuer_path = parsed.path.rstrip("/")
    oidc = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    oauth_path = f"/.well-known/oauth-authorization-server{issuer_path}"
    oauth = urlunsplit((parsed.scheme, parsed.netloc, oauth_path, "", ""))
    return tuple(dict.fromkeys((oidc, oauth)))  # type: ignore[return-value]


def _matches_resource(claims: Mapping[str, Any], expected: str) -> bool:
    audience = claims.get("aud")
    if audience == expected:
        return True
    if isinstance(audience, list) and expected in audience:
        return True
    resource = claims.get("resource")
    if resource == expected:
        return True
    return isinstance(resource, list) and expected in resource


def _token_scopes(claims: Mapping[str, Any]) -> list[str]:
    for name in ("scope", "scp"):
        raw = claims.get(name)
        if isinstance(raw, str):
            return list(dict.fromkeys(raw.split()))
        if isinstance(raw, list):
            return list(dict.fromkeys(item for item in raw if isinstance(item, str)))
    return []


def _first_string(claims: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _challenge_value(value: str) -> str:
    return (
        value.replace("\\", "").replace('"', "'").replace("\r", " ").replace("\n", " ")
    )
