# Tijian 原生 MCP 接入

Paveway 已全面迁移至 Tijian 原生 stdio MCP Server（`tijian-mcp`），由 Rust 上游直接提供原生 MCP 协议支持。

旧有的 Python 桥接器（包括 `tijian_catalog.py`、`tijian_mcp.py`、`tijian.py`、旧 `[tijian]` 运行配置及 `tijian tools/call/doctor/setup/mcp` 等 CLI 命令）**已被彻底删除，不再可用**。Paveway 不再自行定义或重新组装 Tijian 的工具 Schema。

0.21.1 起，旧配置文件中保留的 `[tijian]` 段可兼容读取及原值写回，不会再使整个 Runtime 报 `Unknown top-level configuration keys`。这只用于保护旧设置，不会重新启用桥接器；连接须按下文配置原生 MCP。其他未知字段和非法值仍会拒绝，不静默忽略拼写错误。

---

## 1. 架构与职责分工

```text
Paveway
  ├── Role Policy (bundled/oi-coach.md)
  ├── Built-in Tijian Skill (paveway-tijian)
  ├── Workflow MCP configuration (workflow connect-tijian)
  └── OMP Agent
        |
        | Standard MCP over stdio
        v
     tijian-mcp (Rust 原生进程)
        |
        ├── tools/list
        ├── tools/call
        ├── inputSchema / outputSchema
        └── Tijian Desktop / headless UseCases
```

### Paveway 的职责
- **配置 MCP Server**：通过 `/paveway settings` → Tijian MCP 或 `workflow tijian show/set` 安全读取、修改 workspace 的 `.omp/mcp.json`；旧 `workflow connect-tijian` 保持兼容。
- **业务指导与编排**：内置 `paveway-tijian` Skill 为 GPT 提供业务认知、能力选择与跨工具工作流策略。
- **VJudge 会话维护**：通过 `vjudge auth` CLI 及终端 UI 管理与 Tijian 兼容的共享 VJudge 登录 Cookie。

### Tijian MCP 的职责
- **工具定义与执行**：通过标准的 MCP `tools/list` 暴露真实的工具名称、description、inputSchema、outputSchema 及 annotations。Paveway 不在 Python 中复刻任何工具定义。
- **权限与模式管理**：启动参数 `--write-policy=none|managed|full` 决定 MCP 进程权限；服务端负责过滤／拒绝不允许的工具和操作。旧 `--read-only` 按 None 识别，空 args 按 Full 识别。
- **底层数据交互**：由 Tijian 核心 use cases 处理题目存储、搜索、向量与 AI Job。

---

## 2. 构建或获取 `tijian-mcp`

