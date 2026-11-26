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

## OMP 19.2.1：迁移证据

按版本对照 [19.1.1 → 19.2.0](https://github.com/can1357/oh-my-pi/compare/v19.1.1...v19.2.0)、[19.2.0 → 19.2.1](https://github.com/can1357/oh-my-pi/compare/v19.2.0...v19.2.1)、[19.2.0 发行说明](https://github.com/can1357/oh-my-pi/releases/tag/v19.2.0)、[19.2.1 发行说明](https://github.com/can1357/oh-my-pi/releases/tag/v19.2.1)、[Extension 类型定义](https://github.com/can1357/oh-my-pi/blob/v19.2.1/packages/coding-agent/src/extensibility/extensions/types.ts)、[UI 迁移](https://github.com/can1357/oh-my-pi/blob/v19.2.1/docs/extensions.md#migrating-from-pi-tui)与 [CLI](https://github.com/can1357/oh-my-pi/blob/v19.2.1/docs/cli-reference.md)。

- 用官方 Linux x64 19.2.1、隔离 Runtime 和未修复的 Extension 复现。底层为 `TypeError: Composer shape id must be a non-empty trimmed string`，调用链 `registerComposerShape → registerComposer → registerNativePresentation → paveway`。上游 [19.1.1 loader](https://github.com/can1357/oh-my-pi/blob/v19.1.1/packages/coding-agent/src/extensibility/extensions/loader.ts#L358-L370) 从 `definition.style.id` 读 ID；[19.2.1 loader](https://github.com/can1357/oh-my-pi/blob/v19.2.1/packages/coding-agent/src/extensibility/extensions/loader.ts#L358-L375) 读顶层 ID 并要求 `row`／`sideWidth`。原 `catch` 只保留了注册阶段。
- Composer 同时表达两代形状契约，不按版本字符串放行。Widget 使用宿主尺寸和实时 Theme；管理对话框不再依赖已移除的 TUI 组件，密钥输入仅走已支持的掩码路径。快捷键仍采用当前宿主配置，19.2 的匹配器接收解码后的 KeyEvent；文件路径和 Session 归属检查仍独立生效。Wakeup／Memory 工具沿用默认渲染，不依赖已删除的 `describeCall`／`describeResult`。
- 19.2 起 `--no-tools` 蕴含隔离且与显式 Extension 互斥。Paveway 无工具会话改用独立的发现约束、空工具列表和执行拒绝，保留必需 Extension 与其握手。不能只删掉 `--no-tools` 让工具或 MCP 恢复加载。OMP 的 `ready`、相关 ID 与状态验证继续走现有 RPC adapter。
- 真实验收使用官方 `SHA256SUMS.txt` 校验的 Linux x64 发行件：19.1.1 为 `04e9490f9394da6d6804474cc2e41eb3d18f8e26215df4b34bdd4ad4c0d8a401`；19.2.1 为 `e8b937f9bf023baa147bdd300ad5421af0a6ef8b1f350c47a9a93d5fca94feca`。本节与下方历史 19.1.1 验证分开计数；升级策略见 [原生界面](../native-ui.md#omp-选择诊断与升级)。

### 实测结果

| 验收 | OMP 19.1.1 | OMP 19.2.1 |
| --- | --- | --- |
| 真实 PTY：新建／恢复、Composer／Banner／Windline、管理与掩码输入、Wakeup／Inbox、Memory、Subagent、MCP、Workflow 隔离、拒绝路径 | 20 通过 | 20 通过 |
| 真实 RPC：允许的扩展仍可运行；禁用工具时保留 Memory，并阻止 MCP spawn、自定义工具加载和向模型暴露工具 | 通过 | 通过 |
| `qoj-editorial-completion` | 未要求复验 | 原 manifest／模板、独立工作区和 HOME：已 attested，`/paveway help` 有响应，0 次 Run，正常退出 |

独立 Runtime 和 systemd 各通过一次 **19.1.1 → 19.2.1**：活动期间不切换，原安装被替换时旧快照仍可执行；切换后恢复同一个 Conversation 和物理 Session，模型看到原历史，没有重放，删除新安装来源后 Worker 仍可执行。systemd 验证实际 `MainPID`。用真实 OMP 包装出启动拒绝时，两版都留住原 Runtime；破坏 nonce、cwd、直接子进程身份或启动 JSON 时都没有转发测试输入，且留下私有诊断。

本次另通过 85 项定向 Python 测试（包括 `test_native_entry`、`test_omp_installation`、`test_runtime_lifecycle`、`test_paveway_script`、`test_compatibility`）及 50 项定向 Bun 测试。RPC 正例把合成工具声明为 `loadMode: "essential"`，让它确实进入该版本的 `dumpTools`，原断言不变。升级计数用例仅在隔离 fixture 禁用 `<composer-prediction>` 辅助请求，仍精确检查五次业务请求、三次 Worker Run 和每个 Native turn 只提交一次。

独立评审覆盖 API、启动／诊断和版本交接；修正了禁用工具时的子代理策略、活跃引用目录的持久化、systemd PID 与重启代理状态，以及拒绝和脱敏用例的假阳性。真实二进制通过本机 PTY／NDJSON 和 loopback 模型验证；未使用生产认证，也未测试 Tern 或降级读取新版所写 Session。

## `a8c56ad` 迁移时的运行证据

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

## 后续收尾：能力边界

| 接口 | 分类与取舍 |
| --- | --- |
| Extension 生命周期、命令、`onTerminalInput`、widget、theme、Composer Shape | 公开 API；准入校验必需、视觉 hook 可选。Windline 改用 `belowEditor` 单行组件，不替换 OMP footer。 |
| `@oh-my-pi/pi-tui.getKeybindings()` | 公开包导出，`app.session.resume` action ID 仍有语义变化风险。保留主会话快捷键拦截，读取用户 remap；`session_before_switch` 另拒绝越过 owner 目录的物理 Session。`getShortcutConflict` 只能查询冲突，不能替代按实时键表匹配。 |
| `ctx.sessionManager` | 公开只读 facade（包括 Session ID／文件、分支及 `ensureOnDisk`）；不在运行时导入 SessionManager 实现。 |
| Session JSONL、compaction ancestry | 持久化格式／未承诺稳定的细节。`git_sync.py` 和 `cache_usage.py` 解析可验证的记录，`paveway-memory-context.mjs` 检查 `firstKeptEntryId` 和 replay ancestry，决定恢复前缀，不将损坏或未知物理会话报告为有效。 |
| RPC JSONL v1、Prompt Template 动态字段 | 公开协议仍有语义演进风险；RPC 以 `ready`、请求 ID、Session 核对、settled／abort 证据准入；模板适配集中于生成器，覆盖 19.0／19.1 的两组已观察字段。 |

Tijian 跨 cwd 继承仍 **BLOCKED**。在官方 19.0.0 和 19.1.1 的隔离项目加入 `--write-policy=full` 的 fixture，并用 `--config` 指向含 `mcpServers.tijian`、`--write-policy=none` 的 YAML；`--mode rpc --no-session` 启动并查询 `get_state`，没有发送 prompt。项目 `full` 进程已经启动，overlay 的 `none` 进程未启动。Paveway 没有采用这条路径。`--add-dir` 不注入 MCP；插件根把服务加在插件命名空间；`mcp.enableProjectConfig=false` 丢弃整个项目来源，按名称禁用也会禁用同名受管 server。它们均无法证明同名同义去重、同名异义拒绝并保留其他 native MCP。

上游缺少的是**连接前、去重前、当前进程内**合并单个受管定义的公开入口。依据 [发布版 config.ts:111–192](https://github.com/can1357/oh-my-pi/blob/v19.1.1/packages/coding-agent/src/mcp/config.ts#L111-L192)、[capability/index.ts:248–333](https://github.com/can1357/oh-my-pi/blob/v19.1.1/packages/coding-agent/src/capability/index.ts#L248-L333) 和 [mcp/oauth-reauth.ts:145–263](https://github.com/can1357/oh-my-pi/blob/v19.1.1/packages/coding-agent/src/mcp/oauth-reauth.ts#L145-L263)：最小改进是向 MCP discovery 增加带来源及等价策略的进程级输入，对每个有效同名候选检查 `command/args/cwd/env` 等连接语义；尊重原生 disabled/project/profile 策略，冲突在认证、spawn 之前失败，`/mcp reload/test/reauth` 使用同一份已解析配置。`mcpManager` 或连接后移除工具不能撤销已发生的副作用。

Git Sync 的三项原失败分别在干净源码归档 `753df86` 和 `a8c56ad` 复现：首项 `state='error'`（`Sync filesystem/database operation failed`），双设备 `transferred=False`，PTY 等不到 `exported_runs == 1 && state == 'ok'`。`sqlite3.Row` 直接传给 `_read_session()`，后者在解析前用 `.get()` 校验 cwd；`AttributeError` 中止整个导出周期，所以已有物理历史没有进入提交或 push。修复只在 `git_sync.py` 的 SQLite 边界转为 `dict`。原分支、隐私、远端和队列断言未改。

独立 Reviewer 检查实际差异、测试和运行输出。标准审查发现脚本路径的 `stat/exec` 竞态和 ELF 只具执行权限时的回归；改为 Linux `O_PATH` + descriptor exec，shebang 需要时用同一个 fd 重试。规格审查发现交接取消尚未派发的 Run 时会误扣 attempt；把已插入但未启动的租约和正在启动的租约统一退回，并用显式异步 barrier 验证两条分支。已运行的任务保留原完成／`uncertain` 证据。

安装身份复现：启动真实旧 Runtime 和正在推流的 Worker，用 `os.replace` 在同一绝对路径换成另一份官方二进制；不先调用启动器，从仍存活的前台排定已到期的 Agent Wakeup。旧 Worker 自然成功，`status.omp.installation_status=handoff_required`，Wakeup 保持 `queued/attempt=0`，`agent.restart` 拒绝，纯 Reminder 进入 Inbox。关闭旧前台、原流程安全交接后，新 Runtime 报告另一版本且 Wakeup 仅派发一次。独立和 systemd 临时用户服务均验过；还验证了文件删除后的阻断、软链接改指不隐式切换，以及 OMP 不存在时私有 D-Bus `Notify` 仍仅一次。

本轮真实二进制专用命令（均使用隔离 HOME／状态及 loopback Provider）：

```bash
PYTHONPATH=src:tests python -m unittest discover -s tests -v
bun test tests/*.test.mjs
PAVEWAY_TEST_NATIVE=1 PAVEWAY_TEST_REPLACEMENT_OMP=/absolute/other/omp \
  PYTHONPATH=src:tests python -m unittest -v test_native_omp test_native_mcp.NativeWorkflowMcpPtyTests
PAVEWAY_TEST_CACHE_OMP=1 PYTHONPATH=src:tests python -m unittest -v \
  test_rpc_memory_boundaries test_prompt_cache_omp test_memory_checkpoint_omp
```

选择一份官方 OMP 放在 PATH；要验证反向交接就交换 PATH 与 `PAVEWAY_TEST_REPLACEMENT_OMP`。设置 `PAVEWAY_TEST_SYSTEMD=1` 可跑真实 user manager 上的临时 Runtime unit；不替换用户已有的服务。GitHub Actions 用官方发行页 SHA-256 校验两份二进制，并在两个版本方向上跑真实集成测试。远端 Actions 自身未执行。

本轮没有用真实账户复打 GPT-6.2 Sol。未认证的两份隔离 OMP Profile 不列出该 selector；这不推翻 `a8c56ad` 使用现有认证的真实请求证据。回归使用真实 OMP 进程和 loopback Provider，不把 fixture 回复当作真实模型结果。

| 本轮验收 | 状态 | 证据 |
| --- | --- | --- |
| 官方安装身份与安全交接 | VERIFIED | 两份发行件分别通过官方 SHA-256 校验；真实 Worker 运行中双向原子替换；独立／临时 systemd unit、已到期 Wakeup、删除、软链接改指、restart、Inbox 与私有 D-Bus。 |
| Tijian 同 cwd / 跨 cwd | VERIFIED / BLOCKED | 真实 stdio 捕获权限参数及模型工具目录，None／Managed／Full 不提升，兼容独立全局 Server；跨 cwd 拒绝时配置文件字节级不变、原 owner 不变。成功继承与重名去重等待上游接口。 |
| Git Sync 回归 | VERIFIED | 3 项原失败全部通过，包含双设备、分支、禁止同步隐藏推理／工具内容和 PTY 远端归档。 |
| Python 默认全量 | VERIFIED | 668 项：566 通过、102 条件跳过、0 失败（Linux Bun、私有 D-Bus 依赖及真实 wheel；单独执行有环境门槛的真实 OMP 场景）。 |
| Bun、Wheel、Windline | VERIFIED | Bun 1.4.5：41 通过；非 editable wheel 从工作树外读取 Skill；42 列浅色／92 列深色真实终端，原生草稿、多行粘贴、倒计时、Busy 和未读 Inbox。 |
| Session、Compaction、Memory | VERIFIED | OMP 19.1.1 的 raw RPC、原生 PTY、checkpoint 三组 8 项全通过；保持 Session ID、物理文件、旧 JSONL 前缀与压缩后的 Memory 重建语义。 |
| 真实账号、Windows 登录态、真正的 IME | NOT TESTED / BLOCKED | 没有重用真实模型凭据；没有取得操作 Windows／QOJ 已登录页的授权；PTY 粘贴不等于物理 IME。 |

另跑的 OMP 19.1.1 全量 + native opt-in 快照为 667 项：635 通过、27 条件跳过、5 失败。出现 PTY 就绪／输入竞争及 `/tmp` 压力（含 native addon 提取错误），不是一次全绿运行。用磁盘支持的 `TMPDIR=/var/tmp` 和最终 fixture 逐项复验，5 项均通过。19.0.0 定向集合同样存在旧测试轮次的 `/tmp` 耗尽及 PTY 失败；热替换、真实 Wakeup、Profile 隔离、长输入、MCP、Reminder 重排的后续定向复验通过。本轮未声称所有 opt-in 用例在同一验收命令中全绿。

关键提交：`5c96c638`（Git Sync）、`2c3e1482`／`69548a35`（安装身份和安全导入）、`e592b967`（Windline）、`4219b5bc`（MCP 实证）、`274bbaa8`／`a76e81bc`（真实 Relay）。

### Browser Relay：隔离 Chrome 的实测范围

OMP 19.1.1、Chrome for Testing `158.0.8127.88`、官方扩展 `0.2.0`。先发现 Chrome for Testing 的自定义 `--user-data-dir` 不读取默认 host 注册目录，扩展报告 `Specified native messaging host not found`；把官方生成的原样 manifest 放入**临时测试** profile 的 `NativeMessagingHosts` 后连接。测试 Chrome 在私有 D-Bus、无桌面显示、临时 profile 下运行，不使用账号和已有 Tab。调试 CDP 只用来诊断扩展启动，页面操作明确走 `app:"relay"`。

Relay 是 Chrome 原生消息扩展与 loopback CDP 服务，并非 Paveway 扩展内部资源。官方 [bridge.ts](https://github.com/can1357/oh-my-pi/blob/v19.1.1/packages/coding-agent/src/tools/browser/relay/bridge.ts#L1161-L1205) 以连接管理 claims、以 Chrome tab 复用 debugger attachment；连接关闭时只释放本连接的 claims。跨 Chrome 重启有 browser-run 身份隔离。官方 [browser.md](https://github.com/can1357/oh-my-pi/blob/v19.1.1/docs/tools/browser.md#L179-L189) 区分 OMP-owned 与 borrowed Target；Subagent 必须显式选 Relay，默认不继承。Loopback 就绪检查走直接 TCP，不依赖 HTTP 代理；Windows discovery 不等同于 WSL 的本地 registry。

- **VERIFIED**：单 Agent 开／读／关；2／4 个 Scheduler Worker 同时持有不同 Target；关掉一条后其余继续读。三个 Worker 加前台并发时，定向 `agent.restart` 只取消目标 Worker，旧事件保持 `uncertain`、`attempt=1` 且没有重派，其余会话完成。执行：`PAVEWAY_TEST_NATIVE=1 PAVEWAY_TEST_RELAY_ROOT=/tmp/<private-fixture> python -m unittest discover -s tests -p test_browser_relay_omp.py -v`。
- **FAILED（间歇）**：增强退出断言后有一次单 Worker 正常退出超时，后续四路的第四条无法取得容量。同一测试另一次四个子场景均通过（含首个 Worker 自然退出后其它 Tab 仍能使用，50.297 秒）；没有证据归因于 Chrome、OMP 或 Paveway，未加重试或延时掩盖结果。
- **VERIFIED**：同一 OMP 内 1／2／4 个 Subagent 使用唯一 Tab 名及显式 `app:"relay"`；另做复现时两个兄弟共用同名 Tab，OMP 返回 `Reused` 和 `Tab ... is busy`，不是两个 Target。Paveway 未改 Relay 所有权机制，也不加全局锁或重试。
- **VERIFIED**：真实前台调用关闭端口上的 Relay 时，工具返回 `Browser open timed out ... while launching or connecting to the browser (relay:http://127.0.0.1:1)`，未回退到其他浏览器。
- **NOT TESTED**：独立 Runtime 之间、Subagent 单独取消、已借用 Target、掉线后的自动恢复；**BLOCKED**：未经授权的 Windows 用户 Chrome、QOJ 登录页和官方 PDF。因此不把历史 `target_closed` 归因于 Paveway 或声称已修复。

Windows／WSL 实机复验：在 Chrome 所在的一侧通过官方 `omp browser-relay install` 注册 native host，确认扩展为 `on`；若 WSL 侧不能通过本地 registry 发现 relay，把仅限本机的端口安全转发并设置 `omp config set browser.relayUrl http://127.0.0.1:<port>`。先用公开测试页面和不同的 Tab 名，分别做 1／2／4 路和单路退出，再在获授权的会话只读检查 QOJ/PDF。不要记录 Cookie、题面或私人页面。

## 最终收尾：连接前 MCP 与 Relay 生命周期

起点：本地与 `origin/main` 同为 `8c7ea3ffb43ee09d9188907eadb524c175e04ae4`。GitHub Releases API 返回的最新正式版仍是 [OMP 19.1.1](https://github.com/can1357/oh-my-pi/releases/tag/v19.1.1)；以下兼容矩阵继续用 19.0.0 和 19.1.1，不把测试版本变成运行限制。没有恢复专用 Host、修改官方发行件、改变运行时安装身份或重写 UI。

**Tijian 跨 cwd：BLOCKED。** 复查了三个新候选：ACP 明确关闭原生 MCP 发现，另置连接管理器；SDK 自定义 `mcpManager` 替换而非扩充原生管理器，亦不统一 `/mcp test`、`reauth`；OMP 扩展包可提供未加命名空间的名字，但优先级 90 低于项目原生的 100，同名时静默取优先项。没有重复上一轮已经证明不安全的 `--config` 实验，也没有允许启动后再查冲突。[独立上游方案](./omp-managed-mcp-proposal.md) 把显式输入放进同一发现流程，在去重、凭据解析及 spawn 前检查连接语义，保留其他来源和原生禁用策略，并绑定 reload/test/reauth 的同一代解析结果。当前正式版未包含该接口；没有实现原型，也没有向 OMP 提交 Issue 或 PR。生产仍安全拒绝该跨 cwd 请求。

### Relay 观察与最小反例

`tests/relay_lifecycle_trace.py` 只在测试 Runtime 记录单调时间、实例、Run、guardian/OMP 子进程、工具阶段、RPC 结果、stdin、清理、持久化、回收；不记录请求内容。实际 `targetId` 取自官方 `browser.tabs()`；`browser.targets()` 的 `target` 是 URL，不能当作 Target 身份证明。

- **VERIFIED：受控反例。** 完成四个 Relay Target 的重叠验证。A 关闭 Target 后，故意扣住 A 的最后一段模型 SSE，先确认 A 仍 `running`、没有 `prompt_result`、没有关闭 stdin，第五个任务保持 `queued`。只释放 A 的 SSE，观察 `prompt_result`（+14,164 ms）→ `session_settled`（+14,169 ms）→ stdin 关闭（+14,185 ms）→ guardian 退出码 0（+14,490 ms）→ Run 持久化（+14,499 ms）→ 槽位回收（+14,500 ms）→ 第五个 guardian 启动（+14,556 ms）。三个兄弟在此期间仍持有原 Target，所有正常 Run 只执行一次。时间以 A 的 guardian 启动为零，来自一次实际运行。
- **FAILED（历史，根因未定）。** 上一轮只有“关闭工具已返回、Run 尚未终结”的证据，没有最后 SSE、RPC settlement 或关管道事件；不能将受控扣流反例归因为历史真实故障。当时沿用同一个 Runtime 继续下一组，记录显示第四个任务排队时旧 Run 仍占一个槽。现在拓扑用例分别隔离；连续十轮在首错立即停止，不把同一故障传播伪装成新问题。Fixture 只接续当前 assistant turn 的工具结果；barrier 未到显式报错，不回正常终止文本。不改生产清理、quarantine 或调度语义。
- **FAILED（保留负样本）。** 并行运行多个全量套件及冷启动 Relay 时，发现新失败发生在 **prompt 前**：前台 `Native OMP admission timed out`，后台 `agent_rpc_ready_missing`；原始日志中这些 Run 未发送 Provider 请求，不能解读为 `target_closed` 或已 settled 后未退出。WSL 当时为 2 CPU，CPU PSI `avg10=98.56`，约 1.9 GiB swap 已用。将原生用例改为顺序运行，并使用官方 `PI_NATIVES_DIR` 共享本次测试的版本化 addon，不共享 HOME、Profile、Session 或认证。
- **VERIFIED：隔离 Chrome 的 Target 所有权。** 用 `browser.open({app:"relay", target: fixtureUrl})` 借用已有的 loopback 页面；`close()` 后 Relay 仍报告原 ID。关闭该私有 Chrome，官方工具明确报 `Nothing serves the omp browser relay`，未回退其他浏览器。以同一私有 profile 重启后，同一页面得到新 ID，旧 ID 工具拒绝，借用新页面后不关闭它。首次用 5 秒预算按 ID 借用在 attach 阶段超时，保留这条负样本；后续 URL 借用通过，不将短预算下的按 ID 借用标为通过。此实验只操作一次性的 Chrome profile。
- **BLOCKED：Windows／WSL 用户浏览器。** Windows Chrome、`omp` 和 `sh.omp.browser` 注册存在，但发现文件中的 PID 已消失，端口在 Windows 本机也不能访问，WSL 无本地 Relay。未重载正在使用的 Chrome 扩展，未导航用户 tab，也未读取 QOJ 或 PDF。先由用户检查扩展面板 Connection 诊断并恢复 `on`，从 WSL 验证 `curl --noproxy '*' http://127.0.0.1:<port>/json/version`，再按上面的公开页面步骤验收；QOJ 登录页需单独授权。历史 `target_closed` 无归因结论。

### 可复验的测试环境

WSL2 `6.6.114.1-microsoft-standard-WSL2`、Python 3.14.4、Linux Bun 1.4.5；磁盘支持的 ext4 `/var/tmp`（开始约 934 GiB 可用）。官方 Linux x64 发行件与各自的 `SHA256SUMS.txt` 一致：

| OMP | SHA-256 |
| --- | --- |
| 19.0.0 | `c7cc5c8d505053c8ffaf180417e5d5c781235b9d936f286810560151120109bf` |
| 19.1.1（最新） | `04e9490f9394da6d6804474cc2e41eb3d18f8e26215df4b34bdd4ad4c0d8a401` |

```bash
# Foundation：PATH 不放 OMP，关闭真实账号和 Native opt-in。
TMPDIR=/var/tmp PYTHONPATH=src:tests python -m unittest discover -s tests -v
bun test tests/*.test.mjs

# 每次一个正式版；交换 PATH 中的 omp 和 REPLACEMENT 即可反向验证。
# TMPDIR 为本次测试新建、模式 0700 的磁盘目录。HOME、wheel、依赖亦隔离。
PAVEWAY_TEST_NATIVE=1 PAVEWAY_TEST_CACHE_OMP=1 PAVEWAY_REQUIRE_REAL_OMP_PACKAGE=1 \
  PAVEWAY_TEST_SYSTEMD=1 OICOACH_TEST_SYSTEMD=1 \
  PAVEWAY_TEST_REPLACEMENT_OMP="$REPLACEMENT" PAVEWAY_TEST_WHEEL="$WHEEL" \
  PAVEWAY_TEST_NATIVE_CACHE="$TMPDIR/omp-natives" PYTHONPATH=src:tests \
  python -m unittest discover -s tests -v

# 官方 OMP 19.1.1、已启动的私有 Chrome for Testing 158.0.8127.88、
# 官方 Relay 扩展 0.2.0；不需要付费请求。
PAVEWAY_TEST_NATIVE=1 PAVEWAY_TEST_RELAY_ROOT="$PRIVATE_RELAY_ROOT" \
  PAVEWAY_TEST_NATIVE_CACHE="$TMPDIR/omp-natives" PAVEWAY_TEST_RELAY_TRACE="$PRIVATE_LOG_DIR" \
  PYTHONPATH=src:tests python -m unittest discover -s tests -p 'test_browser_relay_*.py' -v
```

Relay 用例外层单独监督一次性 Chrome；`omp browser-relay install --no-chrome` 仅在新 HOME 执行，照原样复制生成的 native host manifest 到临时 profile，删除 `DISPLAY`、`WAYLAND_DISPLAY` 和现有 `DBUS_SESSION_BUS_ADDRESS` 后，用 `dbus-run-session` 与 `--headless=new --password-store=basic --user-data-dir=... --load-extension=...` 启动。待这个 profile 的 host 就绪后开始；无论断言通过还是失败，停止这个测试 Chrome（包括可能残留的测试 Target），不要让多个验收共用用户浏览器。`PAVEWAY_TEST_RELAY_TRACE` 指向专用日志目录，原始 PTY 和 Provider 数据不进入仓库。

旧 [Actions 45545690604](https://github.com/ETO-leader/Paveway/actions/runs/45545690604) 的 6 项失败来自 OMP-free runner：`test_lifecycle_state` 2 项无法 claim，`test_prompt_cache_contract` 4 项提前得到 `omp_installation_unavailable`，根本没走到它们要断言的合同。改为构造 `Runtime` **之前**指定已有 fake，真实 Native 测试仍强制官方发行件；不调整断言。修正后的三组 Python foundation 在 [45566492760](https://github.com/ETO-leader/Paveway/actions/runs/45566492760) 实际通过。中间的 Native 工作流曾为保护日志全局改 `umask 077`，使 deliberately-mode-0755 的安全测试构造不正确；现在用 `mktemp` 单独保护原始日志，不改变测试文件权限。最终完整发现结果以最终 hosted / 本地套件为准。

中间的 19.1.1 Hosted Native 还记录了 Skill 包只读查看之后的 `exit_ui()` `process.wait` 超时（不是文件读取或权限断言错误）。原测试在收到路径拒绝提示后立刻发 Escape，再等三次 50 ms，未确认焦点返回。`5094eaf` 等待真实终端选择菜单重现、Escape 后菜单消失，再提交 `/exit`；原有只读／不可变包／零 Provider 调用／退出码断言均保留。双版本定向验证均通过（19.0.0：4.013 s，19.1.1：3.512 s）。

### 最终 Relay 运行

`911b8d1` 的 Relay 测试和 `2ea7721` 的私有 addon 缓存在同一次 `discover -p 'test_browser_relay_*.py'` 中：**13 通过，0 失败，285.833 秒**。一份原生 addon 缓存实测 137 MiB。外层随后停止一次性 Chrome。

| 场景 | 状态 | 证据 |
| --- | --- | --- |
| 同一官方 OMP 内的原生 Subagent | VERIFIED | 单路、双路及十个连续四路批次；每批 4 个不同 `targetId`，父 turn 结束后 managed 与 Relay 页面列表均为空；全部不同别名场景累计 43 个不同 ID。 |
| 一个 Runtime 的 Worker | VERIFIED | 文本／Eval／只列 Target／只开页并依靠退出清理／显式关闭后退出；双路和连续十个四路批次。十批为 40 个不同 Target、40 个 guardian 启动、40 次槽回收；无增加 attempt。 |
| 前台 + 三 Worker | VERIFIED | 真实 PTY 完成前台 admission 后开始运行；四条实际 Target 重叠，指定后台任务 restart 后其余继续。 |
| 两个独立 Runtime | VERIFIED | 各持有一个实际 Target；指定 Runtime 的 `agent.restart` 不改变另一 Runtime 的 Runner/Target。 |
| 独立取消、同名对照 | VERIFIED | 原生 Agent Hub `Ctrl+S`、选中 `x`：只终止指定 Subagent，其 owned Tab 消失，兄弟同一 Target 继续完成；两兄弟故意同名时显示 `Reused` 且只有一个 Target。 |
| Chrome 中断／重启／借用 | VERIFIED | 一次性的额外官方 CLI 对照；关闭已借用 Tab 的 handle 后原 Target 仍在，停服务诊断明确，重启后旧 ID 拒绝。 |
| 执行中网络抖动与旧连接自动恢复 | NOT TESTED | 断线实验验证服务已停止后的下一次打开，以及重启后的旧 ID；不外推到任意在途操作。 |
| Windows／WSL 和登录后的 QOJ／PDF | BLOCKED | 无在线用户 Relay，也未获私人页面访问授权。 |
| 历史退出超时和 `target_closed` 归因 | NOT TESTED | 未重现当时的故障链；保留历史失败，未改生产代码掩盖。 |

负样本也保留：最初的 Subagent 组合运行出现一次 prompt 前 admission 超时；后来追踪运行在第 11 个子场景前达到外层命令时限。随后的 13 项是在分离其他高负载测试、使用私有 addon 缓存后的一次完整运行，不是将两次局部结果拼在一起。标准和生命周期 Reviewer 阅读实际差异和原始失败证据：修正了 MCP 延迟 UI 与 SDK awaited 路径描述；将混合、连续和控制场景拆成独立 fixture；确认 trace helper 与测试同一提交。没有待处理的严重差异缺陷。

本地保留 `/var/tmp/paveway-final-logs/{workers-final-reviewed.log,relay-suites.log,faults-url.log}` 和对应的 `pw-native-test-*.jsonl`。四 Worker 最终 trace 中 `round_passed` 的 attempt 为 `1…10`，`target_verified`、`guardian_started`、`slot_released` 各 40 条。Hosted 不上传完整原始 PTY/Provider 输出，只发布测试标识、状态、项目栈位置和计数。

### 完整回归与保留的跳过项

- [Hosted 45567708625](https://github.com/ETO-leader/Paveway/actions/runs/45567708625) 的代码为 `2ea7721`：Python 3.11／3.12／3.13 默认全量各 680 项、560 通过、120 条件跳过、0 失败。默认矩阵不要求安装 OMP；Bun 全量 41 通过、0 失败。
- 同一 hosted run 的两条 Native 全量均一次性零失败：19.0.0 为 680 项、652 通过、28 条件跳过（515.067 秒）；19.1.1 为 680 项、652 通过、28 条件跳过（508.431 秒）。两份分别验证 Session、Compaction、Memory、原生 PTY、Skill 包、Git Sync 三项原失败、双设备、MCP stdio 和交接。外部模型配置 11 项、隔离 Relay 13 项、三个上游源码审计及一个退役的旧 context guard 是明确跳过。真实 Relay 已独立全部执行。MCP stdio 覆盖同 cwd 的受管权限、独立 MCP、冲突时目标配置保持不变，以及跨 cwd 拒绝时保留原 Conversation；不等于官方已支持跨 cwd 继承。
- 系统服务定向实测：官方 19.0.0 → 19.1.1 与 19.1.1 → 19.0.0 的临时 systemd user unit 均通过；另测 systemd 服务恢复队列且不重放活动 prompt。未改实际用户服务。版本切换只证明新旧进程交接，不承诺旧 OMP 能读新版写出的 Session。
- 本轮使用 loopback Provider，**NOT TESTED**：真实 GPT-6.2 Sol 及付费 OAuth 回话；不把 fixture 文本当作真实模型证据。先前真实调用证据见上文。用户升级 OMP 后等待正在运行的任务完成，再 `./paveway.sh --omp /absolute/path/to/omp` 选定新的官方路径并触发安全交接；systemd 用 `agent.executable` 的绝对路径。回退用已经验证的旧官方发行件；要跨版本回读则另验 Session 格式，保留原始档案，不能仅凭二进制能启动就强行复用旧数据。
- 本地同一 `2ea7721` 冻结快照另跑完整 19.0.0 Native：680 项、647 通过、33 条件跳过、0 失败（668.542 秒）。比 hosted 多跳过本地无默认路径的 Chrome 1 项和 4 个 systemd opt-in；两版的 Chrome 与真实原子替换 systemd 用例另有本地通过证据。
- [45568670793](https://github.com/ETO-leader/Paveway/actions/runs/45568670793) 对 `5094eaf` 复验为 **FAILED**：两个版本都在 Skill 导入冲突后重开草稿超时；19.1.1 另遇已回收子进程的 `/proc` 读取异常。前者同样从通知直接 Escape 后固定等 150 ms，未等到当前选择菜单交还焦点；现在按渲染状态同步后保留冲突、并发设置、草稿正文与单次模型调用断言，两个版本定向实测通过。后者用真实 Linux 子进程复现了 open 后、read 前 reap 返回 `ESRCH`；测试现在只把 `ENOENT`/`ESRCH` 视为进程已消失，不改 SIGKILL、退出证据、重启和不重放断言。修正后一项定向验收通过（1.375 秒）。两项修复均在 `b6d60ac`。
- 同一 `2ea7721` 的本地 19.1.1 全量为 **FAILED**：680 项、646 通过、33 跳过、1 失败。旧 restart 用例依赖瞬时 `requested`，而 `2a1e4c2` 起 B 的定向 restart 可以在无关任务 A 仍运行时变成 `running`。`4b762bc` 在 B 的真实子进程就绪前设置确定性 barrier，验证 B 已运行时拒绝重复请求、B 恢复自己的 Session 且先于 A 完成、A 不重派且 attempt 保持 1。所涉四项 Runtime 定向用例均通过；不改变生产调度合同。
- [45570304891](https://github.com/ETO-leader/Paveway/actions/runs/45570304891) 在 `b6d60ac` 再次完成全部五条 Lane；两份官方 Native 各 680 项、652 通过、28 跳过、0 失败（19.0.0：646.423 秒；19.1.1：499.708 秒）。Skill 导入冲突、SIGKILL 不重放、三项 Git Sync 回归在两个日志都明确为 `ok`。本地 OMP-free 默认发现也一次完成 680 项、561 通过、119 跳过、0 失败。
- **VERIFIED，最终测试代码 `4b762bc39f3e2472806e2f0fa00623b11f50a61c`：** [Actions 45571704214](https://github.com/ETO-leader/Paveway/actions/runs/45571704214) 五条 Lane 全部通过。官方 19.0.0／19.1.1 两条分别在 508.856／538.180 秒完成 680 项全量，各 652 通过、28 条件跳过、0 失败；scope restart、SIGKILL、Skill 导入冲突、MCP 原生拒绝和 systemd 双向替换均明确为 `ok`。验证分支为 `verification/extension-first-final-8c7ea3f`，没有向 `origin/main` 推送。单独的本地 Relay 日志证实 13 项通过，不参与 hosted 的通过计数。
