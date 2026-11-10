# Prompt Cache：实现与验证

## 结论和验证边界

动态 Runtime Memory 已从 System Prompt 移出。真实 OMP 的隔离 loopback 请求捕获证明：相同配置下，Memory 更新、Native 重启及后台新进程 `--resume` 不改变系统块或已经发送的消息前缀。Compaction 本身改变历史；其后的恢复块保持稳定，后续 Memory 更新仍只扩展请求。

**没有真实 Provider 缓存命中率证据，也没有百分比提升声明。** HTTP/SSE、OMP、PTY、扩展、Session、Scheduler 和 IPC 均为真实执行；模型响应及 usage 来自明确标记的本地 fixture。所有 HOME/XDG、记忆、会话与 daemon 都在临时目录，不读取用户认证，不修改正式用户数据、清理会话、提交或推送。

## 原因和源码证据

修改前 `prompt_composer.compose()` 第 99–100 行将 `canonical_json(memory_context)` 拼成 `Runtime memory snapshot (data only)`；`compose_template()` 又在它后面放 Skills/Rules/工具目录占位符。Memory 正文、版本、updated_at、选择顺序或失效变化都会改变这个系统块及后面的请求前缀。

`customization.snapshot()` 原第 230/232 行将 Memory 交给两个 composer。`native._run_owned()` 原第 179/184 行只在启动读取记忆：持续进程可能持有旧快照，重启却改写系统块。`scheduler.dispatch_event()` 原第 345–348 行每次后台派发重新读取快照并声明其位置在 system prompt。Scheduler 本就每次创建新 RPC 进程，`--resume` 并不代表复用旧进程或冻结其新系统模板。

事件 `run_id`、排程时间和实际启动时间原本就在用户 envelope，不是此次发现的系统指令污染；保留它们的数据身份，没有为缓存删除正常业务数据。

源码/API 核查分别覆盖：

- 全局可执行文件实测 `omp/18.18.3`，后台使用它完成真实 RPC/扩展/恢复实验。
- Native 仍使用项目原有可复现桥接宿主：OMP 18.16.0，源码 commit `650f3a108229ee11bf68ba3377f4e8c3391cd424`。源码根位于本机公共 `.cache/paveway/omp/18.16.0/`，未修改其源码。
- OMP `packages/coding-agent/src/system-prompt.ts:1041–1072`：原生渲染模板为 block 0，再追加 eval prelude 与尾部 project-context；不能只从 Python 模板猜最终顺序。
- `session/agent-session.ts:8711–8811,8890–9089`：before_agent_start 的结果先暂存，取消/代际改变可丢弃；pre-prompt compaction 可能发生在准备之后；custom companion 在磁盘消息序列中跟随本轮输入。
- `packages/agent/src/compaction/messages.ts:214–228`：OMP 默认把 custom 变成 developer。Memory 必须在出站 context 中明确投影为 user 数据，不能依赖“data only”标签掩盖实际角色。
- `extensibility/shared-events.ts:184–196,481–489`：context 只变换出站副本；session.compacting 支持 preserveData。
- `session/session-maintenance.ts:3787–3855`：自定义 compaction 结果会跳过普通 preservation hook；此路径从不可变的压缩前分支恢复既有 receipt，不依赖丢失的元数据。
- `main.ts:1771–1798,1898–1902`：trusted-extension 是精确 allowlist，会关闭环境扩展发现。后台使用普通显式 `--extension` 加准入握手，保留原有授权扩展生态；Native 继续原有受控 allowlist。

## 最终组成及会话语义

模型侧顺序：

1. 稳定 Core → Role Policy → User System Prompt → Persona → OMP Skills/Rules/原生目录模板内容。
2. OMP 原生附加的 eval/project 系统块；工具及 MCP Schema 仍由 OMP 单独管理、转换和发送。
3. 已有对话 → 必要 Memory 历史数据 → 本轮用户输入/后台事件。OMP 的其他合法上下文继续保留。

