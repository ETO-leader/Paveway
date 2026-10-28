# 对话式终端 UI（0.18.0）

0.9.0 替换 0.8.0 的数字切页/全屏表单交互。现在以对话记录和底部常驻输入框为中心，使用 slash 命令、补全和方向键选择器。参照本机 OMP 18.10.4 实际 PTY 画面与其 pi-tui 编辑器键位（Enter 发送、Ctrl+J 换行、Tab 补全），不宣称兼容 OMP 的全部快捷键、模型/工具流或原生界面组件。

实现使用 [prompt_toolkit Application、布局和编辑器](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html)，由项目依赖自动安装。`tui.py` 管交互，`ui_client.py` 只通过公开 CLI 子进程工作，不直接打开数据库，也不替换 OMP。调度核心不 import UI 库。

## 启动

```bash
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/oicoach-runtime ui --config "$PWD/.runtime/config.toml" --workflow coach --task training
# 不指定 workflow：只有一个时自动选用，多个时用方向键选择，无环境时提示创建。
.venv/bin/oicoach-runtime ui --config "$PWD/.runtime/config.toml"
```

需要 Linux、Python 3.11+、真实 TTY、有效 TERM 和 UTF-8 locale；建议 80×24 或更大。支持窗口缩放，过小窗口的显示空间有限，放大后草稿仍在。非 TTY/TERM=dumb 或 --json 会明确拒绝；脚本继续使用 CLI。

daemon 独立运行。首次可按 README 执行 init/serve；UI 中也可 `/init`，再 `/workflow create` 按输入区提示填写 ID 和已存在的 workspace 绝对路径。服务已安装时可 `/service start`，否则在另一终端运行 serve。UI 不自动安装/启动/启用服务，不修改 linger 或全局模型认证。未自定义 state/workflow 路径的 init 使用 XDG 默认，不根据 config 文件所在目录猜路径。

## 日常操作

右侧滚动条可以点击轨道跳转、拖动滑块或点击上下箭头；正文区域可用鼠标滚轮。PageUp/PageDown 按窗口显示高度翻页，中文及长段落的自动折行也计算在内。滚动不会抢走底部输入框焦点或清除草稿；向上浏览时，新回复到达仍保持阅读位置。滚回底部或按 Ctrl+End 后恢复跟随最新内容。应用启用终端鼠标事件；需要终端原生选字复制时，通常可按住 Shift 再拖选。

| 操作 | 按键 |
| --- | --- |
| 输入请求 | 直接在底部打字，普通数字/q/r 等不会导航或退出 |
| 发送 | Enter |
| 换行 | Ctrl+J 或 Alt+Enter；不依赖终端能否区分 Shift+Enter |
| 命令补全 | 输入 `/`，方向键选择，Enter 补全并执行；Tab 只填入命令 |
| 选择记录/workflow | ↑↓ 选择，Enter 确认，直接打字筛选，Esc 返回 |
| 模式 | `/mode` 选择，或 `/mode plan|checkin|review`；Shift+Tab 轮换训练模式 |
| 编辑 | 编辑器默认的方向键/Home/End/删除和历史；支持中文及 bracketed paste |
| 表单 | Enter 下一项/保存，Ctrl+J 正文换行，Shift+Tab 回上一项，Esc 取消 |
| 滚动 | 滚轮、右侧滚动条点击/拖动、PageUp/PageDown；Ctrl+Home 到开头，Ctrl+End 回到最新；Ctrl+L 重绘 |
| 取消当前界面操作 | Esc 关闭选择器/表单/补全；不 abort 模型 |
| 清草稿/退出 | Ctrl+C 清草稿或取消选择；空输入连续两次 Ctrl+C 退出 |
| 退出 | `/quit`，或普通输入区为空时 Ctrl+D；不停止 daemon/取消事件 |

正文若需以 `/` 开头，可在前面加一个空格以避免命令解析。只有明确按 Enter 才提交，bracketed paste 中的换行和 `q`、数字、`/quit` 不会自行执行。输入上限在提交时检查，超限明确保留草稿，不静默截断。

