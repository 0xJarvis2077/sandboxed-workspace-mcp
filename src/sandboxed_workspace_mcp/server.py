"""MCP adapter for the sandboxed-workspace-mcp application service."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import (
    AuthContextMiddleware,
    get_access_token,
)
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import TokenVerifier
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .oauth import JWTTokenVerifier, OAuthSettings
from .service import SandboxedWorkspace
from .task_manager import TaskManager
from .trash import TrashError

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
TASK_EXECUTION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
TRASH_MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
TRASH_RESTORE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
GIT_INIT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
GIT_BASELINE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_STRICT_TOOL_ARGUMENTS = {
    "git_init": frozenset(),
    "git_create_baseline": frozenset(),
    "git_read_file_at_revision": frozenset({"path", "commit"}),
    "git_status": frozenset({"style"}),
    "git_diff": frozenset({"staged", "path"}),
    "workspace_diff": frozenset({"path"}),
    "git_log": frozenset({"count", "oneline"}),
    "git_show": frozenset({"commit", "path"}),
    "git_branch": frozenset({"show_current"}),
    "git_rev_parse": frozenset({"query"}),
    "git_ls_files": frozenset(),
    "list_tasks": frozenset(),
    "run_task": frozenset({"name"}),
    "start_task": frozenset({"name"}),
    "task_status": frozenset({"task_id"}),
    "task_logs": frozenset({"task_id", "cursor"}),
    "stop_task": frozenset({"task_id"}),
    "list_execution_profiles": frozenset(),
    "python_version": frozenset({"profile"}),
    "run_pytest": frozenset(
        {
            "profile",
            "targets",
            "keyword",
            "quiet",
            "verbosity",
            "exit_first",
            "no_capture",
            "traceback",
        }
    ),
    "run_python_script": frozenset({"profile", "path"}),
    "run_command": frozenset({"profile", "program", "args", "cwd"}),
    "start_command": frozenset({"profile", "program", "args", "cwd"}),
    "trash_file": frozenset({"path", "expected_sha256"}),
    "list_trashed_files": frozenset({"offset", "limit"}),
    "restore_trashed_file": frozenset({"trash_id", "expected_sha256"}),
    "restore_trashed_file_to": frozenset(
        {"trash_id", "expected_sha256", "destination_path"}
    ),
    "purge_trashed_file": frozenset({"trash_id", "expected_sha256"}),
}
_TOOL_SCOPES: dict[str, frozenset[str]] = {
    "project_info": frozenset({"workspace.read"}),
    "list_directory": frozenset({"workspace.read"}),
    "tree": frozenset({"workspace.read"}),
    "read_file": frozenset({"workspace.read"}),
    "read_file_versioned": frozenset({"workspace.read"}),
    "search_text": frozenset({"workspace.read"}),
    "git_status": frozenset({"workspace.read"}),
    "git_diff": frozenset({"workspace.read"}),
    "workspace_diff": frozenset({"workspace.read"}),
    "git_log": frozenset({"workspace.read"}),
    "git_show": frozenset({"workspace.read"}),
    "git_branch": frozenset({"workspace.read"}),
    "git_rev_parse": frozenset({"workspace.read"}),
    "git_ls_files": frozenset({"workspace.read"}),
    "git_read_file_at_revision": frozenset({"workspace.read"}),
    "git_init": frozenset({"workspace.git.write"}),
    "git_create_baseline": frozenset({"workspace.git.write"}),
    "run_shell": frozenset({"workspace.read"}),
    "create_directory": frozenset({"workspace.write"}),
    "write_file": frozenset({"workspace.write"}),
    "replace_text": frozenset({"workspace.write"}),
    "append_file": frozenset({"workspace.write"}),
    "trash_file": frozenset({"workspace.delete"}),
    "list_trashed_files": frozenset({"workspace.delete"}),
    "restore_trashed_file": frozenset({"workspace.delete"}),
    "restore_trashed_file_to": frozenset({"workspace.delete", "workspace.write"}),
    "purge_trashed_file": frozenset({"workspace.delete", "workspace.purge"}),
    "list_tasks": frozenset({"tasks.read"}),
    "task_status": frozenset({"tasks.read"}),
    "task_logs": frozenset({"tasks.read"}),
    "run_task": frozenset({"tasks.run"}),
    "start_task": frozenset({"tasks.run"}),
    "stop_task": frozenset({"tasks.run"}),
    "list_execution_profiles": frozenset({"tasks.read"}),
    "python_version": frozenset({"tasks.run"}),
    "run_pytest": frozenset({"tasks.run"}),
    "run_python_script": frozenset({"tasks.run"}),
    "run_command": frozenset({"tasks.run"}),
    "start_command": frozenset({"tasks.run"}),
}


def _trash_error_result(error: TrashError) -> CallToolResult:
    """Adapt a domain error to an MCP result without leaking host details."""

    return CallToolResult(
        content=[TextContent(type="text", text=error.message)],
        isError=True,
        structuredContent=error.public_error,
    )


def _call_trash(operation: Any, *arguments: Any) -> Any:
    try:
        return operation(*arguments)
    except TrashError as exc:
        return _trash_error_result(exc)


class _SelectiveOAuthApp:
    """Require SDK bearer authentication only on the MCP transport route."""

    def __init__(
        self, app: ASGIApp, oauth: OAuthSettings, streamable_http_path: str
    ) -> None:
        self.app = app
        self.streamable_http_path = streamable_http_path
        self.protected = RequireAuthMiddleware(
            app,
            required_scopes=[],
            resource_metadata_url=oauth.resource_metadata_url,  # type: ignore[arg-type]
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == self.streamable_http_path:
            await self.protected(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SandboxedWorkspaceMCPServer(MCPServer[None]):
    """MCP server that strictly rejects undeclared task-tool arguments."""

    def __init__(
        self,
        *args: Any,
        oauth: OAuthSettings | None = None,
        token_verifier: TokenVerifier | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.oauth = oauth
        self.oauth_token_verifier = token_verifier

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            if tool.name in _STRICT_TOOL_ARGUMENTS:
                tool.input_schema["additionalProperties"] = False
            if self.oauth is not None:
                scopes = _TOOL_SCOPES.get(tool.name)
                if scopes is not None:
                    metadata = dict(tool.meta or {})
                    metadata["securitySchemes"] = [
                        {"type": "oauth2", "scopes": sorted(scopes)}
                    ]
                    tool.meta = metadata
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any | None = None,
    ):
        allowed = _STRICT_TOOL_ARGUMENTS.get(name)
        if allowed is not None:
            unexpected = sorted(set(arguments).difference(allowed))
            if unexpected:
                raise ValueError(
                    f"unexpected argument(s) for {name}: {', '.join(unexpected)}"
                )
        if self.oauth is not None:
            required_scopes = _TOOL_SCOPES.get(name)
            if required_scopes is not None:
                access_token = get_access_token()
                if access_token is None:
                    return self._auth_error(required_scopes, "invalid_token")
                if not required_scopes.issubset(access_token.scopes):
                    return self._auth_error(required_scopes, "insufficient_scope")
        try:
            return await super().call_tool(name, arguments, context)
        except TrashError as exc:
            return _trash_error_result(exc)

    def _auth_error(self, scopes: frozenset[str], error: str) -> CallToolResult:
        assert self.oauth is not None
        scope = " ".join(sorted(scopes))
        description = (
            "Authentication required"
            if error == "invalid_token"
            else f"Required scope: {scope}"
        )
        challenge = self.oauth.challenge(
            scope=scope, error=error, description=description
        )
        return CallToolResult(
            content=[TextContent(type="text", text=description)],
            isError=True,
            _meta={"mcp/www_authenticate": [challenge]},
        )

    def streamable_http_app(self, **kwargs: Any) -> ASGIApp:
        streamable_http_path = kwargs.get("streamable_http_path", "/mcp")
        app = super().streamable_http_app(**kwargs)
        if self.oauth is None or self.oauth_token_verifier is None:
            return app
        protected = _SelectiveOAuthApp(app, self.oauth, streamable_http_path)
        contextual = AuthContextMiddleware(protected)
        return AuthenticationMiddleware(
            contextual,
            backend=BearerAuthBackend(self.oauth_token_verifier),
        )


def create_server(
    settings: Settings,
    task_manager: TaskManager | None = None,
    *,
    oauth: OAuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer[None]:
    """Create an MCP server bound to one validated workspace."""

    computer = SandboxedWorkspace(settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer[None]):
        try:
            yield None
        finally:
            if task_manager is not None:
                await asyncio.to_thread(task_manager.shutdown)

    if oauth is not None:
        required_scopes = {"workspace.read"}
        if settings.allow_writes:
            required_scopes.add("workspace.write")
        if settings.allow_git_writes:
            required_scopes.add("workspace.git.write")
        if settings.allow_trash:
            required_scopes.add("workspace.delete")
        if settings.allow_trash_purge:
            required_scopes.add("workspace.purge")
        if task_manager is not None:
            required_scopes.update({"tasks.read", "tasks.run"})
        missing = sorted(required_scopes.difference(oauth.scopes))
        if missing:
            raise ValueError(
                f"OAuth scopes do not cover enabled tools: {', '.join(missing)}"
            )
        token_verifier = token_verifier or JWTTokenVerifier(oauth)

    server: SandboxedWorkspaceMCPServer = SandboxedWorkspaceMCPServer(
        "sandboxed-workspace-mcp",
        title="Sandboxed Workspace MCP",
        description=(
            "Capability-bounded workspace file operations with optional recycle-bin "
            "trash, recovery, alternate-path restore, and read-only Git access within "
            "one root."
        ),
        instructions=(
            "All paths are confined to the configured workspace. Use replace_text for "
            "precise edits and inspect files again before resolving ambiguous changes."
        ),
        version=__version__,
        lifespan=lifespan if task_manager is not None else None,
        oauth=oauth,
        token_verifier=token_verifier,
    )

    if oauth is not None:

        @server.custom_route(
            "/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"]
        )
        async def oauth_protected_resource_metadata(request: Request) -> Response:
            headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
            }
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=headers)
            return JSONResponse(oauth.protected_resource_metadata(), headers=headers)

    @server.tool(annotations=READ_ONLY)
    def project_info() -> str:
        """Show the workspace root, access mode, and current writability."""

        return computer.workspace.project_info()

    @server.tool(annotations=READ_ONLY)
    def list_directory(path: str = ".") -> str:
        """List entries inside a workspace directory."""

        return computer.workspace.list_directory(path)

    @server.tool(annotations=READ_ONLY)
    def tree(path: str = ".", max_depth: int = 4) -> str:
        """Show a bounded recursive tree while skipping dependencies and caches."""

        return computer.workspace.tree(path, max_depth)

    if settings.allow_writes:

        @server.tool(annotations=MUTATING)
        def create_directory(path: str) -> str:
            """Create a directory and its parents inside the workspace."""

            return computer.workspace.create_directory(path)

    @server.tool(annotations=READ_ONLY)
    def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
        """Read a bounded UTF-8 text file or line range inside the workspace."""

        return computer.workspace.read_file(path, start_line, end_line)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def read_file_versioned(
        path: str, start_line: int = 1, end_line: int = 0
    ) -> dict[str, object]:
        """Read text and SHA-256; use this before modifying an existing file."""

        return computer.workspace.read_file_versioned(path, start_line, end_line)

    if settings.allow_trash:

        @server.tool(annotations=TRASH_MUTATING, structured_output=True)
        def trash_file(path: str, expected_sha256: str) -> dict[str, object]:
            """Move one version-checked regular file into the protected recycle bin."""

            return _call_trash(computer.trash.trash_file, path, expected_sha256)

        @server.tool(annotations=READ_ONLY, structured_output=True)
        def list_trashed_files(offset: int = 0, limit: int = 50) -> dict[str, object]:
            """List bounded recycle-bin metadata without exposing payload contents."""

            return _call_trash(computer.trash.list_trashed_files, offset, limit)

        @server.tool(annotations=TRASH_RESTORE, structured_output=True)
        def restore_trashed_file(
            trash_id: str, expected_sha256: str
        ) -> dict[str, object]:
            """Restore one item to its original path without overwriting it.

            Use the current SHA from list_trashed_files or read_file_versioned.
            This tool supports one regular file only and never directories, globs,
            batches, or permanent deletion.
            """

            return _call_trash(
                computer.trash.restore_trashed_file, trash_id, expected_sha256
            )

        @server.tool(annotations=TRASH_RESTORE, structured_output=True)
        def restore_trashed_file_to(
            trash_id: str, expected_sha256: str, destination_path: str
        ) -> dict[str, object]:
            """Restore or recover one trashed recycle-bin file to an alternate path.

            Use this when the original path is occupied or the file should be
            recovered elsewhere in the workspace. Supply the current SHA from
            list_trashed_files. The destination parent must exist and the target must
            not exist. This tool never overwrites, permanently deletes, or purges data
            and supports only one regular file.
            """

            return _call_trash(
                computer.trash.restore_trashed_file,
                trash_id,
                expected_sha256,
                destination_path,
            )

        if settings.allow_trash_purge:

            @server.tool(annotations=TRASH_MUTATING, structured_output=True)
            def purge_trashed_file(
                trash_id: str, expected_sha256: str
            ) -> dict[str, object]:
                """Permanently delete one verified item; this cannot be undone.

                Use the current SHA from list_trashed_files. Purge is single-item
                only and never accepts directories, globs, batches, or empty-trash
                requests; the payload is verified before it is removed.
                """

                return _call_trash(
                    computer.trash.purge_trashed_file, trash_id, expected_sha256
                )

    @server.tool(annotations=READ_ONLY)
    async def search_text(text: str, path: str = ".", max_results: int = 200) -> str:
        """Search project text files without following directory symlinks."""

        cancellation = threading.Event()
        try:
            return await asyncio.to_thread(
                computer.workspace.search_text,
                text,
                path,
                max_results,
                cancellation_event=cancellation,
            )
        except asyncio.CancelledError:
            cancellation.set()
            raise

    if settings.allow_writes:

        @server.tool(annotations=MUTATING)
        def write_file(
            path: str,
            content: str,
            overwrite: bool = False,
            expected_sha256: str | None = None,
        ) -> str:
            """Create text; overwrite requires SHA-256 from read_file_versioned."""

            return computer.workspace.write_file(
                path, content, overwrite, expected_sha256
            )

        @server.tool(annotations=MUTATING)
        def replace_text(
            path: str,
            old_text: str,
            new_text: str,
            expected_sha256: str | None = None,
        ) -> str:
            """Replace once using SHA-256 from read_file_versioned."""

            return computer.workspace.replace_text(
                path, old_text, new_text, expected_sha256
            )

        @server.tool(annotations=MUTATING)
        def append_file(
            path: str, content: str, expected_sha256: str | None = None
        ) -> str:
            """Append text; existing files require read_file_versioned SHA-256."""

            return computer.workspace.append_file(path, content, expected_sha256)

    @server.tool(annotations=READ_ONLY)
    def git_status(
        style: Literal["default", "short", "porcelain"] = "default",
    ) -> str:
        """Show bounded Git status in the selected stable allowlisted form."""

        return computer.git.status(style)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def git_read_file_at_revision(path: str, commit: str = "HEAD") -> dict[str, object]:
        """Read one policy-approved UTF-8 regular file from a safe Git revision."""

        return computer.git_writer.read_file_at_revision(path, commit)

    if settings.allow_git_writes:

        @server.tool(annotations=GIT_INIT, structured_output=True)
        def git_init() -> dict[str, object]:
            """Initialize the configured workspace root as a Git main repository."""

            return computer.git_writer.init()

        @server.tool(annotations=GIT_BASELINE, structured_output=True)
        def git_create_baseline() -> dict[str, object]:
            """Create the server-owned first baseline commit, never a general commit."""

            return computer.git_writer.create_baseline()

    @server.tool(annotations=READ_ONLY)
    def git_diff(staged: bool = False, path: str | None = None) -> str:
        """Show a bounded Git diff with external drivers and textconv disabled."""

        return computer.git.diff(staged=staged, path=path)

    @server.tool(annotations=READ_ONLY)
    def workspace_diff(path: str | None = None) -> str:
        """Show final tracked changes and safe untracked text files."""

        return computer.git.workspace_diff(path=path)

    @server.tool(annotations=READ_ONLY)
    def git_log(count: int = 10, oneline: bool = False) -> str:
        """Show up to 50 recent one-line commits."""

        return computer.git.log(count, oneline=oneline)

    @server.tool(annotations=READ_ONLY)
    def git_show(commit: str, path: str | None = None) -> str:
        """Show one safe commit and an optional literal, policy-checked path."""

        return computer.git.show(commit, path=path)

    @server.tool(annotations=READ_ONLY)
    def git_branch(show_current: bool = False) -> str:
        """List local branches or show only the current branch name."""

        return computer.git.branches(show_current=show_current)

    @server.tool(annotations=READ_ONLY)
    def git_rev_parse(
        query: Literal["HEAD", "--show-toplevel"] = "HEAD",
    ) -> str:
        """Resolve HEAD or return the configured Git workspace top level."""

        return computer.git.rev_parse(query)

    @server.tool(annotations=READ_ONLY)
    def git_ls_files() -> str:
        """List tracked files after applying blocked-path exclusions."""

        return computer.git.ls_files()

    @server.tool(annotations=READ_ONLY)
    def run_shell(command: str) -> str:
        """Run the documented read-only command grammar, never a real shell."""

        return computer.run_shell(command)

    if task_manager is not None and task_manager.configuration.tasks:

        @server.tool(annotations=READ_ONLY)
        def list_tasks() -> dict[str, object]:
            """List operator-authorized task names, modes, and resource limits."""

            return task_manager.list_tasks()

        @server.tool(annotations=TASK_EXECUTION)
        async def run_task(name: str) -> dict[str, object]:
            """Run one configured run-mode task in a disposable container snapshot."""

            cancellation = threading.Event()
            try:
                return await asyncio.to_thread(
                    task_manager.run_task,
                    name,
                    cancellation_event=cancellation,
                )
            except asyncio.CancelledError:
                cancellation.set()
                raise

        @server.tool(annotations=TASK_EXECUTION)
        async def start_task(name: str) -> dict[str, object]:
            """Start one configured service-mode task for diagnostics and logs."""

            cancellation = threading.Event()
            try:
                return await asyncio.to_thread(
                    task_manager.start_task,
                    name,
                    cancellation_event=cancellation,
                )
            except asyncio.CancelledError:
                cancellation.set()
                raise

        @server.tool(annotations=READ_ONLY)
        def task_status(task_id: str) -> dict[str, object]:
            """Inspect one service task created by this server instance."""

            return task_manager.task_status(task_id)

        @server.tool(annotations=READ_ONLY)
        def task_logs(task_id: str, cursor: int = 0) -> dict[str, object]:
            """Read bounded service stdout/stderr from an absolute byte cursor."""

            return task_manager.task_logs(task_id, cursor)

        @server.tool(annotations=TASK_EXECUTION)
        async def stop_task(task_id: str) -> dict[str, object]:
            """Stop one service task created and tracked by this server instance."""

            return await asyncio.to_thread(task_manager.stop_task, task_id)

    if task_manager is not None and task_manager.configuration.profiles:

        @server.tool(annotations=READ_ONLY)
        def list_execution_profiles() -> dict[str, object]:
            """List enabled profile names, tools, access modes, and public limits."""

            return task_manager.list_execution_profiles()

        if any(
            "python_version" in profile.tools
            for profile in task_manager.configuration.profiles.values()
        ):

            @server.tool(annotations=TASK_EXECUTION)
            async def python_version(profile: str) -> dict[str, object]:
                """Read Python version inside an authorized pinned container image."""

                cancellation = threading.Event()
                try:
                    return await asyncio.to_thread(
                        task_manager.python_version,
                        profile,
                        cancellation_event=cancellation,
                    )
                except asyncio.CancelledError:
                    cancellation.set()
                    raise

        if any(
            "run_pytest" in profile.tools
            for profile in task_manager.configuration.profiles.values()
        ):

            @server.tool(annotations=TASK_EXECUTION)
            async def run_pytest(
                profile: str,
                targets: list[str] | None = None,
                keyword: str | None = None,
                quiet: bool = False,
                verbosity: int = 0,
                exit_first: bool = False,
                no_capture: bool = False,
                traceback: Literal["auto", "short", "long"] = "auto",
            ) -> dict[str, object]:
                """Run structured targeted pytest in an authorized container profile."""

                cancellation = threading.Event()
                try:
                    return await asyncio.to_thread(
                        task_manager.run_pytest,
                        profile,
                        targets=targets,
                        keyword=keyword,
                        quiet=quiet,
                        verbosity=verbosity,
                        exit_first=exit_first,
                        no_capture=no_capture,
                        traceback=traceback,
                        cancellation_event=cancellation,
                    )
                except asyncio.CancelledError:
                    cancellation.set()
                    raise

        if any(
            "run_python_script" in profile.tools
            for profile in task_manager.configuration.profiles.values()
        ):

            @server.tool(annotations=TASK_EXECUTION)
            async def run_python_script(profile: str, path: str) -> dict[str, object]:
                """Execute one policy-checked workspace .py file without arguments."""

                cancellation = threading.Event()
                try:
                    return await asyncio.to_thread(
                        task_manager.run_python_script,
                        profile,
                        path,
                        cancellation_event=cancellation,
                    )
                except asyncio.CancelledError:
                    cancellation.set()
                    raise

        if any(
            "run_command" in profile.tools and profile.allow_arbitrary_commands
            for profile in task_manager.configuration.profiles.values()
        ):

            @server.tool(annotations=TASK_EXECUTION)
            async def run_command(
                profile: str,
                program: str,
                args: list[str] | None = None,
                cwd: str = ".",
            ) -> dict[str, object]:
                """Run caller argv in an explicitly authorized container profile."""

                cancellation = threading.Event()
                try:
                    return await asyncio.to_thread(
                        task_manager.run_command,
                        profile,
                        program,
                        args,
                        cwd,
                        cancellation_event=cancellation,
                    )
                except asyncio.CancelledError:
                    cancellation.set()
                    raise

        command_services_enabled = any(
            "start_command" in profile.tools and profile.allow_arbitrary_commands
            for profile in task_manager.configuration.profiles.values()
        )
        if command_services_enabled:

            @server.tool(annotations=TASK_EXECUTION)
            async def start_command(
                profile: str,
                program: str,
                args: list[str] | None = None,
                cwd: str = ".",
            ) -> dict[str, object]:
                """Start caller argv for bounded diagnostics and log observation."""

                cancellation = threading.Event()
                try:
                    return await asyncio.to_thread(
                        task_manager.start_command,
                        profile,
                        program,
                        args,
                        cwd,
                        cancellation_event=cancellation,
                    )
                except asyncio.CancelledError:
                    cancellation.set()
                    raise

        if command_services_enabled and not task_manager.configuration.tasks:

            @server.tool(annotations=READ_ONLY)
            def task_status(task_id: str) -> dict[str, object]:
                """Inspect one service command created by this server instance."""

                return task_manager.task_status(task_id)

            @server.tool(annotations=READ_ONLY)
            def task_logs(task_id: str, cursor: int = 0) -> dict[str, object]:
                """Read bounded command stdout/stderr from an absolute byte cursor."""

                return task_manager.task_logs(task_id, cursor)

            @server.tool(annotations=TASK_EXECUTION)
            async def stop_task(task_id: str) -> dict[str, object]:
                """Stop one service command tracked by this server instance."""

                return await asyncio.to_thread(task_manager.stop_task, task_id)

    return server
