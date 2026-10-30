# Paveway

**Paveway is An adVanced Efficient Workflow for informAtics olYmpiads.**

Paveway 是运行在现有 OMP Coding Agent 上的 OI GPT框架：持久化用户请求与定时检查，按顺序唤醒正确的会话，连接 Tijian 题库与训练数据，保存可查询的执行结果与恢复记录。

当前版本 **0.20.1**：运行框架 M0–M4、Tijian 原生 stdio MCP 接入、首次启动交互式 Workflow 创建、可编辑的 coach workflow、systemd user 部署、回复读取、L2 长期笔记、L3 历史检索、支持自定义系统提示词/User Skill 的终端 UI，以及独立 Git 多设备配置与历史同步已实现。

用户于 2026-10-30 明确取消固定 10k 限制：对话窗口与压缩改由 OMP 原生管理；Paveway 不再加载固定预算 Guard，也不再要求它的就绪帧。记忆快照仅受 64 KiB IPC 字节边界约束，超出的笔记仍可检索。原生入口重构分阶段进行，旧 TUI/RPC 保留；历史调查见 [M0 兼容性记录](docs/verification/omp-m0-compatibility.md)。

原生 OMP 入口及专属管理菜单见 [原生 UI 使用说明](docs/native-ui.md)：`./paveway.sh --native`，`/paveway` 管理配置、Skills、Persona、记忆、历史、Git 与登录；`/wakeup` 排程，`/inbox` 查看持久消息。`--legacy-ui` 可回到原管理界面。

## 能做什么

| 能力 | 当前行为 |
| --- | --- |
| 训练安排与复盘 | 独立 coach 模板支持 plan、checkin、review，根据用户输入安排训练、处理反馈和复盘 |
| 即时请求 | ask 排队执行，可 --wait 等待并显示最终回复 |
| 终端界面 | ui 全屏交互，中文输入、GPT对话、事件、记忆、历史、workflow 和服务操作，后台调用已有 CLI |
| 定时跟进 | 绝对时刻或当前回合完全结束后的延迟唤醒；发送 CLI 退出后仍由独立 daemon 执行 |
| 会话延续 | 按 workflow + task 隔离持久会话，恢复明确绑定的 session |
| Tijian | 题库读取、题面补齐、状态与历史、异步 AI Job，原生 stdio MCP 接入及内置 paveway-tijian Skill 业务编排 |
| 故障恢复 | 启动前有限重试、退避、熔断、人工 Agent 重启；可能已执行的 prompt 不自动重播 |
| 后台服务 | systemd user unit 的生成、校验、安装、启停、启用/禁用、查询、日志和卸载 |
| 可观测性 | 配置/数据库/OMP/服务诊断，事件、run、最终回复与恢复记录查询 |

GPT提示放在外部 workflow，Runtime 不写死题目选择、训练时长或教学策略。安排仍需结合用户实际情况判断。异步回复保存在本地，目前没有网页、聊天平台推送或桌面通知。

## 环境要求

