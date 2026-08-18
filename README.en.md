# WorkspaceGuard MCP

[中文](README.md) · [Security boundary](SECURITY.md) · [Task template](examples/tasks.json) · [Execution profile template](examples/execution-profiles.json)

WorkspaceGuard MCP is a secure execution layer for AI agents. It keeps workspace access, Git operations, code execution, commands, and operator-authorized tasks inside explicit capability, policy, and resource boundaries.

Coding agents are the most mature use case today: an agent can search code, edit files, review Git diffs, run pytest / Ruff / mypy, and launch controlled debugging commands without automatically gaining access to the rest of the host filesystem, arbitrary Docker settings, or unrestricted Git mutation. Higher-level agent runtimes can also use WorkspaceGuard as a workspace / code / process / task execution backend.

Core capabilities:

- **Safe workspace access** with path confinement, sensitive-path blocking, SHA-256 version checks, and atomic writes.
- **Recoverable changes** through an optional recycle bin, historical file reads, and a controlled first Git baseline.
- **Agent-friendly review** with bounded Git queries, `workspace_diff`, structured Tool Results, and self-describing MCP Resources.
- **Isolated execution** for pytest, Ruff, mypy, coverage, and approved commands inside pinned images and disposable workspace snapshots.
- **Explicit privilege gates** for Git writes, recycle-bin purge, container execution, and HTTP/OAuth exposure.

## Project boundary

WorkspaceGuard focuses on **execution**. It does not provide agent planning, memory, RAG, workflow orchestration, or adapters that turn ERP, CRM, email, databases, and other enterprise systems into business capabilities. Higher-level systems can expose those capabilities separately and use WorkspaceGuard when they need controlled workspace, code, command, or task execution.

Coding agents are therefore an important use case, not the architectural boundary. WorkspaceGuard's job is to provide secure, bounded, observable execution primitives rather than become a complete agent runtime or enterprise capability platform.

## What to know before using it

- One server instance owns one `--root`. Run separate instances for unrelated projects.
- Workspace tools are writable by default. Add `--read-only` when you only want the agent to inspect code.
- `--read-only` does not disable testing: authorized tasks can still run in disposable snapshots without writing back to the real workspace.
- Git initialization, the recycle bin, and container execution do not gain extra privileges automatically; each is explicitly enabled and configured.
- Execution profiles use pinned image digests / full local `sha256` image IDs, and callers cannot add network access, arbitrary host mounts, or swap images at runtime.

For personal local use, a good starting point is `stdio + --read-only`. Remove `--read-only` when you want direct edits, then enable Git writes, the recycle bin, or execution profiles only when you actually need them.

For the full threat model, file semantics, Git restrictions, and container boundary, see [SECURITY.md](SECURITY.md).

## Start in five minutes

### Install

Python 3.10+ is required. Git is only needed when using the Git tools.

```bash
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/workspace-guard-mcp \
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
    "workspaceguard-project": {
      "command": "/absolute/path/to/venv/bin/workspace-guard-mcp",
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
| Read-only Git | `git_status`, `git_diff`, `workspace_diff`, `git_log`, `git_show`, `git_branch`, `git_rev_parse`, `git_ls_files`, `git_read_file_at_revision` | Fixed-argument, bounded Git queries, workspace diff aggregation, and historical file reads |
| Git writes (optional) | `git_init`, `git_create_baseline` | Configured-root `main` initialization and one server-owned first baseline; disabled by default |
| Compatibility commands | `run_shell` | Closed read-only grammar; never starts a shell |
| Fixed tasks | `list_tasks`, `run_task` | Operator-defined synchronous tasks |
| Long-running tasks | `start_task`, `task_status`, `task_logs`, `stop_task` | Bounded stdout/stderr with cursor-based logs; `task_status` retains service compatibility semantics |
| Execution queries | `execution_status`, `execution_events`, `execution_artifacts` | Canonical current truth including terminal resource accounting, bounded lifecycle history, and terminal artifact metadata |
| Execution profiles | `list_execution_profiles`, `python_version`, `run_pytest`, `run_ruff`, `run_mypy`, `run_pytest_coverage`, `run_python_script`, `run_command`, `start_command` | Authorized execution and structured diagnostics in a pinned image and disposable snapshot |

Without a task config, the task manager, container backend, and dynamic execution tools are not created or registered.

## Structured Tool Results

Every actually registered public tool declares a stable `outputSchema`. Normal calls provide machine-readable `structuredContent` while preserving the existing human-readable `content` fallback. Agents should depend on structured fields instead of parsing display text; these result schemas are part of the public MCP contract. For example, `read_file_versioned` exposes `content`, `sha256`, and `size` directly, and later mutations should use that `sha256` as the version token. Failed `run_pytest` calls expose `failures[]`, `frames[]`, and redacted `locals[]` directly.

## Large Results

Small text results remain fully inline. Larger text that has already passed the existing workspace, Git, or execution safety boundary and source bounding keeps its original string field as a UTF-8-safe preview and adds an ephemeral `workspaceguard://result/{id}` URI in `structuredContent`. An execution result may look like:

