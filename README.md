# Sandboxed Workspace MCP

[English](README.en.md) · [安全边界](SECURITY.md) · [任务模板](examples/tasks.json) · [Execution profile 模板](examples/execution-profiles.json)

把一个本地项目根目录安全地暴露为 MCP 工具：可以读取、搜索、编辑文本和查询 Git；也可以在操作者明确授权后，把工作区快照交给 Docker/Podman 中的固定任务或执行 profile。默认不会执行项目代码、清理文件或提供宿主 Shell、端口映射；显式开启后提供受限、可恢复的回收站，永久清理仍需单独授权。

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
.venv/bin/sandboxed-workspace-mcp \
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
| Git 只读 | `git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_rev_parse`, `git_ls_files`, `git_read_file_at_revision` | 固定参数、有界的 Git 查询和 HEAD 历史文件读取 |
| Git 写入（可选） | `git_init`, `git_create_baseline` | 仅配置 root、main 分支和服务器固定首次基线；默认不注册 |
| 兼容命令 | `run_shell` | 只解析封闭的只读语法，从不启动 Shell |
| 固定任务 | `list_tasks`, `run_task` | 操作者预定义的同步任务 |
| 长任务 | `start_task`, `task_status`, `task_logs`, `stop_task` | 有界 stdout/stderr 和 cursor 日志 |
| Execution profile | `list_execution_profiles`, `python_version`, `run_pytest`, `run_python_script`, `run_command`, `start_command` | 固定镜像、一次性快照中的授权执行 |

没有提供 task config 时，任务管理器、容器后端和动态执行工具都不会创建或注册。

`run_shell` 只接受 `pwd`、`ls`、`cat`、`head`、`tail`、`tree`、`grep`、受限 `rg`、`find`、`wc`、`sed` 以及固定 Git 查询。管道、重定向、命令替换、环境变量展开和未列出的参数都会被拒绝。

### 使用受控 Git 基线

启用 `--allow-git-writes`（或 `SANDBOXED_WORKSPACE_MCP_ALLOW_GIT_WRITES=true`）后，服务才注册 `git_init` 和 `git_create_baseline`。它们没有调用方参数：`git_init` 只在当前配置的 workspace root 创建普通、非 bare 的 `main` 仓库，并对已存在的有效 root 仓库幂等返回；不支持 `repo_path`、子目录仓库、template、separate-git-dir、bare 或任意 Git argv。`git_create_baseline` 只能执行一次首次基线，使用固定消息和身份，不是通用 commit；blocked 文件、`.git`、回收站、ignored 目录、symlink 和特殊文件不会进入基线。

首次基线还会过滤跨项目的环境噪声（例如任意深度的 `.DS_Store`、`Thumbs.db`、`Desktop.ini`、Python bytecode/cache 和 coverage 文件），并把同一组固定规则以 managed block 安装到仓库私有的 `.git/info/exclude`，因此基线后新出现的噪声也不会污染 `git_status`。这不会修改项目 `.gitignore` 或用户全局 Git ignore。该迁移边界只保证未来创建的 baseline；旧版本已经 tracked 的噪声不会自动解除跟踪，也不会被重写，需由操作者在 MCP 外明确迁移或重建 baseline。

恢复历史内容时，先用 `read_file_versioned` 取得当前 SHA，再用 `git_read_file_at_revision(path, "HEAD")` 读取基线内容，最后使用现有 `write_file(overwrite=true, expected_sha256=...)` 写回。`run_shell` 仍然只读；task snapshot 仍排除 `.git`，`run_command` 即使在 writable profile 中运行也不会写回真实 workspace。Git 写入能力不属于默认 OAuth scope，HTTP 调用必须同时拥有 `workspace.git.write`。

## 常见工作流

### 读取和编辑代码

先用 `tree`/`read_file`/`search_text` 定位，再使用版本化读取返回的 SHA-256 作为写入操作的 `expected_sha256`。并发修改时，过期令牌会得到明确的 conflict，而不是静默覆盖。

### 使用回收站

`--allow-trash` 注册 `trash_file`、`list_trashed_files`、`restore_trashed_file` 和 `restore_trashed_file_to`。恢复工具都必须使用 list 返回的当前 SHA；原路径已有新文件时不会覆盖，可以先调用 `create_directory("recovered")`，再调用 `restore_trashed_file_to(..., "recovered/basic.txt")`。`restore_trashed_file_to` 需要 `workspace.delete` 和 `workspace.write`，目标父目录必须已存在且目标必须为空。机器应根据结构化错误中的 `error.code` 分支，而不是解析英文 message。

`--allow-trash-purge` 只在同时启用回收站和写入时生效，并注册需要 `workspace.delete` 与 `workspace.purge` 的 `purge_trashed_file`。它必须携带当前 SHA，只能永久清理单个普通文件；不支持目录、glob、批量或 `empty_trash`。quota 满时会拒绝新回收，不会自动清理。

### 运行测试或检查

优先使用操作者定义的 `run_task("test")`。需要受控探索时，使用已授权的 profile：

