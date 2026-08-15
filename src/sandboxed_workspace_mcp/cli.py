"""Command-line entry point for Sandboxed Workspace MCP."""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .config import DEFAULT_IGNORED_DIRS, ConfigurationError, Settings
from .oauth import (
    DEFAULT_OAUTH_SCOPES,
    OAuthConfigurationError,
    OAuthSettings,
    normalize_https_origin,
)
from .server import create_server
from .task_config import (
    TaskConfiguration,
    TaskConfigurationError,
    load_task_config,
)
from .task_manager import TaskManager


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    settings: Settings
    transport: str
    host: str
    port: int
    path: str
    allow_network: bool
    allow_unauthenticated_http: bool
    allowed_hosts: tuple[str, ...]
    public_origin: str | None
    oauth: OAuthSettings | None
    task_configuration: TaskConfiguration | None


def build_parser(
    environment: Mapping[str, str] | None = None,
) -> argparse.ArgumentParser:
    env = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(
        prog="sandboxed-workspace-mcp",
        description="Expose one local workspace through a size-bounded MCP server.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--root",
        default=env.get("SANDBOXED_WORKSPACE_MCP_ROOT"),
        help="workspace root (or set SANDBOXED_WORKSPACE_MCP_ROOT)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=env.get("SANDBOXED_WORKSPACE_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host", default=env.get("SANDBOXED_WORKSPACE_MCP_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=env.get("SANDBOXED_WORKSPACE_MCP_PORT", "3001")
    )
    parser.add_argument(
        "--path", default=env.get("SANDBOXED_WORKSPACE_MCP_HTTP_PATH", "/mcp")
    )
    parser.add_argument(
        "--read-only",
        action=argparse.BooleanOptionalAction,
        default=_parser_environment_bool(
            parser, env, "SANDBOXED_WORKSPACE_MCP_READ_ONLY", False
        ),
        help="disable every mutating tool",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        default=_parser_environment_bool(
            parser, env, "SANDBOXED_WORKSPACE_MCP_ALLOW_NETWORK", False
        ),
        help="permit a non-loopback HTTP bind; public deployments still require OAuth",
    )
    parser.add_argument(
        "--allow-unauthenticated-http",
        action="store_true",
        help="DANGEROUS: temporarily allow public HTTP MCP without OAuth",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=_environment_list(env, "SANDBOXED_WORKSPACE_MCP_ALLOWED_HOSTS"),
        help="accepted HTTP Host name for wildcard/network binds (repeatable)",
    )
    parser.add_argument(
        "--oauth",
        action=argparse.BooleanOptionalAction,
        default=_parser_environment_bool(
            parser, env, "SANDBOXED_WORKSPACE_MCP_OAUTH_ENABLED", False
        ),
        help="validate external-provider OAuth access tokens on streamable HTTP",
    )
    parser.add_argument(
        "--oauth-issuer",
        default=env.get("SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER"),
        metavar="HTTPS_URL",
    )
    parser.add_argument(
        "--oauth-audience",
        default=env.get("SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE"),
        metavar="HTTPS_ORIGIN",
    )
    parser.add_argument(
        "--oauth-jwks-uri",
        default=env.get("SANDBOXED_WORKSPACE_MCP_OAUTH_JWKS_URI"),
        metavar="HTTPS_URL",
    )
    parser.add_argument(
        "--oauth-scope",
        action="append",
        default=(
            _environment_list(env, "SANDBOXED_WORKSPACE_MCP_OAUTH_SCOPES")
            or list(DEFAULT_OAUTH_SCOPES)
        ),
        help="advertised and accepted OAuth scope (repeatable)",
    )
    parser.add_argument(
        "--oauth-jwks-cache-seconds",
        type=float,
        default=env.get("SANDBOXED_WORKSPACE_MCP_OAUTH_JWKS_CACHE_SECONDS", "300"),
        metavar="SECONDS",
    )
    parser.add_argument(
        "--oauth-http-timeout",
        type=float,
        default=env.get("SANDBOXED_WORKSPACE_MCP_OAUTH_HTTP_TIMEOUT", "5"),
        metavar="SECONDS",
    )
    parser.add_argument(
        "--public-host",
        default=env.get("MCP_PUBLIC_HOST"),
        metavar="ORIGIN",
        help=(
            "public HTTP origin whose hostname is allowed for streamable HTTP "
            "(or set MCP_PUBLIC_HOST)"
        ),
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=env.get("SANDBOXED_WORKSPACE_MCP_MAX_FILE_SIZE", str(2 * 1024 * 1024)),
        metavar="BYTES",
    )
    parser.add_argument(
        "--max-output-size",
        type=int,
        default=env.get("SANDBOXED_WORKSPACE_MCP_MAX_OUTPUT_SIZE", "200000"),
        metavar="BYTES",
    )
    parser.add_argument(
        "--max-tree-entries",
        type=int,
        default=env.get("SANDBOXED_WORKSPACE_MCP_MAX_TREE_ENTRIES", "1500"),
    )
    parser.add_argument(
        "--max-tree-depth",
        type=int,
        default=env.get("SANDBOXED_WORKSPACE_MCP_MAX_TREE_DEPTH", "5"),
    )
    parser.add_argument(
        "--max-scan-entries",
        type=int,
        default=env.get("SANDBOXED_WORKSPACE_MCP_MAX_SCAN_ENTRIES", "10000"),
        help="global directory-entry scan budget per list/tree/search request",
    )
    parser.add_argument(
        "--max-search-bytes",
        type=int,
        default=env.get(
            "SANDBOXED_WORKSPACE_MCP_MAX_SEARCH_BYTES", str(64 * 1024 * 1024)
        ),
        metavar="BYTES",
    )
    parser.add_argument(
        "--search-timeout",
        type=float,
        default=env.get("SANDBOXED_WORKSPACE_MCP_SEARCH_TIMEOUT", "10"),
        metavar="SECONDS",
    )
    parser.add_argument(
        "--max-concurrent-searches",
        type=int,
        default=env.get("SANDBOXED_WORKSPACE_MCP_MAX_CONCURRENT_SEARCHES", "1"),
    )
    parser.add_argument(
        "--git-timeout",
        type=float,
        default=env.get("SANDBOXED_WORKSPACE_MCP_GIT_TIMEOUT", "30"),
        metavar="SECONDS",
    )
    parser.add_argument(
        "--ignore-dir",
        action="append",
        default=_environment_list(env, "SANDBOXED_WORKSPACE_MCP_IGNORED_DIRS"),
        help="add a directory base name excluded from tree/search (repeatable)",
    )
    parser.add_argument(
        "--block-path",
        action="append",
        default=_environment_rule_list(env, "SANDBOXED_WORKSPACE_MCP_BLOCKED_PATHS"),
        metavar="PATTERN",
        help=(
            "add a root-relative blocked glob using literals, *, ?, and ** (repeatable)"
        ),
    )
    parser.add_argument(
        "--task-config",
        default=env.get("SANDBOXED_WORKSPACE_MCP_TASK_CONFIG"),
        metavar="ABSOLUTE_JSON_PATH",
        help=(
            "trusted container-task JSON outside the workspace "
            "(or set SANDBOXED_WORKSPACE_MCP_TASK_CONFIG)"
        ),
    )
    return parser


