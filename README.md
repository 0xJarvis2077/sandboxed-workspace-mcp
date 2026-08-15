# Sandboxed Workspace MCP

把一个明确指定的本地项目目录，以默认安全、契约明确且资源有界的方式暴露为 MCP 工具。服务提供文本读取与搜索、原子文本写入、目录树，以及严格白名单化的只读 Git 查询。默认配置仍不执行任何项目代码；服务操作者可以额外提供工作区外的可信 task config，授权一组只能在 Docker/Podman 隔离快照中运行的固定任务。它始终不提供真实 Shell、任意命令/参数执行或删除工具。

## 安装与推荐配置

要求 Python 3.10+；Git 仅影响 Git 工具。

```bash
python -m venv .venv
.venv/bin/pip install .
.venv/bin/sandboxed-workspace-mcp --root /absolute/path/to/project
```

源码开发可安装开发依赖：

```bash
.venv/bin/pip install -e ".[dev]"
```

`stdio` 是默认且推荐的传输。一个通用 MCP 客户端配置：

```json
{
  "mcpServers": {
    "sandboxed-project": {
      "command": "/absolute/path/to/venv/bin/sandboxed-workspace-mcp",
      "args": ["--root", "/absolute/path/to/project", "--read-only"]
    }
  }
}
```

服务坚持“一实例一根目录”。需要访问多个互不相干的目录时，应配置多个 MCP 实例，每个实例使用独立的 `--root`、权限和规则；服务不会在运行时动态切换或增加根目录。

顶层兼容入口继续可用：

```bash
.venv/bin/python server.py --root /absolute/path/to/project
```

## 工具与只读发现

| 分组 | 工具 | 行为 |
| --- | --- | --- |
| 项目信息 | `project_info` | 显示唯一根目录、读写模式和扫描预算 |
| 目录 | `list_directory`, `tree` | 不跟随目录符号链接，扫描预算与返回上限分离 |
| 读取 | `read_file`, `read_file_versioned`, `search_text` | 只处理有界 UTF-8 文本和普通文件；版本化读取返回 SHA-256 |
| 写入 | `create_directory`, `write_file`, `replace_text`, `append_file` | 核心层检查写权限、路径策略、版本令牌和原子替换 |
| Git | `git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_rev_parse`, `git_ls_files` | 结构化只读查询；路径为 literal pathspec，历史输出应用 blocked 排除 |
| 兼容接口 | `run_shell` | 解析封闭命令语法，从不启动 Shell |
| 沙箱任务（仅配置后） | `list_tasks`, `run_task` | 枚举或同步运行操作者预定义的 `run` 任务 |
| 沙箱服务（仅配置后） | `start_task`, `task_status`, `task_logs`, `stop_task` | 管理本实例启动的 `service` 任务和有界日志；不映射端口 |
| Python profile（仅显式配置后） | `list_execution_profiles`, `python_version`, `run_pytest`, `run_python_script` | 服务端编译结构化参数，在固定镜像与一次性快照中执行；需要 `tasks.run` |

当 `--read-only` 或 `SANDBOXED_WORKSPACE_MCP_READ_ONLY=true` 时，MCP 工具发现中不会注册四个写工具；即使调用方绕过工具发现直接使用核心 `Workspace`，核心层仍会拒绝写入。

文件只读模式与任务授权相互独立：`--read-only` 仍可运行已配置任务，因为任务只能写入一次性快照，不能写回真实工作区。未提供 task config 时，任务管理器和容器后端不会创建，任何 task/profile 动态执行工具都不会注册。

`run_shell` 只接受以下形态，并把所有路径继续交给核心策略：

```text
pwd
ls [-alh1] [path]
cat <file>
head [-n lines] <file>          # 也兼容 head <file> [lines]
tail [-n lines] <file>          # 也兼容 tail <file> [lines]
tree [-L depth] [path]
grep <text> <file>
rg [-niSF] [-g GLOB] <pattern> [path]
rg --files [-g GLOB] [path]
find [path] [-maxdepth N] [-type f|d] [-name glob]
wc -l|-w|-c <file>
sed -n '<start>[,<end>]p' <file>
git status [--short|--porcelain]
git diff [--cached|--staged] [-- FILE]
git log [--oneline] [-n N]       # 也保留 git log -N 兼容形态
git show COMMIT [-- FILE]
git branch [--show-current]
git rev-parse (--is-inside-work-tree|--show-prefix|--show-toplevel|HEAD)
git ls-files
```

