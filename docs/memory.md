# 记忆与历史检索（0.19.0）

记忆分为三层，均在本机运行，不需要 embedding 服务或额外 MCP：

| 层 | 实现 | 用途 |
| --- | --- | --- |
| L1 当前上下文 | 已有 per-task OMP session 和显式 resume | 同一任务连续讨论，保持上下文 |
| L2 长期笔记 | Runtime SQLite `memory_notes`，经 daemon CLI 维护 | 偏好、事实、持续任务摘要、待验证推测 |
| L3 历史 | 已保存的事件/run/回复检索；只读原生 OMP JSONL | 找回过去输入、回复及具体工具调用/结果原文 |

本机 OMP 18.10.4 已提供 off/local/mnemopi/hindsight/sharpshooter 原生后端；本实现检查了安装包的 memory-backend 与 mnemopi 类型契约。原生后端的 bank/工作目录作用域、模型/embedding 和配置独立于 Runtime 的 workflow/task。项目复用 OMP 的会话文件，不启动另一套 Agent、不接管原生后端，也不修改全局配置。这里是轻量可审计的笔记接口和原文读取层，使用标准库 SQLite/JSONL，不实现向量库或自动全历史蒸馏。已有原生记忆可继续使用，其行为和权限由 OMP 配置决定。

## 启用及升级

数据库版本从 1 迁移到 2，配置、IPC 和 workflow manifest 版本仍为 1。`init`（已有配置）或 `serve` 在单个 SQLite 事务中执行已知迁移；失败回滚，不删除已有队列、run、回复或绑定。`doctor` 只读，不迁移。升级前停止服务并用 SQLite backup API 备份数据库及 sessions；降级 0.6.0 需恢复升级前的备份，旧程序会拒绝 v2。

新建 `workflow create coach --workspace ABS --template coach` 默认写 `memory_enabled = true`。已有 workflow 不自动覆盖，缺省值 false，保持旧行为。启用方法：

1. 比较并手动合并安装包 `bundled/coach.md` 的记忆段至外部 `event.md`。
2. 在 `workflow.toml` 顶层加入 `memory_enabled = true`；变更模板时提升 workflow version。
3. 升版后的 workflow 用新 task；原 task 的绑定保留原 revision，不能直接偷偷改版续聊。旧任务历史仍可检索。

自定义 workflow 可只开启注入而采用自己的笔记策略。关闭 memory_enabled 只停止自动注入和工具提示，不删除笔记、不禁止同 UID 的 CLI 查询，也不能撤回已写入旧会话的上下文。

## 笔记接口

所有命令在已启动的同配置 daemon 上执行，接受 `--config PATH --json`。省略 `--task` 表示 workflow 共享 scope；给出则只属于该 task。不同 workflow 不自动共享笔记，同 workflow 的新 task 可取得共享偏好；task 记录不会注入其他 task。Runtime 没有“所有用户共用”scope，多位用户应使用独立配置/state/UID。

```bash
# 新建共享偏好，expected-version 默认 0；重复新建返回 conflict。
.venv/bin/oicoach-runtime memory put --workflow coach --key hint-first \
  --title '解题帮助偏好' --kind preference --pinned \
  --content '先给逐步提示，未经请求不要给完整代码' \
  --config "$PWD/.runtime/config.toml" --json

# 读取完整内容及版本。
.venv/bin/oicoach-runtime memory show --workflow coach --key hint-first \
  --config "$PWD/.runtime/config.toml" --json

# 用实际读到的版本更新；这里的 1 仅为刚创建后的示例。
.venv/bin/oicoach-runtime memory put --workflow coach --key hint-first \
  --title '解题帮助偏好' --kind preference --pinned --expected-version 1 \
  --content '先给提示；我明确要求完整解答时才展开' \
  --config "$PWD/.runtime/config.toml" --json

# 列表不输出正文，20 条一页，query 为字面子串，支持中文。
.venv/bin/oicoach-runtime memory list --workflow coach --query '提示' --offset 0 \
  --config "$PWD/.runtime/config.toml" --json

# 查看某个 task 下一次派发会取得的共享+task 笔记。
.venv/bin/oicoach-runtime memory recall --workflow coach --task training \
  --config "$PWD/.runtime/config.toml" --json

# 这里的 2 为上述更新成功后的示例；实际使用 show 返回的版本。
.venv/bin/oicoach-runtime memory delete --workflow coach --key hint-first --expected-version 2 \
  --config "$PWD/.runtime/config.toml" --json
```

`put` 还接受 `--task ID`、`--content-file UTF8_FILE`、`--source-event EVENT_ID`、`--expires-at RFC3339`。文件最多 4096 UTF-8 字节，正文不可空；标题最多 256 字节，key/IDs 沿用 1–128 ASCII ID 契约。source-event 必须存在并属于相同 workflow；task 笔记还需匹配事件 task。共享笔记可引用本 workflow 任意 task 的事件。有效期必须是未来的明确时区时间，过期后不列出或注入，show 仍可查证。