```json
{
  "stdout": "...preview...",
  "source_truncated": false,
  "stdout_inline_truncated": true,
  "stdout_resource_uri": "workspaceguard://result/..."
}
```

`source_truncated` means the upstream safety layer already discarded data beyond a budget such as `max_output_bytes`; `*_inline_truncated` only means the server shortened the already-safe bounded result for MCP context size. Where the legacy `truncated` field applies, it remains backward-compatible as the union of source and inline truncation. A Result Resource can recover only the bounded safe result still held by the server. It cannot recover raw output already discarded by a source limit and never raises an existing output limit.

Result Resources live only in process memory, have a fixed 15-minute TTL, and disappear on server restart, expiration, or capacity eviction. Dynamic result URIs are never enumerated by `resources/list`; clients discover only the `workspaceguard://result/{id}` template. In HTTP/OAuth mode each result is also scoped to the authenticated owner that created it. In stdio/no-OAuth mode, access relies on the high-entropy, non-enumerable ephemeral capability URI.

## Execution Artifacts

The execution snapshot remains mounted only at `/workspace`. Each execution also receives a separate writable `/artifacts` mount and discovers it through `WORKSPACEGUARD_ARTIFACT_DIR=/artifacts`; the server never scans the workspace to guess which files are outputs. Round 3 admits only bounded top-level regular files from `/artifacts`; symlinks, directories, and special files fail closed.

After the execution is terminal, the server re-validates staging, streams admitted bytes into a private store while enforcing limits and computing SHA-256, and only then publishes immutable artifact metadata. Synchronous execution results include `artifacts[]`, and terminal executions can be queried with `execution_artifacts`. Artifact bytes are never inlined, never stored in ResultCache, and never written into the Execution SQLite database. They are exposed through `workspaceguard://artifact/{id}` as opaque `application/octet-stream` resources. The current ArtifactStore is process-local, ephemeral, and bounded by TTL, retained execution count, and total stored bytes, with whole-execution eviction.

## Resource Accounting

Resource Enforcement defines how much an execution is **allowed** to consume. Resource Accounting records the aggregate usage WorkspaceGuard **actually observed** for a terminal execution. Accounting is persisted as `ExecutionRecord.resources`; synchronous execution results, terminal `task_status`, and `execution_status` all project the same canonical truth. Running executions return `resources: null` rather than live partial metrics. The compatibility field `duration_ms` remains unchanged; adding `wall_time_ms` does not redefine its historical semantics.

```json
{
  "execution_id": "...",
  "resources": {
    "wall_time_ms": 1834,
    "cpu_time_ms": null,
    "peak_memory_bytes": null,
    "workspace_initial_bytes": 12873421,
    "workspace_final_bytes": 12881692,
    "workspace_growth_bytes": 8271,
    "stdout_bytes": 15320,
    "stderr_bytes": 3001,
    "output_bytes": 18321
  }
}
```

`wall_time_ms` is monotonic elapsed time from canonical execution creation through terminal completion, including snapshot preparation, runtime execution, artifact collection, and temporary cleanup. The workspace baseline is measured after all server-side snapshot initialization and before runtime startup. Writable executions are measured again after runtime termination and before snapshot cleanup; deletion can reduce final size, but growth is floored at zero. `stdout_bytes` and `stderr_bytes` are lifetime bytes observed from runtime pipes, so they can exceed retained output; server-generated diagnostic stderr is not counted as runtime usage.

The current CLI container backend has no reliable source for exact CPU time or true peak memory. Therefore `cpu_time_ms` and `peak_memory_bytes` are `null`. That means **unavailable**, not zero, and WorkspaceGuard does not substitute timeout, configured CPU/memory limits, or sampled estimates.

## Tool Annotations

Every public tool explicitly declares `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` so MCP clients and agents can reason about behavior. These values are hints, not authorization or security enforcement. The actual security boundary remains the workspace policy, SHA/version checks, recycle-bin transactions, OAuth scopes, execution profiles, and sandbox.

## Built-in Resources