def parse_runtime(
    argv: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> RuntimeOptions:
    parser = build_parser(environment)
    args = parser.parse_args(argv)

    if not args.root:
        parser.error("--root or SANDBOXED_WORKSPACE_MCP_ROOT is required")
    if args.transport not in {"stdio", "streamable-http"}:
        parser.error("transport must be 'stdio' or 'streamable-http'")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if not args.host or any(character.isspace() for character in args.host):
        parser.error("--host must be a non-empty host name or IP address")
    if not args.path.startswith("/") or "?" in args.path or "#" in args.path:
        parser.error("--path must be an absolute URL path without a query or fragment")
    if (
        args.transport == "streamable-http"
        and not _is_loopback(args.host)
        and not args.allow_network
    ):
        parser.error("non-loopback HTTP binding requires --allow-network")

    public_origin: str | None = None
    try:
        configured_hosts = [_validated_host_name(host) for host in args.allowed_host]
        if args.transport == "streamable-http" and args.public_host:
            public_origin = _validated_public_origin(args.public_host)
            configured_hosts.append(_origin_host(public_origin))
        allowed_hosts = tuple(dict.fromkeys(configured_hosts))
    except (ConfigurationError, OAuthConfigurationError) as exc:
        parser.error(str(exc))
    if (
        args.transport == "streamable-http"
        and _is_wildcard(args.host)
        and not allowed_hosts
    ):
        parser.error("wildcard HTTP binding requires --allowed-host or MCP_PUBLIC_HOST")

    try:
        settings = Settings.create(
            args.root,
            max_file_size=args.max_file_size,
            max_output_size=args.max_output_size,
            max_tree_entries=args.max_tree_entries,
            max_tree_depth=args.max_tree_depth,
            max_scan_entries=args.max_scan_entries,
            max_search_bytes=args.max_search_bytes,
            search_timeout_seconds=args.search_timeout,
            max_concurrent_searches=args.max_concurrent_searches,
            git_timeout=args.git_timeout,
            allow_writes=not args.read_only,
            ignored_dirs=DEFAULT_IGNORED_DIRS.union(args.ignore_dir),
            blocked_patterns=args.block_path,
        )
    except ConfigurationError as exc:
        parser.error(str(exc))

    task_configuration = None
    if args.task_config:
        try:
            task_configuration = load_task_config(
                args.task_config, workspace_root=settings.root
            )
        except TaskConfigurationError as exc:
            parser.error(str(exc))

    oauth_required = args.transport == "streamable-http" and (
        public_origin is not None or not _is_loopback(args.host)
    )
    if args.transport == "stdio" and args.oauth:
        parser.error("OAuth is available only with streamable-http transport")
    if args.oauth and args.allow_unauthenticated_http:
        parser.error("--oauth and --allow-unauthenticated-http cannot be combined")
    if oauth_required and not args.oauth and not args.allow_unauthenticated_http:
        parser.error(
            "public streamable HTTP requires OAuth; configure "
            "SANDBOXED_WORKSPACE_MCP_OAUTH_* "
            "or use --allow-unauthenticated-http only for temporary development"
        )

    oauth: OAuthSettings | None = None
    if args.oauth:
        missing = [
            name
            for name, value in (
                ("SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER", args.oauth_issuer),
                ("SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE", args.oauth_audience),
                ("MCP_PUBLIC_HOST", public_origin),
            )
            if not value
        ]
        if missing:
            parser.error(f"OAuth configuration is missing: {', '.join(missing)}")
        try:
            oauth = OAuthSettings(
                issuer=args.oauth_issuer,
                audience=args.oauth_audience,
                public_origin=public_origin,
                jwks_uri=args.oauth_jwks_uri,
                scopes=tuple(args.oauth_scope),
                jwks_cache_seconds=args.oauth_jwks_cache_seconds,
                http_timeout=args.oauth_http_timeout,
            )
        except OAuthConfigurationError as exc:
            parser.error(str(exc))

        required_scopes = {"workspace.read"}
        if settings.allow_writes:
            required_scopes.add("workspace.write")
        if task_configuration is not None:
            required_scopes.update({"tasks.read", "tasks.run"})
        missing_scopes = sorted(required_scopes.difference(oauth.scopes))
        if missing_scopes:
            parser.error(
                "OAuth scopes do not cover enabled tools: " + ", ".join(missing_scopes)
            )

    return RuntimeOptions(
        settings=settings,
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
        allow_network=args.allow_network,
        allow_unauthenticated_http=args.allow_unauthenticated_http,
        allowed_hosts=allowed_hosts,
        public_origin=public_origin,
        oauth=oauth,
        task_configuration=task_configuration,
    )


def main(argv: Sequence[str] | None = None) -> int:
    runtime = parse_runtime(argv)
    task_manager = (
        TaskManager(runtime.settings, runtime.task_configuration)
        if runtime.task_configuration is not None
        else None
    )
    server = create_server(
        runtime.settings, task_manager=task_manager, oauth=runtime.oauth
    )
    try:
        if runtime.transport == "stdio":
            server.run(transport="stdio")
            return 0

        if runtime.allow_unauthenticated_http:
            print(
                "!!! SECURITY WARNING: OAuth is disabled for streamable HTTP; "
                "use only for temporary local development !!!",
                file=sys.stderr,
            )
        print(
            f"Sandboxed Workspace MCP root: {runtime.settings.root}\n"
            f"Endpoint: http://{runtime.host}:{runtime.port}{runtime.path}",
            file=sys.stderr,
        )
        server.run(
            transport="streamable-http",
            host=runtime.host,
            port=runtime.port,
            streamable_http_path=runtime.path,
            transport_security=_transport_security(runtime.host, runtime.allowed_hosts),
        )
        return 0
    finally:
        if task_manager is not None:
            task_manager.shutdown()


def _environment_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def _parser_environment_bool(
    parser: argparse.ArgumentParser,
    environment: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    try:
        return _environment_bool(environment, name, default)
    except ConfigurationError as exc:
        parser.error(str(exc))


def _environment_list(environment: Mapping[str, str], name: str) -> list[str]:
    value = environment.get(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def _environment_rule_list(environment: Mapping[str, str], name: str) -> list[str]:
    value = environment.get(name)
    if value is None or not value.strip():
        return []
    return [item.strip() for item in value.split(",")]


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _is_wildcard(host: str) -> bool:
    return host.strip("[]") in {"0.0.0.0", "::"}


def _transport_security(
    host: str, additional_hosts: Sequence[str] = ()
) -> TransportSecuritySettings:
    host_names = {_host_header_name(name) for name in additional_hosts}
    if not _is_wildcard(host):
        host_names.add(_host_header_name(host))
    if _is_loopback(host):
        host_names.update({"127.0.0.1", "[::1]", "localhost"})

    allowed_hosts = sorted(host_names.union({f"{name}:*" for name in host_names}))
    origins: set[str] = set()
    for name in host_names:
        origins.update(
            {
                f"http://{name}",
                f"http://{name}:*",
                f"https://{name}",
                f"https://{name}:*",
            }
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=sorted(origins),
    )


def _host_header_name(host: str) -> str:
    stripped = host.strip("[]")
    return f"[{stripped}]" if ":" in stripped else stripped


def _validated_host_name(host: str) -> str:
    stripped = host.strip().strip("[]")
    if (
        not stripped
        or "://" in stripped
        or "/" in stripped
        or any(character.isspace() for character in stripped)
    ):
        raise ConfigurationError(f"invalid allowed host name: {host}")
    if ":" in stripped:
        try:
            ipaddress.ip_address(stripped)
        except ValueError as exc:
            raise ConfigurationError(
                f"allowed hosts must not include a port: {host}"
            ) from exc
    return stripped


def _validated_public_origin(origin: str) -> str:
    return normalize_https_origin(origin, name="MCP_PUBLIC_HOST")


def _origin_host(origin: str) -> str:
    parsed = urlsplit(origin)
    assert parsed.hostname is not None
    return _validated_host_name(parsed.hostname)
