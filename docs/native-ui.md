# OMP 原生界面

Paveway 直接启动用户安装的官方 OMP。OMP 负责聊天、模型／认证、工具、MCP、滚动和原生 Session；Paveway Extension 负责 Runtime 握手、管理命令和 Paveway 状态呈现。后台使用同一个绝对 OMP 路径启动 `--mode rpc`；前后台的物理 Session 独立，不同时写同一个文件。

## 品牌、输入与状态

- Aurora × Pathline 字标现在是官方 `ui.setWidget` 提供的静态启动组件，显示 Paveway／OMP 版本和 Workflow；首次提交或开始运行后移除。窄终端缩短字标与提示。没有启动首帧替换、入场光流或驻留到滚动历史的动画。
- Paveway Chrome 通过 `registerComposerShape()` 注册，受管启动选择 `paveway.chrome`。实际输入、光标、IME、粘贴、补全、草稿、历史和键位由 OMP 自己实现。不再覆写普通 User／Assistant 消息，因此没有旧的紫色用户行或逐消息 GPT 标签。
- Windline 是输入框下方一个 `ui.setWidget`：显示 Profile／task，空间充足时显示不同的 Workflow，优先给前台运行、Wakeup 和未读 Inbox 留空间；没有待办和零未读时不绘制这些项。不替换 OMP 的模型、Thinking、费用或 Context／Compaction 状态栏；旧 TOML 的 `[statusline]` 字段仍保留读取和写回。

这些是显示差异；Workflow、Prompt、Memory、Session 持久化及 Runtime 所有权不依赖视觉接口。

## OMP 选择、诊断与升级

启动优先级为 `./paveway.sh --omp PATH`（或显式环境变量 `PAVEWAY_OMP_PATH`）→ TOML `agent.executable` → `PATH` 中的 `omp`。无效的显式路径不会回退。独立 Runtime 使用已解析的绝对路径；路径变更或可执行文件被原地替换时先走安全交接，有活动任务时不替换。systemd 不继承终端的临时选择，请设 `agent.executable` 的绝对路径，并在空闲时使用 `--restart-runtime` 重新加载服务。

```bash
omp update
./paveway.sh
oicoach-runtime doctor --config /absolute/config.toml --json
oicoach-runtime status --config /absolute/config.toml --json
oicoach-runtime verify-omp --config /absolute/config.toml --json
```