上游仓库：[zjpanhongxuan/Tijian](https://github.com/zjpanhongxuan/Tijian)（已知基线 `90e2c75`）。

使用 Rust 工具链在本地构建 `tijian-mcp` 可执行文件：

```bash
git clone git@github.com:zjpanhongxuan/Tijian.git
cd Tijian
cargo build --release --bin tijian-mcp
```

构建完成后，二进制文件位于 `target/release/tijian-mcp`。请记录其绝对路径。

> **注意**：Paveway 不自动下载、安装或构建 Rust 项目，也不修改 Tijian 的数据库、profile 或运行时环境。用户需显式提供已构建的 `tijian-mcp` 绝对路径。

---

## 3. 设置界面与三档权限

在原生 OMP 中输入 `/paveway settings`，选择 **Tijian MCP**；`/paveway tijian` 是同一页面的直接入口，不再启动连续输入向导。首次只需在 **MCP executable** 项输入可执行文件路径，默认建议 Managed。已有路径和权限从当前 Workflow 的 `.omp/mcp.json` 读取，不自动升级或降级。

| 模式 | 启动参数 | 能力 |
| --- | --- | --- |
| None — 只读 | `--write-policy=none` | 查询、搜索、查看题面与历史等只读工具 |
| Managed — 受控维护（推荐） | `--write-policy=managed` | 全部只读工具，以及 `tijian_problem_ensure`、`tijian_problem_content_ensure`、`tijian_problem_embedding_ensure`；禁止直接编辑题面／属性／状态、删除等 |
| Full — 完整读写 | `--write-policy=full` | 全部 MCP 工具，包括直接修改与删除 |

在 **Permission** 项按 Enter，用原生 SelectList 选择权限；从 None／Managed 切换 Full 必须由用户通过确认框明确同意。只改权限不需要填写路径；只改路径保留原权限及旧参数编码。路径输入框用于填写替换值，空输入保留已有路径，Esc 丢弃未提交输入；权限子菜单 Esc 返回 Tijian 页，Tijian 页 Esc 返回设置中心。首次尚未填写路径时选择的权限只作为本页草稿，离开不写盘。

保存后显示 `Saved (restart required)` 并通知：

```text
Saved. Restart the Paveway native OMP session to apply Tijian MCP changes.
```

这只是已保存状态；无法可靠获知当前 MCP 进程权限，因此不展示“已立即生效”。由用户自行重启 Paveway 原生 OMP Session，不自动杀进程、停止 Worker／AI Job，不实现热重载，也不发送 MCP 工具调用验证权限。设置操作不进入聊天，不注册模型可调用的设置工具。

### 配置读取与安全更新

- 唯一有效来源为 Workflow Workspace 的 `.omp/mcp.json`。不新增 `[tijian]`、数据库表或独立权限副本。只读查询不会创建文件／目录，不验证或执行 MCP 可执行文件。
- 仅接受 `command` 为绝对路径字符串、`args` 为已知完整参数列表的标准 Tijian 定义。旧 `["--read-only"]` → None，`[]` → Full；三个单独的 `["--write-policy=…"]` 分别识别。未知参数、混合参数或额外 Server 字段明确报错，拒绝猜成 Full 或自动覆盖。
- 新增／替换的 executable 须存在且可执行；只改权限不要求旧文件仍存在，不自动重建 Workflow 或 GPT Profile。
- 仅更改 `mcpServers.tijian`，保留其他 Server、`disabledServers` 和其他字段；Tijian 被显式禁用时拒绝修改，须人工核对。
- 使用整个文件内容的 SHA-256 revision 检测过期编辑，提交前再次核对原始字节；临时文件 fsync 后原子创建／替换。拒绝 symlink、不安全目录和并发冲突，写入文件权限为 `0600`。
- 未改变配置时不重复写盘，保留原文件字节、inode 和修改时间。保存结果及返回页面再次读取的配置均不等同 MCP 握手成功。

### `workflow tijian show/set`

CLI 接口供本机管理和原生设置控制器使用。`show` 返回 `configured`、`executable`、`permission`、`revision` 等字段；`set` 必须传最近读取的 `--expected-revision`，返回更新后的配置和 `changed`。首次省略 `--permission` 使用 Managed。

```bash
export PAVEWAY_CONFIG="$PWD/.runtime/config.toml"
oicoach-runtime workflow tijian show coach --config "$PAVEWAY_CONFIG" --json

# 每次保存前重新读取 revision；并发变化会拒绝本次保存。
REV=$(oicoach-runtime workflow tijian show coach --config "$PAVEWAY_CONFIG" --json |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
oicoach-runtime workflow tijian set coach \
  --executable /absolute/path/to/tijian-mcp --permission managed \
  --expected-revision "$REV" --config "$PAVEWAY_CONFIG" --json

# 已连接时仅修改权限，不重复提供 executable。
REV=$(oicoach-runtime workflow tijian show coach --config "$PAVEWAY_CONFIG" --json |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
oicoach-runtime workflow tijian set coach --permission none \
  --expected-revision "$REV" --config "$PAVEWAY_CONFIG" --json
```

仅改路径时传 `--executable`，不传 `--permission`。新建 Full 或从其他模式升至 Full 还须明确传 `--confirm-full`；设置界面只在用户确认后添加此标志。CLI 是本机用户管理接口，不是同 UID 进程之间的安全沙箱。

### 旧 `workflow connect-tijian` 兼容性

```bash
# 保持原默认只读语义，写入 args: ["--read-only"]。
oicoach-runtime workflow connect-tijian coach \
  --executable /absolute/path/to/tijian-mcp --config "$PAVEWAY_CONFIG"
```

`--read-only`／`--read-write` 仍互斥，后者写入空 args（Full）。旧命令不负责修改已有不同定义，仍拒绝冲突；需要切换时使用设置页或新的 revision 接口。仅此旧命令在原生配置不存在时可显式导入 workspace 根 `.mcp.json`，旧文件保持原样；新设置接口不读取或导入旧文件。

---

## 4. 运行边界与状态验证

### 配置成功 vs 实际连接成功
- 旧 `workflow connect-tijian` 与新设置接口在已配置时返回 `availability: "configured_not_verified"`，仅表示 `.omp/mcp.json` 配置已保存；未配置的查询返回 `not_configured`。
- **配置成功不代表 MCP 进程已经成功启动或连接**。
- 启动与连接由 OMP 在派发回合时负责：OMP 读取 workspace 的 `.omp/mcp.json`，通过 stdio 启动 `tijian-mcp` 子进程并完成 MCP 初始化握手。

### 检查 OMP 是否发现工具
可以通过以下方式确认工具已被发现：
1. 观察 OMP 会话启动日志或 MCP 连接状态。
2. 在 OMP Agent 中核对是否通过 `tools/list` 发现了以 `tijian_` 为前缀的原生 MCP 工具。
3. 按已启动进程的模式核对服务端工具发现结果；不能用设置页的已保存权限推断旧进程已经切换。

Paveway 现已切换至 OMP 原生 `--system-prompt-template` 机制。在此机制下，OMP 原生模板中的 `# xd:// Tool Devices` 与 `xdevDocs` 完整保留并由 OMP 动态求值。
当连接 Tijian MCP 时，OMP 会自动在 Project Context 中注入 `## MCP Tool Routes`（例如 `"tijian_problem_get" → xd://mcp__tijian_problem_get`），并在系统指令中明确指导模型：
- 通过 `read xd://...` 查看工具使用文档与 JSON Schema（按需读取，不预先向系统提示词全量塞入所有 Schema）；
- 通过原生 `write` 向对应设备路径（如 `xd://mcp__...`）写入 JSON 参数完成工具调用。

工具名和设备路径以运行中 OMP 动态发现的结果为准，Paveway Core 与 Skill 不硬编码任何工具 Schema。
### Windows 与 WSL2 运行边界
- 在 WSL2 Linux 环境中运行的 `tijian-mcp` 默认访问 Linux 的 XDG 数据目录（如 `~/.local/share/tijian`）。
- Windows Desktop 版 Tijian 运行在 Windows 主机环境，默认使用 Windows 的 AppData/Local 数据目录。
- **不要假定 WSL2 中的 MCP 进程能无条件与 Windows Desktop 共享同一个数据库或 Profile**。若需跨环境联调，需显式验证数据目录映射与文件锁支持。

---

## 5. 内置 Skill 与 MCP 工具契约

Paveway 提供内置 Skill `paveway-tijian`（位于 `src/oicoach_runtime/bundled/skills/paveway-tijian/SKILL.md`）。

- **Skill 定位**：业务编排与上下文指导层，协助 Agent 识别何种训练场景应采用何种能力（如题面读取、状态登记、相似题检索等）。
- **工具契约**：工具具体名称、参数、返回值及错误信息由运行中的原生 MCP 动态提供，Skill 不复刻工具 Schema。
- **未支持能力**：在已知基线 `90e2c75` 下，**Note、Todo、Folder 的原生 MCP 尚未实现**，Agent 严格根据实际 `tools/list` 结果使用工具，不调用虚构接口。
- **异步 AI Ensure 的区别**：
  - `tijian_problem_ensure`：同步收录或补齐题面。
  - `tijian_problem_content_ensure`：提交异步 AI 译文/摘要 Job。
  - `tijian_problem_embedding_ensure`：提交异步 AI 向量 Job。
  AI Ensure 需要配合 Job 状态查询跟踪，不是立即同步返回产物。

> **注意（既有 Workflow 升级提醒）**：
> 既有 workflow 目录中的 `event.md` 是创建 workflow 时复制的独立文件，不会随 bundled `coach.md` 模板自动更新。若既有 workflow 的 `event.md` 中仍残留旧版 CLI（如 `tijian call`、`problem_add` 等）调用指令，建议人工核对并更新为原生 MCP 工具约定。

---

## 6. VJudge 登录与会话管理

VJudge 认证功能已从旧 `tijian` 命令组解耦，迁移为独立的 `vjudge auth` CLI 命令，所有认证逻辑与文件安全性完整保留。

### TUI 界面
终端交互界面中保留 `/vjudge` 与 `/tijian` 两种斜杠命令入口（二者等效）：
- `/vjudge login`：交互式输入用户名与隐藏密码，登录并保存 Cookie。
- `/vjudge cookie`：导入在浏览器完成验证后复制的 Cookie 文本。
- `/vjudge test`：在线测试当前保存的 Cookie 登录有效性。
- `/vjudge status`：查看本地 Cookie 与账号绑定配置状态。
- `/vjudge clear`：清除本地 Cookie（保留原有账号绑定）。

### CLI 命令
```bash
# 检查本地会话配置状态
oicoach-runtime vjudge auth status --config .runtime/config.toml --json

# 在线测试当前登录会话
oicoach-runtime vjudge auth test --config .runtime/config.toml --json

# 交互式登录
oicoach-runtime vjudge auth login --config .runtime/config.toml

# 使用私密文件导入 Cookie（文件权限需为 0600）
oicoach-runtime vjudge auth cookie --cookie-file /path/to/cookie.txt --config .runtime/config.toml

# 清除会话
oicoach-runtime vjudge auth clear --config .runtime/config.toml
```

会话保存在共享的 `vjudge_session.json`（权限 `0600`，原子写入），供 Tijian 题目抓取器复用。