## 命令

| 命令 | 行为 |
| --- | --- |
| `/help` | 操作说明 |
| `/proxy` | 同步当前终端代理，检查代理 TCP 端口；新 OMP 回合使用最新设置 |
| `/workflow [ID]` | 选择/切换 workflow，验证其存在 |
| `/workflow create [ID "PATH"]` | 在输入区创建 coach，PATH 为已存在的绝对目录，不覆盖已有文件 |
| `/task ID`、`/new [ID]` | 接续指定 task，或创建新任务 ID |
| `/mode [plan\|checkin\|review]` | 安排/反馈/复盘 |
| `/select-model [provider/model\|default]` | 本机 OMP 模型目录，输入筛选并选择；明确模型恢复 thinking=auto |
| `/select-thinking-hardness [LEVEL\|default]` | 选择思考等级；从下次派发生效，default 恢复继承 |
| `/prompt [show\|edit\|reset]` | 当前 workflow 自定义系统提示词，append/replace，确认恢复默认 |
| `/skills [add\|show NAME\|edit NAME\|enable NAME\|disable NAME\|delete NAME]` | 用户 Skill 管理，方向键选择及内联表单 |
| `/events`、`/events cancel ID` | 所有任务事件列表、详情；取消前读取版本并确认 |
| `/schedule` | 输入 RFC3339（带时区）或 now、请求正文后安排事件 |
| `/timers` | 当前 workflow/task 待执行及执行中的任务，方向键选择查看详情，最多 100 项 |
| `/memory [shared\|task]` | 当前 workflow 共享/当前 task 笔记，方向键选中读取 |
| `/memory add [KEY]` | 输入 key、标题、正文、kind、pinned、有效期 |
| `/memory edit KEY`、`/memory forget KEY` | 读取现版本，编辑或确认删除，不盲目覆盖冲突 |
| `/history [QUERY]` | 当前 workflow 跨 task 搜索输入/最终回复，方向键读取记录 |
| `/history messages TASK [QUERY]` | 原文查询，保留 all_branches/parent_id/has_more/truncated 语义 |
| `/next`、`/prev` | 翻当前查询页，事件/恢复每页 100，其余 20 |
| `/refresh` | 刷新当前查询，或恢复当前 task 最近 5 个 run |
| `/sync [setup\|now\|status\|pause\|resume\|conflicts\|history]` | 多设备 Git 配置/历史；后台自动同步，留空 remote 仅本地保存 |
| `/status`、`/doctor`、`/init` | 状态、诊断、初始化/已知迁移 |
| `/service [status\|install\|start\|stop\|logs]` | 默认 unit 的操作；stop 明确输入 yes，不自动启用登录启动 |
| `/tijian [login\|cookie\|test\|status\|clear]` | VJudge 共享登录；密码与 Cookie 隐藏输入，保存前验证，清除需确认 |
| `/recover` | 查询待核对事件，不自动重播 uncertain |
| `/retry-send` | 用上次 UI 提交的相同 scope/正文/mode/key 幂等核对 |
| `/discard-send` | 确认后丢弃 UI 内存提交记录，不取消可能已入库的任务 |
| `/quit` | 仅退出界面 |

命令参数按 shell 风格引号拆分，但从不送入 shell 执行；例如有空格的路径需引号。API/数据库细节仍通过现有 CLI 验证，Runtime 的版本、作用域、CAS 和权限规则不改变。

## 时间与倒计时

顶栏为当前系统时间；用户消息使用事件创建时间，GPT回复使用保存的完成时间，重新进入时从历史恢复原时间。系统通知使用通知生成时间。全部日期直接转换为系统时区（尊重对应日期的夏令时）；没有固定时区或 UI 时区配置，存储仍为 UTC。旧记录缺少时间时显示“时间未知”。

输入区上方显示当前 workflow/task 的最多三项任务，`/timers` 读取最多 100 项并打开详情选择器。UI 经公开 `events upcoming` CLI 每五秒刷新队列摘要，时钟和倒计时随界面刷新更新，不每秒请求后端。表单及选择器打开时暂缓队列读取，避免阻塞确认操作；倒计时继续显示，关闭后恢复刷新。切换作用域丢弃旧摘要，后台更新保留输入草稿。

