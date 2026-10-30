# OMP 原生重构 M0：兼容性门槛记录

Stage: **M0 调查与门槛判定已交付；不等于 M1–M6 完成。**
日期：2026-10-30。依据用户提供的 Implementation Contract 与 Native Refactor Plan；D4 失败分支要求禁用受管 TUI、保留旧 RPC/TUI。本次没有新增受管入口、切换默认入口或修改 OMP 上游。

## 基线与数据边界

| 项目 | 本轮观察 |
| --- | --- |
| Code base HEAD | `35cf9a911c9df90598f664d215ea74cf29f6faa9` |
| 分支／初始未提交修改 | `main`；`git status --short` 为空 |
| Paveway | `0.20.0`，本次不升级应用版本、不宣称原生重构已发布 |
| OMP | `omp/18.12.6`，Bun 全局安装的 `@oh-my-pi/pi-coding-agent` |
| 实际入口 | `~/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent/dist/cli.js` |
| 平台 | Linux `6.14.0-37-generic`，x86_64；Python `3.13.3` |
| 正式 Runtime 配置／数据库 | 仓库 `.runtime/config.toml`、默认 XDG 配置与默认数据库均未找到；未创建正式配置，未连接其他位置的用户实例 |
| 正式 Workflow／Session 数量 | 未探测未提供配置的其他用户数据目录；不是“用户没有会话”的结论 |
| DB 迁移 | 无；现有 `001_initial.sql`、`002_memory.sql` 不变 |
| Tijian 版本／Windows→WSL | 本轮未连接、未实测，不能据 Linux fixture 声称 Windows 路径可用 |
| SQLite／Session／Workflow 私密备份 | 未执行：没有已定位且要修改的正式数据库或 Session；正式迁移前仍须执行 SQLite Backup API 与私密文件备份 |
| Commit SHA | 见包含本记录的 M0 提交；Code base HEAD 是修改前基线，不冒充本记录自身的提交 SHA |

环境安装：系统 `venv` 缺少 ensurepip；通过 `pip --python .venv/bin/python install -e '.[test]'` 完成隔离环境安装，未修改系统 Python 或全局 OMP。

## 已运行的检查

### 旧路径基线

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

`Ran 266 tests in 214.220s — OK (skipped=14)`：252 通过、14 跳过，无失败。包括旧后台 after-turn、CAS/dedupe、uncertain 不重播、进程回收、熔断、归档及旧 UI 的 PTY 场景。不是付费模型、systemd 或 Windows 验收。

原始日志：本次执行记录 `artifact://7`。出现 Python 3.13 的既有 SQLite `ResourceWarning`；未以重跑或抑制警告冒充修复。

同一命令在改动后的最终回归运行 186.130 秒，**250 通过、14 跳过、1 failure、1 error**，日志 `artifact://16`。因此旧基线不能被宣称稳定全绿，完整 M0 验收未通过：

- `test_runtime.RuntimeTests.test_restart_preserves_future_event_and_single_instance`：事件设为 0.6 秒后到期；停止 daemon 时已领取，最终为 `blocked`／`daemon_shutdown`，未达到测试期望的 succeeded。源码 `tests/test_runtime.py:417–426` 在启动第二个 CLI 后才停止首个 daemon，不能保证事件一直在未来；本次不通过延长时间或忽略 blocked 来改变验收。
- `test_tui.TerminalIntegrationTests.test_pty_custom_prompt_and_skill_crud_reach_native_dispatch`：`pyte.Screen.display` 在 `screens.py:241` 对空字符单元执行 `char[0]`，抛出 `IndexError`。发生在等待回复的屏幕渲染阶段；不是 prompt/skill native 验证的失败。尚未证明是应用重绘还是 emulator 对宽字符残留单元处理所致，不冒称已修复。

上述 Runtime／PTY 测试及其应用路径未被本次修改，也没有为了消除失败而重跑、跳过或修改这些断言。保留最初通过与最终失败两份证据；这两项仍是恢复完整 M0 绿色基线前的未解决问题。

### 原生 OMP 探针

可复现入口：

```bash
.venv/bin/python examples/verify_omp_m0.py
```

依赖项目的 `[test]` 环境以及 PATH 中的 `omp`。探针为 Linux PTY 实验，不进入普通 unittest discover。它创建临时 HOME、XDG 目录、workspace、Session、Runtime 配置和本地 HTTP fixture；不继承凭据、代理、OMP profile、父会话配置，不加载正式用户数据。模型 selector 固定为本地 `paveway-m0/probe`。

