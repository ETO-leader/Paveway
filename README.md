# Paveway

**Paveway is An adVanced Efficient Workflow for informAtics olYmpiads.**

Paveway 是运行在现有 OMP Coding Agent 上的 OI 教练框架：持久化用户请求与定时检查，按顺序唤醒正确的会话，连接 Tijian 题库与训练数据，保存可查询的执行结果与恢复记录。

当前版本 **0.8.0**：运行框架 M0–M4、Tijian JSON CLI/MCP 适配、可编辑的 coach workflow、systemd user 部署、回复读取、L2 长期笔记、L3 历史检索和终端交互 UI 已实现。

## 能做什么

| 能力 | 当前行为 |
| --- | --- |
| 训练安排与复盘 | 独立 coach 模板支持 plan、checkin、review，根据用户输入安排训练、处理反馈和复盘 |
| 即时请求 | ask 排队执行，可 --wait 等待并显示最终回复 |
| 终端界面 | ui 全屏交互，中文输入、教练对话、事件、记忆、历史、workflow 和服务操作，后台调用已有 CLI |
| 定时跟进 | 绝对时刻或当前回合完全结束后的延迟唤醒；发送 CLI 退出后仍由独立 daemon 执行 |
| 会话延续 | 按 workflow + task 隔离持久会话，恢复明确绑定的 session |
| Tijian | 26 个题库、状态/历史、评分、计划、日报、题单、备份操作，以及 stdio MCP bridge |
| 故障恢复 | 启动前有限重试、退避、熔断、人工 Agent 重启；可能已执行的 prompt 不自动重播 |
| 后台服务 | systemd user unit 的生成、校验、安装、启停、启用/禁用、查询、日志和卸载 |
| 可观测性 | 配置/数据库/OMP/服务诊断，事件、run、最终回复与恢复记录查询 |

教练提示放在外部 workflow，Runtime 不写死题目选择、训练时长或教学策略。安排仍需结合用户实际情况判断。异步回复保存在本地，目前没有网页、聊天平台推送或桌面通知。

## 环境要求