after-turn 尚未锚定时显示“待起算”和相对间隔；锚定后显示触发时间与剩余时长。到时仍在队列中显示“已到时、等待派发”，领取后显示“启动中”或“执行中”，完成或取消后从面板移除。超过十五秒未更新标记“状态待刷新”，读取失败显示错误并自动重试；倒计时归零本身不证明已执行。关闭 UI 不影响 daemon 调度。

记忆编辑保留来源和已读取版本。保存失败时表单及各项答案保留，可 Shift+Tab 返回修改；conflict 需核对新版本，不能假定覆盖成功。取消编辑意味着放弃该表单；如果操作已提交，Esc 只关闭表单，操作仍可能落盘，应查看返回结果。忘记笔记不删除历史/备份，详见 [记忆接口](memory.md)。

## 等待、草稿与恢复

UI 用 ask 提交，确认后约每 2 秒查询 events show，终态读取 runs output。等待时显示从事件创建起的耗时，明确正在等待模型或工具完成；这不是实际工具进度流。重新进入 UI，恢复到运行中的 run 时继续追踪，不显示“没有已保存的最终回复”作为结果，也不提前标记已读完成。请求执行期间仍可输入下一条草稿、查看命令或编辑笔记；同一界面只追踪一个待完成请求，不用新 key 重复发送。后台回复更新记录，不覆盖当前草稿；表单不使用阻塞的独立输入循环，编辑时也会收取回复。

CLI 操作尚未确认时，下一条 slash 命令可按 Enter 排队（最多一条），状态栏显示等待，确认后自动执行；Esc 或空输入 Ctrl+C 可撤回等待命令。切换任务会丢弃旧作用域的等待命令。保存开始后输入区清空，后续草稿不会拼到已提交正文或被保存确认覆盖。保存失败保留表单答案，等待命令暂停到表单处理完毕。

结果不明时保留原请求/key，新的请求保持为草稿，先 `/retry-send` 核对。该操作是重复同一个幂等入队请求，不是强制重播模型。显式 `/discard-send` 仅丢弃当前 UI 内存记录，退出后仍可经 `/events` 或 `/history` 核对数据库中已有事件。CLI 默认预算 30 秒，不会因 UI 超时或退出撤销 daemon 已接收的操作。

模型失败时对话、重新打开后的恢复记录及历史详情显示错误码和处理提示，例如 HTTP 401 会提示检查 daemon 凭证；事件选项同时显示错误码。不会将失败当成空回复，也不会自动重发。旧记录若未保存错误摘要，只能显示原有错误码。

空闲时约每 15 秒核对当前 task 最近 5 个 run，只读取尚未展示的记录，发现后台或其他窗口的回复后自动补入；活跃请求仍用约 2 秒轮询。按 event 去重用户输入、按终态 run 去重回复，不重发请求或覆盖草稿。

当前显示最终回复，不伪装 token/tool 实时流。无最终输出、失败、uncertain、截断均保留原有含义。恢复最近记录按 run ID 去重，切任务不会把旧任务的异步历史结果灌入新任务。原文包含所有分支的文字片段，不能当当前执行路径或完整隐藏推理，限制见记忆文档。

退出会回收本 UI 拥有的 CLI 子进程并恢复终端；daemon 独立存活。服务停止属于 `/service stop` 的明确操作，可能将活动回合变为 uncertain。接口存在不代表 daemon 已部署；UI 不默默启用系统服务。

正文通过 0700 临时目录中的 0600 文件传递，避免进入 argv，正常结束后清理。输入历史仅在当前 UI 内存，不另写聊天历史文件。显示过滤终端 ESC/控制/双向控制符；同 UID 工具权限及终端录像不属于隔离边界。

## 验证

```bash
.venv/bin/python -m pip install '.[tijian,test]'
.venv/bin/python -m unittest discover -s tests -p test_tui.py -v
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_tui_omp.py -v
```

