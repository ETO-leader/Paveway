# 终端 UI（0.8.0）

`oicoach-runtime ui` 是现有 CLI 的全屏交互前端。实现位于 `src/oicoach_runtime/tui.py`，依赖标准库 curses/threading/subprocess；不增加第三方依赖、不新建 API、不直接读写 Runtime/Tijian 数据库。每个操作使用当前 Python/venv 执行 `-m oicoach_runtime ... --config PATH --json`，以 argv 传递参数，解析既有 JSON 结果与错误。模型仍通过独立 daemon 执行。

## 启动

```bash
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/oicoach-runtime ui --config "$PWD/.runtime/config.toml" --workflow coach --task training
# 尚未选择 workflow：
.venv/bin/oicoach-runtime ui --config "$PWD/.runtime/config.toml"
```

需要 Linux、带 curses 的 Python 3.11+、真实交互终端、有效 TERM 和 UTF-8 locale。最少 60×12，建议 100×30；缩放后重绘，过小时显示调整提示并可 q 退出。不支持非 TTY/TERM=dumb 或 --json；自动化继续使用独立 CLI。没有网页服务、图形窗口或浏览器依赖。

UI 不自动启动/停止 daemon。初次使用仍可按 README 的 init/serve 快速开始；也可在概览页按 i 创建默认配置，在 Workflow 页 n 选择 ID 和已存在的绝对 workspace，创建 coach。daemon 离线时错误会显示在状态行；如已安装归属于当前配置的 systemd unit，可在服务页按 b 启动。使用前景 serve 时，应在另一终端运行它。UI 初始化未自定义的 state/workflow 路径采用现有 init 的 XDG 默认，不凭配置文件所在目录猜测。

## 页面和按键

| 全局按键 | 行为 |
| --- | --- |
| 1–8 / Tab / Shift+Tab | 切页 |
| r | 刷新当前页 |
| s | 切 workflow/task；新 ID 建新任务，相同 ID 接续原会话 |
| j/k / 上下箭头 | 选择列表项 |
| Enter | 查看所选记录；对话页打开输入框；Workflow 页选用 |
| PgUp/PgDn / [/] | 滚动对话或详情 |
| > / < | 翻查询页；查看原文时翻原文页 |
| ? | 帮助；任意键关闭帮助 |
| q / Ctrl+C | 退出 UI，保留独立 daemon 和已提交事件 |

输入框支持 UTF-8 中文、多行、左右/Home/End、Backspace/Delete；Ctrl+U 清空，Esc 取消。单行 Enter 确认，多行 Enter 换行、Ctrl+D 确认。按键只在输入框外触发操作，所以消息中的 q/m/s 等普通字符不触发快捷键。没有外部编辑器/复杂 Markdown 编辑能力。

| 页 | 操作 |
| --- | --- |
| 1 概览 | daemon/队列状态；c 诊断；i 初始化配置（包括已知 DB 迁移） |
| 2 对话 | Enter/n 输入并提交，m 切 plan/checkin/review，r 恢复当前任务最近 5 个 run |
| 3 事件 | 所有任务的事件，Enter 查事件和回复；n 安排 at/now 请求；d 取消未派发事件（确认+版本检查） |
| 4 记忆 | 当前 workflow 的共享或当前 task 笔记；w 切 scope，n 创建，Enter 读取，e 修改，d 删除（确认+CAS），f 搜索 |
| 5 历史 | 当前 workflow 跨 task 的输入/回复搜索；f 字面搜索，Enter 查看 run，a 读所选 task 会话原文 |
| 6 Workflow | 浏览、Enter 选用、n 创建可编辑 coach；不覆盖已有文件 |
| 7 服务 | r 状态，o 安装，b 启动，x 确认后停止，l 读取日志；当前 UI 使用默认 unit 名 |
| 8 恢复 | 查看待核对事件及回复，不提供无确认的自动重试 |

