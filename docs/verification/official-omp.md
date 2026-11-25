# 官方 OMP Extension-First：迁移与验收

旧前台把 `agent.executable` 换成由 Paveway 安装器构建的 OMP 18.16.0（`650f3a1`），后台则直接使用配置中的 OMP。现在前台用同一 `omp_installation.resolve_omp` 解析官方可执行文件，经 PTY guardian 加载一个 `--trusted-extension`；后台通过同一绝对路径使用官方 `--mode rpc`。运行中的 Runtime 若发现所选路径或可执行文件已变更，仍须经过原来的空闲检查与安全交接，不会因更新强杀活动任务。无需构建或安装专用 Host。

## 迁移清单

| 原补丁 | 处理 | 仍有差异 |
| --- | --- | --- |
| `statusline.patch` | 迁移到 `ui.setStatus`；Paveway 提供 Profile、Workflow、`Busy`、Wakeup、Inbox，OMP 提供模型、Thinking、费用与 Context | 不再替换整条状态栏；无独立胶囊、Chevron 波浪与自定义双阈值轨 |
| `conversation.patch` | 删除，普通消息使用 OMP 原生 Transcript | 无额外 User 强调线和逐条 GPT 标签 |
| `composer.patch` | 迁移到 `registerComposerShape` 和受管配置覆盖 | 不再替换编辑器；保留原生输入、草稿、IME、补全及历史 |
| `banner.patch` 与 preload | 迁移到 `ui.setWidget`，保留 Aurora × Pathline 字标及版本信息 | 无首帧替换、入场动画及持久 Banner 滚动历史 |
| `workspace.patch` | 迁移到官方 `input`、`onTerminalInput`、会话生命周期、graceful shutdown 及启动配置 | 原生 `/resume`、`/move`、`/wt`、持久化 `!cd` 和进程内重启改走 Paveway 管理入口 |
| `workflow-mcp.patch` | 同 cwd 直接用官方原生 MCP；删除 OMP 源码注入 | **BLOCKED**：官方无连接前的单 Server 注入及跨来源冲突拒绝接口。受管 Tijian Workflow 的跨 cwd 会话安全拒绝，不复制或改写配置 |

删除隔离安装器、版本/依赖下载、Bun 与 native 插件专用缓存路径、补丁源码与 wheel 入口。旧 `[statusline]` TOML 仍可读写以保留用户设置，不再参与绘制。旧缓存和用户数据没有被删除。

## 能力与剩余耦合

- 前台只加载可信 Extension；nonce、Extension 源文件哈希、guardian PID、实际 cwd 和物理 Session 文件校验通过后才转发终端输入。缺少准入或必要上下文能力不会放行。Profile、Prompt／Skill 快照、Memory、Workflow、WakeBinding、调度器及所有权 fencing 保留原职责。
- RPC 使用官方 JSONL v1 兼容模式、相关 ID、真实 `get_state`、独立的 `prompt_result`／settlement 和持久文件。模型、Provider、认证、Thinking 等级由实际 OMP 提供，无 Paveway 枚举。Prompt 模板只适配实际出现的 OMP 动态字段差异：19.0 的 `xdevTools/xdevDocs` 与 19.1.1 的 `mountedCommands/toolCommandDocs`。
- 当前运行不导入 OMP 私有源码。还需理解 OMP 的 Session JSONL（用于验证归属、只读历史和 Git 文字归档）及 Memory compaction ancestry 字段（避免重放后改变旧前缀）。这部分受实际 Session 格式约束，不能把二进制可回退等同于会话格式可回退。另有显式 opt-in 源码审计，不是运行或构建依赖。

