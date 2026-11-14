# OMP 原生界面

Paveway 使用原生 OMP TUI 处理聊天、流式回复、工具展示、滚动、模型选择和 Session。后台仍由原有 Runtime 队列与 RPC 执行，退出 UI 不停止 daemon。前后台使用独立物理 Session，不同时写同一个文件；通过配置快照、记忆和持久收件箱衔接。

## Aurora × Pathline 顶部 Banner

正常对话顶部的 Welcome 现在显示 Paveway 字标，不另开 Splash。宽屏使用 63 列、6 行的标准块字符字标，横向渐变为 `#E45BC4 → #A463F4 → #687EFF → #55C5E5`；下方为 65 列暗蓝灰 Pathline、靠右的青色光点、运行信息和暖金色斜体 Tip。各层按终端显示列宽居中，保留终端默认背景；黑底终端最接近设计图，不修改全局主题。

Paveway 版本来自 Runtime 的 `__version__`，Workflow 来自当前前台 lease 的 `workflow_id`，通过已有 0600 临时启动数据传递。OMP 版本由实际 `WelcomeComponent` 提供，不使用概念图版本或另起进程／网络探测。标准信息行是 `Paveway v{版本} · {Workflow} · OMP {版本}`。长 Workflow 按 Unicode 显示列宽截短；不足 67 列或 ASCII 预设时改为单行 `PAVEWAY`，进一步缩窄时拆分版本信息并截短 Tip。

固定 Host 的 `setHeader()` 在此版本是 no-op，而且 CLI 会在 Extension 的 `session_start` 之前预绘制 Welcome。因此 `banner.patch` 增加显式启用的 Welcome 呈现桥接；启动器仅在携带原生前台 admission 数据时预加载固定随包 `paveway-banner-host.mjs`，在首帧前注册 `paveway-banner.mjs`。不删除／过滤聊天组件，不替换 Transcript、输入区或胶囊状态栏。`Session-only model` 等真实运行提示仍由宿主显示；未注册呈现时保留 OMP 原行为。

入口动画复用 Welcome 的 33ms 定时器、组件级重绘、静态帧缓存和历史提交门控：等待真实 `tui.motion` 设置，从首次渲染开始计时，约 700ms 内完成淡入、柔和扫光和 Pathline 点亮。新建和恢复对话均在本次进程首显时播放一次；原生 `/new` 不重播。恢复会话不再套用 OMP 对原始 Welcome 动画的跳过策略；未调度、被跳过或已停止的动画直接显示完整亮色静态帧，不把“尚未播放”误当成“正在等待”，避免字标和光点冻结在暗色初始位置。结束后停表，完整静态 Banner 随宿主进入终端历史，Resize 由原机制重排。关闭 **Status Line Motion**／`tui.motion: false`、`REDUCED_MOTION=1`、`PREFERS_REDUCED_MOTION=reduce`、非 TTY 或无色模式时静态显示。TrueColor 使用 RGB，有限色终端量化至 xterm 256 色；`NO_COLOR`、`FORCE_COLOR=0`、`TERM=dumb` 不输出 Banner ANSI 样式。

验证入口（隔离用户目录与本地 loopback 模型，无付费请求）：

```bash
bun test tests/paveway-banner.test.mjs
PAVEWAY_TEST_NATIVE=1 .venv/bin/python -m unittest discover -s tests -p test_native_banner.py -v
```

PTY 检查首帧不含原 OMP Logo、真实版本与 Workflow、静态／Reduced Motion／有限色模式、120→40→80→24 列 Resize、原生模型切换提示、后续流式回复及草稿保留；恢复已有历史时另检查实际动画颜色变化、动画禁用后的完整亮色帧、光点右端位置、历史不重发，以及 `/new` 后的静态帧。可设置 `PAVEWAY_STATUSLINE_EVIDENCE=/tmp/paveway-banner-evidence` 保存真实终端单元格和 ANSI。单元格重建图可检查比例、渐变、居中与留白，但不证明 Windows Terminal 的具体字体栅格效果；块字边缘、256 色阶和终端是否支持斜体仍受终端字体／配色限制。Banner 不依赖 Nerd Font、字体资源、图片或 SVG。

## 对话与输入区域

