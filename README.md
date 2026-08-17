# WorkspaceGuard MCP

[English](README.en.md) · [安全边界](SECURITY.md) · [任务模板](examples/tasks.json) · [Execution profile 模板](examples/execution-profiles.json)

WorkspaceGuard MCP 是 AI Coding Agent 与真实开发工作区之间的 capability-bounded 安全边界。它允许 Agent 在明确授权下读取、搜索、编辑和审查真实代码仓库，并通过隔离执行环境运行测试与诊断，同时限制宿主 Shell、敏感路径、危险 Git 操作和未经授权的执行能力。

## 先看这三点

- 一个服务实例只负责一个 `--root`。需要多个项目时，启动多个实例。
- 默认工作区工具可写；生产或只读场景应显式传 `--read-only`。
- `--read-only` 只关闭工作区写工具；已授权的任务仍在一次性快照中运行，不会写回真实工作区。
- Git 初始化和首次基线提交默认关闭；`--allow-git-writes` 只能在可写模式下开启，并需要额外的 `workspace.git.write` OAuth scope。
- 回收站默认关闭；`--allow-trash` 需要同时允许写入，提供单文件回收、原路径恢复和安全备用路径恢复。不可恢复的单项 purge 还需要单独的 `--allow-trash-purge`。
- 容器任务是可选能力，必须通过工作区外的可信 JSON 显式开启；配置文件中的 image 必须是完整 digest 或完整本地 `sha256` ID。

详细的威胁模型、文件安全语义、Git 约束和容器边界见 [SECURITY.md](SECURITY.md)。

## 5 分钟启动

### 安装

需要 Python 3.10+。Git 只在使用 Git 工具时需要。

```bash
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/workspace-guard-mcp \
  --root /absolute/path/to/project \
  --read-only
```

源码开发和测试：

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

### 连接 MCP 客户端

`stdio` 是默认且推荐的本地传输。通用客户端配置如下，把两个绝对路径替换成实际值：

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

兼容入口仍然可用：

```bash
.venv/bin/python server.py --root /absolute/path/to/project --read-only
```

## 工具地图

| 能力 | 工具 | 说明 |
| --- | --- | --- |
| 项目信息 | `project_info` | 根目录、读写模式和资源预算 |
| 目录 | `list_directory`, `tree` | 有界遍历，不跟随目录符号链接 |
| 文件 | `read_file`, `read_file_versioned`, `search_text` | 有界文本读取、搜索和 SHA-256 版本令牌 |
| 写入 | `create_directory`, `write_file`, `replace_text`, `append_file` | 原子写入；需要非只读模式 |
| 回收站（可选） | `trash_file`, `list_trashed_files`, `restore_trashed_file`, `restore_trashed_file_to` | 默认不注册；受限单文件回收和不覆盖恢复 |
| 永久清理（可选） | `purge_trashed_file` | 仅单独开启 purge 时注册；必须 SHA 校验，不可恢复 |
| Git 只读 | `git_status`, `git_diff`, `workspace_diff`, `git_log`, `git_show`, `git_branch`, `git_rev_parse`, `git_ls_files`, `git_read_file_at_revision` | 固定参数、有界的 Git 查询、工作区聚合 diff 和 HEAD 历史文件读取 |
| Git 写入（可选） | `git_init`, `git_create_baseline` | 仅配置 root、main 分支和服务器固定首次基线；默认不注册 |
| 兼容命令 | `run_shell` | 只解析封闭的只读语法，从不启动 Shell |
| 固定任务 | `list_tasks`, `run_task` | 操作者预定义的同步任务 |
| 长任务 | `start_task`, `task_status`, `task_logs`, `stop_task` | 有界 stdout/stderr 和 cursor 日志 |
| Execution profile | `list_execution_profiles`, `python_version`, `run_pytest`, `run_ruff`, `run_mypy`, `run_pytest_coverage`, `run_python_script`, `run_command`, `start_command` | 固定镜像、一次性快照中的授权执行与结构化诊断 |

没有提供 task config 时，任务管理器、容器后端和动态执行工具都不会创建或注册。

## Structured Tool Results