事件/恢复查询沿用每页最多 100，记忆/历史 20；历史原文每页 20 个片段，a 后 >/< 翻原文，r 返回历史搜索模式。原文保留 all_branches/parent_id/has_more/truncated 标记，不把不同分支当同一执行路径；文件大小/内容限制沿用 [记忆接口](memory.md)。详情为格式化 JSON，方便核对状态与 ID。

记忆编辑包含标题、正文、kind、pinned 和有效期，保留已有来源事件，用读取到的 expected_version 保存。删除清空的是笔记，不擦除旧会话/备份。冲突时返回错误，不覆盖其他客户端的修改，r 刷新后再判断。新建笔记正文最多 4096 字节；对话最多 32 KiB，长正文不进入 argv。

## 对话与生命周期

UI 先用 ask 提交即时事件，得到 event_id 后每约 2 秒经 CLI 查询 events show，终态再读取 runs output。CLI 在工作线程执行，界面可以切页、缩放或退出。一次界面只跟踪一个待完成的请求；其他客户端及定时提醒仍由同一全局串行 daemon 调度。切换 task 不会把旧 task 的回复写进新 task 的显示记录。

只显示已保存的最终回复，不提供模型 token/工具调用实时流。旧 run、local-only 或没有最终回复时明确显示缺失，不冒充完成；回复超过 Runtime 的 16 KiB 限制会提示查询原文。自动提醒的回复和界面退出期间的结果可通过 r 刷新历史找回，当前没有额外消息推送。

每次新消息使用独立稳定 dedupe-key。若提交 CLI 断线/超时，前端不能断言未入库：保留原正文、模式、scope 和 key，阻止直接用新 key 再提交，按 v 用同一请求/key 幂等核对。只有 CLI 确认成功才显示“已持久提交”。z 经明确确认可丢弃本地记录，但不会取消可能已入库的事件；清除前应核对事件页。该临时提交记录只在本次 UI 内存中，退出后用事件/历史页查原队列。

UI 默认 CLI 调用预算 30 秒，超时不会自动重试写入；必要时 CLI 进程被回收，独立 daemon 不受影响。正常 q/Ctrl+C 退出会恢复 curses 终端状态、回收界面拥有的 CLI 进程组及临时文件，不发送 abort/cancel/stop 给 daemon。停止服务是另外的明确操作 x，可能使活动回合 uncertain；具体核对/恢复沿用 [操作手册](operations.md)。

正文临时目录 0700、文件 0600，操作完成清理；终端显示过滤 ESC/控制字符/双向控制符。界面不记录独立聊天日志，CLI/daemon 既有持久化规则不变。这不对同 UID 的工具建立沙箱，也不对终端历史录像或 SIGKILL 后文件残留提供安全擦除保证。

## 范围

当前是基于最终回复的终端客户端。自定义 systemd unit 名、启用/禁用/卸载服务、人工风险确认后的 retry、agent restart、Tijian 全部业务调用及模板编辑仍可用独立 CLI/外部文件完成；UI 不自动代办这些设置。Tijian 已配置的 workflow 继续由 OMP 工具使用，与用 CLI ask 时相同。配置/API/DB/workflow 版本不因 UI 改动，仍分别是 v1/v1/v2/v1。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p test_tui.py -v
# 可选，沿用已有 OMP 认证；实际调用一个模型回合：
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_tui_omp.py -v
```

专项测试覆盖中文输入、光标编辑/取消/帮助、终端控制符过滤、结果不明时同 key 核对、防过期页面结果、原文分页和记忆 scope/来源/CAS。真实 CLI 集成执行请求/笔记/冲突；PTY 模拟实际 curses 输入、缩放、退出及 termios 恢复，验证 UI 退出后独立 daemon 继续完成正在执行的请求。真实模型用例从 UI controller 经 CLI/daemon/OMP 取得最终回复并绘制；它与 PTY fixture 一起验证界面与产品路径，不宣称人工视觉验收或实时流支持。