```python
run_pytest(profile="python-debug", targets=["tests"], quiet=True)
run_command(profile="coding", program="ruff", args=["check", "."])
```

`run_pytest` 的 argv 由服务端生成，并自动把 pytest cache 放到 `/tmp/cache/pytest`。通用 `run_command`/`start_command` 不改写 caller argv；直接调用 pytest 时显式传入：

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
- `run_pytest`：服务端验证 target 并生成 pytest argv。
- `run_python_script`：只接受一个真实的 workspace `.py` 文件。
- `run_command`/`start_command`：只有同时声明 `allow_arbitrary_commands: true` 才能使用；这代表容器内任意代码执行授权。

调用方不能覆盖环境变量、镜像、网络、挂载、端口、资源限制或容器 ID。每次执行都会先建立过滤后的临时快照；快照修改不会写回真实工作区。

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

同时禁用 Python bytecode 和 pip cache。`build/`、`dist/`、`htmlcov/` 等显式构建或报告产物不会自动重定向；只读 profile 应通过工具参数写入 `/tmp`，需要写 workspace 时使用受限的 writable task/profile。

## 配置和部署

命令行参数优先于环境变量。完整参数和环境变量列表：

```bash
.venv/bin/sandboxed-workspace-mcp --help
```

最常用的配置包括：

| 参数/环境变量 | 用途 |
| --- | --- |
| `--root` / `SANDBOXED_WORKSPACE_MCP_ROOT` | 唯一 workspace 根目录 |
| `--read-only` / `SANDBOXED_WORKSPACE_MCP_READ_ONLY` | 关闭所有工作区写工具 |
| `--allow-git-writes` / `SANDBOXED_WORKSPACE_MCP_ALLOW_GIT_WRITES` | 开启受控 Git 初始化和首次基线；要求可写模式，且不支持任意 Git 参数 |
| `--max-git-baseline-files` / `SANDBOXED_WORKSPACE_MCP_MAX_GIT_BASELINE_FILES` | 首次基线允许的普通文件数上限 |
| `--max-git-baseline-bytes` / `SANDBOXED_WORKSPACE_MCP_MAX_GIT_BASELINE_BYTES` | 首次基线 payload 总字节上限 |
| `--allow-trash` / `SANDBOXED_WORKSPACE_MCP_ALLOW_TRASH` | 开启受限、可恢复的单文件回收站 |
| `--allow-trash-purge` / `SANDBOXED_WORKSPACE_MCP_ALLOW_TRASH_PURGE` | 单独开启经过 SHA 校验的不可恢复单项 purge |
| `--max-trash-items` / `SANDBOXED_WORKSPACE_MCP_MAX_TRASH_ITEMS` | 回收站最多保留的条目数（默认 200） |
| `--max-trash-bytes` / `SANDBOXED_WORKSPACE_MCP_MAX_TRASH_BYTES` | 回收站 payload 总字节上限（默认 256 MiB） |
| `--block-path` | 追加 root-relative blocked glob |
| `--ignore-dir` | 追加不主动扫描的目录 basename |
| `--task-config` / `SANDBOXED_WORKSPACE_MCP_TASK_CONFIG` | 工作区外的可信任务 JSON |
| `--transport` | `stdio` 或 `streamable-http` |

`stdio` 适合本机连接。Streamable HTTP 默认只监听 `127.0.0.1:3001/mcp`；非回环或公开部署需要明确的网络开关、Host/Origin 配置、HTTPS 和外部 OAuth/OIDC。`--allow-unauthenticated-http` 仅用于临时开发，不应作为部署方案。完整 OAuth 拓扑和 RFC 9728 细节见 [SECURITY.md](SECURITY.md)。

## 项目结构

```text
src/sandboxed_workspace_mcp/
  workspace.py          # 安全路径、文件 IO、遍历和原子写入
  access_policy.py      # blocked glob、Git 排除和 baseline noise 策略
  trash.py              # 受保护回收站元数据、事务和恢复
  git_reader.py         # 有界只读 Git 适配器
  git_writer.py         # 受控初始化、首次基线和 revision blob 读取
  service.py            # run_shell 语法和应用编排
  server.py             # MCP 工具注册、scope 检查和认证 challenge
  cli.py                # stdio/HTTP 启动和 OAuth 配置
  task_config.py        # 工作区外 task/profile JSON 的验证与冻结
  python_execution.py   # 结构化 Python/pytest argv 编译
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
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
PYTHONPATH=src .venv/bin/python -m coverage run -m unittest discover -s tests -v
.venv/bin/python -m coverage report --fail-under=85
.venv/bin/python -m compileall -q server.py src tests scripts
.venv/bin/python -m build
.venv/bin/python scripts/wheel_smoke.py dist/*.whl
```

CI 在 Python 3.10 和 3.13 上执行 lint、format、测试、覆盖率、compile、build 和 wheel smoke。普通测试不需要 Docker/Podman。

## 许可证

Apache License 2.0。详见 [LICENSE](LICENSE)。