所有实际注册的公开工具都声明稳定的 `outputSchema`，正常调用同时提供 machine-readable `structuredContent` 和原有的人类可读 `content` fallback。Agent 应优先依赖结构化字段，而不是解析展示文本；这些 result schema 属于公开 MCP contract。例如 `read_file_versioned` 会直接暴露 `content`、`sha256`、`size` 等字段，后续写入应使用其中的 `sha256` 作为版本令牌；`run_pytest` 失败时可直接读取 `failures[]`、`frames[]` 和经过脱敏的 `locals[]`。

## Large Results

小型文本结果保持完整 inline。已经经过现有 workspace、Git、execution 安全边界并完成 source bounding 的较大文本，会保留原字符串字段作为 UTF-8 安全 preview，并在 `structuredContent` 中附加 ephemeral `workspaceguard://result/{id}` URI。例如 execution 结果可能包含：

```json
{
  "stdout": "...preview...",
  "source_truncated": false,
  "stdout_inline_truncated": true,
  "stdout_resource_uri": "workspaceguard://result/..."
}
```

`source_truncated` 表示上游安全层已经丢弃了超出 `max_output_bytes` 等预算的数据；`*_inline_truncated` 仅表示 Server 为了 MCP 上下文大小缩短了当前已经安全、有界的结果。兼容字段 `truncated` 在适用的文本/执行结果中表示两者的并集。Resource 只能恢复 Server 当前仍持有的 bounded safe result，不能恢复此前已经被 source limit 丢弃的 raw output，也不会提高任何原有输出上限。

Result Resource 仅保存在当前进程内存中，固定 TTL 为 15 分钟，Server 重启、过期或容量淘汰后 URI 会失效。URI 不会作为动态条目出现在 `resources/list`；客户端只会发现 `workspaceguard://result/{id}` template。HTTP/OAuth 模式下结果还会绑定生成结果时的 authenticated owner；stdio/无 OAuth 模式依赖高熵、不可枚举的 ephemeral capability URI。

## Tool Annotations

每个公开工具都显式声明 `readOnlyHint`、`destructiveHint`、`idempotentHint` 和 `openWorldHint`，用于帮助 MCP client/agent 理解工具行为。它们只是行为提示，不是授权或安全机制；实际安全仍由 workspace policy、SHA/version checks、回收站事务、OAuth scopes、execution profiles 和 sandbox 强制执行。

## 内置 Resources

Server 通过 MCP Resources 自描述当前可用能力和推荐工作流，Agent 不需要先记住完整 README 或 profile taxonomy。`internal://tool-catalog` 只列当前实际注册的公开 Tools，并直接复用 Tool Contract Registry；完整单工具 contract 通过 `internal://tool-info/{name}` 按需读取，未注册能力不会通过 self-description 暴露。

- `internal://instructions`：当前 Server 的安全操作原则和能力感知使用指南。
- `internal://tool-catalog`：机器可消费、按 tool name 稳定排序的当前 Tool 摘要。
- `internal://tool-info/{name}`：一个当前已注册 Tool 的完整 input/output contract 与 annotations。
- `workspaceguard://result/{id}`：按需读取当前进程中的 ephemeral bounded large result；具体 URI 不可枚举。
- `internal://workflows/edit-file`、`debug-python`、`recover-file`、`review-changes`：面向 Agent 的紧凑工作流。

README 只提供入口概览；详细 SOP 由这些 Resources 自己承担，并随当前实际 capability surface 生成。

`run_shell` 只接受 `pwd`、`ls`、`cat`、`head`、`tail`、`tree`、`grep`、受限 `rg`、`find`、`wc`、`sed` 以及固定 Git 查询。管道、重定向、命令替换、环境变量展开和未列出的参数都会被拒绝。

Git review 可使用两个只读视图：

```python
git_diff(path="src")  # 原生 tracked Git diff
workspace_diff(path="src")  # tracked final state + safe untracked text
```

`workspace_diff` 不会 stage 文件或修改仓库；ignored、blocked、binary、oversized 和其他不安全文件不会输出内容。输出与扫描都受全局预算限制，大型审查应按 `path` 分区调用。

### 使用受控 Git 基线

启用 `--allow-git-writes`（或 `WORKSPACE_GUARD_MCP_ALLOW_GIT_WRITES=true`）后，服务才注册 `git_init` 和 `git_create_baseline`。它们没有调用方参数：`git_init` 只在当前配置的 workspace root 创建普通、非 bare 的 `main` 仓库，并对已存在的有效 root 仓库幂等返回；不支持 `repo_path`、子目录仓库、template、separate-git-dir、bare 或任意 Git argv。`git_create_baseline` 只能执行一次首次基线，使用固定消息和身份，不是通用 commit；blocked 文件、`.git`、回收站、ignored 目录、symlink 和特殊文件不会进入基线。