其中 `ls` 的 `-a/-l/-h/-1` 作为常见调用兼容选项接受，返回仍是 MCP
统一的有界目录列表。`rg` 默认使用本项目内置的 Thompson NFA 非回溯正则子集：
支持字面字符、`.`、字符类/范围、分组、`|`、`*`/`+`/`?` 和 `^`/`$`；
`-F` 改为字面量，`-i` 忽略大小写，`-S` 在 pattern 不含大写字符时忽略大小写，
`-n` 显示行号。反向引用、lookaround、`\d` 等 shorthand class 和 `{m,n}`
会得到明确“不支持”错误，不会交给 Python `re` 或静默转成字面量。`-g` 的受控
glob 只过滤已由核心策略发现的候选，不能重新包含 blocked、ignored 或符号链接内容。
`find` 只支持深度、类型和文件名 glob 三类筛选，且不跟随符号链接。`wc` 只统计
单个普通文件，`sed` 只实现只读行区间打印。以上命令继续受 blocked、文件大小、
扫描条目、实际读取字节、搜索时间、并发、结果数和输出大小限制。

这不是通用 Shell：管道、重定向、命令替换、环境变量展开、通配符展开、
`find -exec` 以及未列出的参数仍会被拒绝。

Git 参数逐项白名单化，不会转发任意用户参数。`FILE` 先经过 workspace、blocked
和符号链接策略校验，再由服务端编码成 `:(literal)` pathspec；以 `-`、`:` 开头
的文件名不会成为 option 或 pathspec magic。`git show` 的 revision 只接受 `HEAD`
或 7–40 位十六进制 ID，并由服务端强制解析为 commit 对象（blob ID 会失败）；完整
commit diff 也强制添加 blocked 排除，因此历史
revision/path 语法不能绕过策略。因此 `git diff --no-index`、外部路径、hooks、pager、
prompt、external diff、textconv 和 fsmonitor 均不能借此执行额外程序或越界读取。
Git 非零退出、启动失败、超时和输出溢出都会成为 MCP 错误，而不是看似成功的文本结果。

## ignored 与 blocked

二者用途不同：

- `ignored` 只表示 tree/search 不主动递归扫描该目录，用于跳过依赖、缓存和构建产物。它不是安全边界；直接指定 ignored 路径仍可访问，除非该路径同时 blocked。
- `blocked` 是核心访问策略。命中的路径不能被 list、tree、read、search、write、replace、append、create_directory 或兼容 `run_shell` 访问；直接路径、根内绝对路径和指向 blocked 目标的符号链接都不能绕过。

默认 blocked 规则包括：

```text
.git
.env
.env.*
*.key, *.pem, *.p12, *.pfx, *.ppk
id_dsa*, id_ecdsa*, id_ed25519*, id_rsa*
```

`.env.example`、`.env.sample` 和 `.env.template` 是内置的精确安全示例例外，可由文件工具直接访问；`.env.production.example` 等其他名字仍被 `.env.*` 阻止。用户可以额外添加精确 blocked 规则再次阻止这些示例。为了防止 Git diff 泄露，Git status/diff 的排除 pathspec 对 `.env.*` 采取更保守行为，因此也会省略这些示例文件。

本版本不提供用户级 allow override，避免一个宽泛允许规则削弱默认安全策略。

追加 blocked 规则：

```bash
.venv/bin/sandboxed-workspace-mcp \
  --root /path/to/project \
  --block-path 'secrets/**' \
  --block-path '*.credential'
```

或使用逗号分隔环境变量：

```bash
SANDBOXED_WORKSPACE_MCP_BLOCKED_PATHS='secrets/**,*.credential'
```

规则仅支持字面量、`/`、`*`、`?` 和 `**`：不含 `/` 的规则匹配任意深度的文件名或目录名；含 `/` 的规则从根目录匹配；命中目录即阻止其所有后代。绝对规则、Windows drive/UNC 形式、`..`、`.`、空组件、空规则、反斜杠和未定义的 glob 语法都会在启动时拒绝。CLI/环境规则只追加，不替换默认规则。

追加 ignored 目录名可使用 `--ignore-dir` 或 `SANDBOXED_WORKSPACE_MCP_IGNORED_DIRS`。ignored 只接受目录 basename。