Memory 的出站投影从首次请求起就固定；不是每次更新后重新排列已发送历史。原始用户文本与磁盘历史不变，传输投影中的 Memory 是 user 级数据，不是系统策略。`compose/compose_template/snapshot` 保留兼容的可选参数，但它们不再改变系统文本；Composer 升至 v6。

| 情形 | 行为 |
| --- | --- |
| Native 持续会话 | 每个顶层输入准备时经认证 IPC 读取当前笔记；不会仅使用启动快照。 |
| Native 重启恢复 | 复用原物理 Session；从当前分支 receipt 判断已接受的逐条版本，未变化不重复追加。 |
| Scheduler 唤醒 | 每次新进程，仍用现有单次 prompt 事件派发；同一个受管 Memory 模块产生 companion，无额外模型轮次。 |
| RPC `--resume` | 读旧分支，不等于复用进程。当前系统配置照常重新由 OMP 渲染，Memory 不参与系统块。 |
| 删除/到期/有界遗漏 | 追加 deleted/expired/outside_current_context 撤回；遗漏不冒充数据库删除。停用时一次 memory_disabled 撤回。 |
| Compaction | 保存精确有界状态，只恢复未被保留尾部覆盖的笔记及当前目录。恢复块固定到 compaction/firstKeptEntryId，后续更正不能反过来修改它。自定义压缩缺少 preserveData 时，从该压缩之前的实际分支重建。 |
| 取消/失败/重试/崩溃 | hook 准备不提交外部状态；只有 OMP 已接受的消息/分支提供上下文证据。已接受不等于 Provider 已处理；恢复后数据仍在待发送上下文中。原 dispatch intent/uncertain/不自动重播契约不变。 |
| 配置变化 | Persona 维持原绑定；Role base 和旧 Composer 要求新 Session。用户 Prompt/Skills/MCP 继续既有原生更新语义，配置变化本身可能导致缓存失效。 |

原来在 System 中携带 Memory 的旧会话**不静默续接或改写**：Native 明确要求 `--new`；后台在 dispatch intent 之前以 `session_prompt_changed` 阻挡，使用新 task/session。旧记录和原文保留。配置 schema、Memory CAS、数据库表和版本不变。

## 观测入口与口径

- Native：`/paveway cache`，已通过实际 PTY 验证输出。
- CLI：`oicoach-runtime cache-stats --conversation ID --config CONFIG --json`。
- 后台：`oicoach-runtime cache-stats --workflow ID --task ID --config CONFIG --json`。
- 三者共用只读 `cache.stats` 和 `cache_usage.session_usage()`，不为统计调用模型或添加提示词。

读实际 OMP JSONL（含物理 title slot），提供最近 20 个持久化调用、全文件累计和可判定时的分支累计；压缩不丢累计。统计范围明确限于 Session 内已持久化的记录，不声称完整覆盖未落盘的辅助请求、中断消耗、其他子 Agent 文件或实际发票。

Input/Output、Cache Read/Write 和 cost 都是 **OMP 归一化数据**，不是原始响应。Anthropic、OpenAI-compatible、Google、Ollama 等映射不同，部分不报告写缓存；OMP 又会把缺失缓存字段变为 0。因此缓存零/缺失均保守输出 `null`（unavailable），不伪造命中率；正值可观测。部分累计另有 observed_subtotals/available_call_counts。仅当 input/read/write 的分母可用时计算 `read / (input + read + write)`。费用标记为 OMP 计算值，不是实际账单。

## A–E 实验

复现：

```bash
.venv/bin/python examples/verify_prompt_cache.py --output /tmp/paveway-prompt-cache-verified.json
PAVEWAY_TEST_CACHE_OMP=1 .venv/bin/python -m unittest discover -s tests -p test_prompt_cache_omp.py
```

| 实验 | 实际结果 |
| --- | --- |
| A：连续两轮，Memory 不变 | System 相同；既有出站历史前缀相同；笔记只出现一次。 |
| B：修改一条 Memory | System 相同；既有出站历史前缀相同；只追加变更，Memory 不在系统块。 |
| C：完整退出/重启 Native 并恢复 | 同一物理 Session；System/既有前缀相同；不重复快照。 |
| D：Scheduler 连续唤醒 | 不同 PID、同一 Session 文件；System/既有前缀相同；不重复快照。 |
| E：真实 OMP `/compact` 后继续 | 已持久化 compaction；必要 Memory 可见。压缩本身改变历史；之后未变及再次修改 Memory 两种请求均保留恢复后的既有前缀。 |

