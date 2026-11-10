# Prompt Cache 审计修复（b809568 基准）

基准为远端 `main` 的 `b809568f1f6b6e1336304beb9d14c81ebcfd134f`；开始时工作树干净，fetch 后远端仍为该提交。保留静态 System Prompt + 追加式 Runtime Memory；不改数据库 Schema、Composer v6、用户 Session 或 OMP 工具系统。

## 已确认的问题与修复

### `/clear` 的可见性边界

OMP 18.16.0 源码 commit `650f3a108229ee11bf68ba3377f4e8c3391cd424`：

- `session/agent-session.ts:6615–6705` 的 `resetSessionContext()` 追加 reset boundary，不删除磁盘历史。
- `session/session-manager.ts:3717–3726` 的 `appendResetBoundary()` 只写追加标记。
- `session/session-context.ts:353–357,485–504` 在最新 reset 比最新 compaction 更新时只取 reset 之后的记录；`getBranch()` 则仍包含旧记录。

原 `memoryState()` 从整个分支寻找 receipt，误把不可见的旧 Memory 当作已提供；`restoreCompactedMemory()` 也可能跨 reset 取旧 compaction。现在两者均受当前分支最新 `reset_boundary` 限制。第一次请求重新提供**当前选择**的必要笔记；后续未变化不再追加。不把旧对话、旧版本、已删除笔记复制回来；原始输入和磁盘消息不修改。

旧 schema 1 的 `details.state` 是当时的选择集，不必然是该 reset 之后实际提供的数据：例如 clear 后旧实现可能只发新增 B，但把未发出的 A 也写进 state。新实现对 reset 之后的这种旧 checkpoint 不采信，改重放其实际 delta；没有 reset 的旧 Session 保持可读。新内部 checkpoint 使用 schema 2 标记“由实际 receipt 重放得到的状态”，模型侧 delta 仍为 schema 1。无需再次改变 Composer 会话版本或清理旧记录。

### 分支统计的证据边界

OMP `session/session-manager.ts:4015–4022` 的 `branch()/resetLeaf()` 可只改变内存 leaf，不写磁盘；`:3588–3612` 的 `appendModelUsage()` 可向非活动分支追加记录，`activeLeafId` 是可选回放提示，不是实时持久化的权威 leaf。

因此只读 JSONL 不能证明实时 Active Branch。`cache_usage.py` 不再输出 `active_branch` / `on_active_branch`，改为：

- `cumulative`：全文件、全分支的持久化调用累计，保持原有准确范围。
- `last_persisted_entry_ancestry`：最后持久化逻辑条目及其父链；离分支的末尾 usage 会改变此范围。
- 每调用 `on_last_persisted_entry_ancestry`；实时叶的限制见 `semantics.live_active_branch`，明确 unavailable。

Native `/paveway cache`、CLI `cache-stats`、RPC `cache.stats` 共用同一结果，不维护另一份活跃叶或 Session 树。16 MiB 文件 / 1 MiB 单行上限保持不变；超限拒绝，不冒充完整统计。

### Session 完整 Memory 状态的重复存储

基准 `planMemory()` 每次变化都把完整选择集写进 `details.state.notes`，而模型输入本来就是增量。压力实测证明磁盘成本显著，因此改为：普通 receipt 只保留已有 `details.delta`，每 64 条**已接受的变化 receipt** 在同一个 `details.state` 字段保存一次 checkpoint。恢复从最近可用 checkpoint 重放增量；计数只从真实分支推导，准备、失败和重试不更新外部计数。Compaction 仍在 OMP 原有 `preserveData.pavewayMemory` 保存必要状态。没有新表、独立日志、历史管理器或缓存系统。

## Provider Compaction：确认修复与未增加的代码

`session/session-context.ts:505–566` 表明兼容宿主的普通、Anthropic native 和 OpenAI native Compaction 均有宿主 `compactionSummary`；原生 payload 附在 summary 的 `providerPayload`。没有证据表明有效的这三条路径会无 summary，故保留无 summary 时原样返回，不发明恢复锚点。