kind 是 `fact | preference | summary | hypothesis`。这是笔记作者的分类，不代表 Runtime 已验证事实。模型应保留证据、区分推断与独立完成，不将猜测的能力标签当事实。来源为空表示未关联某条运行事件，不自动伪造来源。

修改是完整替换：需再次传入希望保留的 kind/pinned/source/expiry。`expected_version=0` 仅用于不存在的新 key；更新必须等于现版本。并发只能有一方成功，另一方收到 conflict 后重新读取、判断。删除清空正文/标题/来源等并递增版本，保留版本墓碑防止旧写入复活；show 可读取墓碑版本，确需恢复必须用该版本显式 put。过期和删除笔记不自动删除原始会话、旧回复、WAL 或备份，不是物理安全擦除。

所有写入和 audit 在同一事务中；audit 只含作用域/key/版本/来源 ID，不记录正文。state 目录 0700、DB 0600，控制 socket 只允许同 UID。该隔离是注入/检索范围约束，不是对同 UID shell/插件的安全沙箱。

笔记写入是可独立完成的工具副作用，不与整个模型回合共同回滚。若保存成功后 OMP 崩溃，笔记可以存在而 run=uncertain；恢复时核对 source_event_id 和现版本，不自动重播原 prompt，也不因笔记存在就把回合标成成功。

## 自动注入和模型维护

仅在 workflow.memory_enabled=true 时，派发前读取有效笔记，注入系统 prompt 的 `Runtime memory snapshot` 数据段。快照含 `contract_version=1`、`notes`、`truncated`、`token_limit=null`、`byte_limit=65536` 和可信度说明。pinned、task 专属及近期更新优先，最多检查 101 条候选；超出 64 KiB 传输边界的笔记可经 list/show 查询。单条笔记仍最多 4096 UTF-8 字节，记忆总库不受快照大小限制。

### 对话窗口

2026-10-30 用户明确取消固定 10k token 限制。Runtime 不再自动加载 `context-window.mjs`，不再截短用户输入／工具结果或要求 `paveway_context_ready` 握手；对话上下文及压缩由 OMP 和所选模型管理。旧算法文件及故障探针仅保留作历史回归资料，不代表当前运行策略。及时维护记忆及历史检索要求继续有效；模型原生上下文窗口仍有自身上限。

## 对话窗口

Runtime 为每个 OMP 进程加载自己的 `context-window.mjs`，在每次模型请求前对消息副本执行独立的 **10,000 tokens** 滚动窗口，按 o200k_base 的消息内容 JSON 计数（OMP 原生 strict tokenizer），排除不会作为消息内容回放的 usage、工具 details、耗时等本地 metadata。优先删除最早的完整用户轮次；单个工具密集轮次超限时删除最早的完整 assistant/toolResult 交换，保留最新用户输入。最新工具交换本身超限时，在消息副本中缩短结果文本，保留首尾、调用 ID 和完整配对，并明确标记省略，避免整份新证据消失而诱发重复调用；无法缩短的调用参数或非文本结果仍可能整组移除。用户输入本身超限时保留首尾并明确标记中间省略；模型会收到上下文截断通知。工具调用及结果成组移除，避免孤立结果。被裁掉的当前轮工具交换会保留最多 1,500 tokens 的原文片段摘要（包含工具调用线索和结果前缀，明确标注可能不完整、仅作历史数据），计入同一个 10k 对话预算。摘要不包含 assistant 隐藏思考，不另建持久存储，不替代完整结果验证；每次从本轮原始消息重建，防止模型因裁剪而重做已经完成的查找。该操作不写回原始会话；数据库、磁盘完整历史与独立 Git 归档保持原有保留方式。

10k 记忆与 10k 对话独立计额，系统规则、Skill 目录、工具定义及 provider 封装不在这两个数据预算内；不是整个请求或账单 token 的严格 20k 上限，也不是所有模型共享同一原生 tokenizer。系统 prompt 和 event.md 告知 Agent 限制、及时维护笔记与按需检索要求。禁用工具时不能写新笔记。实际维护仍由 Agent 调用成功才算完成，不能把提示词要求视作保存保证。

该守卫属于 Runtime 必需组件，即使 `extensions_enabled=false` 仍显式加载；该选项继续关闭用户扩展的自动发现。启动必须收到守卫就绪标志，否则在发送业务 prompt 前拒绝派发，不静默无限上下文回退。已验证本机 OMP 18.10.4 / Bun 安装包；需可从 OMP 安装位置解析 `@oh-my-pi/pi-agent-core`。Python 依赖 tiktoken，首次加载 o200k_base 需要下载编码表；离线部署先在相同用户环境预热 `python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'` 并保留缓存。