Paveway 只调整原生消息的呈现与输入 chrome，不替换 Transcript、Markdown 或编辑器。用户消息使用紧凑的细紫色强调线；GPT 消息保留透明背景及原生 Markdown，以低调的 `GPT` 标识区分角色。普通消息之外的 Tool Calls、自定义事件、Recap、压缩摘要继续走 OMP 各自的渲染器，不按显示文字猜测角色。

消息由 `pi-tui/chat/user-message.ts`、`assistant-message.ts` 渲染。OMP 18.16.0 的 `registerMessageRenderer()` 仅针对自定义消息，`registerAssistantTextRenderer()` 是正文替换接口，不适合添加稳定的消息级角色标记。因此随包 `conversation.patch` 增加显式启用的 `ui.setConversationPresentation()`，在原生渲染器内部选择紧凑样式，并保留 Assistant 的流式稳定前缀账本；未设置时使用 OMP 原样行为。

输入区通过公开 `registerComposerStyle()` 注册 Paveway chrome，由公开 `setEditorComponent()` 创建原生 `CustomEditor` 子类。形状元数据参与原生内容宽度、光标、IME 标记和高度预算，不把编辑结果事后套框或位移。`composer.patch` 仅补充 `CustomEditor.composerStyle`，让宿主同步偏好时保留工厂明确选择的形状；普通编辑器不受影响。输入、粘贴、历史、补全、Unicode 编辑、滚动与键盘分发仍属于 OMP。

配色限定在本次两个区域：`paveway-presentation.mjs` 提供品牌紫、边界紫灰、次要灰和正文浅灰白语义色，复用状态栏的品牌值；不改全局主题或青色运行状态。TrueColor 使用 RGB，256 色使用校准色号；`NO_COLOR`、`FORCE_COLOR=0`、`TERM=dumb` 时新增呈现不输出颜色。标记不要求 Nerd Font。输入提示只占用可用空白，窄屏、低高度或无足够空间时隐藏；快捷键取自宿主绑定，而不是写死默认值。

补丁按 `statusline.patch`、`conversation.patch`、`composer.patch`、`banner.patch` 顺序应用。安装缓存由全部补丁的组合哈希确定；升级建立新的隔离源码目录，不覆写已运行的缓存、全局 OMP 或后台 Worker。保存草稿、退出并重新进入原生 UI 后生效。

验证入口（仅使用隔离用户目录和本地 loopback 模型，不发送付费请求）：

```bash
PAVEWAY_TEST_NATIVE=1 .venv/bin/python -m unittest discover -s tests -p test_native_conversation.py -v
```

真实终端前端的中文 IME 预编辑／候选窗、鼠标选区和具体 Windows Terminal 字体栅格效果不能由 PTY 或渲染单测证明；原生编辑路径与 IME 光标标记保留，不把模拟按键通过写成这些体验的人工验收。

实际 PTY 截图、复现命令与验收边界见 [对话与 Composer 验收记录](verification/conversation-composer/verification.md)。

## Windline 统一状态栏

输入框下方只有一条状态栏。左右组件使用相同的独立 Bubble：每个胶囊都有自己的左右圆帽、实色背景和内边距，胶囊之间留一列状态栏背景，不再连接成 Powerline 色带。左组为 PAVEWAY、Profile／模型／Thinking 身份、Profile/task；右组为有信息的 Wakeup、非零 Inbox、低对比度会话费用及青色上下文胶囊。无事件与零未确认消息不常驻；工作目录、Git 分支和重复的 `OMP native` 也不常驻，原生路径／Git 功能没有删除。

配色沿用紫色品牌、蓝色身份、蓝灰 Workflow 和青色容量胶囊。Context 由连续实心已用区和固定位置的 Chevron 未用区组成；工作时只让箭头前景亮度随既有 OMP 125ms 相位向右流动，16 帧／2 秒一个周期，首尾亮度连续。箭头背景、占用填充、标签与圆帽不随动画改变；True Color 与校准的 ANSI-256 色号使用同一相位模型。只剩 1–2 格、占用未知、空闲或关闭动画时，仍显示完整静态箭头。布局与其他胶囊跨帧缓存，上下文数据、符号或宽度改变立即重算；不新增定时器或后台任务。空闲、取消、切换会话和退出沿用宿主停表／复位机制；在 OMP `/settings` 的外观设置关闭 **Status Line Motion**，或在 OMP 配置中设置：