## 资源上限与文件语义

`max_tree_entries` 是 list/tree 的返回条目上限；`max_scan_entries` 是 list/tree/search 每次请求的全局目录扫描预算。`search_text` 还受实际读取字节数、总执行时间和并发搜索数限制，默认分别为 64 MiB、10 秒和 1；达到条目、字节、时间或并发上限时会快速停止，并返回可区分且受输出上限约束的诊断。文件通过安全描述符分块读取和增量 UTF-8 解码，不会先把每个文件完整读入内存。遍历基于 `os.scandir`，排序候选集合保持有界；当扫描预算或返回上限耗尽时，结果会用不同诊断标记说明。权限错误、损坏链接和并发消失的条目会局部跳过并给出计数，显式目标错误仍会返回失败。

所有文本读取、搜索、grep、tail、replace 和 append 共用安全打开语义：

- 打开后使用 `fstat` 确认普通文件，并基于已打开描述符检查大小。
- FIFO、socket、设备文件和其他特殊文件会被拒绝。
- POSIX 使用逐级目录描述符、`O_NOFOLLOW` 和 `O_NONBLOCK` 降低符号链接竞态与命名管道阻塞风险。
- Windows 使用规范化根内路径、非继承二进制描述符、打开前检查和打开后 `fstat`；Windows reparse-point 竞态仍属于剩余风险。

`read_file_versioned` 返回内容、SHA-256、字节数和 `mtime_ns`。覆盖已有文件、替换文本或追加内容时，调用方必须传回该 SHA-256 作为 `expected_sha256`；缺少令牌或文件已变化都会返回 `conflict`，不会覆盖并发编辑。创建新文件不需要令牌，同一路径的进程内写入也会串行化。这个机制面向正常编辑协作的乐观并发控制，会在最终替换前再次校验已打开文件的身份和内容；它不是跨不可信 OS 账户的内核级 CAS。

写入先在目标目录创建私有临时文件、写入并刷盘，再以 `os.replace` 原子替换，失败会清理临时文件并保留原文件。更新已有文件时保留 POSIX 权限位，但原子替换会创建新 inode：原文件 ACL、扩展属性和原所有者不保证保留，新文件通常继承目标目录的安全描述符/ACL并归服务进程所有。依赖这些元数据的工作区应使用只读模式或在外层恢复元数据。

## 配置