- Linux，支持 pidfd、parent-death signal 和 child subreaper。本机验证环境为 Ubuntu 24.04、Python 3.12.3、systemd 255。
- Python **3.11+**、venv、pip。核心与 Tijian CLI 适配只用标准库，MCP bridge 额外安装 `.[tijian]`。
- 可执行且已完成模型认证的 **OMP**，本机验证版本 **18.10.4**。安装与模型配置见 [OMP 项目](https://github.com/can1357/oh-my-pi)；先确认 OMP 自身能调用模型。
- 后台部署需要 `systemctl --user`、`systemd-analyze`、`journalctl`；前景 serve 不需要 systemd。
- Tijian 可选，需要提供 JSON CLI 的可执行文件。本轮核对的上游 commit 为 `b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f`。

没有 Tijian 也可使用基于用户输入的 coach。Paveway 不自动登录模型/OJ，不修改 OMP 全局 profile、认证或已有聊天。

## 快速开始

在源码仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/oicoach-runtime --version
omp --version

.venv/bin/oicoach-runtime init --config "$PWD/.runtime/config.toml" \
  --state-dir "$PWD/.runtime/state" --workflow-root "$PWD/.runtime/workflows"
mkdir -p "$PWD/.runtime/workspace"
.venv/bin/oicoach-runtime workflow create coach \
  --workspace "$PWD/.runtime/workspace" --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime workflow list --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime doctor --config "$PWD/.runtime/config.toml" --json
```

init 可重入，不覆盖配置；workflow create 不覆盖已有 workflow。已经创建过 coach 时直接使用，或选择另一个 ID。尚未启动服务时 doctor 的 daemon 离线 WARN 正常；FAIL 需先修复。

终端 A：

```bash
.venv/bin/oicoach-runtime serve --config "$PWD/.runtime/config.toml"
```

终端 B 发请求。下面时间仅为输入示例，请替换成你的实际安排：

```bash
.venv/bin/oicoach-runtime ask --config "$PWD/.runtime/config.toml" \
  --workflow coach --task training --mode plan --dedupe-key plan-1 \
  --message '时区 Asia/Singapore，今天 16:00–18:00 训练，17:00–17:15 休息。P1001 已有思路但没写，P1002 尚未读题。请安排思考、编码和检查点，不猜测题面，不安排自动提醒。' \
  --wait --timeout 180
```

--wait 显示保存的最终回复。去掉它立即返回事件 JSON，daemon 继续执行。等待超时只结束等待，不取消事件或重新调用模型。大段输入可改用 `--message-file /absolute/request.txt`，要求非空 UTF-8、最多 32 KiB。

反馈和复盘使用相同 workflow/task，新的请求使用新的去重键：

```bash
.venv/bin/oicoach-runtime ask --config "$PWD/.runtime/config.toml" \
  --workflow coach --task training --mode checkin --dedupe-key checkin-1 \
  --message 'P1001 已独立编码通过；P1002 有思路但没写，未看题解。还剩 30 分钟，请调整安排，不自动修改题库。' --wait

.venv/bin/oicoach-runtime ask --config "$PWD/.runtime/config.toml" \
  --workflow coach --task training --mode review --dedupe-key review-1 \
  --message 'P1001 用时 20 分钟独立通过；P1002 用时 55 分钟，看提示后口胡但没有编码。请分析卡点并给出下次训练建议。' --wait
```

mode 指导外部 coach，不改变 Runtime 调度语义。任务 ID 由你决定，不强制每天新建会话。

## 终端 UI

完成上面的初始化，并在另一终端运行 serve 或启动已安装的 systemd 服务后：

```bash
.venv/bin/oicoach-runtime ui --config "$PWD/.runtime/config.toml" --workflow coach --task training
# 不指定 workflow 时，从 Workflow 页选择或创建：
.venv/bin/oicoach-runtime ui --config "$PWD/.runtime/config.toml"
```

不需要安装额外 UI 包，使用 Python 标准库 curses。需要交互终端、有效 TERM 和 UTF-8 locale；至少 60 列 × 12 行，建议 100 × 30。管道/脚本使用原有 CLI，ui 不接受 --json。

`1–8` / Tab 切换概览、对话、事件、记忆、历史、Workflow、服务、恢复。按 `?` 查看帮助，`r` 刷新，`s` 切换 workflow/task。对话页按 Enter 输入中文或多行内容，Ctrl+D 提交，Esc 取消；`m` 切换安排/反馈/复盘。等待期间界面可操作，完成后显示最终回复；当前没有 token/工具执行的实时流。

记忆页支持新建/读取/编辑/删除，`w` 切共享与当前 task，保存使用现版本防止覆盖。事件页可安排和取消未派发事件；历史页可搜索输入/回复和查询会话原文。服务页可安装、启动、停止和读日志，均需明确按键，不会自动启用登录启动或 linger。恢复页只查看，不自动重播 uncertain。

UI 的所有数据操作由独立 CLI 子进程执行，使用相同配置和 daemon，界面不直接修改数据库。输入正文通过 0600 临时文件传递，正常操作后清理；终端输出过滤控制序列。提交未确认时按 `v` 用原去重标识核对，避免换标识重复提交。`q` / Ctrl+C 关闭界面，不停止独立 daemon、不取消已入库的请求；再次打开可刷新历史找回结果。完整快捷键、首次部署、分页及限制见 [终端 UI 说明](docs/terminal-ui.md)。

## systemd 后台服务

先停止使用同一配置运行的前景 serve，避免单实例锁冲突。venv 和配置应放在长期存在的位置。

```bash
# 先查看真实 unit。
.venv/bin/oicoach-runtime service render --config "$PWD/.runtime/config.toml"
# 先 systemd-analyze --user verify，再安装；此步不启动、不启用。
.venv/bin/oicoach-runtime service install --config "$PWD/.runtime/config.toml"
# 明确启用并立即启动。
.venv/bin/oicoach-runtime service enable --now --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service status --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime status --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime service logs --lines 50 --config "$PWD/.runtime/config.toml" --json
```

默认 unit 为 `oicoach-runtime.service`，安装到 `$XDG_CONFIG_HOME/systemd/user`，未设置时为 `~/.config/systemd/user`。自定义 XDG 目录需与 user manager 的搜索环境一致。各 service 命令可用 `--name training-runtime.service` 选择自定义名称；同一 state/socket 仍只允许一个 daemon。

unit 包含绝对 ExecStart、明确 WorkingDirectory、当前 PATH 快照、UMask=0077、Restart=on-failure、启动频率限制、60 秒停止预算及 KillMode=control-group。不会把 API key 等环境变量复制进 unit；使用 OMP 已有认证或自行管理服务环境。服务不会自动继承之后打开的终端变量。[systemd 执行环境文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

```bash
.venv/bin/oicoach-runtime service stop --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service start --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service restart --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service disable --now --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service uninstall --config "$PWD/.runtime/config.toml"
```

- disable 不带 --now 只取消自动启用，不停止当前服务。
- uninstall 先 disable/stop，再删除属于当前配置的 unit，**保留 DB、workflow 和会话**；拒绝操作其他程序或其他配置的同名 unit。
- 更新已托管 unit 使用 `service install --replace`，不会自动重启活动服务。
- WorkingDirectory 支持空格、引号、百分号和美元符，拒绝反斜线与末尾空白；可用 `--working-directory /absolute/existing/path` 明确选择。
- 需要注销后继续运行时，由你自行决定是否执行 `loginctl enable-linger "$USER"`；Paveway 不更改 linger 或登录/开机策略。

## Workflow 与教练数据

```bash
.venv/bin/oicoach-runtime workflow create my-coach --template coach \
  --workspace /absolute/existing/workspace --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime workflow show my-coach --config "$PWD/.runtime/config.toml"
```

模板作为可编辑的 workflow.toml/event.md 安装到 workflow_root 下，安装后不依赖源码 checkout。coach 支持训练安排、进度、提示/题解使用情况与复盘；example 是最小验证模板。

缺少时区、时间窗口、题目或结果时，coach 会指出缺失信息，或明确标注假设，不能凭空获得学习数据。口胡、题解使用、编码未过和独立通过应分开报告。结构化数据可编辑 [安排](examples/coach-plan.json)、[反馈](examples/coach-checkin.json)、[复盘](examples/coach-review.json) 示例后排程：

```bash
.venv/bin/oicoach-runtime schedule --config "$PWD/.runtime/config.toml" \
  --workflow coach --task training --at now --reason '训练安排' \
  --dedupe-key structured-plan-1 --payload-file examples/coach-plan.json
```

自动跟进默认关闭。需要一次后续检查时，在 payload 明确给 `auto_followup: true` 与用户指定的 `next_check_seconds`，并给 OMP 调用 Runtime CLI 的工具权限。coach 被要求使用当前 run 的 after_turn 排程，检查成功后才声明已安排；后续事件关闭自动跟进，避免无限调用。是否真已排程以 events list/show 为准。关机/休眠期间无法准时执行，恢复后依次处理逾期事件。

调整模板应更新 manifest.version。已有 task 绑定原 workspace/revision；变更后旧 task 会 blocked，不会偷偷新建会话。继续旧 task 需恢复原条件，新版本使用新 task ID。细节见 [workflow 契约](docs/workflow-contract.md)。

## Tijian 题库与 MCP

Tijian 日计划不是 Runtime 唤醒队列。适配器调用 Tijian 自己的 JSON CLI/canonical application，不直接读写其 SQLite。

安装 Tijian 后，修改 Runtime 配置中已有的 [tijian] 表；旧配置没有时再新增，避免重复 TOML 表：

```toml
[tijian]
enabled = true
executable = "/absolute/path/to/tijian"
timeout_seconds = 120
max_output_bytes = 8388608
read_only = true
```

```bash
.venv/bin/oicoach-runtime tijian doctor --config "$PWD/.runtime/config.toml" --with-data --json
.venv/bin/oicoach-runtime workflow connect-tijian coach --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime tijian tools --json
.venv/bin/oicoach-runtime tijian call problem_get --arguments '{"problem":"P1001"}' \
  --config "$PWD/.runtime/config.toml" --json
```

connect-tijian 合并 workspace/.mcp.json，保留其他 servers/settings。已有不同的 tijian server、无效 JSON 或 symlink 会拒绝；disabledServers 也保留。configured 不等于已验证。修改活动 Runtime 配置后需在适当时机重启服务；新 wake 建立新 OMP 进程读取 workspace 配置。

read_only=true 禁止适配器业务写入和文件导出，MCP 只列读操作；不是 OS sandbox。需要状态/计划写入时由你改为 false，coach 仍只应依据明确反馈操作。删除、清空、备份导入导出、文章文件导出要求 confirm=true 或 dry_run=true。

| 类别 | operation（MCP 名称加 tijian_ 前缀） |
| --- | --- |
| 能力/题库 | help, problem_get, problem_list, problem_stats |
| 收录/更新 | problem_add, problem_refetch, problem_vector_compute |
| 状态/评分/历史 | problem_status, problem_edit, problem_remove, history_import |
| 检索 | search |
| 计划/日报 | plan_add, plan_remove, plan_target, plan_show, plan_carry, plan_clear, daily_show |
| 备份 | backup_export, backup_import, backup_verify |
| 题单 | article_import, article_list, article_show, article_export |

search 是本地 quick_search；真正向量重算需要运行中的 Tijian Desktop executor。可读已有译文/向量，不代表 CLI 暴露了翻译触发、GUI 语义检索、提交同步、竞赛或附件/文件夹管理。题目四种状态没有单独“看过题解”字段，不把 explored/胡 当成独立 AC 或题解使用记录。

`tijian mcp` 可独立启动 stdio server，不需要 daemon；`tijian mcp-config --config ABS_CONFIG` 可生成手动合并用的配置。完整 schema、原码 CLI 建置与限制见 [Tijian 文档](docs/tijian.md)。

## 定时、查询与回复

绝对时间必须为带时区的 RFC3339；now 由 daemon 按自己的时钟立即入队。以下时刻是示例，使用时请调整：

```bash
.venv/bin/oicoach-runtime schedule --config "$PWD/.runtime/config.toml" \
  --workflow coach --task training --at '2026-10-18T17:30:00+08:00' \
  --reason '检查进度' --dedupe-key checkpoint-1 --payload-file examples/coach-checkin.json

.venv/bin/oicoach-runtime status --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime events list --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime events show EVENT_ID --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime events wait EVENT_ID --timeout 180 --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime runs list --task training --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime runs show RUN_ID --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime runs output RUN_ID --config "$PWD/.runtime/config.toml"
```

EVENT_ID 从 schedule/ask 取得，RUN_ID 在事件被执行后出现在 events show。runs output 或 ask --wait 普通输出为回复正文，--json 为结构化结果。只保存最终 assistant 文本，不保存思考过程/原始 stderr；最多 16 KiB UTF-8，截断时标记/提示，完整会话仍在私密 sessions 目录。运行中为 pending；旧 run、local-only 命令或不支持查询的 Agent 可为 unavailable，不会拿上一轮回复冒充本轮。

Agent 在当前 run 内安排后续事件时：

```bash
oicoach-runtime schedule --workflow coach --task training \
  --after-turn 1200 --run-id CURRENT_RUN_ID --reason '20 分钟后跟进' \
  --dedupe-key followup-CURRENT_EVENT_ID --config /absolute/config.toml
```

after_turn 先 waiting_anchor，父 run 成功且完全 settled 后起算；父 run 失败则 blocked。CLI 不在 PATH 时使用事件 envelope 的 argv_prefix；Runtime 也注入 OICOACH_CONFIG/OICOACH_RUN_ID。

同 workflow/task 的 dedupe-key 幂等：重发相同 ask/at now 请求返回原事件；同键不同内容/原始排程会 conflict。ask 未指定时每次生成新键。修改/取消使用 events show 返回的 version：

```bash
.venv/bin/oicoach-runtime events reschedule EVENT_ID --at '2026-10-18T18:00:00+08:00' \
  --expected-version 1 --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime events cancel EVENT_ID --expected-version 2 --config "$PWD/.runtime/config.toml"
```

只有未提交事件能修改/取消，不能撤销已执行副作用。list 每页最多 100 条，用 --offset 翻页。payload 编码 JSON 最多 64 KiB，IPC 最多 128 KiB；workflow/task ID 为 1–128 位 ASCII 字母、数字、点、下划线或连字符，首位字母/数字。

## 状态与恢复

| 状态 | 含义 |
| --- | --- |
| waiting_anchor | 等待父 run 完全成功结束 |
| queued / leased / running | 等待、已领取、已记录派发意图 |
| succeeded | 匹配 completed + settled、session 已落盘、进程正常退出 |
| failed | 已知失败，例如 provider error，不自动重播可能已执行的回合 |
| cancelled | 提交前取消 |
| blocked | workflow、会话、路径等条件需修复 |
| uncertain | 可能已执行，但没有确定完成证据 |
| dead_letter | 安全的启动前重试已耗尽 |

succeeded 证明回合完成，不保证建议正确、工具全成功或训练目标达成。应核对回复与真实业务结果。

```bash
.venv/bin/oicoach-runtime recover list --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime recover retry EVENT_ID --confirm-duplicate-risk --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime agent restart --task training --workflow coach --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime agent show RESTART_ID --config "$PWD/.runtime/config.toml" --json
```

只有能证明尚未派发的启动/传输失败才自动限次重试。退避/熔断跨重启保存，等待期间仍可管理队列。intent 后未知结果不重播；先核对会话、题库、文件等副作用，再决定是否承担重复风险 retry。旧 run/audit 保留，attempt 不重置。

agent restart 是异步控制：结束该任务活动进程，以原 session 新建进程检查后回收，不发 prompt；中断事件仍 uncertain。其他 task 的回合不被中断。--reset-circuit 仅在成功 probe 后清除全局冷却/失败计数，不重设 attempt 或清除 uncertain。

Tijian 的 outcome=uncertain 是另一层：模型正常收到工具错误时，Runtime 仍可能 succeeded。需分别核对，不自动重试写入。没有端到端 exactly-once 保证。

## 长期记忆与历史检索

当前任务仍通过独立 OMP session 恢复上下文（L1）。长期笔记（L2）支持用户偏好、事实、摘要和待验证推测；历史（L3）支持搜索既有输入/回复及只读查询原生 OMP 会话文字。无需 embedding 服务或额外 MCP，不修改 OMP 全局记忆设置。

新建 coach workflow 默认开启记忆注入；旧 workflow 不自动覆盖，需在 manifest 顶层加入 `memory_enabled = true` 并合并新版 coach 记忆说明。修改模板时提升 workflow version，并使用新 task，原任务绑定的旧 revision 保留。共享笔记跨本 workflow 的 task 使用，带 --task 的笔记只注入该任务。每轮注入最多 8 KiB，不自动塞入全部历史。

```bash
# 共享偏好；新建默认 expected-version=0。
.venv/bin/oicoach-runtime memory put --workflow coach --key hint-first \
  --title '提示偏好' --kind preference --pinned \
  --content '先给逐步提示，不直接给完整代码' --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime memory show --workflow coach --key hint-first \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime memory list --workflow coach --query '提示' \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime memory recall --workflow coach --task training \
  --config "$PWD/.runtime/config.toml" --json

# 查旧请求/回复，再按实际 run/task 标识读取。
.venv/bin/oicoach-runtime history search --workflow coach --query '线性基' \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime history show RUN_ID --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime history messages --workflow coach --task training --query '消元' \
  --config "$PWD/.runtime/config.toml" --json

# 用 show 的实际 version 更新/删除；此处 1 仅为刚创建后的示例。
.venv/bin/oicoach-runtime memory delete --workflow coach --key hint-first --expected-version 1 \
  --config "$PWD/.runtime/config.toml" --json
```

put 还支持 --task、--content-file、--source-event、--expires-at；正文最多 4096 UTF-8 字节。更新必须带 --expected-version 读到的版本，防止并发覆盖；更新为完整替换，需重新传希望保留的属性。过期笔记不注入，删除会清空笔记内容并保留版本墓碑。历史会话/旧回复/备份不随笔记删除，不能把该命令称为彻底擦除所有记录。

搜索是支持中文的字面子串检索，不是向量/语义检索。messages 返回所有历史分支的文字片段（含 parent_id），不包含图像/隐藏思考，不代表当前分支或某次 run 的完整快照。用户可让有工具权限的 coach 记住/更正/忘记明确事实；只有实际工具成功才算保存。完整限制、升级和 API 见 [记忆说明](docs/memory.md)。

## 完整配置参考

优先使用 init 生成的配置。以下绝对路径须替换；--config 优先，其次 OICOACH_CONFIG，最后 XDG 默认 `~/.config/oicoach-runtime/config.toml`。默认状态为 `~/.local/state/oicoach-runtime`。

```toml
schema_version = 1
state_dir = "/absolute/private/state"
workflow_root = "/absolute/trusted/workflows"

[server]
socket_path = "" # 空值使用 state_dir/runtime.sock
poll_interval_seconds = 1
max_concurrent_runs = 1

[agent]
adapter = "omp-rpc"
executable = "omp" # 可为绝对路径，不接受 shell 命令字符串
profile = ""
model = ""
tools_enabled = true
extensions_enabled = true
skills_enabled = true
rules_enabled = true
startup_timeout_seconds = 30
ack_timeout_seconds = 30
turn_timeout_seconds = 3600
settle_timeout_seconds = 900
graceful_stop_seconds = 15

[restart]
max_failures_per_window = 5
window_seconds = 300
backoff_initial_seconds = 2
backoff_max_seconds = 120
cooldown_seconds = 60
jitter_ratio = 0.2

[queue]
default_max_attempts = 3
expired_event_policy = "keep"
uncertain_event_policy = "manual_review"

[tijian]
enabled = false
executable = "tijian"
timeout_seconds = 120
max_output_bytes = 8388608
read_only = false
```

配置、API 和 workflow manifest 保持 v1；DB 为 v2，由 init/serve 事务式迁移已有 v1，doctor 只读。拒绝未知字段/版本/策略。state/sessions 要求当前用户拥有且为 0700，配置/DB/socket 为 0600；不要自动 chmod 共享目录绕过检查。Unix socket 路径最多 107 字节。控制 socket 仅接受同 UID，不构成对同 UID 不可信插件的隔离。

工具、MCP、扩展和 Skill 权限由 OMP/用户配置决定。关掉工具时 coach 可根据输入回复，但不能靠文字承诺修改题库或安排唤醒。

## 数据、备份与升级

- state_dir/runtime.sqlite3：事件、run、audit、会话绑定、长期笔记、退避状态和有界最终回复。
- state_dir/sessions：按 scope 隔离的 OMP 持久会话。
- workflow_root：安装后独立、可编辑的 manifest/模板。
- workspace：用户题目/代码及项目 MCP 配置。
- Tijian 数据由其自身管理，Runtime 备份不包含完整 Tijian 题库。

SQLite 使用 WAL，不能只复制主 DB 而遗漏 WAL；使用 SQLite backup API。一致性备份脚本见 [操作手册](docs/operations.md#備份)，Tijian 使用自己的 backup 接口。回复与会话可能含个人数据，应按私密文件管理。

升级前备份，在适当时机停止服务，然后安装新 wheel：

```bash
.venv/bin/oicoach-runtime service stop --config "$PWD/.runtime/config.toml"
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/oicoach-runtime service install --replace --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service start --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime doctor --config "$PWD/.runtime/config.toml" --json
```

停止活动回合可能产生 uncertain。已有 workflow 不随 wheel 升级覆盖，应自行比较并更新模板。0.7.0 启动服务会把数据库 v1 迁移到 v2；降级需要恢复升级前备份，旧程序不能读取 v2。迁移失败或未知 schema 不会清空 DB；不要删锁、删数据库或直接改状态假装恢复。

## 测试与验证

没有模型凭证也可跑单元/fixture/MCP SDK 主流程，真实模型/systemd/Tijian 源码测试为 opt-in：

```bash
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v

# 真实 transport，不调用模型。
.venv/bin/oicoach-runtime verify-omp --config "$PWD/.runtime/config.toml" --json
# 三个小型模型回合，同 task 接续与跨 task 隔离。
.venv/bin/oicoach-runtime verify-omp --config "$PWD/.runtime/config.toml" --with-model --json
```

完整本机验证（先按 [Tijian 文档](docs/tijian.md) 建置原码 CLI/seeder）：

```bash
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_SYSTEMD=1 \
OICOACH_TEST_TIJIAN_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_TIJIAN_SOURCE_CLI=/tmp/paveway-tijian-build/debug/tijian-source-cli \
OICOACH_TEST_TIJIAN_SOURCE_SEED=/tmp/paveway-tijian-build/debug/tijian-source-seed \
  .venv/bin/python -m unittest discover -s tests -v
```

systemd 测试创建独立 UUID unit，执行启停/启用/禁用、SIGKILL 和卸载后清理，不启用默认服务或 linger。模型测试使用临时 workspace/state/profile。包含 CLI 退出后的执行、20 个事件串行、交易/dispatch 边界、崩溃清理、熔断、原码题库读写，以及完整 CLI → daemon → OMP → MCP → Tijian → 成功落盘；coach 另测无 Tijian 时的真实安排回复。最新结果见 [进度](docs/progress.md) 和 [运行报告](docs/runtime-testing.md)，不把本机结果冒充远端 CI。

0.7.0 安装包本机全量回归 **128 项：127 通过、1 跳过，无失败（391.692 秒）**；跳过的是未另装的正式 Tijian 发行版检查，原码 CLI 已实测。包括真实模型保存笔记、重启后由关闭工具的新 task 读取、coach plan/checkin/review、Tijian 全链路及 systemd 恢复；另通过强化后的迁移保留与笔记写入后崩溃用例。完整 Desktop/OJ 环境的限制见下文。

0.8.0 的无 opt-in 主回归 **138 项：125 通过、13 跳过，无失败（127.383 秒）**。随后新增真实 UI 模型验收并单独通过 **1 项（15.748 秒）**；补强 UTF-8 locale 边界后重跑 UI 专项 **11 项全部通过（5.662 秒）**，含实际 PTY 中文输入、缩放、终端恢复和退出后任务继续。未把 0.7.0 的全部实机结果称为本次重跑；测试命令与时间点见运行报告。

只想试队列且不调用真实模型，可从 checkout 生成 fake demo，目的配置须不存在：

```bash
.venv/bin/python examples/prepare_smoke.py --config "$PWD/.runtime-smoke/config.toml"
.venv/bin/oicoach-runtime serve --config "$PWD/.runtime-smoke/config.toml"
# 另一终端：
.venv/bin/oicoach-runtime schedule --config "$PWD/.runtime-smoke/config.toml" \
  --workflow example --task demo --at now --reason smoke --dedupe-key demo-1
```

该 demo 的回复来自 fixture，不是模型或教练结果。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| doctor daemon 离线 WARN | 启动 serve 或已安装的服务；安装 CLI 不会启动 daemon |
| 没有 workflow | workflow create，或安装符合 v1 manifest 的外部目录 |
| 服务启动失败/锁冲突 | 停止同 state/socket 的前景进程，查 service status/logs，不删锁 |
| systemctl --user 不可用 | 先前景 serve，配置好主机 user manager 后再部署 |
| 启动频率超限 | 修复问题后 systemctl --user reset-failed oicoach-runtime.service，再 service start |
| 有 OMP 但模型失败 | 先完成 OMP 自身模型/认证配置，再 verify-omp --with-model |
| ask 等待超时 | 查事件；它仍可能执行，不要无去重键盲目重发 |
| 回复 unavailable | 查 run/Agent 版本；旧 run 或 local-only 没有本轮回复 |
| workflow 改后 blocked | 恢复原 workspace/version，或新版本使用新 task |
| Tijian 失败 | tijian doctor --with-data，核对 executable、数据/profile、read_only 和 Desktop 依赖 |
| succeeded 但工具失败 | succeeded 是回合完成；核对回复及真实外部结果 |

## 范围与文档

已交付单机、单前景 run 框架、初版外部教练 workflow、Tijian 公开 CLI 适配和 L2 笔记/L3 字面历史检索。尚不含跨设备同步、向量语义检索、全历史自动蒸馏、网页/消息平台前端、自动荐题质量保证或多 Agent 并行。

完整 Tijian Desktop UI、运行中 Desktop projection routing、真实外部 OJ 抓取、需 Desktop executor 的向量重算，以及 CLI 未公开的 GUI-only 功能，仍受上游与环境限制；接口存在不等于这些实机验收已完成。未承诺 Windows/macOS Runtime；其他版本请运行验证。

- [架构与里程碑](docs/architecture.md)
- [状态机](docs/state-machine.md)
- [CLI/API](docs/runtime-api.md)
- [终端 UI](docs/terminal-ui.md)
- [外部 workflow](docs/workflow-contract.md)
- [初版 coach 策略与输入](docs/coach.md)
- [长期记忆与历史检索](docs/memory.md)
- [Tijian 功能与 MCP](docs/tijian.md)
- [部署与服务](docs/deployment.md)
- [操作、备份与恢复](docs/operations.md)
- [运行测试](docs/runtime-testing.md)
- [开发与验证记录](docs/progress.md)

许可：[GNU GPL v3](LICENSE)。