```yaml
tui:
  motion: false
```

模型显示名（缺失时用 ID）、费用、上下文估算和压缩边界均由 OMP StatusLine 提供，不在 Python 或 Paveway 扩展中另计。占用未知显示 `?%`，容量未知显示 `?`。160 列有余量时仍显示完整 `auto… (spec…)` 边界说明。容量条按预算使用 4／8／16 格；0% 全部为箭头，100% 全部为实心，非零且未满时保留至少一格已用和未用区。更窄时先省装饰，保留百分比与能容纳的窗口信息，模型名为数值预留宽度。

Context 使用 Dual-limit Rail：自动压缩明确开启时，未占用格依次使用 Normal 深青 `#203f49`、Spec 蓝绿 `#30545b`、Auto 琥珀棕 `#574939`；背景不随光流变化，Chevron 不增加、不分隔、不加下划线或粗体。区间起点为 `ceil(格数 × 阈值 / 100)`，8 格、75%／85% 时为 6 格 Normal、1 格 Spec、1 格 Auto；4 格时 Auto 起点在条外，不强行搬入最后一格。相同边界由 Auto 优先；逆序时忽略 Spec，保留 Auto；缺失或非有限阈值不生成对应区间或警告，关闭自动压缩则不显示色带。100% 阈值没有条内区间。已用格始终统一青色填充，不被色带切开；数值标签按真实占用率依次使用青色、柔和提示色、Warning，未知占用不报警。无色模式保留原字符布局，不以额外符号模拟阈值。

预算包含圆帽、内边距和间隔；异常及非零 Inbox 优先于模型、上下文、品牌、Workflow 和费用。80 列省去 Workflow，身份细节按余量恢复；长字段按终端显示列宽缩短，整行不截断完整胶囊。默认 `auto` 沿用原有胶囊契约：Nerd 与 Unicode 均使用 Powerline 圆帽 ``／``，Context 未用区使用空心 Chevron ``（U+E0B1）；仅 ASCII 默认使用括号和 `>`。现有显式 `powerline` 风格复用同一 Chevron 实现（ASCII／`TERM=dumb` 的箭头仍为 `>`），`plain` 使用括号和 `>`；`TERM=dumb` 禁用颜色并使用 ASCII 仪表文字，不改变圆帽风格选择。OMP 本身没有名为 `powerline` 的 `symbolPreset`。圆帽前景与自身胶囊背景一致，外部使用终端默认背景；胶囊内部始终有明确背景，深色与浅色主题下均不依赖默认背景维持箭头对比。圆帽或箭头不是单列时分别退回括号或 `>`；宽度检查只用于布局预算，不检测字体覆盖率。支持 True Color、256 色、`NO_COLOR`／无色模式；256 色保留语义色与前景光流层次。无色已用区用连续 `━`（ASCII 为 `=`），未用区保留箭头，不输出 ANSI 动画或压缩边界样式。

显式覆盖圆帽风格可在 Paveway 配置中设置：

```toml
[statusline]
# auto: Nerd/Unicode 用 Powerline 圆帽与 ，ASCII 用括号与 >
# powerline / plain: 强制对应风格
cap_style = "auto"
```

`PAVEWAY_STATUSLINE_CAPS=powerline|plain` 提供一次性的显式环境覆盖，优先于配置文件；两者都只表达用户选择，不做终端品牌或字体探测。

Chevron 验证入口：

```bash
bun test tests/paveway-statusline.test.mjs
PAVEWAY_TEST_NATIVE=1 .venv/bin/python -m unittest discover -s tests -p test_native_statusline.py -v
```

单测宿主默认使用 Unicode，并显式覆盖 Nerd／ASCII、配置与环境覆盖的优先级，以及非 Context 胶囊跨相位的字符、颜色和位置。原生 PTY 的 `test_unicode_auto_restores_powerline_caps_and_chevrons` 使用 Unicode + `auto`，检查 TrueColor、ANSI-256、无色模式和 80／100／120／160 列的圆帽、Chevron 与默认外部背景；光流矩阵也使用 Unicode + `auto`，避免显式 `powerline` 掩盖默认行为回归。