升级也可使用 [官方发行页](https://github.com/can1357/oh-my-pi/releases)。不用构建或应用补丁，不会改动 OMP 安装、会话或用户凭据。`doctor` 报告解析路径、版本和 CLI 检查；Runtime `status.omp` 区分已观察到的 Extension attestation、RPC 兼容性以及正在运行的 RPC 数量。无模型时 `verify-omp` 可能不能启动 RPC；具备凭据后，`--with-model` 才发真实请求。

`status.omp.installation_status` 为 `selected`、`unavailable` 或 `handoff_required`。若安装文件在 Runtime 运行时被删除、替换、失去执行权限或改变身份，已启动的前台与后台会话可以自然结束；新前台、Worker、Agent restart 和该会话发起的 OMP 查询不得自动切到新文件。待执行 Wakeup 保持排队，纯 Reminder、Inbox 和控制面不依赖 OMP，照常运行。独立 `doctor` 和直接从终端运行的 `workflow models` 仍可检查用户当前选择的安装。

Linux／WSL 在 `exec` 前验证打开的文件描述符并从该描述符运行，原子替换不能重定向这次启动。常见 shebang 包装器也执行同一个已验证文件，但解释器看到的是 fd 路径；依赖 `$0` 所在目录或可变下游链接的 shim，请改为指向官方 ELF 的绝对路径。只固定已选文件，不审计包装器依赖或把可变文件变成不可变快照，不承诺抵御任意原地并发改写。软链接只在选择安装时解析；已运行的 Runtime 不跟随原始别名改指。

如果新版不再满足能力契约，保留文件和原会话，改选一份已验证的官方 OMP（`--omp` 或 `agent.executable`），空闲时安全重启 Runtime。能回退可执行文件不代表旧 OMP 能读取新版写过的全部 Session 格式；先保留备份，不编辑或重放 uncertain 对话。

## 启动与恢复

```bash
./paveway.sh
./paveway.sh --native --workflow test01
./paveway.sh --native --coach alpha
./paveway.sh --new
./paveway.sh --conversation CONVERSATION_ID
./paveway.sh --legacy-ui
# 已启动 Runtime 时也可直接进入：
oicoach-runtime native --config /absolute/config.toml --workflow test01
```

同一 workflow/task 默认接续最近的原生 Conversation；不传 task 时使用最近使用的 task。多 Profile 的第一次启动可用方向键选择，已有原生记录时优先恢复最近使用的 Profile。`--new` 从 Workflow 默认 Workspace 新建 Conversation；`--conversation ID` 明确恢复，未显式提供的 Workflow 和 task 从该记录推导。Persona 更新要求新的 Paveway Conversation，不会热改当前身份；原生 `/new` 只在同一 Conversation 与目录下创建另一个 OMP Session。要加载新 Persona，请用 `/paveway profile` 的 **New conversation**，或重新启动加 `--new`。

`--new` 与 `--conversation ID` 互斥，且仅适用于原生入口；`--legacy-ui` 或 `PAVEWAY_UI=legacy` 下明确拒绝，不会静默忽略。两者都不传时保留默认续接行为，不会为了绕过所有权冲突而自动新建。

初次升级需在空闲时重启 daemon，使其加载新的 frontend IPC；旧 daemon 不支持该协议时入口明确拒绝，已有请求不被打断。原生入口目前只精确加载 Paveway trusted extension，不自动发现其他用户 Extension；内建工具、Workspace MCP、Skills 和 Rules 仍由 OMP 提供，并遵守配置权限。

默认接续的旧对话尚未释放时，启动入口显示 Session recovery 选择：uncertain 可选择恢复旧对话、新建或取消；仍处于 active/idle/starting 的旧 owner 只提供新建或取消。方向键／空格选择，Tab 切到确认按钮，Enter 确认。恢复必须经 Runtime 检查旧进程死亡、锁已释放及历史完整性，不能凭失去心跳接管；失败不会偷偷改为新对话，也不重发旧输入。新建保留原记录，缺失或损坏的 Session 仍需核查并使用新对话。

## 原生项目目录

- `/paveway cwd`，或从 `/paveway` 菜单选择 `cwd`：显示当前目录和 Workflow 默认 Workspace，用 OMP 原生输入框输入路径，再确认或取消。
- `/paveway cwd <路径>`：直接切换，无第二次确认。支持绝对路径、`~`、`~/…`、相对于当前目录的路径，以及包含空格的路径；可加一对外层引号。目录必须已存在、可读取并可进入；不会创建目录。指向当前目录时不重启。
- `/paveway profile` → **Resume conversation**：在当前 Workflow/task 中选择另一条已释放的对话。也可用 `./paveway.sh --conversation ID` 恢复；`/coach` 显示当前 Conversation ID 和目录。`uncertain` 仍走启动时的显式恢复。

切换只在前台空闲时接受，活动回合或工具尚未结束时拒绝。Runtime 确认旧 owner 和 Session 文件锁均已释放后才启动新 OMP。新 Conversation 有独立 ID 和 Session 目录，不搬迁 Session、不重发输入，也不更改后台 Run 的绑定或 Workflow 默认 Workspace。目标目录如果在切换期间消失，启动会报错，不回退到其他目录；旧对话可用原 ID 打开。

OMP 用实际目标 cwd 发现工具、Git、`AGENTS.md`、Agent、Rules、Skills 和 MCP。Paveway 保留同一 Workflow、Profile、Persona、附加 Prompt、个人 Skills 与 Memory 作用域，并带上当前模型与 Thinking。官方接口无法安全复现受管 Tijian 的跨目录继承和冲突拒绝；源 Workflow 配置该工具时会拒绝跨目录切换，不改动配置或会话。详见 [Tijian MCP](tijian.md#在其他项目使用-workflow-的-tijian-mcp)。

受管前台拦截 `/move`、`/wt`、持久化 `!cd`、原生 `/resume` 和进程内重启；用 `/paveway cwd`、`/paveway profile` 或 `/paveway restart` 走 Runtime 所有权交接。普通 `!pwd`、`!cd dir && command` 等局部 shell 操作不迁移对话。

cwd 是本机 Session 身份，不是跨设备 Git 文字归档的可移植设置。目录移动或删除后，恢复会报错，需恢复原路径或新建 Conversation。直接使用 `oicoach-runtime native` 时，先用 `./paveway.sh` 安全升级并启动匹配构建的 Runtime。

## 原生菜单

选择已安装的完整包后显示专用只读菜单：**查看入口 SKILL.md**、**查看资源文件清单**（路径、大小、媒体类型、SHA256 前缀、文本/二进制）、**查看指定文本资源**（输入包内相对路径）、启用／禁用、**重新导入替换**、删除。内容经 CLI `skill-files` / `skill-show` 读取，后端校验 Profile 绑定、manifest 与安全相对路径；二进制只显示类型/大小/摘要，不尝试解码；查看不修改包。

`/paveway skills` 的 **Import standard SKILL.md / ZIP · local** 是标准 ZIP 的默认导入路径（菜单第一项），原样安装完整 OMP Skill 目录，不调用模型：解析必要的 YAML frontmatter（name、description）后，把 `SKILL.md` 与 `references/`、`scripts/`、`assets/` 等全部资源、原始字节和相对路径一起安装。多 Skill ZIP 展示每项名称、替代关系、文件数量、总大小与启停状态；可多选、全选、查看只读文件清单（路径／大小／SHA256）、切换启停，最后确认整批安装。已有同名项默认保留原启停状态，并明确标注是替换单文件还是完整包；确认界面同时提示完整托管包会进入用户数据 Git 同步范围。一次 revision CAS 原子提交所选项；并发修改、源文件在预览后变化都会拒绝整批，需要重新预览。取消不会写入。详见 [customization.md](customization.md#原样安装完整-skill-包本地-zip)。

CLI 预览入口：

```bash
oicoach-runtime workflow customize skill-local-preview WORKFLOW --file /path/to/package.zip --config /absolute/config.toml --json
```

预览只读，返回 `file`、`sha256`、`revision` 与每项 `name`、`description`、`category`、`enabled`、`conflict`、`replaces`、`digest`、`file_count`、`bytes`、`resources`（不含文件内容）。批量保存使用：

```bash
oicoach-runtime workflow customize skills-import WORKFLOW --expected-revision N --content-file /private/batch.json
```

batch 为 `{ "file": "<预览的路径>", "sha256": "<预览的 ZIP 摘要>", "skills": [{"name": …, "category": …, "enabled": …}], "replace": [已确认替换的同名名称] }`；`file`/`sha256` 必须与预览一致，`replace` 必须与选择集里的同名集合完全相同。CLI 会重新读取源文件，按预览摘要校验后写入内容库并提交引用。源文件大于 1 MiB、多于 2048 条目或超过 32 MiB 解压内容时拒绝；每个 Skill 最多 512 个文件／16 MiB，单个文件最多 4 MiB。ZIP 不执行任何内容，也不安装依赖。

输入 `/paveway` 用方向键选择；也可 `/paveway prompt` 等直接打开。以下均为用户操作界面，编辑正文不发送给模型。

| 菜单 | 功能 |
| --- | --- |
| settings | SettingsList 设置中心：Tijian MCP、Model preference、Thinking preference |
| prompt | 查看／编辑／清空用户自定义追加 Prompt |
| skills | 新建、查看、编辑、启停、删除单文件 Skill；原样安装／替换完整包；OI Introduce、user information、personal skill 同时按需发现 |
| persona | 编辑独立 Persona，CAS 保存，新的 Paveway Conversation 生效 |
| profile | 创建／切换 Profile、新建／恢复 Conversation、重启原生进程 |
| cwd | 显示当前目录、输入或直接切换到新目录并新建 Conversation |
| model / thinking | 可携带到其他设备的配置偏好，下次启动生效；本次 OMP 的模型／思考选择仍使用原生命令和快捷键 |
| events | 当前作用域的事件详情、CAS 取消／重排 |
| memory | Profile 或 task 的笔记 CRUD；不与 OMP 原生 Memory 混为一谈 |
| history | 登记原生 Session 的只读分支文字搜索与分页，保留来源 ID |
| recover | 后台不确定事件诊断及明确确认后的重试，不自动重放 |
| sync | 私有 Git 状态、remote、本地同步、冲突、远端配置绑定及跨设备文字归档 |
| vjudge | 状态／测试／密码或 Cookie 登录／清除；隐藏输入、私密文件传递，不进聊天 |
| tijian | 直接打开与 settings 相同的 Tijian MCP 页；None / Managed / Full，保存后重启生效 |
| service | 已有 systemd 管理功能；操作服务生命周期时保留未知状态的恢复语义 |
| notifications | Ubuntu／WSL 桌面到期弹窗的启停、隐藏正文、状态及测试；独立服务不打断当前 GPT |
| agent | 后台 worker 重启及不重播说明 |
| legacy | 正常释放原生进程后进入原管理 UI；退出后恢复原生 Conversation，覆盖其余低频旧操作 |

`/paveway status` 读取 Runtime 健康状态；`/coach` 显示当前 GPT Profile 及 Session。不覆盖 OMP 自己的 `/memory`、`/skills` 等命令，因此 Paveway 的专属功能统一放在 `/paveway` 菜单中。

登录使用宿主 OMP 的 masked Input 组件，实际测试屏幕不含明文。不能仅依赖 `ui.input(..., {secret:true})`，本机 18.10.4 会忽略这个选项；组件预检失败时拒绝收集秘密，保留旧受保护 UI。用户名／密码只通过临时 0600 文件交给原有认证 CLI，密码不保存；Cookie 仅写 Tijian 自己的本机会话位置。

## 原生设置中心

`/paveway` 菜单选择 `settings`，或直接输入 `/paveway settings`。Model preference 和 Thinking preference 复用原有 `/paveway model`、`/paveway thinking` 操作，保存的是下次原生启动使用的偏好。Prompt、Skills、Persona、Memory 等独立命令不变，不注入 OMP 自己的 `/settings`。

Tijian MCP 页也可直接通过 `/paveway tijian` 打开：

- **MCP executable**：当前 `.omp/mcp.json` 中保存的路径。Enter 打开 OMP `ui.input()` 输入替换路径；空输入保留已有路径，Esc 取消。只修改路径不改变权限。
- **Permission**：Enter 打开原生 SelectList。None 只读；Managed（首次推荐）允许只读与三种 ensure，禁止直接编辑／删除；Full 完整读写。从 None／Managed 升至 Full 时由原生 `ui.confirm()` 明确确认。
- **Configuration**：只读状态。保存后显示 `Saved (restart required)`；不是 MCP 已连接或权限已立即改变的证明。

列表的导航、选中样式、搜索、滚动、Esc、Theme 和截断由 OMP 公共 UI 组件提供。权限子菜单 Esc 回到 Tijian 页，Tijian 页 Esc 回设置中心，退出后保留原编辑器和未提交文本；文件写入期间保留对话框焦点。

每次打开／返回 Tijian 页都读取真实配置；已有连接不自动调整权限。未知参数或非标准 Server 定义显示错误并拒绝覆盖，修复后可重新操作，不清除其他设置。保存带文件内容 revision；并发变化时拒绝旧编辑并重新读取。所有这些界面操作不发给模型，也不注册模型可调用的设置工具；RPC、后台和子 Agent 不挂载设置页。

成功提示为 `Saved. Restart the Paveway native OMP session to apply Tijian MCP changes.`。需用户自行在空闲时退出并重启原生 Session；不自动停止当前 Session、后台 Worker 或 AI Job，不实现 MCP 热重载，也不通过 MCP 工具调用验证权限。三档能力、安全写入及旧 CLI 兼容见 [Tijian 文档](tijian.md)。

## 唤醒与收件箱

```text
/reminder in 10m 休息一下
/reminder at 21:00 该睡觉了
/reminder list
/reminder cancel EVENT_ID VERSION
/reminder reschedule EVENT_ID VERSION 2026-11-11T21:00:00+08:00
/wakeup in 10m 检查训练进度
/wakeup at 21:00 总结今天的训练
/wakeup list
/wakeup cancel EVENT_ID VERSION
/wakeup reschedule EVENT_ID VERSION 2026-11-01T21:00:00+08:00
/inbox
/inbox 2
```

`in` 从 Runtime 收到并提交请求时起算；`at HH:mm` 使用系统时区的下一次该时刻，RFC3339 必须带时区。状态栏无事件时隐藏 Wakeup，有待执行事件显示 `◷ 12m` 一类倒计时，执行中显示 `◷ Running`；完整触发时间仍由 `/wakeup list` 查看。Inbox 为零时隐藏，非零显示 `✉ 2` 一类未确认数。到零不代表已执行；实际状态来自 Runtime。Runtime 不可用或 owner 不确定时优先显示 `Wakeup !` 和 `Inbox ?` 异常胶囊，初始化尚未知的状态也不伪装为零。这里只改变展示，不修改排程、消息确认、通知、所有权或持久化语义。

Agent 可用 `paveway_wakeup_schedule/list/cancel/reschedule`；工具绑定当前 owner/generation 和作用域，用户不能通过参数指向另一 Profile。默认维持 opt-in，不创建无限自主跟进。前台 `after_turn_settled` 明确不提供，旧后台 `schedule --after-turn --run-id` 继续原样使用，二者不会混淆。

原生前台优先使用这些工具；旧流程中 `schedule --after-turn` 只能绑定 Runtime 提供的真实后台 run_id，不能拿原生 Session/Conversation ID 代替。Composer v3 明确覆盖这项运行边界，不改写旧 event.md。已排程的 Agent Wake 直接校验固定设置快照，不先加载后来编辑的用户设置；Workflow manifest、模板与 revision 仍会正常校验。纯 reminder 不需要模型快照。

更新代码后仍运行的旧 daemon 不会自动重载 Python。再次执行 `./paveway.sh` 时，启动器通过 Runtime 的 `lifecycle.*` IPC 原子地进入 draining：拒绝新的后台任务与前台租约，把尚未 dispatch 的租约退回持久队列，确认没有活动 Run、实时前台会话或其他未释放 owner 后，才请求旧实例退出并等待 `runtime.lock`、socket lock 与进程身份全部释放，再用当前代码启动新实例。持久提醒、`/wakeup`、未读 Inbox 和已释放 Session 不会阻止或丢失；存在活动工作时保留旧实例并输出 Run/Conversation 标识及安全处理方式。旧 daemon 若没有该交接协议，则回退到明确的手动指引，不盲目 kill。

`/reminder` 到期本地持久化 Inbox 正文并结算事件，不创建 Run、不启动 OMP RPC、不调用模型；没有桌面服务也能完成，不占用正在执行的 Agent 槽位。`/wakeup` 仅为 agent_wake，继续使用固定配置、Workflow、Memory 与后台 RPC，结果进入 Inbox，不弹普通提醒。没有旧提醒别名。两种命令的 list 只列各自类型，cancel/reschedule 校验类型、作用域与 CAS；已结算 reminder 可改期，Agent Wake 的既有终态编辑限制保持不变。

created、offered、用户明确 acknowledged 分开；事件结算、模型执行成功、工具返回或菜单通知不冒充用户已读。UI 关闭时仍保存，重新打开 `/inbox` 查看；可用 `/paveway notifications` 启用 [Ubuntu／WSL 桌面弹窗](desktop-notifications.md)，目前没有手机推送，也不自动把消息重注入 Agent。Agent 工具仍共用 paveway_wakeup_schedule：用户说“提醒我”默认 reminder，明确未来 AI 工作才用 agent_wake，必须有用户授权或既有 opt-in。

UI 在线时轮询到新的提醒／结果，会直接显示提醒正文及“GPT · 后台定时任务回复／执行状态”，并保留 `/inbox` 查看与确认入口。超过预览长度时明确标记“节选”，完整内容在收件箱中；正文过滤终端控制字符。显示不会触发模型、确认已读或清空输入草稿；重新打开 UI 只显示已有未确认数，不重播旧通知。前后台 Session 仍分离，UI 展示不代表内容已加入当前模型上下文。后台刷新间隔约 10 秒，后台执行结果可能晚于到期提醒。更新这部分扩展后，保存草稿并重新进入原生 UI 生效，不必为此重启 daemon 或重新排程已有任务。

## 隔离、归档与回退

Profile 的逻辑身份独立于流程模板。新 Profile 复用选定流程并分配私有 runtime workflow，原工作流隐式映射原同名 Profile；不会复制源 Profile 的私人 Skills、Prompt 和笔记，不重写旧历史。Persona 与通用 state 使用现有版本／CAS，原配置保留私密备份。模型配置、Prompt、Skill 和 Persona 通过原独立用户数据 Git 同步；没有 dedicated remote 时只在本地 commit。

原生 Session 是聊天原文权威来源。只读投影保留各 Session/分支的 entry/parent ID；结束后的用户／助手文字进入独立 Git，不包含 tool 原文、隐藏思考、启动 capability 或登录秘密。`sync sessions` 可发现原生和 RPC 归档，再用 `sync messages` 查询；跨设备不把归档当作可直接写回的原始 Session。

```bash
oicoach-runtime frontend list --config /absolute/config.toml --json
oicoach-runtime frontend show CONVERSATION --config /absolute/config.toml --json
oicoach-runtime frontend messages CONVERSATION --query TEXT --offset 0 --config /absolute/config.toml --json
oicoach-runtime frontend recover CONVERSATION --config /absolute/config.toml --json
oicoach-runtime sync sessions --config /absolute/config.toml --json
```

第二 owner、旧 generation、目录逃逸或未释放的锁被拒绝；心跳超时不证明进程死亡。异常退出／daemon 重启留下 uncertain，需检查副作用并确认旧进程与锁已退出才显式恢复；也可新建 Conversation，原数据保留。前台没有严格 settled 的成功声明，不自动重播用户输入。

空闲界面的 Ctrl+D 和 `/exit` 走同一所有权收尾路径：按键透传给 OMP，等待 OMP 与 guardian 退出并回收子进程，关闭本次 `owner.lock` 描述符，再以最后一次已认证的 generation/token 提交 `frontend.finish`。Runtime 校验进程已退出、读取已落盘 Session 后，提交 `released` 与 `exit_code=0`。`session_shutdown` 只是观察记录，不能代替 finish；已释放记录在 Runtime 重启后仍可直接续接。

如果收尾请求失败，入口报告 `native_finish_failed`，其中 `release_diagnostic` 给出 `daemon_unavailable`、`conflict`、`internal_error`、`native_archive_failed` 及具体 `native_session_*`／锁错误等类别，并单独显示 OMP 的退出码（或 unknown）；不回显请求载荷、token 或 Session 正文。OMP 正常退出但释放未确认时，入口返回非零；原有非零退出码／启动异常优先保留，并额外报告收尾失败。归档校验未通过时也不宣称成功释放。退出与恢复使用流式 Session 安全校验，完整文字导出的 16 MiB 上限不再阻止有效会话释放；物理锁未释放或文件安全校验失败仍 fail-closed。根因、确定性复现和保留边界见 [生命周期审查](native-lifecycle-review.md)。

遇到此诊断，先用同一配置启动 Runtime，再用 `frontend show/list` 查看持久化状态。仅对 `uncertain` 且确认旧进程、锁和副作用安全的记录显式 `frontend recover`；也可 `--new` 保留旧历史继续。不会自动 recover、重试退出提交或重发模型请求。Runtime 失联时可能连 finish 都未提交，重启时仍按原有规则保留 `uncertain`，不从关闭事件或心跳推断成功。

`--shutdown-on-exit` 复用同一条收尾路径，但只对本次启动创建、非 systemd 托管且所有权记录（实例 ID、进程身份、配置与 socket）匹配的独立 Runtime 生效；还需通过 Runtime 的活动检查。共享 Runtime、其他活动会话、活动后台 Run 或释放失败都会保留实例并说明原因。默认仍保留后台 Runtime 以支持提醒、唤醒和跨会话续接。

使用 `--legacy-ui` 或 `PAVEWAY_UI=legacy` 可切回原界面；旧 CLI/RPC 保留。这里没有 schema migration，旧表语义不变。回退 UI 与降级旧二进制不同：新增 Persona/coach 设置字段需要新版本读取；要使用旧二进制须先停服务并从私密配置备份恢复，不能删除用户数据或重试 uncertain 来“修复”。