- Linux，支持 pidfd、parent-death signal 和 child subreaper。本机验证环境为 Ubuntu 24.04、Python 3.12.3、systemd 255。
- Python **3.11+**、venv、pip。调度核心只用标准库；终端 UI 使用随包安装的 prompt_toolkit。Tijian 原生 MCP 服务端（`tijian-mcp`）由 Rust 二进制独立运行。
- 可执行且已完成模型认证的 **OMP**，本机验证版本 **18.10.4**。安装与模型配置见 [OMP 项目](https://github.com/can1357/oh-my-pi)；先确认 OMP 自身能调用模型。
- 后台部署需要 `systemctl --user`、`systemd-analyze`、`journalctl`；前景 serve 不需要 systemd。
- 用户数据 Git 保存/同步需要可执行的 Git；daemon 默认创建本地 Git 归档；Git 不可用时会报告同步错误，不影响 Runtime/模型工作。
- Tijian 可选，需要提供原生 stdio MCP Server（`tijian-mcp`）可执行文件。已知基线为 `90e2c75`。

没有 Tijian 也可使用基于用户输入的 coach。Paveway 不自动登录模型/OJ，不修改 OMP 全局 profile、认证或已有聊天。

## 快速开始

已有 Python 3.11+、OMP 和模型认证时，可直接在交互终端执行：

```bash
./paveway.sh
# 也可指定已有 workflow/task，或在首次启动时预先指定要创建的 workflow 名称：
./paveway.sh --workflow oi-coach --task training
./paveway.sh --config /absolute/private/config.toml
```

脚本可从任意工作目录运行。首次自动建立 `.venv`、安装依赖（需要网络）并初始化缺少的私密配置。若当前尚未创建任何 Workflow，脚本会在终端提示输入第一个 Workflow 的名称（ID），用户也可通过 `./paveway.sh --workflow NAME` 非交互指定。Workflow 名称由用户自行决定，与使用的语言模型无关；已有 Workflow 不需要每次重新创建，再次启动时直接进入现有选择流程。默认读取项目 `.runtime/config.toml`；已有 daemon 直接复用，离线时优先启动同配置的已安装 systemd 服务，否则启动独立后台 `serve`，启动日志在配置的 state_dir/startup.log。退出 UI 不停止后台任务。脚本不安装或替换 systemd 服务；登录自启动见下文部署说明。已有设置、Skill 和历史保留。

以下是手动安装和启动步骤：

在源码仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
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

0.9.0 按本机 OMP 18.10.4 的交互安排重做：上方保留对话记录，底部始终有输入框，直接打字，**Enter 发送、Ctrl+J / Alt+Enter 换行**。`q`、数字等均为普通输入，不再用于全局切页或退出。

输入 `/` 查看命令，Tab 补全，方向键选择，Enter 确认，Esc 返回。例如 `/workflow` 选环境，`/task training` 接续任务，`/new` 新任务，`/mode review` 切复盘，`/events` 查队列，`/memory` 管笔记，`/history` 查历史，`/service` 管服务，`/help` 看完整说明。选单和表单在输入区中展开，后台回覆仍能更新；保存失败时笔记草稿保留。保存确认期间输入的下一条命令会排队并显示状态，确认后执行，避免连续操作时被清空。

可用鼠标滚轮、点击/拖动右侧滚动条或 PageUp/PageDown 浏览对话；Ctrl+End 回到最新，Ctrl+Home 到开头。浏览历史时新回复不会强制拉回底部，输入草稿保持；需要终端原生选字时通常可按住 Shift 拖选。

需要交互终端、有效 TERM 和 UTF-8 locale，建议 80×24 或更大；编辑器负责中文折行、多行贴上、滚动和缩放。prompt_toolkit 由 pip 自动安装，测试的屏幕解析器 pyte 属于 `.[test]` 可选依赖。当前显示保存的最终回复，没有 token/tool 实时流。

UI 顶栏显示当前系统时间，消息显示时间戳；日期直接使用系统时区（含对应日期的夏令时），数据库仍保存 UTC。输入框上方显示当前 workflow/task 最近的待执行任务、触发时间与实时倒计时，`/timers` 可查看最多 100 项及详情。after-turn 任务在本回合结束前显示“待起算”；倒计时到零显示“等待派发”，由 daemon 实际状态确认是否执行。重新打开 UI 会恢复队列状态。

界面与 Agent 自称统一为 GPT，默认/自定义系统提示都会附加命名约定；保留 event.md、个人 Skill 和历史原文。工程 Skill 与通用模板已在源代码远端，自定义 Skill/对话则进入独立用户数据 Git，本机目前未配置该数据仓库的 remote。完整检查结果见 [Git 资料稽核](docs/privacy-audit.md)。

UI 启动时会同步终端代理并检查代理 TCP 端口，也可用 `/proxy` 重查。HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY（含小写）以及 OMP 的 PI_PROXY/PI_PROXY_PROVIDER 保存在本机私密 `config.service.env`，不进入 Git。service install/start/restart 与 serve 启动会同步检查；无终端代理变量时保留已保存配置，变量显式设为空可清除此项。每次新建 OMP 子程序都会读取最新代理，无需为后续同步重启 daemon；已运行回合保持原环境。端口可达不代表模型认证或生成请求成功。

所有数据操作仍调用独立 CLI 子进程，UI 不直接写数据库，也不自动启动/停止 daemon。正文用私密临时文件传递；提交未确认时 `/retry-send` 用原标识幂等核对。Ctrl+C 清空草稿；空输入连续两次 Ctrl+C、空输入 Ctrl+D 或 `/quit` 退出，不取消已入库事件。完整操作见 [终端 UI 说明](docs/terminal-ui.md)。

配置 Tijian 的 VJudge 登录：重开 UI 后输入 `/vjudge`（或 `/tijian`），选择「用户名和密码登录」或「从浏览器导入 Cookie」；也可直接输入 `/vjudge login`、`/vjudge cookie`。密码/Cookie 隐藏输入，Enter 提交，Esc 取消。登录成功后只在本机 Tijian 的原生会话文件保存 Cookie，不保存密码、不写入 GPT 对话或用户数据 Git；所有 workflow 共用。`/vjudge test`（或 `/tijian test`）实际验证登录，`/vjudge status` 仅查看配置；`/vjudge clear` 确认后清除 Cookie，保留已有 Tijian 账号绑定。若网站要求浏览器验证，请在浏览器登录后导入请求 Cookie。此功能不修改 Tijian 上游，也不绕过网站验证，详见 [Tijian 登录说明](docs/tijian.md#6-vjudge-登录与会话管理)。

选择模型和思考等级（0.16.0）：

```text
/select-model                       读取本机 OMP 模型清单，输入筛选、↑↓ Enter 选择
/select-model provider/model         直接指定模型；default 恢复继承
/select-thinking-hardness            选择思考等级
/select-thinking-hardness high       直接指定；default 恢复继承
```

设置按 workflow 保存，所有 task 从下一次派发起生效，正在执行的回合保留旧设置；不需要为每次选择重启服务。切换到明确模型时思考等级设为 `auto`，再用第二个命令调整。菜单按模型元数据列出等级；直接输入的等级为 off/minimal/low/medium/high/xhigh/max/auto，具体兼容性由 OMP 校验。设置随现有独立 Git 用户数据仓库同步，其他设备仍需自行配置对应 provider 和凭证。升级程序后先重启空闲 daemon 并重开 UI。

在 UI 中自定义系统提示词和三类用户 Skill（0.12.0）：

```text
/prompt           查看 / 编辑 / 恢复默认系统提示词
/prompt edit      选择 append（追加）或 replace（替换），输入正文后保存
/skills           选择 OI Introduce / user information / personal skill
/skills add       选择分类，再输入名称、用途描述、Markdown 正文
/skills category training-review  移动已有 Skill 的分类
```

一次性提醒可以直接说“10 分钟后提醒我检查进度”或“定时任务测试：请你设计一个 10min 的定时任务”，不需要填写 JSON。明确说“仅设计、不执行”时只给方案。默认不会主动循环跟进，排程成功以真实命令结果为准。

长任务等待期间显示耗时；重新进入 UI 会接着追踪运行中的请求，完成后自动显示最终回复。空闲时每 15 秒核对当前 task 最近 5 个 run，补上其他窗口或后台完成的结果。此为状态轮询，暂不显示逐 token/工具实时流。

Enter 下一项/保存，Ctrl+J 换行；改动保存在当前 workflow，其他 workflow 不受影响。下次派发请求时由 OMP 原生系统提示词/Skill 加载器读取，不需要重启 daemon，现有 task 也可继续。旧历史和已开始的回合不变；保存冲突会保留草稿。三类已启用 Skill 共同提供名称、分类和用途索引，正文通过 OMP 原生 `skill://名称` 按需读取，不预先加入系统上下文；旧 Skill 默认归入 personal skill。Skill 禁用时不发现；读取正文需要工具权限。完整范围、生效规则、上限与 CLI 见 [自定义说明](docs/customization.md)。从旧版升级时需先在空闲状态重启 daemon 一次，让它加载新版程序。

多设备 Git 保存与自动同步（0.11.1）：

```text
/sync remote      手动设置 remote；清空后仅本地保存
/sync setup       高级设置：现有数据目录、分支和同步间隔
/sync now         请求后台立即同步，完成后显示结果
/sync status      查看最近结果
/sync conflicts   选择并发修改版本 / 查看新设备待绑定配置
/sync history     查询所有设备已拉取的历史
/sync pause       暂停自动同步
```

保存自定义 prompt、User Skill，以及已结束对话的输入/回复和过滤后的原生文字历史。daemon 首次启动自动在 `state_dir/user-data-git` 建立独立仓库，同一 Runtime 的所有 workflow/task 和GPT对话共用它，不必先设置 remote。默认每 60 秒本地 commit，有明确配置的 remote 才自动 pull/push；UI 退出后继续运行。另一设备使用自己的数据目录与认证，配置同一 remote；已有同 ID 的未修改 workflow 自动应用，新 ID 用 `/sync bind ID` 指定本机 workspace。

机器配置/凭证、活跃 SQLite、执行队列和 OMP 可执行会话不导入其他设备；并发配置编辑保留两边版本，不强推覆盖。remote 留空时仅本地 Git 保存，不复用源码 origin。完整范围、离线/冲突行为、CLI 与限制见 [多设备 Git 同步](docs/git-sync.md)。

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

unit 包含绝对 ExecStart、明确 WorkingDirectory、当前 PATH 快照、UMask=0077、Restart=on-failure、启动频率限制、60 秒停止预算及 KillMode=control-group。服务不会自动继承终端的 API key；可用下面的显式命令，把指定变量保存到配置旁的私密 `config.service.env`（0600），供下次登入/重启读取。金钥不进入 unit、命令行或日志；默认安装不复制凭证。[systemd 执行环境文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

```bash
.venv/bin/oicoach-runtime service stop --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service start --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service restart --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service disable --now --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service uninstall --config "$PWD/.runtime/config.toml"
```

如果 OMP 依赖当前终端的 `OPENAI_API_KEY`，确认该变量已正确设置后执行；参数只传变量名，不传金钥：

```bash
.venv/bin/oicoach-runtime service install --replace --capture-env OPENAI_API_KEY --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service restart --config "$PWD/.runtime/config.toml"
```

其他实际需要的模型变量可重复 `--capture-env NAME`。变量缺失会明确报错，重新捕获会更新指定变量并保留其他已捕获值；普通重新安装不会清空凭证。更换金钥后重新执行以上命令。卸载服务保留此私密文件，彻底移除凭证时自行删除；不要提交或分享 `*.service.env`。服务重启前应等待活动回合结束。

- disable 不带 --now 只取消自动启用，不停止当前服务。
- uninstall 先 disable/stop，再删除属于当前配置的 unit，**保留 DB、workflow 和会话**；拒绝操作其他程序或其他配置的同名 unit。
- 更新已托管 unit 使用 `service install --replace`，不会自动重启活动服务。
- WorkingDirectory 支持空格、引号、百分号和美元符，拒绝反斜线与末尾空白；可用 `--working-directory /absolute/existing/path` 明确选择。
- 需要注销后继续运行时，由你自行决定是否执行 `loginctl enable-linger "$USER"`；Paveway 不更改 linger 或登录/开机策略。

## Workflow 与GPT数据

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

自动跟进默认关闭。用户明确请求一次定时检查（例如“定时任务测试：请你设计一个 10min 的定时任务”）即可授权本次排程，也可在 payload 明确给 `auto_followup: true` 与用户指定的 `next_check_seconds`；仅讨论设计或明确要求不执行时不排程。OMP 仍需有调用 Runtime CLI 的工具权限。coach 被要求使用当前 run 的 after_turn 排程，检查成功后才声明已安排；后续事件关闭自动跟进，避免无限调用。是否真已排程以 events list/show 为准。关机/休眠期间无法准时执行，恢复后依次处理逾期事件。

调整模板应更新 manifest.version。已有 task 绑定原 workspace/revision；变更后旧 task 会 blocked，不会偷偷新建会话。继续旧 task 需恢复原条件，新版本使用新 task ID。细节见 [workflow 契约](docs/workflow-contract.md)。

## Tijian 题库与原生 MCP

Paveway 直接接入 Tijian 原生 stdio MCP Server（`tijian-mcp`）。旧 Python 桥接器（包括 `tijian_catalog`、`tijian_mcp`、`tijian.py`、旧 `[tijian]` 运行配置及 `tijian tools/call/doctor/setup/mcp` 等 CLI 命令）已被完整删除；Paveway 不再在 Python 中复刻工具 Schema。

题面使用原则：已收录题目直接读取本地持久化数据；尚未收录或缺失题面时，依权限按需确保或抓取，普通阅读不重复刷新。具体工具能力与 Schema 以运行中 `tijian-mcp` 实际暴露的 `tools/list` 为准。

通过 Paveway 命令配置 workflow workspace 的 `.mcp.json`：

```bash
# 默认推荐只读模式（包含 --read-only 参数）
.venv/bin/oicoach-runtime workflow connect-tijian coach \
  --executable /absolute/path/to/tijian-mcp \
  --config "$PWD/.runtime/config.toml"

# 显式读写模式
.venv/bin/oicoach-runtime workflow connect-tijian coach \
  --executable /absolute/path/to/tijian-mcp \
  --read-write \
  --config "$PWD/.runtime/config.toml"
```

`connect-tijian` 安全合并 `workspace/.mcp.json`，保留已有其他 servers 及 `disabledServers`。相同配置重复执行保持幂等，存在不同定义时报 `ConflictError` 且原文件不变；拒绝 symlink 并保持 `0600` 权限。配置成功仅代表 `.mcp.json` 已正确写入，MCP 进程由 OMP 派发时按需启动。

VJudge 登录功能已独立为 `vjudge auth` CLI（支持 `status`、`test`、`login`、`cookie`、`clear`），TUI 中 `/vjudge` 与 `/tijian` 均可直接使用。详细架构、构建与使用说明参见 [Tijian 原生 MCP 接入](docs/tijian.md)。
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

新建 coach workflow 默认开启记忆注入；旧 workflow 不自动覆盖，需在 manifest 顶层加入 `memory_enabled = true` 并合并新版 coach 记忆说明。修改模板时提升 workflow version，并使用新 task，原任务绑定的旧 revision 保留。共享笔记跨本 workflow 的 task 使用，带 --task 的笔记只注入该任务。记忆快照受 **64 KiB** 传输字节边界约束，最近对话不再由 Paveway 施加固定 token 上限，使用 OMP 原生模型窗口与压缩。记忆快照注入系统 prompt；事件 memory 字段仅保留截断状态和位置。完整会话、记忆总库和 Git 历史保留，遗漏内容按需检索。

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

搜索是支持中文的字面子串检索，不是向量/语义检索。messages 返回所有历史分支的文字片段（含 parent_id），不包含图像/隐藏思考，不代表当前分支或某次 run 的完整快照。Agent 应在关键进展及每轮结束前及时维护有证据的记忆，用户也可要求有工具权限的 GPT 记住/更正/忘记明确事实；只有实际工具成功才算保存。完整限制、升级和 API 见 [记忆说明](docs/memory.md)。

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
thinking = ""
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
.venv/bin/python -m pip install -e .
.venv/bin/oicoach-runtime service install --replace --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime service start --config "$PWD/.runtime/config.toml"
.venv/bin/oicoach-runtime doctor --config "$PWD/.runtime/config.toml" --json
```

停止活动回合可能产生 uncertain。已有 workflow 不随 wheel 升级覆盖，应自行比较并更新模板。0.7.0 启动服务会把数据库 v1 迁移到 v2；降级需要恢复升级前备份，旧程序不能读取 v2。迁移失败或未知 schema 不会清空 DB；不要删锁、删数据库或直接改状态假装恢复。

## 测试与验证

没有模型凭证也可跑单元/fixture/MCP SDK 主流程，真实模型/systemd/Tijian 源码测试为 opt-in：

```bash
.venv/bin/python -m pip install '.[test]'
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

0.9.0 重做对话式 UI 后，最终安装包主回归 **147 项：133 通过、14 跳过，无失败（119.460 秒）**，其中 UI 专项 18 项通过，覆盖实际终端连续增改笔记、排程、取消及保存确认竞态。另跑真实 UI → CLI → daemon → OMP 模型验收 **1 项通过（11.367 秒）**。本轮未重跑上述全部 Tijian/systemd 实机 opt-in，详见运行报告。

2026-10-20 后续修正 systemd 凭证持久载入及失败原因显示，最终主回归 **152 项：138 通过、14 跳过，无失败（140.787 秒）**，另两项真实 systemd 验收通过。当前后台服务已实际通过模型请求并保存 `CONNECTION_OK`；对话和历史可显示 HTTP 401 等安全原因，详见运行报告。

0.10.0 新增 UI 自定义系统提示词与用户 Skill 后，最终主回归 **162 项：146 通过、16 跳过、无失败（152.133 秒）**，含 UI 21 项。另两项真实 OMP 自定义验收通过（65.902 秒），确认系统提示词进入真正的 system 层、模型读取 Skill 正文，以及同会话下一轮替换生效。具体边界见运行报告。

0.11.0 新增独立 Git 多设备配置/历史同步后，最终安装包主回归 **174 项：158 通过、16 跳过、无失败（178.887 秒）**。含两个 daemon 自动同步、实际 bare remote 往返/并发冲突、离线提交与队列隔离，以及 UI 22 项中的真实终端配置/后台 push/历史查询/暂停。本机已实际运行本地 Git 保存；互联网 remote 尚未指定，完整范围见运行报告。

0.11.1 默认本地 Git 与 UI remote 配置补充后，完整回归 **178 项：162 通过、16 跳过、无失败（195.150 秒）**，覆盖未配置远端的自动归档、多GPT共用仓库、remote 移除保留数据和实际终端操作。本机服务已升级并核对归档成功，仍为纯本地模式。

0.14.0 增加系统时区时间戳与实时倒计时：基础定向 **20 项通过（26.695 秒）**，修复后台刷新与表单确认竞争后，完整 UI 加 after-turn 集成 **32 项通过（54.223 秒）**，两组覆盖 49 个不同测试。实际终端验证倒计时、到时执行一次、草稿保留及取消操作；本机服务已升级，未创建额外用户定时任务。完整范围见 [运行报告](docs/runtime-testing.md)。

0.15.0 将自定义 Skill 改为原生按需读取。自定义设置/UI **38 项通过（58.498 秒）**，实际 OMP 无模型验收 **2 项通过（7.117 秒）**，确认三类索引可发现、正文不预先注入、停用工具/Skill 不退回全文注入。当前模型服务返回回合错误，模型实际读取文件的端到端验收未通过；没有将原生发现验证称作模型验收成功，见 [运行报告](docs/runtime-testing.md)。

0.16.0 新增 `/select-model` 与 `/select-thinking-hardness`。配置/自定义设置/Git/完整 UI 与回合切换定向回归 **86 项通过（104.652 秒）**，实际 OMP 模型与 high/low 参数验收 **1 项通过（5.379 秒）**。包括真实 PTY 选择、下一回合生效、保持已有会话及双设备同步；本机已升级，未替用户选择模型或发起训练回合。验收不调用模型服务，详见 [运行报告](docs/runtime-testing.md)。

0.17.0 增加启动代理同步/检查及后续 OMP 代理热加载。定向回归 **77 项通过（81.752 秒）**，真实 openai-codex/gpt-6-luna、low 调用返回 PROXY_OK。本机服务已取得代理，先前卡住的回合保留为 uncertain，未自动重播；可用 /new 后重新发送，或人工核对恢复原事件。详见 [运行报告](docs/runtime-testing.md)。

0.18.0 统一 GPT 称谓及系统命名约定。回归 **56 通过、1 条件略过（78.777 秒）**，最终定向 **12 项通过（18.814 秒）**，真实 Luna 自述验证通过；event.md、个人配置与历史原文保留。工程 Skill 等文件确实已在源码远端，个人 Skill/对话目前在没有 remote 的独立本机 Git，见 [Git 稽核](docs/privacy-audit.md)。

0.12.0 三类 Skill 同时加载后，完整回归 **180 项：164 通过、16 跳过、无失败（214.227 秒）**。实际 OMP 原生加载另验收通过，确认 append/replace 都包含三类完整正文；本机服务已升级。

只想试队列且不调用真实模型，可从 checkout 生成 fake demo，目的配置须不存在：

```bash
.venv/bin/python examples/prepare_smoke.py --config "$PWD/.runtime-smoke/config.toml"
.venv/bin/oicoach-runtime serve --config "$PWD/.runtime-smoke/config.toml"
# 另一终端：
.venv/bin/oicoach-runtime schedule --config "$PWD/.runtime-smoke/config.toml" \
  --workflow example --task demo --at now --reason smoke --dedupe-key demo-1
```

该 demo 的回复来自 fixture，不是模型或GPT结果。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| doctor daemon 离线 WARN | 启动 serve 或已安装的服务；安装 CLI 不会启动 daemon |
| 没有 workflow | workflow create，或安装符合 v1 manifest 的外部目录 |
| 服务启动失败/锁冲突 | 停止同 state/socket 的前景进程，查 service status/logs，不删锁 |
| systemctl --user 不可用 | 先前景 serve，配置好主机 user manager 后再部署 |
| 启动频率超限 | 修复问题后 systemctl --user reset-failed oicoach-runtime.service，再 service start |
| 有 OMP 但模型失败 | 对话/历史显示错误码与处理提示；HTTP 401 检查 OMP 认证和服务的 `--capture-env`，更新后重启 |
| ask 等待超时 | 查事件；它仍可能执行，不要无去重键盲目重发 |
| 回复 unavailable | 查 run/Agent 版本；旧 run 或 local-only 没有本轮回复 |
| workflow 改后 blocked | 恢复原 workspace/version，或新版本使用新 task |
| Tijian 失败 | tijian doctor --with-data，核对 executable、数据/profile、read_only 和 Desktop 依赖 |
| succeeded 但工具失败 | succeeded 是回合完成；核对回复及真实外部结果 |

## 范围与文档

已交付单机、单前景 run 框架、初版外部GPT workflow、Tijian 公开 CLI 适配和 L2 笔记/L3 字面历史检索。尚不含跨设备同步、向量语义检索、全历史自动蒸馏、网页/消息平台前端、自动荐题质量保证或多 Agent 并行。

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
