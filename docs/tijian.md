# Tijian 原生 MCP 接入

Paveway 已全面迁移至 Tijian 原生 stdio MCP Server（`tijian-mcp`），由 Rust 上游直接提供原生 MCP 协议支持。

旧有的 Python 桥接器（包括 `tijian_catalog.py`、`tijian_mcp.py`、`tijian.py`、旧 `[tijian]` 运行配置及 `tijian tools/call/doctor/setup/mcp` 等 CLI 命令）**已被彻底删除，不再可用**。Paveway 不再自行定义或重新组装 Tijian 的工具 Schema。

---

## 1. 架构与职责分工

```text
Paveway
  ├── Coach Prompt (bundled/coach.md)
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
- **配置 MCP Server**：通过 `oicoach-runtime workflow connect-tijian` 安全写入 workflow workspace 的 `.omp/mcp.json`。
- **业务指导与编排**：内置 `paveway-tijian` Skill 为教练 Agent 提供业务认知、能力选择与跨工具工作流策略。
- **VJudge 会话维护**：通过 `vjudge auth` CLI 及终端 UI 管理与 Tijian 兼容的共享 VJudge 登录 Cookie。

### Tijian MCP 的职责
- **工具定义与执行**：通过标准的 MCP `tools/list` 暴露真实的工具名称、description、inputSchema、outputSchema 及 annotations。Paveway 不在 Python 中复刻任何工具定义。
- **权限与模式管理**：启动参数 `--read-only` 决定整个 MCP 进程的服务端权限；服务端直接负责拒绝写工具或写操作。
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

## 3. 使用 `workflow connect-tijian` 配置 MCP

使用 Paveway 的 CLI 命令为目标 workflow 连接原生 `tijian-mcp`：

```bash
# 默认推荐只读模式（包含 --read-only 参数）
oicoach-runtime workflow connect-tijian coach \
  --executable /absolute/path/to/tijian-mcp \
  --config "$PWD/.runtime/config.toml"

# 或显式指定只读模式
oicoach-runtime workflow connect-tijian coach \
  --executable /absolute/path/to/tijian-mcp \
  --read-only \
  --config "$PWD/.runtime/config.toml"

# 显式读写模式（不带 --read-only 参数）
oicoach-runtime workflow connect-tijian coach \
  --executable /absolute/path/to/tijian-mcp \
  --read-write \
  --config "$PWD/.runtime/config.toml"
```

### 规则与安全性
- **默认只读**：省略模式标志时，默认采用 `--read-only`，限制 MCP 进程只暴露和允许只读能力。
- **互斥标志**：`--read-only` 与 `--read-write` 严格互斥，不能同时提供。
- **明确路径**：`--executable` 必须为有效且具备可执行权限的路径，绝不隐式回退到旧 CLI。
- **合并配置**：写入对应 workflow workspace 下的 `.omp/mcp.json`，保留已有其他 MCP Server 与 `disabledServers`。
- **幂等与冲突检测**：相同配置重复执行保持幂等；若存在不同定义的 `tijian` 配置则安全报错（`ConflictError`），原文件严格保持不变。
- **原子性与安全权限**：通过临时文件原子替换，拒绝 symlink，文件权限保持 `0600`。

生成的 `.mcp.json` 示例：

```json
{
  "mcpServers": {
    "tijian": {
      "command": "/absolute/path/to/tijian-mcp",
      "args": [
        "--read-only"
      ]
    }
  }
}
```

---

## 4. 运行边界与状态验证

### 配置成功 vs 实际连接成功
- `workflow connect-tijian` 返回 `availability: "configured_not_verified"`，仅表示 `.mcp.json` 配置文件已正确写入。
- **配置成功不代表 MCP 进程已经成功启动或连接**。
- 启动与连接由 OMP 在派发回合时负责：OMP 读取 workspace 的 `.omp/mcp.json`，通过 stdio 启动 `tijian-mcp` 子进程并完成 MCP 初始化握手。

### 检查 OMP 是否发现工具
可以通过以下方式确认工具已被发现：
1. 观察 OMP 会话启动日志或 MCP 连接状态。
2. 在 OMP Agent 中核对是否通过 `tools/list` 发现了以 `tijian_` 为前缀的原生 MCP 工具。
3. 在只读模式下，确认写工具已被服务端过滤；读写模式下则具备完整操作能力。

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