命令行参数优先于环境变量。完整列表见 `sandboxed-workspace-mcp --help`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SANDBOXED_WORKSPACE_MCP_ROOT` | 必填 | 唯一允许访问的项目根目录 |
| `SANDBOXED_WORKSPACE_MCP_TRANSPORT` | `stdio` | `stdio` 或 `streamable-http` |
| `SANDBOXED_WORKSPACE_MCP_HOST` | `127.0.0.1` | HTTP 监听地址 |
| `SANDBOXED_WORKSPACE_MCP_PORT` | `3001` | HTTP 端口 |
| `SANDBOXED_WORKSPACE_MCP_HTTP_PATH` | `/mcp` | HTTP MCP 路径 |
| `SANDBOXED_WORKSPACE_MCP_ALLOWED_HOSTS` | 空 | 逗号分隔 HTTP Host 白名单 |
| `MCP_PUBLIC_HOST` | 空 | 公网 HTTPS Origin；OAuth canonical resource/audience 和 Host/Origin 白名单来源 |
| `SANDBOXED_WORKSPACE_MCP_ALLOW_NETWORK` | `false` | 允许非回环监听；不替代 OAuth 或 TLS |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_ENABLED` | `false` | 为 streamable HTTP 启用外部 OAuth/OIDC JWT 验证 |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER` | 空 | 外部 IdP 的规范化 HTTPS issuer URL |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE` | 空 | 必须与规范化 `MCP_PUBLIC_HOST` 完全相同 |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_JWKS_URI` | discovery | 可选 HTTPS JWKS URL；未设置时从 provider metadata 发现 |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_SCOPES` | 四个内置 scope | 逗号分隔的受支持 scope；必须覆盖已启用工具 |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_JWKS_CACHE_SECONDS` | `300` | JWKS 缓存时间，范围 1–86400 秒 |
| `SANDBOXED_WORKSPACE_MCP_OAUTH_HTTP_TIMEOUT` | `5` | discovery/JWKS 连接与读取超时，范围 0.1–60 秒 |
| `SANDBOXED_WORKSPACE_MCP_READ_ONLY` | `false` | 核心拒绝写入且不发现写工具 |
| `SANDBOXED_WORKSPACE_MCP_BLOCKED_PATHS` | 空 | 追加的逗号分隔 blocked glob |
| `SANDBOXED_WORKSPACE_MCP_IGNORED_DIRS` | 空 | 追加的 ignored basename |
| `SANDBOXED_WORKSPACE_MCP_MAX_FILE_SIZE` | `2097152` | 单个文件/内容最大字节数 |
| `SANDBOXED_WORKSPACE_MCP_MAX_OUTPUT_SIZE` | `200000` | 单次输出最大 UTF-8 字节数 |
| `SANDBOXED_WORKSPACE_MCP_MAX_TREE_ENTRIES` | `1500` | list/tree 返回条目上限 |
| `SANDBOXED_WORKSPACE_MCP_MAX_TREE_DEPTH` | `5` | tree 最大深度 |
| `SANDBOXED_WORKSPACE_MCP_MAX_SCAN_ENTRIES` | `10000` | 单次目录扫描全局预算 |
| `SANDBOXED_WORKSPACE_MCP_MAX_SEARCH_BYTES` | `67108864` | 单次搜索实际读取内容的总字节预算 |
| `SANDBOXED_WORKSPACE_MCP_SEARCH_TIMEOUT` | `10` | 单次搜索总时间预算（秒） |
| `SANDBOXED_WORKSPACE_MCP_MAX_CONCURRENT_SEARCHES` | `1` | 每实例并发搜索上限，超出时快速失败 |
| `SANDBOXED_WORKSPACE_MCP_GIT_TIMEOUT` | `30` | Git 查询超时秒数 |
| `SANDBOXED_WORKSPACE_MCP_TASK_CONFIG` | 空（关闭） | 工作区外可信任务 JSON 的绝对路径；等价于 `--task-config` |

## 可选的容器沙箱任务

task config 是服务操作者授予的执行权限，不是不可信 MCP 调用方的输入。配置必须是工作区外的绝对路径、普通且非符号链接文件；服务以安全打开和 `fstat` 校验有界 JSON，在启动时完成严格解析并冻结，运行期间不热重载。版本 1 配置可以包含原有 `tasks`、新增 `profiles` 或二者；原有仅含 `tasks` 的文件保持兼容。MCP 只能提交已定义的任务/ profile 名、本工具 schema 声明的结构化字段或本实例签发的随机 `task_id`，不能提交通用 argv、环境变量、镜像、工作目录、挂载、端口或容器 ID。

[通用配置模板](examples/tasks.json) 提供 `test`、`lint`、`build`、`dev` 四个任务槽位，其中镜像和 argv 都是故意不可用的明显占位符。`image` 可以是 registry 的完整 `repository@sha256:<64位digest>`，也可以是 Docker/Podman 本机已有镜像的完整 `sha256:<64位image ID>`；tag 和短 ID 都会被拒绝。复制到工作区外后，替换所需任务并删除不用的任务；仓库内模板本身会被“配置必须位于工作区外”的规则拒绝，不能直接用于生产启动。

[Python profile 模板](examples/python-profiles.json) 使用相同的 runtime 和全局 `limits`，
但不保存 caller argv。每个 profile 固定名称、完整 image digest/ID、允许的工具集合与
`workspace_access`。只有至少一个 profile 显式允许相应工具时，服务才注册
`list_execution_profiles` 以及对应的执行工具；发现结果只返回名称、允许工具、
访问模式和资源上限，不返回 image、生成的 argv 或配置文件路径。未知字段、可变 tag、
未知/重复工具和非法访问模式会使整个配置在启动时失败。

Python profile 的结构化能力如下：

- `python_version(profile)` 在同一固定容器镜像中运行 `python --version`，不会读取宿主 Python。
- `run_pytest(...)` 只接受 `targets`、有界 `keyword`、`quiet`、0–2 的 `verbosity`、`exit_first`、`no_capture` 和 `auto|short|long` traceback。服务端生成 `python -m pytest` argv；caller 不能添加 pytest option。
- pytest target 最多 32 个、每个最多 1024 UTF-8 字节。目录、文件及 `FILE::NODE` 的文件前缀必须真实存在于 workspace，且不能穿越、命中 blocked/ignored、经过符号链接或指向特殊文件。
- `run_python_script(profile, path)` 只接受一个真实的 workspace `.py` 普通文件，不支持 script args、`-c` 或 caller 选择的 `-m MODULE`。

`run_python_script` 授权的是“在容器内执行任意 workspace Python 代码”，不是只授权
某几个 Python API。脚本可以在容器内部调用 `subprocess`；边界来自受控 snapshot、
固定镜像、无网络和容器资源/权限限制，而不是 grammar 对脚本内部行为的限制。
因此 Python/pytest 永远不会加入只读 `run_shell`，执行工具需要 OAuth `tasks.run`。
容器隔离降低风险但不是 VM 安全边界；主动敌对代码应使用额外隔离的专用主机或 VM。

先由可信操作者在服务外准备镜像。任务本身使用 `--pull=never`，绝不会隐式下载：

```bash
docker pull registry.example/sandboxed-tests@sha256:<真实的64位digest>
docker image inspect --format '{{json .RepoDigests}}' registry.example/sandboxed-tests
install -m 600 examples/tasks.json /etc/sandboxed-workspace-mcp/tasks.json
# 编辑副本，把每个明显占位符替换为上一步已在本机存在的固定 RepoDigest。
```

自定义镜像应在可信构建流程中构建并推送到可信 registry，再按 digest 拉取到运行 MCP 的主机；Podman 使用等价命令。镜像必须已经包含任务所需的解释器与依赖，因为任务默认无网络。

只在单机使用时无需 registry。仓库提供了不复制项目源码的 [本地任务镜像](examples/Dockerfile.task)，因此镜像只固定解释器和依赖，运行时仍测试最新工作区快照：

```bash
docker build \
  --file examples/Dockerfile.task \
  --tag sandboxed-workspace-mcp-task:local \
  examples