**真实的是安装的 OMP CLI、原生 TUI、RPC、Extension、provider 请求转换与 Session 写入；模型端点返回固定 fixture，不是真实模型验收。** 默认关闭工具、Skills、Rules 与自动发现扩展，仅加载显式实验扩展。原生 Prompt/Skill 验证在独立子进程中按测试要求启用所需能力，不发送模型 prompt。

输出是 JSONL，包含 provider 请求次数、Hook 错误、可见回复、TUI 屏幕以及正常 Guard 的对照；不输出完整系统 Prompt 或凭据。退出码：`2` 表示实际观察到 fail-open 阻碍，`1` 表示探针或对照失败；`0` 也不代表完整 D1–D7 认证。探针退出后清理临时数据，不启动正式 daemon 或服务。

本轮已观察：

| 场景 | 结果 | 含义 |
| --- | --- | --- |
| RPC `context` Hook 抛出 `M0_CONTEXT_FAILURE` | `extension_error` 后，本地端点收到 1 次请求；`prompt_result=completed` | 抛错不会自动阻止未裁剪上下文继续发送 |
| 正常的现有 Guard，对同一超长输入 | 1 次请求，出现裁剪提示；请求明显缩小 | 正常路径能改变发往 provider 的副本；不能据此宣称错误路径 fail-closed |
| RPC 显式加载不存在的扩展 | 仍收到 1 次请求并 completed | `--extension` 不是 required-extension 拒绝执行机制 |
| 原生 TUI 的 `context` Hook 抛错 | 屏幕同时出现扩展错误和回复；本地端点收到 1 次请求 | **D4 在实际 TUI 上失败**，不是仅靠源码推断 |
| 原生 TUI 显式加载不存在的扩展 | 仍显示回复；本地端点收到 1 次请求 | 启动导入失败也不是 fail-closed |
| 原生 RPC transport + append/replace + Skill 读取/权限 | 4 项通过 | 无模型 Prompt 的真实 OMP 接口兼容；不代表新 Composer 已实现 |

早期实验的完整输入为 `M0_INPUT ` 加 12,000 次 `漢字 filler `。未裁剪 RPC provider messages JSON 长度为 127,843 字符，正常 Guard 对照为 32,475 字符；包含系统消息，**不是对话 token 数**，不可把字符数当 10k-token 证明。现有 `test_native_window_budget_tool_pairs_multibyte_and_archive_immutability` 在基线中通过原生 Tokenizer 的预算、配对与不修改原文检查。

原始观测日志：`artifact://11`（早期 RPC）、`artifact://13`（早期实际 TUI 屏幕）。保留探针的整合运行日志为 `artifact://15`：退出码 `2`，`controls_passed=true`，4 项原生检查 `exit_code=0`，正常 Guard 保留 Session 中完整原文。首次 PTY 实验停在新 HOME 的 onboarding，0 请求，不能作为安全通过；之后显式关闭临时配置中的 setup wizard 才得到上述有效 TUI 结果。

## D1–D7 决策矩阵

| 分歧 | 证据等级／尚缺检查 | 本轮选择 |
| --- | --- | --- |
| D1 交替恢复同一物理 Session | **受限，未完成** TUI→RPC→TUI 的工具配对、分支、Compaction 和正常退出恢复验收；未操作正式 Session | 采用文档的独立物理 Session 设计分支；不实现共享接管、不直接拼写 JSONL |
| D2 前台输入与所有权 | 已安装源码：input Hook 的普通异常被报告并吞掉；Extension 注入并非统一经过 input。未通过双 TUI／失联／旧 generation 故障矩阵 | 不开放共享 Session 接管；不以心跳或 DB generation 代替实际文件写入 fencing |
| D3 前台严格 settled | 已安装公共 Extension API 无与 RPC watcher 等价的完整完成证明；未运行后台继续／队列／取消矩阵 | 前台 `after_turn_settled` 关闭；不把 agent_end/session_stop 作为 settled。旧后台 CLI `--after-turn --run-id` 不变 |
| D4 Guard fail-closed | **FAIL：真实原生 TUI 与 RPC 均已故障注入复现**；缺失扩展仍推理。未继续做热重载／内部继续／超长工具／Subagent 矩阵 | **禁用受管 TUI；M1–M6 不启动**；保留旧 TUI/RPC |
| D5 Prompt/Skills | `--help` 支持互斥的 template/plain flags；真实 get_state 验证现有 append/replace、Skill 发现、原生 skill:// 读取及权限，4 项通过。自定义模板完整动态展开未验收 | 保留旧快照与原始配置；新统一 Composer、Persona 迁移均未实施，不能直接叠加两个系统 Prompt 参数 |
| D6 在线投递 | 源码显示 `pi.sendMessage()` 返回 void；未实测 idle/streaming/background/reconnect 顺序和交付证据 | 自动注入关闭；选择未来持久 Inbox 分支，**本轮未实现 Inbox**；不得声称 accepted/processed 或重播未知投递 |
| D7 原生历史/Git | 现有导出只发现 Paveway session_bindings；没有原生 TUI 的受信注册与只读索引。未通过长文本/分支/切换/恢复归档矩阵 | 新入口不启用、不设默认、不宣称新会话已同步；旧归档路径保持 |