PTY 验证字符单元格、颜色、动画生命周期与 resize；当前 [Dual-limit Rail 验收](verification/dual-limit-rail/verification.md) 包含真实 PTY 的 Nerd Font 重绘、Windows Terminal 独立窗口回放截图及复现命令。截图不证明其他字体、行高或回退字体的像素连续性。旧 [Context 波浪验收](verification/windline-statusline/context-wave/verification.md) 只记录已替换的表现，不是 Chevron 的视觉验收。


### 可复现的宿主集成

原生入口在获取前台所有权之前准备隔离的 **18.16.0** 宿主（commit `650f3a108229ee11bf68ba3377f4e8c3391cd424`），不检查或限制系统／配置中 OMP 的版本。该版本原来的 `setFooter()` 是 no-op；随包补丁增加 `ui.setStatusLine(renderer)`，直接挂入现有 StatusLine；对话与输入外观由上面的独立 opt-in 接口提供，不替换 Transcript、Scrollback、编辑逻辑或鼠标滚轮。OMP 自身在 Resize／全屏选择器中短暂借用 Alternate Screen 的行为保留，不强制常驻 Alternate Screen。

安装器、补丁和启动器随 Python 包一起分发：`bundled/omp/{install.py,statusline.patch,conversation.patch,composer.patch,banner.patch,omp}`。Banner 的前台预加载模块和字符渲染器位于 `bundled/paveway-banner{,-host}.mjs`。前台运行补丁后的源码宿主；后台 Worker 仍运行配置中的原始 OMP。不会覆写 `~/.local/bin/omp`，也不会把用户设置、认证、会话或代理值复制进源码缓存。

首次准备需要 Git、网络和 Linux x86-64 glibc；下载固定 Bun／原生插件并校验 SHA-512，JS 依赖使用上游冻结锁文件和独立依赖缓存。缓存默认在 `~/.cache/paveway/omp/18.16.0`，可用 `PAVEWAY_OMP_CACHE` 指定。后续启动离线复用已准备宿主。可以提前准备：

```bash
.venv/bin/python packaging/omp/install.py
# 已有精确版本 checkout 时，导出固定 commit，而不是复制工作树修改：
.venv/bin/python packaging/omp/install.py --source /path/to/oh-my-pi
packaging/omp/omp --version
```

版本固定仅适用于隔离 Host，不是系统／后台 Worker 的版本要求；补丁不会应用到配置的 OMP。隔离 Host 准备失败或启动器无效时，在取得会话所有权之前明确拒绝启动。Runtime 协议、Build ID、Session 所有权及启动身份检查保持不变。旧集成记录见 [Windline 验收](verification/windline-statusline.md)，独立胶囊的真实 PTY 图片与复现见 [Bubble 验收](verification/windline-statusline/bubble-verification.md)。

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

`/paveway skills` 的 **Import standard SKILL.md / ZIP · local** 直接解析标准 YAML frontmatter（name、description）及 Markdown 正文，不调用模型。多 Skill ZIP 展示每项名称、同名冲突和启停状态；可多选、全选、查看正文、切换启停，最后确认整批导入。已有同名项默认保留原启停状态。一次 revision CAS 原子写入所选项；并发修改会拒绝整批，需要重新预览。取消不会写入，正文及换行保持原样。包内 references、脚本等资源不会安装，确认界面会提示；需要整理引用或总结非标准输入时继续使用 **Import from file · GPT analysis**。

CLI 预览入口：

```bash
oicoach-runtime workflow customize skill-local-preview WORKFLOW --file /path/to/package.zip --config /absolute/config.toml --json
```

预览只读。批量保存使用 `workflow customize skills-import WORKFLOW --expected-revision N --content-file /private/batch.json`，batch 为 `{ "skills": [已选择的完整 Skill 记录], "replace": [已确认替换的同名名称] }`。每项包含 name、description、content、category、enabled；不要包含预览的 source/conflict 字段。输入限额沿用既有导入机制：源文件 1 MiB、解包 64 项／256 KiB，每项正文及描述各 64 KiB，所有 Skills 最多 32 项。ZIP 不解压到磁盘，不执行任何内容。

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