docker image inspect --format '{{.Id}}' sandboxed-workspace-mcp-task:local
```

将第二条命令输出的完整 `sha256:<64位image ID>` 填入每个任务的 `image`。镜像重新构建后 ID 会改变，需要同步更新工作区外 task config。

任务的 `argv` 由操作者在 JSON 中完整固定。可将模板中的通用任务槽位替换为以下命令（每个 `image` 都要换成对应生态、已在本机存在的真实 digest）：

```json
{
  "python-unittest": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
  "pytest": ["python", "-m", "pytest", "-q"],
  "python-lint": ["python", "-m", "ruff", "check", "."],
  "node-test": ["node", "--test"],
  "node-build": ["npm", "run", "build"],
  "go-test": ["go", "test", "-mod=vendor", "./..."],
  "go-build": ["go", "build", "-mod=vendor", "./..."]
}
```

上面只是各任务对象中 `argv` 的简写说明，不是完整 task config。pytest 和项目依赖应预装在 Python 镜像中；Node 示例使用内置 test runner；Go 示例使用工作区内 vendored 依赖。`.venv`、`node_modules` 和常见缓存默认不会复制，任务又没有网络，所以不能依赖运行时下载或宿主依赖目录。

启用 stdio：

```bash
.venv/bin/sandboxed-workspace-mcp \
  --root /absolute/path/to/project \
  --task-config /etc/sandboxed-workspace-mcp/tasks.json
```

启用 streamable HTTP（原有 Host/Origin 规则保持不变）：

```bash
.venv/bin/sandboxed-workspace-mcp \
  --root /absolute/path/to/project \
  --task-config /etc/sandboxed-workspace-mcp/tasks.json \
  --transport streamable-http