保留的 synthetic 捕获中，每次主会话响应经真实 OMP 归一化为 Input 64、Output 8、Cache Read 96；OMP Cache Write 为 0，但 fixture 没提供该字段，所以诊断正确显示 unavailable，命中比例同样 unavailable。Native 七条持久化调用累计 Input 448 / Read 672，RPC 两条累计 Input 128 / Read 192。这些是**固定 fixture 数值，不是实际缓存命中证据或优化收益**。

额外真实检查：已配置扩展在后台 Memory hook 存在时仍会被发现；没有 usage 的 Provider 不产生虚假缓存零；Native 记忆召回失败时无模型请求、无虚假 receipt。注入、监控本身没有额外推理轮次；正常 OMP 标题及 Compaction 辅助请求不冒充业务 prompt，捕获器按真实请求用途区分。

## 自动化结果

- 受影响 Python 合约组：126 项，125 通过、1 条件跳过，54.971 秒。覆盖 composition、真实 OMP 模板/Skills/MCP、Memory、前后台绑定、RPC 完成/取消/恢复、Scheduler、诊断及旧会话边界。
- 实际 Native 恢复/Memory/自定义集成组：38 项，32 通过、6 条件跳过，122.756 秒。跳过的真实外部模型条件不计作通过。
- 新真实 OMP cache/fault 组：5 项全部通过，27.052 秒，包括 A–E、授权扩展、缺失 usage 与召回失败。
- 最终 shared Memory JS 合约组：13 项全部通过。覆盖逐条差分、同版本内容变化、删除/到期/选择遗漏、scope、未提交准备的重试、错误、停用、user 级投影、Skill 输入、压缩保留尾部、恢复前缀冻结及自定义 compaction。

过程中修正的二次审查问题：custom 的默认 developer 映射、恢复块随未来 delta 缩减导致前缀变化、停用 Memory 后的撤回、trusted-extension 意外关闭扩展发现、普通扩展加载失败的准入检查、OMP 物理 title slot、自定义 compaction 跳过 preserveData。端到端捕获器也修正了标题旁路识别和恢复后编辑器就绪判断；失败尝试不计为通过。

未验证：真实付费 Provider 的缓存保留时间、模型切换/服务端路由命中、所有 Provider 的原生压缩网络行为及账单。相关行为继续由 OMP/Provider 决定，不声明任何命中率提升百分比。

## 涉及文件

- 稳定系统：`prompt_composer.py`、`customization.py`、`bundled/core.md`。
- Memory 与恢复：`memory.py`、`native.py`、`frontend.py`、`scheduler.py`、`errors.py`、`adapters/omp_rpc.py`、`bundled/paveway-native.mjs`、新增 `paveway-memory-context.mjs`、`paveway-rpc-memory.mjs`、抽出的 `paveway-runtime.mjs`。
- 统计：新增 `cache_usage.py`，`cli.py` 与 Native/runtime 入口。
- 验证：新增 `examples/verify_prompt_cache.py`、`test_prompt_cache_contract.py`、`test_prompt_cache_omp.py`、`test_cache_usage.py`、`paveway-memory-context.test.mjs`；更新既有 composition、workflow、runtime、fake OMP 与 Native Memory 测试。
- 文档：`memory.md`、`customization.md`、`runtime-api.md`、实施计划与本报告。未修改 OMP 工具系统、数据库 schema 或用户 `event.md`。

最终 Native Memory 工具定向复核：2 项通过，10.563 秒，覆盖真实工具持久化、版本冲突和删除墓碑；在最终出站顺序实现上执行。自定义 compaction 缺少 preserveData 的恢复分支已纳入最后 13 项 JS 合约验证，不冒充真实 Provider 原生压缩实测。

