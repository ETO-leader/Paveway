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