## 历史检索

```bash
# 默认整个指定 workflow，可加 --task 限制；20 条一页。
.venv/bin/oicoach-runtime history search --workflow coach --query '线性基' --offset 0 \
  --config "$PWD/.runtime/config.toml" --json

# RUN_ID 用 search 结果替换；完整事件输入、run 状态及保存的最终回复。
.venv/bin/oicoach-runtime history show RUN_ID --config "$PWD/.runtime/config.toml" --json

# 读取指定 task 的原生会话文字；不启动 OMP，不发 prompt，不改变 leaf。
.venv/bin/oicoach-runtime history messages --workflow coach --task training \
  --query '消元' --offset 0 --config "$PWD/.runtime/config.toml" --json
```

search 对 reason、payload JSON 和已有最终回复做字面子串匹配，SQLite lower 处理 ASCII 大小写，不承诺 Unicode 全语言折叠；中文可直接匹配。`%`、`_`、引号都不作为 SQL 通配或命令。它不是语义/向量检索，也不全文索引所有工具原文；需要细节时对定位到的 task 使用 messages。query 最多 256 字节；offset 0–1000000。search 返回 run/event/task/status/started_at 和最多 512 字节片段。failed/uncertain 等照实保留状态，检索到不等于成功。

show 使用现有 run/event/output，升级前已有数据立即可检索，不重播模型；旧 run 没有最终回复时明确 unavailable。回复仍最多 16 KiB，truncated 表示需查 session 原文。

messages 只读取已验证绑定的任务 sessionFile，核对 header session ID、workspace、已知版本 1–3，拒绝路径逃逸、最终文件 symlink、非当前 UID、非普通文件。原文文件最多 16 MiB，单行最多 1 MiB，任务活动时返回 conflict，请等待回合结束。压缩/损坏/未知格式显式失败，不静默新开会话。

返回最多 20 个匹配 message，每条最多 2048 UTF-8 字节，包含 user/assistant/toolResult 文字和 toolCall 的 name/arguments；不输出隐藏 thinking、图像或二进制。返回 entry_id/parent_id 与 **archive_mode=all_branches**，这是文件中的所有历史分支，不代表当前执行分支或精确某个 run 的快照。query 在截断前匹配，片段定位匹配附近；truncated/has_more 明确表示不完整。新一页用 offset+20，读取期间应避免该任务继续修改会话。需要更多细节由用户检查私密原始 session，不能把片段当完整论证。

不扫描 OMP 全局会话、不导入其他项目历史、不读 Tijian DB、不发送记忆到同步 remote。跨设备同步、语义检索和所有历史的自动蒸馏不在本次范围。

## IPC 契约

沿用 API v1 的 request/result/error 和原有同 UID/大小限制；参数名称为下划线形式：

| command | 参数 |
| --- | --- |
| memory.put | workflow_id/key/title/content 必需；task_id、expected_version=0、kind=fact、pinned=false、source_event_id、expires_at 可选 |
| memory.show | workflow_id/key 必需，task_id 可选；含墓碑及过期笔记 |
| memory.list | workflow_id 必需，task_id/query=""/offset=0 可选；仅指定 scope |
| memory.delete | workflow_id/key/expected_version 必需，task_id 可选 |
| memory.recall | workflow_id/task_id 必需；与自动注入相同的受限快照 |
| history.search | workflow_id 必需，task_id/query=""/offset=0 可选 |
| history.show | run_id 必需 |
| history.messages | workflow_id/task_id 必需，query=""/offset=0 可选 |

未知参数/无效类型返回 invalid_input；版本或来源/活动归档冲突返回 conflict；缺失返回 not_found。CLI --json 成功 result，错误 stderr.error 和退出码 1。查原文失败不会修改 run 的历史状态或触发重试。

## 验证命令

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -p 'test_memory*.py' -v
.venv/bin/python -m unittest discover -s tests -p test_runtime.py -k memory -v

# 使用已有 OMP 认证和模型，实际调用两个模型回合；隔离的临时 state/workspace。
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_memory_omp.py -v
```

单元测试验证 scope/来源/中文检索/有效期/8 KiB 上限、并发 CAS、删除墓碑、防路径逃逸和原文分支分页。迁移故障注入验证 DDL 回滚，以及既有 events/runs/session_bindings/metadata/audit 逐行不变。假 OMP 经真实子进程 CLI 保存笔记，验证重启、独立 task、禁用注入和保存后崩溃不重播。真实模型测试先用 CLI 保存带随机识别码的共享偏好，再重启 daemon 并关闭工具，在全新 task 读取注入偏好；同时查询原生 OMP 文字和旧回合结果，确保私密内容不进 daemon 日志。