`.[test]` 提供 pyte 屏幕解析，仅用于测试。PTY 测试对实际终端格子断言，覆盖直接键入 `1qrs`/中文、多行 bracketed paste 不自动提交、Enter 发送、slash 菜单/补全、方向键选 workflow、缩放、回复到达时保留下一条草稿、退出与 termios 恢复、退出后 daemon 完成原请求。还覆盖笔记新增/修改、预填光标位置、排程及确认取消的连续实际操作。控制层另测保存确认期间排队下一条命令、幂等核对、内联表单后台更新、保存失败保留草稿、来源/CAS、历史作用域/分页和时间顺序。真实模型用例独立验证新 UI → CLI → daemon → OMP → 保存回覆；全部实机链路的旧测试结果保留为历史，不冒充每次 UI 修改均重跑。

0.10.0 的 `/prompt` 与 `/skills` 保存到当前 workflow，下一次派发生效，已有 task 可续聊；经过 OMP 原生系统提示词/Skill 加载，不改变每轮 coach/user 输入的角色。旧回合和历史不变，CAS 冲突保留草稿；权限限制、正文上限及 CLI 见 [自定义说明](customization.md)。

0.11.0 新增 `/sync` 方向键菜单和设置向导。`/sync now` 在 daemon 后台执行并由 UI 轮询结果，模型请求不等待网络；`/sync history [QUERY]`、`/sync messages DEVICE SESSION [QUERY]` 可用 `/sync next`、`/sync prev` 翻页。冲突菜单确认选择本机/某个远端版本；新设备先 `/sync bind ID` 指定本机 workspace。数据范围和安全边界见 [Git 同步](git-sync.md)。

0.11.1 起无需手动 setup 即可由 daemon 维护本地 Git，所有GPT对话共用一个数据仓库。`/sync remote` 单字段表单用于添加/清空 remote，清空后仅本地保存；菜单显示当前为“仅本地 Git”或“远端同步”。修改 remote 不恢复已暂停的自动同步。

0.12.0 将用户 Skill 分为 OI Introduce、user information、personal skill。UI `/skills` 按三类管理，`/skills category NAME` 可移动分类；0.15.0 起三类已启用项共同提供原生索引，正文由 Agent 通过 `skill://名称` 按需读取，append/replace 都生效，停用项不注册；停用工具时无法读取正文。旧记录默认 personal skill，按原文读取，保持 Git 内容寻址历史有效。

0.17.0 启动时先执行公开 proxy sync，再读取 workflow/恢复对话；显示代理检查结果，不显示代理 URL、用户名、密码或其他凭证。无代理变量时保留已有保存值，显式空值清除该项；同步不自动中断正在执行的回合。详细行为见 [部署说明](deployment.md)。

0.18.0 界面中的助手称谓统一为 GPT，包括当前对话、恢复对话、历史详情和跨设备历史；系统命名指示也要求 Agent 自称 GPT。event.md 和历史原文保留，不将名称替换写回个人 Skill 或既有对话记录。


## blocked / 会话冲突

0.19.1 会显示具体会话原因和处理指引；历史记录中只有 `blocked/conflict` 的情况也提示 `/new` 与 `/recover`。旧任务因上轮中断而 uncertain 时，继续向同一任务发送请求会在模型调用前被阻挡。输入 `/new` 创建新任务后再发送新请求即可继续，原历史保留；该操作不解除旧任务锁，也不自动重发原文。先核对旧事件副作用，再决定是否恢复。升级后重新进入 UI 以加载新版错误展示。


运行中，UI 会显示启动/模型/工具阶段、累计工具调用次数、已运行时间与距最近 RPC 活动的时间。此信息来自 daemon 处理的实时 OMP 事件，只含阶段、次数和时间；不回传命令参数、工具结果、路径或隐藏思考，不将“有活动”当作任务已成功完成。终态后实时进度清除，仍以 prompt_result 和 settled 为完成依据。需要重新进入 UI 加载新版展示。