```

每次 task 或 Python profile 执行都先从当前唯一根目录建立临时快照。核心 blocked 路径、ignored 目录、所有符号链接和特殊文件不会进入快照；文件数与总字节数有硬上限。总 timeout 从快照创建前开始计算，遍历和每个复制块都会检查 deadline/cancellation。容器只挂载该快照到固定 `/workspace`，真实根目录从不 bind mount。执行成功、失败、超时、取消、停止或服务退出后都会清理快照，修改不会同步回来。

每个任务的 `workspace_access` 默认为 `read-only`，此时 `/workspace` 使用只读 bind mount。确实需要生成构建产物的任务必须在工作区外配置中显式声明 `"workspace_access": "writable"`，同时显式设置 `max_workspace_file_bytes`、`max_workspace_growth_bytes` 和 `"allow_best_effort_disk_limit": true`，否则配置加载即失败。可写任务使用容器 `fsize` ulimit 限制单文件，并由宿主线程监视快照总增长，越限时停止容器并报告 `workspace_limit_exceeded`。总增长检查存在采样窗口，是降低宿主磁盘写满风险的 best-effort 防线，不是文件系统或内核级硬配额；高风险任务仍应运行在有独立磁盘配额的专用主机或 VM 上。

生产后端只用参数数组调用配置指定的 Docker 或 Podman，不使用 Shell，也不回退到宿主机运行 Python、Node、Go、pytest 或项目代码。容器使用 `--pull=never`、无网络、只读根文件系统、drop 全部 capabilities、`no-new-privileges`、非 root 用户、CPU/内存/PID 限制及隔离 tmpfs；不会继承宿主代理、凭证、SSH agent、用户 HOME 或 Docker socket。

典型调试闭环：先用文件工具读取和精确修改代码，调用 `run_task("test")`，根据分离且有界的 stdout/stderr/traceback 再修改。长运行任务使用 `start_task("dev")`，再以 `task_status` 和带 cursor 的 `task_logs` 检查启动状态，最后 `stop_task`。第一版不支持端口映射，`start_task` 主要用于启动诊断和日志观察。

## streamable-http

默认端点是 `http://127.0.0.1:3001/mcp`。仅回环、没有配置公开 Origin 的本地开发仍可匿名运行；一旦设置 `MCP_PUBLIC_HOST` 或监听非回环地址，启动默认要求 OAuth。`--allow-unauthenticated-http` 是仅命令行可用的危险逃生开关，会在 stderr 输出高可见警告，只应用于临时开发。

推荐的长期部署拓扑：

```text
ChatGPT ── HTTPS/OAuth ──> ngrok ── HTTP loopback ──> 127.0.0.1:3001/mcp
             │
             └── 外部 OAuth/OIDC Provider（授权、PKCE、JWT、JWKS）
```