The server self-describes its currently available capabilities and recommended workflows through MCP Resources, so agents do not need to memorize the full README or the profile taxonomy first. `internal://tool-catalog` contains only currently registered public tools and reuses the Tool Contract Registry directly. A full contract can be read on demand through `internal://tool-info/{name}`; unregistered capabilities are not exposed through self-description.

- `internal://instructions`: capability-aware safe operating guidance for the current server.
- `internal://tool-catalog`: a machine-readable current Tool summary with stable tool-name ordering.
- `internal://tool-info/{name}`: the full input/output contract and annotations for one currently registered Tool.
- `workspaceguard://result/{id}`: on-demand access to an ephemeral bounded large result in the current process; concrete URIs are not enumerable.
- `workspaceguard://artifact/{id}`: on-demand access to an immutable admitted binary artifact from a terminal execution, delivered as opaque binary by default.
- `internal://workflows/edit-file`, `debug-python`, `recover-file`, and `review-changes`: compact agent workflows.

The README remains an entry-point overview. Detailed agent SOPs live in these Resources and are generated against the current public capability surface.

`run_shell` accepts only `pwd`, `ls`, `cat`, `head`, `tail`, `tree`, `grep`, bounded `rg`, `find`, `wc`, `sed`, and fixed Git queries. Pipes, redirects, command substitution, environment expansion, and unlisted arguments are rejected.

Git review has two read-only views:

```python
git_diff(path="src")  # native tracked Git diff
workspace_diff(path="src")  # tracked final state + safe untracked text
```

`workspace_diff` does not stage files or modify the repository. Ignored, blocked, binary, oversized, and otherwise unsafe files never have their contents emitted. Output and scanning are bounded; partition large reviews with `path`.

### Use a controlled Git baseline

Enable `--allow-git-writes` (or `WORKSPACE_GUARD_MCP_ALLOW_GIT_WRITES=true`) to register `git_init` and `git_create_baseline`. They accept no caller parameters. `git_init` creates an ordinary non-bare `main` repository only at the configured workspace root and returns an idempotent result for an already-valid root repository. `repo_path`, subdirectory repositories, templates, separate Git directories, bare/shared/object-format options, and arbitrary Git argv are unsupported. `git_create_baseline` is allowed only once before the first commit, uses a fixed message and identity, and is not a general commit tool; blocked files, `.git`, the recycle bin, ignored directories, symlinks, and special files are excluded.

The first baseline also filters cross-project environment noise such as `.DS_Store`, `Thumbs.db`, `Desktop.ini`, Python bytecode/cache, and coverage files at any depth. It installs the same fixed rules in a managed block in the repository-private `.git/info/exclude`, so noise created after the baseline does not pollute `git_status`. This does not modify the project `.gitignore` or the user's global Git ignore. The migration boundary applies only to future baselines: noise already tracked by an older baseline is not automatically untracked or rewritten and must be explicitly migrated or rebuilt outside MCP.

To restore historical content, first call `read_file_versioned` for the current SHA, then call `git_read_file_at_revision(path, "HEAD")`, and finally use the existing `write_file(overwrite=true, expected_sha256=...)`. `run_shell` remains read-only. Task snapshots still exclude `.git`, and even a writable `run_command` profile never writes back to the real workspace. Git writes are not in the default OAuth scopes; HTTP callers must also hold `workspace.git.write`.

## Common workflows

### Read and edit code

Use `tree`, `read_file`, and `search_text` to locate code. For an existing file, pass the SHA-256 returned by versioned reads as `expected_sha256` when writing. A stale token returns an explicit conflict instead of silently overwriting a concurrent edit.

### Use the recycle bin

`--allow-trash` registers `trash_file`, `list_trashed_files`, `restore_trashed_file`, and `restore_trashed_file_to`. Every restore call requires the current SHA from the list result. If the original path now contains a new file, it is never overwritten: call `create_directory("recovered")` first and then use `restore_trashed_file_to(..., "recovered/basic.txt")`. The alternate-path tool requires both `workspace.delete` and `workspace.write`; its parent must already exist and its target must be empty. Agents should branch on the structured `error.code`, not parse English messages.

`--allow-trash-purge` is available only with trash and writes enabled and registers `purge_trashed_file`, which requires `workspace.delete` and `workspace.purge`. It requires the current SHA and permanently removes exactly one regular file; directories, globs, batches, and `empty_trash` are unsupported. A full quota rejects new trash and never evicts items automatically.

### Run tests or checks

Prefer an operator-defined `run_task("test")`. For controlled debugging and analysis, structured tools resolve a suitable profile server-side, so `list_execution_profiles` is normally capability discovery rather than a required preflight:

```python
run_pytest(targets=["tests/test_auth.py"], max_failures=3, show_locals=True)
run_ruff(paths=["src", "tests"])
run_mypy(paths=["src"])
run_pytest_coverage(targets=["tests"], branch=True, fail_under=85)
```

`run_pytest`, `run_ruff`, `run_mypy`, and `run_pytest_coverage` compile argv on the server, validate workspace paths, and return bounded structured diagnostics/failures. Pytest failure inspection includes the test, exception, workspace frames, and optional bounded locals with basic secret redaction. Generic `run_command`/`start_command` preserve caller argv and remain the escape hatch for project-specific probes, benchmarks, and uncommon analyzers:

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
- `run_pytest`: validates targets, compiles pytest argv, and collects bounded failure/frame/local information.
- `run_ruff`: runs a fixed Ruff check and returns JSON diagnostics; `fix=true` requires a writable profile.
- `run_mypy`: runs fixed mypy arguments; `strict=true` only adds `--strict`.
- `run_pytest_coverage`: runs pytest and coverage in one execution; data stays under `/tmp` and does not create workspace `.coverage`. By default it respects the project's coverage `source`/`branch` configuration, while `branch=true` explicitly enables branch coverage.
- `run_python_script`: accepts one real workspace `.py` file.
- `run_command`/`start_command`: require `allow_arbitrary_commands: true`; this grants arbitrary code execution inside the container.

An execution profile is operator security/environment policy, not a tool taxonomy the agent must memorize. Without `profile`, a structured tool first uses the top-level `default_profile`, then a unique capability candidate; multiple remaining candidates produce an explicit ambiguous-profile error. `run_command` and `start_command` always require an explicit profile and `allow_arbitrary_commands=true`. `list_execution_profiles` remains available for capability discovery and operator inspection and marks the default profile.

Callers cannot override environment, image, network, mounts, ports, resource limits, or container IDs. Every execution starts from a filtered temporary snapshot; snapshot changes are never written back to the real workspace.

### Building and switching the execution image

`examples/Dockerfile.task` is the source for the standard execution image and preinstalls offline tools such as pytest, coverage, Ruff, and mypy. Editing the Dockerfile does not update an already running MCP service automatically; the operator must rebuild the image, obtain its immutable image ID/digest, update the execution profile configuration outside the workspace, and restart the MCP service. For example:

```bash
docker build -f examples/Dockerfile.task -t workspace-guard-mcp-execution:local .
docker image inspect workspace-guard-mcp-execution:local --format '{{.Id}}'
```

Write the resulting full `sha256:...` value to the external `execution-profiles.json` `image` field, then restart the service. Use `list_execution_profiles` to inspect the profiles/capabilities currently exposed by the service, and verify the new image with `run_mypy` or `run_command` running `python -m mypy --version`. Runtime execution containers stay offline; dependencies belong in the image build stage rather than an agent-time `pip install`.

CI builds this Dockerfile and runs `python -m mypy --version` inside the resulting container with `--network none`, preventing drift where the Dockerfile claims mypy support but the built execution image does not actually provide it.

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
| Python explicit bytecode | `/tmp/cache/python` |

Normal Python import bytecode writes are disabled; explicit bytecode compilation such as `python -m compileall` is redirected to `/tmp/cache/python`. All paths share the 64 MiB `/tmp` tmpfs; modes such as `compileall -b` that explicitly request adjacent `.pyc` output are not supported for a read-only workspace. Explicit build and report artifacts such as `build/`, `dist/`, and `htmlcov/` are not redirected automatically; use tool arguments to place them under `/tmp` in a read-only profile, or use a constrained writable task/profile when workspace output is required.

## Configuration and deployment

Command-line arguments take precedence over environment variables. See the complete list with:

```bash
.venv/bin/workspace-guard-mcp --help
```

Common settings:

| Argument/environment variable | Purpose |
| --- | --- |
| `--root` / `WORKSPACE_GUARD_MCP_ROOT` | The one workspace root |
| `--read-only` / `WORKSPACE_GUARD_MCP_READ_ONLY` | Disable all workspace write tools |
| `--allow-git-writes` / `WORKSPACE_GUARD_MCP_ALLOW_GIT_WRITES` | Enable controlled Git initialization and first baseline; requires writable mode and does not accept arbitrary Git arguments |
| `--max-git-baseline-files` / `WORKSPACE_GUARD_MCP_MAX_GIT_BASELINE_FILES` | Maximum regular files in the first baseline |
| `--max-git-baseline-bytes` / `WORKSPACE_GUARD_MCP_MAX_GIT_BASELINE_BYTES` | Maximum aggregate payload bytes in the first baseline |
| `--allow-trash` / `WORKSPACE_GUARD_MCP_ALLOW_TRASH` | Enable the restricted recoverable single-file recycle bin |
| `--allow-trash-purge` / `WORKSPACE_GUARD_MCP_ALLOW_TRASH_PURGE` | Enable verified, irreversible single-item purge separately |
| `--max-trash-items` / `WORKSPACE_GUARD_MCP_MAX_TRASH_ITEMS` | Maximum retained recycle-bin entries (default 200) |
| `--max-trash-bytes` / `WORKSPACE_GUARD_MCP_MAX_TRASH_BYTES` | Maximum aggregate payload bytes (default 256 MiB) |
| `--block-path` | Add a root-relative blocked glob |
| `--ignore-dir` | Add a directory basename excluded from automatic scans |
| `--task-config` / `WORKSPACE_GUARD_MCP_TASK_CONFIG` | Trusted task JSON outside the workspace |
| `--execution-db` / `WORKSPACE_GUARD_MCP_EXECUTION_DB` | Optional ExecutionRecord + ExecutionEvent SQLite database outside the workspace |
| `--transport` | `stdio` or `streamable-http` |

WorkspaceGuard can optionally persist bounded, public-safe `ExecutionRecord` current truth and `ExecutionEvent` lifecycle history to an operator-controlled SQLite database outside the workspace. Record transitions and event appends commit in the same durable transaction. Without `--execution-db`, both are process-local in the InMemory store; with it, both persist across server restarts. Version 1 execution databases are automatically migrated to version 2 without fabricating pre-audit history, and `execution_events` reports `history_complete=false` for those partial histories. Persistence never scans for, reconnects to, or adopts previously running containers. Unfinished records left by an earlier process are reconciled to `CRASHED / SERVER_RESTARTED` on startup.

`stdio` is intended for local clients. Streamable HTTP listens on `127.0.0.1:3001/mcp` by default; non-loopback or public deployments need explicit network, Host/Origin, HTTPS, and external OAuth/OIDC configuration. `--allow-unauthenticated-http` is for temporary development only. See [SECURITY.md](SECURITY.md) for the full OAuth topology and RFC 9728 details.

## Project layout

```text
src/workspace_guard_mcp/
  workspace.py          # Safe paths, file I/O, traversal, and atomic writes
  access_policy.py      # Blocked globs, Git exclusion, and baseline noise policy
  trash.py              # Protected recycle-bin metadata, transactions, and recovery
  git_reader.py         # Bounded read-only Git adapter
  git_writer.py         # Controlled init, first baseline, and revision blob reads
  service.py            # run_shell grammar and application orchestration
  resources.py          # Pure self-description Resource builders and workflows
  server.py             # MCP tool/Resource registration, scopes, and auth challenges
  cli.py                # stdio/HTTP startup and OAuth configuration
  task_config.py        # Trusted task/profile JSON validation and freezing
  python_execution.py   # Structured Python/pytest/analysis argv compilation
  diagnostics.py        # Bounded Ruff/mypy/pytest/coverage result adapters
  pytest_debug_plugin.py # Snapshot-injected controlled pytest failure collector
  command_execution.py  # Generic argv and workspace cwd validation
  task_snapshot.py      # Filtered bounded temporary snapshots
  execution.py          # Canonical Execution record/event lifecycle domain model
  execution_store.py    # In-memory / SQLite record + append-only audit stores
  task_runner.py        # Docker/Podman argv, pipes, and synchronous execution
  task_manager.py       # Execution lifecycle, concurrency, and service sessions
tests/                   # Unit, boundary, and transport regression tests
examples/                # Digest-based config templates and task image
SECURITY.md              # Security boundary, threat model, and residual risk
```

## Development and quality gates

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python scripts/install_hooks.py

# Apply safe Ruff fixes and canonical formatting.
.venv/bin/python scripts/fix.py

# Run the same complete quality gate as CI.
.venv/bin/python scripts/check.py
```

`python scripts/check.py --fast` runs only Ruff lint, Ruff format checking, and `mypy src tests`; the repository pre-push hook uses this fast gate. The full gate additionally runs tests, branch coverage (minimum 85%), compile, build, and wheel smoke. CI on Python 3.10 and 3.13 invokes the same `scripts/check.py`, preventing local and CI command drift. The ordinary test suite does not require Docker/Podman.

## License

Apache License 2.0. See [LICENSE](LICENSE).