真实 SDK 构造上下文发现另一处实际缺陷：Anthropic 的 `firstKeptEntryId == ""` 配合 `providerReplayThroughEntryId` 仍保留后者之后的尾部；旧恢复代码忽略该尾部，重复恢复其中的笔记。修复按 `session-context.ts:598–644` 的实际范围判定覆盖：

- 普通压缩使用 `firstKeptEntryId`。
- Anthropic 缺失 kept anchor 时，使用 replay-through 之后的尾部。
- OpenAI replacement history 不使用 kept anchor：完整替换不保留旧尾部；带 replay-through 时只保留其之后的记录。

SDK 探针覆盖 OpenAI 完整替换（没有 replay-through）和前缀替换（replay-through 在 kept anchor 之前，中间有 Memory receipt），以及普通、自定义、Anthropic。只恢复既有状态中未被原生尾部覆盖的笔记，不改变原生 payload。恢复块固定于该 compaction 之前的不可变状态和范围，后续更新/删除不重写过去的块。自定义 compaction 丢失 preservation hook 时仍从压缩前的真实分支恢复，不依据未来的数据库快照。

上述验证执行真实 `SessionManager` / `buildSessionContext()`，原生 payload 是协议形状 fixture；没有向真实 Provider 请求或验证不透明加密内容的服务端语义。

## 定量压力实验

`examples/prepare_memory_pressure.py` 使用临时数据库中的生产 `Memory.put()` CAS 和完整 `Memory.context()` 选择器，保留所有元数据、trust、byte_limit 等 envelope。固定测试时钟保证选择顺序；64 条笔记，初始实际 Context 为 **65,396 字节**，每轮只更新一条，执行 1,000 次。JS 中间序列最终与真实 Python CAS 的最终 Context 逐字段一致。比较保存的 b809568 模块与修改后的模块，无压缩、无网络 Provider。

| 更新轮数 | 基准 JSONL 字节 | 修复 JSONL 字节 | 基准 JSON 解析中位 ms | 修复 JSON 解析中位 ms |
| --- | ---: | ---: | ---: | ---: |
| 100 | 7,088,873 | 558,673 | 11.727 | 0.753 |
| 300 | 20,866,473 | 1,471,779 | 27.101 | 3.966 |
| 500 | 34,646,562 | 2,385,996 | 49.692 | 6.672 |
| 1,000 | 69,126,496 | 4,705,923 | 97.147 | 14.533 |

解析计时为两次预热后五次本地样本的中位数，含 GC/调度噪声，不是性能保证。1,000 轮状态重放中位为基准 0.226 ms、修复 0.179 ms；100 轮为 0.0076 ms、0.0533 ms。checkpoint 重放增加少量增量处理，收益主要是磁盘和 JSON 解析，而非宣称恒定时间恢复。

基准第 241 次更新跨过 16 MiB：16,733,193 字节可读，16,802,081 字节被真实 `session_usage()` 拒绝。修复后 1,000 次更新仍可读，准确返回 1,001 个 fixture 调用。完整 `state` 累计从 65,391,605 降至 979,906 字节。

两实现追加给模型的 Memory 文本完全一致：累计 1,550,233 UTF-8 字节，OMP 原生严格 tokenizer 计数 300,067；所有采样点的文本 SHA256 相同。这是**新追加 Memory 文本**的总量，不是历史反复重放后的 Provider 输入 Token 或账单；减少 checkpoint 磁盘副本不冒充减少模型输入或提高缓存命中率。1,000 轮内容 SHA256 为 `af9e98f36d3b708798c68aca081580c9e2d1113965816b754a52ba3e96be6194`。

复现压力实验（BUN / OMP_SOURCE 取公共 pinned `runtime.json`，输出必须在临时目录）：

```bash
PYTHON=.venv/bin/python "$BUN" examples/stress_memory_context.mjs \
  src/oicoach_runtime/bundled/paveway-memory-context.mjs /tmp/paveway-memory-pressure "$OMP_SOURCE"
PAVEWAY_TEST_CACHE_OMP=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_omp_memory_boundaries -v
```

