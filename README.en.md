# Sandboxed Workspace MCP

[中文](README.md) · [Security boundary](SECURITY.md) · [Task template](examples/tasks.json) · [Execution profile template](examples/execution-profiles.json)

Expose one local project root as a bounded MCP server. Clients can read, search, edit text, and inspect Git; trusted operators can optionally run fixed tasks or execution profiles inside Docker/Podman against a disposable workspace snapshot. The server does not execute project code, clean up files, or provide a host shell or port mapping by default; when explicitly enabled, cleanup remains bounded and permanent deletion requires separate authorization.

## Three things to know first

- One server instance owns one `--root`. Run separate instances for unrelated projects.
- Workspace tools are writable by default; pass `--read-only` explicitly for production or read-only use.
- `--read-only` disables workspace write tools. Authorized tasks still run in a disposable snapshot and never write back to the real workspace.
- Git initialization and the first baseline commit are disabled by default. `--allow-git-writes` requires writable mode and the separate `workspace.git.write` OAuth scope.
- The recycle bin is disabled by default. `--allow-trash` also requires writes and provides single-file trash plus non-overwriting restoration to the original or a safe alternate path. Irreversible single-item purge requires the separate `--allow-trash-purge` flag.
- Container execution is opt-in through a trusted JSON file outside the workspace. Images must be full registry digests or full local `sha256` image IDs.

For the threat model, file semantics, Git restrictions, and container boundary, see [SECURITY.md](SECURITY.md).

## Start in five minutes

### Install

Python 3.10+ is required. Git is only needed when using the Git tools.

```bash
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/sandboxed-workspace-mcp \
  --root /absolute/path/to/project \
  --read-only
```

For source development and tests:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

### Connect an MCP client

`stdio` is the default and recommended local transport. Replace both absolute paths:

```json
{
  "mcpServers": {
    "sandboxed-project": {
      "command": "/absolute/path/to/venv/bin/sandboxed-workspace-mcp",
      "args": [
        "--root",
        "/absolute/path/to/project",
        "--read-only"
      ]
    }
  }
}
```

The compatibility entrypoint remains available:

```bash
.venv/bin/python server.py --root /absolute/path/to/project --read-only
```

## Tool map

| Capability | Tools | What it does |
| --- | --- | --- |
| Project | `project_info` | Root, write mode, and resource budgets |
| Directories | `list_directory`, `tree` | Bounded traversal without following directory symlinks |
| Files | `read_file`, `read_file_versioned`, `search_text` | Bounded text access, search, and SHA-256 version tokens |
| Writes | `create_directory`, `write_file`, `replace_text`, `append_file` | Atomic writes; disabled in read-only mode |
| Recycle bin (optional) | `trash_file`, `list_trashed_files`, `restore_trashed_file`, `restore_trashed_file_to` | Disabled by default; bounded single-file trash and non-overwriting restore |
| Permanent purge (optional) | `purge_trashed_file` | Registered only with separate purge enablement; SHA-checked and irreversible |
| Read-only Git | `git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_rev_parse`, `git_ls_files`, `git_read_file_at_revision` | Fixed-argument, bounded Git queries and historical file reads |
| Git writes (optional) | `git_init`, `git_create_baseline` | Configured-root `main` initialization and one server-owned first baseline; disabled by default |
| Compatibility commands | `run_shell` | Closed read-only grammar; never starts a shell |
| Fixed tasks | `list_tasks`, `run_task` | Operator-defined synchronous tasks |
| Long-running tasks | `start_task`, `task_status`, `task_logs`, `stop_task` | Bounded stdout/stderr with cursor-based logs |
| Execution profiles | `list_execution_profiles`, `python_version`, `run_pytest`, `run_python_script`, `run_command`, `start_command` | Authorized execution in a pinned image and disposable snapshot |

Without a task config, the task manager, container backend, and dynamic execution tools are not created or registered.

`run_shell` accepts only `pwd`, `ls`, `cat`, `head`, `tail`, `tree`, `grep`, bounded `rg`, `find`, `wc`, `sed`, and fixed Git queries. Pipes, redirects, command substitution, environment expansion, and unlisted arguments are rejected.

### Use a controlled Git baseline

