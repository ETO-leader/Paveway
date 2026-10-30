# OMP 原生界面

Paveway 使用原生 OMP TUI 处理聊天、流式回复、工具展示、滚动、模型选择和 Session。后台仍由原有 Runtime 队列与 RPC 执行，退出 UI 不停止 daemon。前后台使用独立物理 Session，不同时写同一个文件；通过配置快照、记忆和持久收件箱衔接。

## 启动与恢复

```bash
./paveway.sh
./paveway.sh --native --workflow test01
./paveway.sh --native --coach alpha
./paveway.sh --legacy-ui
# 已启动 Runtime 时也可直接进入：
oicoach-runtime native --config /absolute/config.toml --workflow test01
```

同一 workflow/task 默认接续最近的已释放原生 Conversation；不传 task 时恢复最近使用的 task。多 Profile 的第一次启动可用方向键选择，已有原生记录时优先恢复最近使用的 Profile。`--new` 新建 Conversation；`--conversation ID` 明确恢复。Persona 更新要求新的 Paveway Conversation，不会热改当前身份；原生 `/new` 只在本次加载的配置中创建另一个 OMP Session，要加载新 Persona 请用 `/paveway profile` 的 New conversation 或重新启动加 `--new`。

初次升级需在空闲时重启 daemon，使其加载新的 frontend IPC；旧 daemon 不支持该协议时入口明确拒绝，已有请求不被打断。原生入口目前只精确加载 Paveway trusted extension，不自动发现其他用户 Extension；内建工具、Workspace MCP、Skills 和 Rules 仍由 OMP 提供，并遵守配置权限。

## 原生菜单

输入 `/paveway` 用方向键选择；也可 `/paveway prompt` 等直接打开。以下均为用户操作界面，编辑正文不发送给模型。

| 菜单 | 功能 |
| --- | --- |
| prompt | 查看／编辑用户自定义 Prompt，保留 append/replace 原文与模式 |
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
| tijian | 连接原生 MCP，明确选择只读或读写；保存后重启原生 Session 加载 |
| service | 已有 systemd 管理功能；操作服务生命周期时保留未知状态的恢复语义 |
| agent | 后台 worker 重启及不重播说明 |
| legacy | 正常释放原生进程后进入原管理 UI；退出后恢复原生 Conversation，覆盖其余低频旧操作 |

`/paveway status` 读取 Runtime 健康状态；`/coach` 显示当前 GPT Profile 及 Session。不覆盖 OMP 自己的 `/memory`、`/skills` 等命令，因此 Paveway 的专属功能统一放在 `/paveway` 菜单中。

登录使用宿主 OMP 的 masked Input 组件，实际测试屏幕不含明文。不能仅依赖 `ui.input(..., {secret:true})`，本机 18.10.4 会忽略这个选项；组件预检失败时拒绝收集秘密，保留旧受保护 UI。用户名／密码只通过临时 0600 文件交给原有认证 CLI，密码不保存；Cookie 仅写 Tijian 自己的本机会话位置。

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

明确提醒在到期领取时先持久化，后台 GPT 执行结果另存一条消息。created、offered、用户明确 acknowledged 分开；模型执行成功、工具返回或菜单通知不冒充用户已读。UI 关闭时仍保存，重新打开 `/inbox` 查看；目前没有外部桌面／手机推送，也不自动把消息重注入 Agent。

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

使用 `--legacy-ui` 或 `PAVEWAY_UI=legacy` 可切回原界面；旧 CLI/RPC 保留。这里没有 schema migration，旧表语义不变。回退 UI 与降级旧二进制不同：新增 Persona/coach 设置字段需要新版本读取；要使用旧二进制须先停服务并从私密配置备份恢复，不能删除用户数据或重试 uncertain 来“修复”。