本项目只实现 OAuth 2.1 resource server，不保存密码或 Client Secret，也不签发 token。外部 Provider 必须提供 HTTPS issuer、provider discovery、JWT JWKS、`exp`/`nbf`、issuer、resource/audience 和 scope claims，并支持授权码流程与 PKCE S256。ChatGPT 客户端注册还需要以下二者之一：Provider 支持 Dynamic Client Registration/CIMD，或由操作者预先创建 ChatGPT 客户端。回调 URI 应从 ChatGPT 的 MCP 连接界面读取并逐字登记，不要在配置或文档里硬编码。具体发现流程见 OpenAI 的 [MCP Authentication](https://developers.openai.com/plugins/build/auth) 与 [Build an MCP server](https://developers.openai.com/plugins/concepts/mcp-server)。

服务发布 RFC 9728 metadata：

```text
https://<public-origin>/.well-known/oauth-protected-resource
```

缺失或无效 token 会同时得到 HTTP `WWW-Authenticate: Bearer ...resource_metadata=...` 和 MCP tool result 的 `_meta["mcp/www_authenticate"]`。每个工具声明 `securitySchemes`，并在服务端逐次检查实际 token scope：

| Scope | 工具 |
| --- | --- |
| `workspace.read` | `project_info`, `list_directory`, `tree`, `read_file`, `read_file_versioned`, `search_text`, all `git_*` tools, `run_shell` |
| `workspace.write` | `create_directory`, `write_file`, `replace_text`, `append_file` |
| `tasks.read` | `list_tasks`, `task_status`, `task_logs`, `list_execution_profiles` |
| `tasks.run` | `run_task`, `start_task`, `stop_task`, `python_version`, `run_pytest`, `run_python_script` |

先启动仍只监听本机的 MCP。下面所有尖括号内容都是部署时替换的占位符，`audience` 必须与规范化后的公开 HTTPS Origin 完全一致：

```bash
MCP_PUBLIC_HOST='https://<your-ngrok-host>' \
SANDBOXED_WORKSPACE_MCP_OAUTH_ENABLED=true \
SANDBOXED_WORKSPACE_MCP_OAUTH_ISSUER='https://<your-idp-issuer>' \
SANDBOXED_WORKSPACE_MCP_OAUTH_AUDIENCE='https://<your-ngrok-host>' \
SANDBOXED_WORKSPACE_MCP_OAUTH_SCOPES='workspace.read,workspace.write,tasks.read,tasks.run' \
.venv/bin/sandboxed-workspace-mcp \
  --root /absolute/path/to/project \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 3001
```

如果 discovery 没有返回正确 JWKS 地址，可额外设置 `SANDBOXED_WORKSPACE_MCP_OAUTH_JWKS_URI='https://<your-idp-jwks>'`。服务只接受 HTTPS issuer/JWKS；JWKS 有界缓存，遇到未知 `kid` 时只强制刷新一次。JWT 只接受受支持的 RSA/ECDSA 非对称算法，并校验签名、`kid`、issuer、audience/resource、`exp`、`nbf` 与工具 scope。

另一个终端启动 ngrok，并在 ChatGPT 连接器中填写 `https://<your-ngrok-host>/mcp`：

```bash
ngrok http 3001
```

ngrok 终止 TLS，但本地监听仍保持 `127.0.0.1`，因此不需要 `--allow-network`。Host/Origin DNS-rebinding 校验继续生效，公开 Origin 的主机名自动加入白名单。`MCP_PUBLIC_HOST` 必须是没有凭证、路径、查询或片段的规范化 HTTPS Origin，不能使用本地 `http://127.0.0.1` 作为公网 audience。

确实需要直接监听局域网地址时，还必须使用 `--allow-network`，通配监听还要求 `--allowed-host`。该开关只授权 bind 行为，不提供 TLS、身份认证、速率限制或多租户隔离；公网路径仍应使用 OAuth 和 HTTPS。

`stdio` 保持兼容的无认证本地模式，不读取 `MCP_PUBLIC_HOST`，也不允许启用 `--oauth`：

```bash
.venv/bin/sandboxed-workspace-mcp --root /absolute/path/to/project --transport stdio
```

## 项目结构

```text
src/sandboxed_workspace_mcp/
  access_policy.py # blocked glob 验证、匹配和 Git 排除策略
  config.py        # 单根配置、权限和资源上限
  safe_regex.py    # 非回溯安全正则子集的 parser 与 Thompson NFA
  workspace.py     # 安全路径、文件 IO、有界遍历和原子写入
  git_reader.py    # 有界且严格失败的只读 Git 适配器
  service.py       # run_shell 小语法与应用编排
  oauth.py         # RFC 9728 metadata、discovery/JWKS 缓存与 JWT 验证
  server.py        # MCP 工具注册、scope 检查和认证 challenge
  cli.py           # stdio/HTTP 启动、OAuth 配置和传输安全
  task_config.py   # 工作区外可信 JSON 的安全加载、验证与冻结
  python_execution.py # 结构化 pytest/Python 输入验证与容器 argv 编译
  task_snapshot.py # blocked/ignored 感知的无跟随、有界临时快照
  task_runner.py   # 容器后端 Protocol、安全 argv 与同步执行
  task_manager.py  # 并发、服务生命周期、随机 ID 和日志环形缓冲
tests/             # 临时目录中的单元与真实传输回归测试
scripts/           # 构建产物隔离冒烟测试
examples/          # 仅供复制且必须替换 digest 的任务配置示例
server.py          # 兼容 python server.py 入口
```

## 开发质量门禁

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
PYTHONPATH=src .venv/bin/python -m coverage run -m unittest discover -s tests -v
.venv/bin/python -m coverage report --fail-under=85
.venv/bin/python -m compileall -q server.py src tests scripts
.venv/bin/python -m build
.venv/bin/python scripts/wheel_smoke.py dist/*.whl
```

CI 在 Python 3.10 和 3.13 执行全部门禁。普通测试全部使用临时目录和 fake backend，不要求或调用真实 Docker/Podman；配置要求总分支覆盖率至少 85%。真实 stdio 和 streamable-http 握手、HTTP Host/Origin 拒绝、特殊文件、Git 失败以及 wheel 默认/任务工具枚举均有回归测试。真实容器集成只能由环境显式启用，不属于普通 CI 的稳定依赖。

文件策略是一层应用级安全边界；容器隔离降低运行不可信工作区代码的风险，但不是虚拟机安全边界。剩余风险与报告方式见 [SECURITY.md](SECURITY.md)。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。