Enable `--allow-git-writes` (or `SANDBOXED_WORKSPACE_MCP_ALLOW_GIT_WRITES=true`) to register `git_init` and `git_create_baseline`. They accept no caller parameters. `git_init` creates an ordinary non-bare `main` repository only at the configured workspace root and returns an idempotent result for an already-valid root repository. `repo_path`, subdirectory repositories, templates, separate Git directories, bare/shared/object-format options, and arbitrary Git argv are unsupported. `git_create_baseline` is allowed only once before the first commit, uses a fixed message and identity, and is not a general commit tool; blocked files, `.git`, the recycle bin, ignored directories, symlinks, and special files are excluded.

To restore historical content, first call `read_file_versioned` for the current SHA, then call `git_read_file_at_revision(path, "HEAD")`, and finally use the existing `write_file(overwrite=true, expected_sha256=...)`. `run_shell` remains read-only. Task snapshots still exclude `.git`, and even a writable `run_command` profile never writes back to the real workspace. Git writes are not in the default OAuth scopes; HTTP callers must also hold `workspace.git.write`.

## Common workflows

### Read and edit code

Use `tree`, `read_file`, and `search_text` to locate code. For an existing file, pass the SHA-256 returned by versioned reads as `expected_sha256` when writing. A stale token returns an explicit conflict instead of silently overwriting a concurrent edit.

### Use the recycle bin

`--allow-trash` registers `trash_file`, `list_trashed_files`, `restore_trashed_file`, and `restore_trashed_file_to`. Every restore call requires the current SHA from the list result. If the original path now contains a new file, it is never overwritten: call `create_directory("recovered")` first and then use `restore_trashed_file_to(..., "recovered/basic.txt")`. The alternate-path tool requires both `workspace.delete` and `workspace.write`; its parent must already exist and its target must be empty. Agents should branch on the structured `error.code`, not parse English messages.

`--allow-trash-purge` is available only with trash and writes enabled and registers `purge_trashed_file`, which requires `workspace.delete` and `workspace.purge`. It requires the current SHA and permanently removes exactly one regular file; directories, globs, batches, and `empty_trash` are unsupported. A full quota rejects new trash and never evicts items automatically.

### Run tests or checks

Prefer an operator-defined `run_task("test")`. For controlled exploration, use an authorized profile:

```python
run_pytest(profile="python-debug", targets=["tests"], quiet=True)
run_command(profile="coding", program="ruff", args=["check", "."])
```

`run_pytest` compiles its argv on the server and automatically places pytest cache at `/tmp/cache/pytest`. Generic `run_command`/`start_command` preserve caller argv; pass the cache option explicitly when invoking pytest directly:

```python
run_command(
    profile="coding",
    program="pytest",
    args=["-o", "cache_dir=/tmp/cache/pytest", "-q", "tests"],
)
```

### Observe a long-running service

```python
started = start_command(
    profile="coding",
    program="uvicorn",
    args=["app:app"],
)
task_status(started["task_id"])
task_logs(started["task_id"])
stop_task(started["task_id"])
```

`start_task` and `start_command` do not map ports. They are intended for startup diagnostics and log inspection.

## Container tasks and execution profiles

### Fixed tasks

Copy [examples/tasks.json](examples/tasks.json) outside the workspace, replace its obvious placeholders, and remove unused tasks. Each task fixes its image, argv, mode, and workspace access; dependencies are not downloaded at runtime.

A task that must create workspace artifacts must explicitly use `"workspace_access": "writable"` and configure per-file, aggregate-growth, and best-effort disk limits. Ordinary tasks use a read-only bind mount.

### Execution profiles

Copy [examples/execution-profiles.json](examples/execution-profiles.json) outside the workspace. A profile fixes its image, tool set, and workspace access:

- `python_version`: runs `python --version` inside the container.
- `run_pytest`: validates targets and compiles the pytest argv on the server.
- `run_python_script`: accepts one real workspace `.py` file.
- `run_command`/`start_command`: require `allow_arbitrary_commands: true`; this grants arbitrary code execution inside the container.

Callers cannot override environment, image, network, mounts, ports, resource limits, or container IDs. Every execution starts from a filtered temporary snapshot; snapshot changes are never written back to the real workspace.

## Temporary caches in read-only profiles

The fixed container environment places common caches in `/tmp`, within a 64 MiB tmpfs:

| Tool/state | Path |
| --- | --- |
| `HOME`, `TMPDIR` | `/tmp/home`, `/tmp` |
| XDG cache | `/tmp/cache` |
| Ruff | `/tmp/cache/ruff` |
| mypy | `/tmp/cache/mypy` |
| coverage | `/tmp/.coverage` |
| npm | `/tmp/npm-cache` |