首次基线还会过滤跨项目的环境噪声（例如任意深度的 `.DS_Store`、`Thumbs.db`、`Desktop.ini`、Python bytecode/cache 和 coverage 文件），并把同一组固定规则以 managed block 安装到仓库私有的 `.git/info/exclude`，因此基线后新出现的噪声也不会污染 `git_status`。这不会修改项目 `.gitignore` 或用户全局 Git ignore。该迁移边界只保证未来创建的 baseline；旧版本已经 tracked 的噪声不会自动解除跟踪，也不会被重写，需由操作者在 MCP 外明确迁移或重建 baseline。

恢复历史内容时，先用 `read_file_versioned` 取得当前 SHA，再用 `git_read_file_at_revision(path, "HEAD")` 读取基线内容，最后使用现有 `write_file(overwrite=true, expected_sha256=...)` 写回。`run_shell` 仍然只读；task snapshot 仍排除 `.git`，`run_command` 即使在 writable profile 中运行也不会写回真实 workspace。Git 写入能力不属于默认 OAuth scope，HTTP 调用必须同时拥有 `workspace.git.write`。

## 常见工作流

### 读取和编辑代码

先用 `tree`/`read_file`/`search_text` 定位，再使用版本化读取返回的 SHA-256 作为写入操作的 `expected_sha256`。并发修改时，过期令牌会得到明确的 conflict，而不是静默覆盖。

### 使用回收站

`--allow-trash` 注册 `trash_file`、`list_trashed_files`、`restore_trashed_file` 和 `restore_trashed_file_to`。恢复工具都必须使用 list 返回的当前 SHA；原路径已有新文件时不会覆盖，可以先调用 `create_directory("recovered")`，再调用 `restore_trashed_file_to(..., "recovered/basic.txt")`。`restore_trashed_file_to` 需要 `workspace.delete` 和 `workspace.write`，目标父目录必须已存在且目标必须为空。机器应根据结构化错误中的 `error.code` 分支，而不是解析英文 message。

`--allow-trash-purge` 只在同时启用回收站和写入时生效，并注册需要 `workspace.delete` 与 `workspace.purge` 的 `purge_trashed_file`。它必须携带当前 SHA，只能永久清理单个普通文件；不支持目录、glob、批量或 `empty_trash`。quota 满时会拒绝新回收，不会自动清理。

### 运行测试或检查

优先使用操作者定义的 `run_task("test")`。需要受控调试或静态检查时，structured tools 会在服务端自动解析合适的 profile，通常不需要先调用 `list_execution_profiles`：

```python
run_pytest(targets=["tests/test_auth.py"], max_failures=3, show_locals=True)
run_ruff(paths=["src", "tests"])
run_mypy(paths=["src"])
run_pytest_coverage(targets=["tests"], branch=True, fail_under=85)
```

`run_pytest`、`run_ruff`、`run_mypy` 和 `run_pytest_coverage` 的 argv 都由服务端生成，路径经过 workspace validator，并返回有界的结构化 diagnostics/failures。pytest failure inspection 会返回 test、exception、workspace frame 和可选的受限 locals；敏感 local 名称会脱敏。通用 `run_command`/`start_command` 不改写 caller argv；它们仍是用于项目特有探针、benchmark 和不常见 analyzer 的 escape hatch：

```python
run_command(
    profile="coding",
    program="pytest",
    args=["-o", "cache_dir=/tmp/cache/pytest", "-q", "tests"],
)
```

### 观察长运行服务

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

`start_task` 和 `start_command` 不映射端口，只用于启动诊断和日志观察。

## 容器任务和 Execution profile

### 固定任务

复制 [examples/tasks.json](examples/tasks.json) 到工作区外，替换明显的占位符并删除不需要的任务。每个任务固定 image、argv、运行模式和 workspace 访问模式；服务不会在运行时下载依赖。

需要生成 workspace 产物的任务必须显式使用 `"workspace_access": "writable"`，并配置文件大小、总增长和 best-effort 磁盘限制。普通任务默认使用只读 bind mount。

### Execution profile