完整 Context 选择器样本替代早期仅含简化 note 字段的预实验；预实验数字不作为严格 64 KiB 验收数据。

## 观测与未验证边界

静态 Core → Role → User Prompt → Persona → OMP 原生系统模板保持原架构；动态 Memory 是 user 级历史数据，追加在已有上下文之后、当前输入之前；OMP 的 Tools、MCP、Skills、Rules 和权限处理不变。reset / compaction 自身会改变可见历史；边界之后的正常 Memory 变化不能重写既有前缀。

观测仍只读取 OMP 归一化的 Input / Output / Cache Read / Cache Write / cost。正缓存值可观测；缺失或被 OMP 归零而无法确认原生 provenance 的字段为 unavailable。分母不可靠时不计算命中比例，费用不冒充实际账单。loopback 的 cached_tokens 是固定 synthetic fixture，不是真实缓存命中证据；没有调用付费 Provider，没有真实命中率提升百分比。

## 实际请求与故障实验

最终 `examples/verify_prompt_cache.py --output /tmp/paveway-prompt-cache-audit-wire-final.json` 成功，A/B/C/D 的实际系统块和既有消息前缀均相同；E 的压缩本身改变历史，压缩后的连续请求及再次修改 Memory 保留恢复后的前缀。额外 clear 场景验证原磁盘历史保留、未变化记忆重新提供一次、重启后的前缀相同、压缩后更新/删除仍只追加。

共同版本验证强制 Native 与 RPC 使用同一个 `prepare_native_host()` 产物；`--omp` 只接受同一实际路径，不能用不同全局版本制造一致性证据。

**Native/TUI 与原生 RPC 的命令行为不同：** `modes/rpc/rpc-mode.ts:1938` 经 ACP built-in dispatcher 派发；`slash-commands/acp-builtins.ts:66` 跳过没有通用 handler 的命令，`slash-commands/builtin-lifecycle.ts:192–200` 的 clear 只有 `handleTui`。真实 RPC 测试确认 prompt `"/clear"` 仍是普通用户输入，调用模型但不追加 reset boundary。没有为测试发明 RPC reset 接口。Native 的真正 `/clear` 已由 PTY 测试覆盖；RPC 另测真实 `compact`、新进程恢复、更新/撤回及冻结恢复块。

`verify_memory_checkpoint.py` 用同一真实 OMP RPC 进程完成 63 个本地 loopback 对话，逐条 CAS 更新实际笔记；在第 64 条变化上，观察生产 `before_agent_start` 准备出的 checkpoint 后分别硬退出和调用真正 RPC abort。两者均无新模型请求、无第 64 条 durable receipt，原 63 条 receipt 原样保留。新进程恢复重试只提交一个 checkpoint；下一次未变输入不重复注入，系统和历史前缀相同。共 65 次 fixture HTTP 请求，没有付费 Provider。

## 二次独立审查

两个独立 reviewer 完成源码复查，修正了验证而非降低断言：

- 压力输入改为生产 Context 选择器及完整元数据，不再用简化 note 声称 64 KiB。
- 原生 OpenAI 夹具区分完整替换与前缀替换，覆盖 replay-through 与 kept anchor 之间的 Memory。
- checkpoint 取消/崩溃不再仅用数组模拟，而是执行上述真实 host 交付生命周期。
- 一致性入口拒绝不同 Native/RPC 可执行版本。

复查后无剩余 finding。另修复两个既有回归夹具的问题：archival guard 的 tokenizer 从固定 Linux 宿主解析，避免 WSL PATH 中 Windows Bun / 漂移的全局 native 包；ASCII TUI 测试排除居中的 splash `PAVEWAY`，识别实际带其他内容的 footer。实际终端观察确认 splash 与正常气泡 footer 同时存在，并非生产 footer 丢失。没有修改生产 TUI 或放宽气泡断言。删除只回显 mock 字典/转发参数的 CLI 测试，保留非法选择器、真实归档与真实诊断验证。