“受限／未完成”是明确的门槛状态，不是伪造所有探针通过。D4 给出了文档规定的停止分支，因此后续依赖它的状态表、唤醒工具、Persona、默认启动器不提前落地。

## 安装版本的源码证据

下列路径均相对于已安装 `@oh-my-pi/pi-coding-agent`，不是上游 main：

- `src/extensibility/extensions/runner.ts:1632–1695`：捕获普通 Hook 异常后 emitError，返回可选 onFailure；`2101–2167` 的 context 路径未提供失败拒绝策略，保留原消息。实际 CLI 行为已由上述端点实验确认。
- `src/sdk.ts:4360–4363`：provider 的 transformContext 接入此 Extension runner。
- `src/extensibility/extensions/runner.ts:2074–2099` 与 `src/session/agent-session.ts:9809–9865`：input 与内部注入不是同一授权边界。
- `src/modes/rpc/rpc-session-settle.ts:40–120`：RPC 的 host-aware settled watcher 是额外逻辑，不等同于一个 Extension agent_end。
- `src/session/session-storage.ts:599–631`、`src/session/session-manager.ts:1096–1127`：原生写入 ownership 锁以及争用时分离 sibling session；不是 Paveway coach/generation 授权。
- `src/modes/controllers/extension-ui-controller.ts:1420–1444`：sendMessage 的异步投递错误在 UI 处理，不返回持久的交付确认。
- Paveway `src/oicoach_runtime/git_sync.py:366–448`：归档从现有 bindings 读取，不自动收集任意原生会话；`memory.py:180–266` 也依赖该受信绑定。

## 最小上游缺口建议（未修改上游）

需要一个**显式选择的 required context transform / required extension 策略**：

1. 必需扩展缺失、导入失败或被卸载时，阻止受管会话产生新 provider 请求；不能仅显示错误。
2. 必需 context handler 的异常／超时必须使当前 provider 请求失败或被中止，不返回未经该 Guard 处理的消息。普通可选扩展保持现有策略。
3. 暴露所选策略及启用版本，覆盖普通输入、内部继续、队列与相应 Subagent 路径；扩展失联不能被旧 ready 标志掩盖。

这只是最小 API 建议，不是 Patch 已获授权或已被实现。另一个独立缺口是公开、关联输入身份且可核验落盘的前台严格 settled 能力；不为此 Fork OMP。

## 改动与兼容性

- 新增 `examples/verify_omp_m0.py`，供未来 OMP 版本重复运行同一故障实验。
- `tests/test_customization_omp.py` 删除“原生 Prompt 必须包含未截断的完整 Skill 描述”这一措辞断言。18.12.6 会在索引中裁短长描述；保留 Skill 发现、禁用项隔离、正文不内联以及 **真实 `omp read skill://...:raw` 返回完整正文**的检查，不把断言改钉到新的截断文案。修改后 4 项原生检查均通过。
- 本记录与 `docs/progress.md` 说明门槛和剩余风险。Runtime、队列、schema、Prompt 原值、event.md、用户数据归档政策均不改。

## 回退、剩余风险与下一允许阶段

Rollback tested: **未做入口／schema 切换，故没有新运行路径可供回退演练**。旧 CLI/RPC 和旧 TUI 的 PTY 在最初基线中通过，但最终回归出现上文两项失败，不能宣称稳定全绿，更不能把旧 UI 可启动说成新 schema 已验证可降级。

复原方式：本次提交仅为验证脚本、措辞断言和文档；继续使用原来的 `./paveway.sh` 或 CLI 即为原有行为，不需要恢复数据库或重播 Prompt。若要撤销本次源码改动，可在确认后撤销该 M0 提交；不要重置用户数据、删除 Session 或重试 uncertain 任务。

**下一允许阶段：继续 M0，或在具备 fail-closed API 的 OMP 版本上重新执行探针。** 只有 D4 不再失败并完成缺失矩阵后才能验收 M1；M2–M6 仍分别受它们自己的门槛限制。没有测试真实模型、Browser、Subagent、Tijian 服务端授权、Windows→WSL MCP 或完整交替恢复；这些均不得标为完成。当前安装 OMP 的普通 Hook 报错策略也适用于 RPC；旧 ready 双握手不是“任何运行时异常都能阻止推理”的证明，本次未擅改旧派发状态语义。