复制 [examples/execution-profiles.json](examples/execution-profiles.json) 到工作区外。profile 固定 image、工具集合和 workspace 访问模式：

- `python_version`：在容器内运行 `python --version`。
- `run_pytest`：服务端验证 target、生成 pytest argv，并返回有界 failure/frame/local 信息。
- `run_ruff`：固定 Ruff check argv，返回 JSON diagnostics；`fix=true` 仅允许 writable profile。
- `run_mypy`：固定 mypy argv，`strict=true` 只增加 `--strict`。
- `run_pytest_coverage`：在一次 execution 内完成 pytest 与 coverage，数据只写入 `/tmp`，不产生 workspace `.coverage`；默认尊重项目自己的 coverage `source`/`branch` 配置，`branch=true` 可显式启用分支覆盖率。
- `run_python_script`：只接受一个真实的 workspace `.py` 文件。
- `run_command`/`start_command`：只有同时声明 `allow_arbitrary_commands: true` 才能使用；这代表容器内任意代码执行授权。

profile 是 operator 的安全与环境策略，不是 agent 必须记住的工具分类。structured tool 未指定 profile 时优先使用顶层 `default_profile`，否则使用唯一 capability 候选；仍有多个候选时返回明确的 ambiguous profile error。`run_command` 和 `start_command` 始终要求显式 profile，并继续要求 `allow_arbitrary_commands=true`。`list_execution_profiles` 仍用于 capability discovery/operator inspection，并标记默认 profile。

调用方不能覆盖环境变量、镜像、网络、挂载、端口、资源限制或容器 ID。每次执行都会先建立过滤后的临时快照；快照修改不会写回真实工作区。

### 构建和切换 Execution image

`examples/Dockerfile.task` 是标准 execution image 的来源，预装 pytest、coverage、Ruff、mypy 等离线执行工具。修改 Dockerfile 并不会自动更新已经运行的 MCP；operator 必须显式重建镜像、取得不可变 image ID/digest、更新工作区外的 execution profile 配置，然后重启 MCP 服务。例如：

```bash
docker build -f examples/Dockerfile.task -t workspace-guard-mcp-execution:local .
docker image inspect workspace-guard-mcp-execution:local --format '{{.Id}}'
```

将输出的完整 `sha256:...` 写入外部 `execution-profiles.json` 的 `image` 字段，再重启服务。可用 `list_execution_profiles` 检查服务当前公开的 profile/capability，并用 `run_mypy` 或 `run_command` 执行 `python -m mypy --version` 验证新镜像已经生效。运行中的 execution container 保持无网络；依赖应在 image build 阶段安装，而不是在 agent 执行期间临时 `pip install`。

CI 会实际构建该 Dockerfile，并在 `--network none` 的运行容器中执行 `python -m mypy --version`，防止“Dockerfile 声称支持 mypy，但发布镜像缺失”的漂移。

## 只读 profile 的临时缓存

固定容器环境把常见缓存放在受 64 MiB tmpfs 限制的 `/tmp`：

| 工具/状态 | 路径 |
| --- | --- |
| `HOME`, `TMPDIR` | `/tmp/home`, `/tmp` |
| XDG cache | `/tmp/cache` |
| Ruff | `/tmp/cache/ruff` |
| mypy | `/tmp/cache/mypy` |
| coverage | `/tmp/.coverage` |
| npm | `/tmp/npm-cache` |
| Python explicit bytecode | `/tmp/cache/python` |

普通 Python import 的 bytecode 写入被禁用；`python -m compileall` 等显式 bytecode compilation 重定向到 `/tmp/cache/python`。所有路径共享 64 MiB `/tmp` tmpfs；`compileall -b` 等要求把 `.pyc` 写到源文件旁的模式不适用于只读 workspace。`build/`、`dist/`、`htmlcov/` 等显式构建或报告产物不会自动重定向；只读 profile 应通过工具参数写入 `/tmp`，需要写 workspace 时使用受限的 writable task/profile。

## 配置和部署

命令行参数优先于环境变量。完整参数和环境变量列表：

```bash
.venv/bin/workspace-guard-mcp --help
```

最常用的配置包括：