参考：[官方 19.1.1 release](https://github.com/can1357/oh-my-pi/releases/tag/v19.1.1)、[Extension API](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/extensions.md)、[Extension 加载](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/extension-loading.md)、[RPC 协议](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/rpc.md)、[原生 MCP 配置](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/mcp-config.md)。

## 实际运行

| 验收 | 状态 | 证据 |
| --- | --- | --- |
| 两份未经修改的官方发行件 | VERIFIED | GitHub 与 npm 确认为正式最新版 19.1.1。19.1.1 的下载二进制与系统安装的 19.0.0 均匹配各自官方 `SHA256SUMS.txt` |
| 前台启动与模型、现有认证 | VERIFIED | 同一 Paveway 代码分别用 `./paveway.sh --omp` 启动 19.0.0／19.1.1；真实 PTY 握手通过，`openai-codex/gpt-6.2-sol` 真实返回并持久化预期文本；Ctrl+D 后自有 Runtime 结束。两版 `workflow models` 均发现 GPT-6.2 Sol |
| 后台 RPC、Workflow、同任务续聊及隔离 | VERIFIED | 19.1.1 `verify-omp --with-model --json` 返回真实 Sol selector、相关 `get_state/get_messages/abort`，两个同任务回合保持稳定的 Session ID 并能回忆随机 token，其他任务不含该 token。19.0.0 的真实本地 Provider PTY 用例也验证了前后共用所选 OMP 与 Session 恢复 |
| 活动取消、退出与所有权恢复 | VERIFIED | 两份发行件分别通过活动 RPC restart：只执行一次，原 Event 保留 `uncertain`，新 Worker 只探测同一会话。真实 PTY 的 Ctrl+C 双击也通过跨重启释放、恢复和不重发。19.1.1 回归覆盖 Ctrl+D、`/exit`、大 Session、guardian／launcher SIGKILL 与 `setsid` 工具清理 |
| Memory、Compaction、重启恢复 | VERIFIED | `examples/verify_rpc_memory_boundaries.py` 使用 19.1.1 官方二进制、真实 Runtime IPC 与 loopback Provider：一条持久 compaction、真实 summary 请求、旧 JSONL 前缀不变、新进程打开同一 Session ID 和文件 |
| Skills 包与 Prompt | VERIFIED | 19.1.1 完整 Skill ZIP 安装、删除原 ZIP、重启和 `skill://` 原字节读取通过；模板显示、工具目录与 MCP 挂载用例以官方二进制执行，不需 OMP 源码 |
| Agent、Wakeup、Reminder、工作目录和 UI | VERIFIED | loopback PTY 验证原生 Subagent 独立 Session、后台 Wakeup／Inbox、纯 Reminder、CWD 项目发现与 Profile/模型隔离。两个版本的 Banner、草稿／多行粘贴、运行中 Windline、倒计时、未读状态通过实测 |
| MCP | VERIFIED / BLOCKED | 官方原生 MCP 挂载工具分别在 19.0.0、19.1.1 的真实 RPC 检查通过；受管 Tijian 跨 cwd 继承 **BLOCKED**，拒绝切换和文件不变由隔离 PTY 检查 |
| 可执行文件原地升级与路径别名 | VERIFIED | 路径不变的原子文件替换触发安全交接，新 Runtime 报告新版本；两份官方发行件均通过前台与后台共用起始选定路径的 PTY 复测，即使原始软链接在 daemon 运行中改指另一份程序，Worker 也不会自行切换 |
| 安装件与 JavaScript | VERIFIED | 独立 wheel 包含 Paveway Extension，不包含 Host installer、launcher 或补丁；独立 Bun 1.4.5 跑 41 个 JS 测试全通过 |
| Python 全量 | FAILED | `unittest discover -s tests`：656 项，553 通过、100 条件跳过、3 失败：`test_native_text_history_excludes_thinking_tools_and_preserves_branches`、`test_automatic_two_device_git_sync_without_ui_or_queue_import`、`test_pty_git_sync_setup_remote_push_history_and_pause`。同样三项在干净 `HEAD` 归档中逐项重现，未改动 Git 同步实现 |
| Browser Relay / Chrome | BLOCKED | 环境无测试要求的 Linux Chrome；OMP `browser-relay` 入口可用，不等同于浏览器扩展、已有用户会话或实际工具连通 |
| OAuth 首次登录、真实 Tijian 数据、系统服务部署、物理 IME | NOT TESTED | 只验证已有认证的真实请求及隔离 stdio MCP；没有修改真实凭据、真实 Tijian 数据、Chrome 配置或 systemd 用户服务。输入/编辑为 PTY 验收，不证明物理 IME 或 Windows Terminal 字体 |

升级与安全回退见 [原生 UI](../native-ui.md#omp-选择诊断与升级)。若官方发行版不满足契约，在全部活动工作结束后选择另一份官方可执行文件；不恢复 patched Host，不删除数据，不自动重发 uncertain 工作。