## 文件范围

| 范围 | 文件 |
| --- | --- |
| 生产 Memory | `src/oicoach_runtime/bundled/paveway-memory-context.mjs`：reset 可见范围、兼容旧 receipt、增量/checkpoint 重放、原生压缩保留范围。 |
| 生产诊断 | `src/oicoach_runtime/cache_usage.py`：持久化祖先链命名、明确实时活动叶不可得，全文件累计不变。 |
| 合约回归 | `tests/paveway-memory-context.test.mjs`、`tests/test_cache_usage.py`、`tests/test_prompt_cache_omp.py`。 |
| 真实 SDK / host 回归 | `tests/test_omp_memory_boundaries.py`、`tests/test_memory_checkpoint_omp.py`、`tests/test_rpc_memory_boundaries.py`。 |
| 可复现实验 | `examples/verify_prompt_cache.py`、`examples/verify_omp_memory_boundaries.mjs`、`examples/verify_memory_checkpoint.py`、`examples/verify_rpc_memory_boundaries.py`、`examples/stress_memory_context.mjs`、`examples/prepare_memory_pressure.py`。 |
| 既有夹具纠正 | `tests/test_context_budget.py`、`tests/test_native_statusline.py`；只修改测试依赖/实际 footer 定位。 |
| 文档 | `docs/memory.md`、`docs/runtime-api.md`、本报告及旧验证报告的后续审计链接；实施计划位于 `docs/superpowers/plans/2026-11-10-prompt-cache-audit.md`。 |

`native.py`、`scheduler.py`、`adapters/omp_rpc.py`、Composer、MCP/Skills/Tools、权限和 Session 所有权的生产代码不需要第二套实现，保持原有通道与策略。

## 最终自动化执行

使用公共 pinned manifest 中的 Linux Bun，禁用外部模型配置、systemd 与 wheel opt-in；真实 Native、loopback RPC 和 SDK opt-in 均启用：

```bash
export PATH="$(dirname "$BUN"):$PATH"
env -u OICOACH_TEST_REAL_OMP_CONFIG -u OICOACH_TEST_SYSTEMD \
  -u PAVEWAY_TEST_WHEEL -u PAVEWAY_TEST_CACHE_OMP_EXECUTABLE \
  PAVEWAY_TEST_NATIVE=1 PAVEWAY_TEST_CACHE_OMP=1 PYTHONPATH=src:tests \
  .venv/bin/python -m unittest discover -s tests
"$BUN" test tests/*.test.mjs
PYTHONPATH=src .venv/bin/python examples/verify_prompt_cache.py \
  --output /tmp/paveway-prompt-cache-audit-wire-final.json
```

| 最终检查 | 通过 | 跳过 | 失败 | 实际耗时 |
| --- | ---: | ---: | ---: | ---: |
| Python 全量 471 项 | 455 | 16 | 0 | 654.061 s |
| JS 全量 134 项 / 5 文件 | 122 | 12 | 0 | 0.944 s |
| 完整 A–E + Native clear/restart 捕获 | 全部不变量成立 | 0 | 0 | 22.68 s |

跳过不算通过；本次新增的真实 OMP 回归均已启用执行。Python 日志仍含 SQL 连接 `ResourceWarning` 和 loopback 断连的 `ConnectionResetError` 栈，unittest 最终结果为 OK；没有为得到通过而屏蔽日志。

开发期首次并行捕获曾遇到一次 Native 恢复进程提前退出，当时仅有通用报错，根因未确认。现捕获器保留退出码及终端尾部；后续专项、顺序完整捕获及最终全量均未复现。此项不声称已经找到并修复生产故障。真实 Provider 命中率和服务端 native-compaction 密文处理仍未验证；诊断的 16 MiB 上限也未扩张。

全部修改留在工作树；没有 commit/push，没有修改真实用户数据或删除现有 Session。
