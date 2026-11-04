# OMP 原生界面

Paveway 使用原生 OMP TUI 处理聊天、流式回复、工具展示、滚动、模型选择和 Session。后台仍由原有 Runtime 队列与 RPC 执行，退出 UI 不停止 daemon。前后台使用独立物理 Session，不同时写同一个文件；通过配置快照、记忆和持久收件箱衔接。

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

同一 workflow/task 默认接续最近的已释放原生 Conversation；不传 task 时恢复最近使用的 task。多 Profile 的第一次启动可用方向键选择，已有原生记录时优先恢复最近使用的 Profile。`--new` 新建 Conversation；`--conversation ID` 明确恢复。Persona 更新要求新的 Paveway Conversation，不会热改当前身份；原生 `/new` 只在本次加载的配置中创建另一个 OMP Session，要加载新 Persona 请用 `/paveway profile` 的 New conversation 或重新启动加 `--new`。

`--new` 与 `--conversation ID` 互斥，且仅适用于原生入口；`--legacy-ui` 或 `PAVEWAY_UI=legacy` 下明确拒绝，不会静默忽略。两者都不传时保留默认续接行为，不会为了绕过所有权冲突而自动新建。

初次升级需在空闲时重启 daemon，使其加载新的 frontend IPC；旧 daemon 不支持该协议时入口明确拒绝，已有请求不被打断。原生入口目前只精确加载 Paveway trusted extension，不自动发现其他用户 Extension；内建工具、Workspace MCP、Skills 和 Rules 仍由 OMP 提供，并遵守配置权限。

默认接续的旧对话尚未释放时，启动入口显示 Session recovery 选择：uncertain 可选择恢复旧对话、新建或取消；仍处于 active/idle/starting 的旧 owner 只提供新建或取消。方向键／空格选择，Tab 切到确认按钮，Enter 确认。恢复必须经 Runtime 检查旧进程死亡、锁已释放及历史完整性，不能凭失去心跳接管；失败不会偷偷改为新对话，也不重发旧输入。新建保留原记录，缺失或损坏的 Session 仍需核查并使用新对话。

## 原生菜单

输入 `/paveway` 用方向键选择；也可 `/paveway prompt` 等直接打开。以下均为用户操作界面，编辑正文不发送给模型。

| 菜单 | 功能 |
| --- | --- |
| settings | SettingsList 设置中心：Tijian MCP、Model preference、Thinking preference |
| prompt | 查看／编辑／清空用户自定义追加 Prompt |
| skills | 新建、查看、编辑、启停、删除；OI Introduce、user information、personal skill 同时按需发现 |
| persona | 编辑独立 Persona，CAS 保存，新的 Paveway Conversation 生效 |
| profile | 创建／切换 Profile、新 Conversation、重启原生进程；先正常释放旧进程再启动目标环境 |
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

列表的导航、选中样式、搜索、滚动、Esc 由宿主 OMP SettingsList／SelectList 提供，Theme 与 ANSI 安全截断沿用 OMP；已验证 18.16.0 的实际组件 API。长路径在窄终端中截断，路径输入使用 OMP 自身的单行滚动。权限子菜单 Esc 回到 Tijian 页，Tijian 页 Esc 回设置中心，设置中心 Esc 退出并恢复原编辑器、焦点及未提交文本。提交后的文件 I/O 期间保留原生 UI 焦点，避免按键落入聊天。

每次打开／返回 Tijian 页都读取真实配置；已有连接不自动调整权限。未知参数或非标准 Server 定义显示错误并拒绝覆盖，修复后可重新操作，不清除其他设置。保存带文件内容 revision；并发变化时拒绝旧编辑并重新读取。所有这些界面操作不发给模型，也不注册模型可调用的设置工具；RPC、后台和子 Agent 不挂载设置页。

成功提示为 `Saved. Restart the Paveway native OMP session to apply Tijian MCP changes.`。需用户自行在空闲时退出并重启原生 Session；不自动停止当前 Session、后台 Worker 或 AI Job，不实现 MCP 热重载，也不通过 MCP 工具调用验证权限。三档能力、安全写入及旧 CLI 兼容见 [Tijian 文档](tijian.md)。

## 唤醒与收件箱

```text
/wakeup in 10m 检查训练进度
/wakeup at 21:00 晚间提醒
/wakeup list
/wakeup cancel EVENT_ID VERSION
/wakeup reschedule EVENT_ID VERSION 2026-11-01T21:00:00+08:00
/inbox
/inbox 2
```

`in` 从 Runtime 收到并提交请求时起算；`at HH:mm` 使用系统时区的下一次该时刻，RFC3339 必须带时区。状态栏显示系统时间格式的触发时间、秒级倒计时和未确认消息数。到零不代表已执行；实际状态来自 Runtime。

Agent 可用 `paveway_wakeup_schedule/list/cancel/reschedule`；工具绑定当前 owner/generation 和作用域，用户不能通过参数指向另一 Profile。默认维持 opt-in，不创建无限自主跟进。前台 `after_turn_settled` 明确不提供，旧后台 `schedule --after-turn --run-id` 继续原样使用，二者不会混淆。

原生前台优先使用这些工具；旧流程中 `schedule --after-turn` 只能绑定 Runtime 提供的真实后台 run_id，不能拿原生 Session/Conversation ID 代替。Composer v3 明确覆盖这项运行边界，不改写旧 event.md。已排程的原生事件直接校验固定设置快照，不先加载后来编辑的用户设置；Workflow manifest、模板与 revision 仍会正常校验。

更新代码后仍运行的旧 daemon 不会自动重载 Python。保存草稿并退出原生 UI 后，在空闲时重启 Runtime 再进入界面；`invalid_workflow` 等 blocked 事件需要核查并明确重新安排，不因服务升级自动重发。

明确提醒在到期时先持久化，后台 GPT 执行结果另存一条消息。created、offered、用户明确 acknowledged 分开；模型执行成功、工具返回或菜单通知不冒充用户已读。UI 关闭时仍保存，重新打开 `/inbox` 查看；可用 `/paveway notifications` 启用 [Ubuntu／WSL 桌面弹窗](desktop-notifications.md)，目前没有手机推送，也不自动把消息重注入 Agent。

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

如果收尾请求失败，入口报告 `native_finish_failed`，区分 `daemon_unavailable`、`conflict`、`internal_error` 等类别，并单独显示 OMP 的退出码（或 unknown）；不回显请求载荷、token 或 Session 正文。OMP 正常退出但释放未确认时，入口返回非零；原有非零退出码／启动异常优先保留，并额外报告收尾失败。归档校验未通过时也不宣称成功释放。

遇到此诊断，先用同一配置启动 Runtime，再用 `frontend show/list` 查看持久化状态。仅对 `uncertain` 且确认旧进程、锁和副作用安全的记录显式 `frontend recover`；也可 `--new` 保留旧历史继续。不会自动 recover、重试退出提交或重发模型请求。Runtime 失联时可能连 finish 都未提交，重启时仍按原有规则保留 `uncertain`，不从关闭事件或心跳推断成功。

使用 `--legacy-ui` 或 `PAVEWAY_UI=legacy` 可切回原界面；旧 CLI/RPC 保留。这里没有 schema migration，旧表语义不变。回退 UI 与降级旧二进制不同：新增 Persona/coach 设置字段需要新版本读取；要使用旧二进制须先停服务并从私密配置备份恢复，不能删除用户数据或重试 uncertain 来“修复”。