| 参数/环境变量 | 用途 |
| --- | --- |
| `--root` / `WORKSPACE_GUARD_MCP_ROOT` | 唯一 workspace 根目录 |
| `--read-only` / `WORKSPACE_GUARD_MCP_READ_ONLY` | 关闭所有工作区写工具 |
| `--allow-git-writes` / `WORKSPACE_GUARD_MCP_ALLOW_GIT_WRITES` | 开启受控 Git 初始化和首次基线；要求可写模式，且不支持任意 Git 参数 |
| `--max-git-baseline-files` / `WORKSPACE_GUARD_MCP_MAX_GIT_BASELINE_FILES` | 首次基线允许的普通文件数上限 |
| `--max-git-baseline-bytes` / `WORKSPACE_GUARD_MCP_MAX_GIT_BASELINE_BYTES` | 首次基线 payload 总字节上限 |
| `--allow-trash` / `WORKSPACE_GUARD_MCP_ALLOW_TRASH` | 开启受限、可恢复的单文件回收站 |
| `--allow-trash-purge` / `WORKSPACE_GUARD_MCP_ALLOW_TRASH_PURGE` | 单独开启经过 SHA 校验的不可恢复单项 purge |
| `--max-trash-items` / `WORKSPACE_GUARD_MCP_MAX_TRASH_ITEMS` | 回收站最多保留的条目数（默认 200） |
| `--max-trash-bytes` / `WORKSPACE_GUARD_MCP_MAX_TRASH_BYTES` | 回收站 payload 总字节上限（默认 256 MiB） |
| `--block-path` | 追加 root-relative blocked glob |
| `--ignore-dir` | 追加不主动扫描的目录 basename |
| `--task-config` / `WORKSPACE_GUARD_MCP_TASK_CONFIG` | 工作区外的可信任务 JSON |
| `--transport` | `stdio` 或 `streamable-http` |

`stdio` 适合本机连接。Streamable HTTP 默认只监听 `127.0.0.1:3001/mcp`；非回环或公开部署需要明确的网络开关、Host/Origin 配置、HTTPS 和外部 OAuth/OIDC。`--allow-unauthenticated-http` 仅用于临时开发，不应作为部署方案。完整 OAuth 拓扑和 RFC 9728 细节见 [SECURITY.md](SECURITY.md)。

## 项目结构

```text
src/workspace_guard_mcp/
  workspace.py          # 安全路径、文件 IO、遍历和原子写入
  access_policy.py      # blocked glob、Git 排除和 baseline noise 策略
  trash.py              # 受保护回收站元数据、事务和恢复
  git_reader.py         # 有界只读 Git 适配器
  git_writer.py         # 受控初始化、首次基线和 revision blob 读取
  service.py            # run_shell 语法和应用编排
  resources.py          # Self-description Resource 纯构建逻辑和工作流
  server.py             # MCP 工具/Resource 注册、scope 检查和认证 challenge
  cli.py                # stdio/HTTP 启动和 OAuth 配置
  task_config.py        # 工作区外 task/profile JSON 的验证与冻结
  python_execution.py   # 结构化 Python/pytest/analysis argv 编译
  diagnostics.py        # 有界的 Ruff/mypy/pytest/coverage 结果适配
  pytest_debug_plugin.py # 快照内注入的受控 pytest failure collector
  command_execution.py  # 通用命令 argv 和 workspace cwd 校验
  task_snapshot.py      # 过滤后的有界临时快照
  task_runner.py        # Docker/Podman argv、pipe 和同步执行
  task_manager.py       # 并发、服务生命周期和日志 ring buffer
tests/                   # 单元、边界和传输回归测试
examples/                # 必须替换 digest 的配置模板和任务镜像
SECURITY.md              # 安全边界、威胁模型和剩余风险
```

## 开发和质量门禁

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python scripts/install_hooks.py

# 自动修复 Ruff 可安全修复的问题并统一格式化
.venv/bin/python scripts/fix.py

# 与 CI 相同的完整质量门禁
.venv/bin/python scripts/check.py
```

`python scripts/check.py --fast` 只执行 Ruff lint、Ruff format check 和 `mypy src tests`，仓库的 pre-push hook 使用这个快速门禁。完整门禁在此基础上继续执行测试、分支覆盖率（最低 85%）、compile、build 和 wheel smoke。CI 在 Python 3.10 和 3.13 上直接调用同一个 `scripts/check.py`，避免本地与 CI 的检查命令漂移。普通测试不需要 Docker/Podman。

## 许可证

Apache License 2.0。详见 [LICENSE](LICENSE)。