Python bytecode and pip cache are disabled. Explicit build and report artifacts such as `build/`, `dist/`, and `htmlcov/` are not redirected automatically; use tool arguments to place them under `/tmp` in a read-only profile, or use a constrained writable task/profile when workspace output is required.

## Configuration and deployment

Command-line arguments take precedence over environment variables. See the complete list with:

```bash
.venv/bin/sandboxed-workspace-mcp --help
```

Common settings:

| Argument/environment variable | Purpose |
| --- | --- |
| `--root` / `SANDBOXED_WORKSPACE_MCP_ROOT` | The one workspace root |
| `--read-only` / `SANDBOXED_WORKSPACE_MCP_READ_ONLY` | Disable all workspace write tools |
| `--allow-git-writes` / `SANDBOXED_WORKSPACE_MCP_ALLOW_GIT_WRITES` | Enable controlled Git initialization and first baseline; requires writable mode and does not accept arbitrary Git arguments |
| `--max-git-baseline-files` / `SANDBOXED_WORKSPACE_MCP_MAX_GIT_BASELINE_FILES` | Maximum regular files in the first baseline |
| `--max-git-baseline-bytes` / `SANDBOXED_WORKSPACE_MCP_MAX_GIT_BASELINE_BYTES` | Maximum aggregate payload bytes in the first baseline |
| `--allow-trash` / `SANDBOXED_WORKSPACE_MCP_ALLOW_TRASH` | Enable the restricted recoverable single-file recycle bin |
| `--allow-trash-purge` / `SANDBOXED_WORKSPACE_MCP_ALLOW_TRASH_PURGE` | Enable verified, irreversible single-item purge separately |
| `--max-trash-items` / `SANDBOXED_WORKSPACE_MCP_MAX_TRASH_ITEMS` | Maximum retained recycle-bin entries (default 200) |
| `--max-trash-bytes` / `SANDBOXED_WORKSPACE_MCP_MAX_TRASH_BYTES` | Maximum aggregate payload bytes (default 256 MiB) |
| `--block-path` | Add a root-relative blocked glob |
| `--ignore-dir` | Add a directory basename excluded from automatic scans |
| `--task-config` / `SANDBOXED_WORKSPACE_MCP_TASK_CONFIG` | Trusted task JSON outside the workspace |
| `--transport` | `stdio` or `streamable-http` |

`stdio` is intended for local clients. Streamable HTTP listens on `127.0.0.1:3001/mcp` by default; non-loopback or public deployments need explicit network, Host/Origin, HTTPS, and external OAuth/OIDC configuration. `--allow-unauthenticated-http` is for temporary development only. See [SECURITY.md](SECURITY.md) for the full OAuth topology and RFC 9728 details.

## Project layout

```text
src/sandboxed_workspace_mcp/
  workspace.py          # Safe paths, file I/O, traversal, and atomic writes
  access_policy.py      # Blocked globs and Git exclusion policy
  trash.py              # Protected recycle-bin metadata, transactions, and recovery
  git_reader.py         # Bounded read-only Git adapter
  git_writer.py         # Controlled init, first baseline, and revision blob reads
  service.py            # run_shell grammar and application orchestration
  server.py             # MCP tools, scope checks, and auth challenges
  cli.py                # stdio/HTTP startup and OAuth configuration
  task_config.py        # Trusted task/profile JSON validation and freezing
  python_execution.py   # Structured Python/pytest argv compilation
  command_execution.py  # Generic argv and workspace cwd validation
  task_snapshot.py      # Filtered bounded temporary snapshots
  task_runner.py        # Docker/Podman argv, pipes, and synchronous execution
  task_manager.py       # Concurrency, service lifecycle, and log ring buffer
tests/                   # Unit, boundary, and transport regression tests
examples/                # Digest-based config templates and task image
SECURITY.md              # Security boundary, threat model, and residual risk
```

## Development and quality gates

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
PYTHONPATH=src .venv/bin/python -m coverage run -m unittest discover -s tests -v
.venv/bin/python -m coverage report --fail-under=85
.venv/bin/python -m compileall -q server.py src tests scripts
.venv/bin/python -m build
.venv/bin/python scripts/wheel_smoke.py dist/*.whl
```

CI runs lint, format, tests, coverage, compile, build, and wheel smoke on Python 3.10 and 3.13. The ordinary test suite does not require Docker/Podman.

## License

Apache License 2.0. See [LICENSE](LICENSE).
