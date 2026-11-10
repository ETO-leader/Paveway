# 提醒、同步、Session 与多 Skill 直导验收

本页记录上一轮验收。后续已将 reminder 改为不调用模型的纯提醒，命令使用 `/reminder`；`/wakeup` 只执行 Agent Wake。最新补充审计见 [归档可观察性与提醒类型](archive-reminder-audit.md)。

基线：`436785a`，开工时 HEAD 即该提交，工作区干净。日期：2026-11-10。先阅读 AGENTS.md 与 `.codex/skills/SKILL.md`，再检查各条实际调用链。所有复现使用临时目录、独立数据仓库、本地 provider 或私有 D-Bus；未改真实用户会话、未重启正式 daemon、未 commit/push。

## 基线复现与结论

将 `git archive 436785a` 导出到临时目录，复制新增回归测试后，在基线源码上运行：

| 组 | 确定性用例及基线结果 | 结论 |
|---|---|---|
| 1 | Runtime 实际 IPC：一个 hold Run 正在执行，桌面关闭，另一个 reminder 已到期；Inbox 期望 1，实际 0 | 到期投递仍耦合 dispatcher 领取 |
| 1 | 第一次 reminder 已读，改期后再到期；期望 2 条，实际 1 条 | Inbox 旧身份只含 event/kind/run，未区分到期 |
| 1 | 真实私有 D-Bus 尚无通知服务，到期第一次发送得到 unavailable/desktop_rejected，期望 retry_wait | 服务就绪故障与拒绝混为一谈，按已尝试去重后无法补发 |
| 2 | 本地裸仓库发布 format version 999，接收端同步报错但 HEAD 已改变 | 格式/配置语义验证晚于快进 |
| 3 | 实际 Unix IPC 执行 finish，慢扫描 fixture 读取 `connection.in_transaction` 为 True | 全文件扫描在写事务内且阻塞事件循环 |
| 4 | 调用 `workflow customize skill-local-preview`，argparse 报不存在（退出 2） | 只有 GPT 分析入口，没有多项本地直导 |

基线中的大型 Session 流式验证、Guardian 独占 owner.lock、Ctrl+D 回收和失败启动撤销已存在，本轮保留并跑真实 Native 回归，不重复改写 `child.py` / `native_terminal.py`。本轮 Session 改动集中于扫描的事务边界、IPC 调度与提交时持锁。

## 第一组：到期、改期、Ubuntu 登录时序

修改：`inbox.py`、`scheduler.py`、`store.py`、`notifications.py`、`bundled/paveway-management.mjs`。

- 现有调度循环等待 Run 时仍按 poll interval 投递到期 Inbox，不依赖模型领取；通知 worker 也复用同一投递逻辑。关闭桌面不关闭 Inbox。到期提醒、Run 结果及已读记录独立。
- Inbox 与桌面共同使用规范化的 event ID + due 的 occurrence 身份。重复轮询、worker 重启与改回同一个到期时刻不会重复发送；新到期时刻生成新未读消息，保留旧已读记录。
- 允许已成功完成的明确 reminder 改期，保留原 Run；不借此重新执行 uncertain/失败请求。
- 旧 Inbox/desktop-attempt/history 兼容去重，并保留原 delivery ID、已读状态及版本。缺少原始到期证据的旧已改期 Inbox 无法还原时刻；保留它并为当前到期投递，不将新消息误标已读。
- 用通知子进程的同一环境执行真实 gdbus 探测。仅在 Notify 之前可确认未发送的总线／服务就绪故障有限重试；首次尝试起 120 秒，2/4/8/16/30 秒退避并跨重启保存。发送超时、发送后断链或中断保留 unknown，不自动重发。

复现/回归命令：

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest -v \
  test_runtime.RuntimeTests.test_inbox_due_projection_does_not_wait_for_busy_model_or_desktop \
  test_notifications
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest -v \
  test_native_recovery.NativeRecoveryTests.test_completed_read_reminder_can_be_rescheduled_as_new_unread_occurrence \
  test_native_management.NativeManagementTests.test_due_inbox_notice_preserves_draft_and_does_not_ack_or_replay \
  test_native_management.NativeManagementTests.test_desktop_notification_failure_is_visible_without_a_model_turn
PAVEWAY_TEST_SYSTEMD=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_notifications_dbus -v
```

`test_notifications_dbus` 使用真实 notify-send 0.8.3、gdbus、私有 dbus-daemon，以及系统 Python Gio 协议服务。所需包：`dbus-daemon libnotify-bin libglib2.0-bin python3-gi`。它不伪造 notify-send 返回值，也不显示真实桌面横幅。

| 验收层 | 本轮结果 |
|---|---|
| 自动化：忙 Run、桌面关闭、Inbox 一次投递 | 通过，真实 Runtime IPC 与受控 Agent 子进程 |
| 自动化：已读改期、旧记录升级、同次去重 | 通过；当前命令为 `/reminder reschedule`，后续也重跑验证 |
| 真实 D-Bus：到期无服务，随后上线 | 通过；先 retry_wait，后接受一次 Notify |
| 真实 D-Bus：服务一直不可用 | 通过；窗口后停止，Inbox 保留 |
| 真实 D-Bus：Notify 超时未知 | 通过；真实请求已到协议服务，8 秒超时；重建 worker 不重发 |
| 真实 D-Bus：总线不可达／通知明确拒绝 | 通过，诊断不同 |
| 真实 systemd 用户单元 | 通过；临时单元实际 enable/start，先无通知服务、后上线，两次 restart 仍一次投递；finally disable/remove |
| 桌面可见横幅：Xfce | 用户此前报告已看到，本轮未重验 |
| 桌面可见横幅：默认 GNOME | 未验收，本机 WSLg 没有 GNOME 会话／通知服务器 |
| GNOME 实际注销／登录后的自启 | 未验收；临时启用单元测试不能代替真实登录 |

GNOME 实机复验（在目标用户的 GNOME 终端运行；`PAVEWAY_CONFIG` 指向待验收配置）：

```bash
gdbus call --session --dest org.freedesktop.Notifications \
  --object-path /org/freedesktop/Notifications \
  --method org.freedesktop.Notifications.GetServerInformation
oicoach-runtime notifications test --backend linux --config "$PAVEWAY_CONFIG" --json
oicoach-runtime notifications enable --backend linux --config "$PAVEWAY_CONFIG"
oicoach-runtime notifications install --now --config "$PAVEWAY_CONFIG"
systemctl --user is-enabled paveway-notifications.service
systemctl --user status paveway-notifications.service
```

若已有本项目拥有的 unit 需要更新，用 `notifications install --replace --now`。检查 GNOME 通知设置、关闭勿扰，观察实际横幅。用 Native `/reminder in 2m GNOME 登录到期验收` 排程后注销并在到期前登录；检查横幅、`/inbox` 与 `journalctl --user -u paveway-notifications.service -b`，记录到期、服务就绪、accepted 的时间。无法保证服务在登录后才就绪时，私有 D-Bus 测试覆盖确定性时序，实屏测试记录实际时序。不要启用 linger 代替图形会话测试。

GNOME 控制横幅时长，expire-time 是提示。accepted 只表示通知服务接受请求，不证明用户看见或读过。详见 [通知说明](../desktop-notifications.md)、[协议](https://specifications.freedesktop.org/notification/latest/protocol.html)、[GNOME 通知设置](https://help.gnome.org/gnome-help/shell-notifications.html)。

## 第二组：Git 先验证候选，再更新受管分支

修改：`git_sync.py`，复用原配置记录校验器验证远端 blob；格式与配置验证移到 fetch 后、merge/ff 前。检查 format.json 存在、JSON 与精确格式版本，profile 路径/内容哈希、schema、provenance、父记录及字段限制。错误保留具体原因。受管 HEAD 不随不兼容候选移动；本地正常导出形成的提交仍保留。

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_git_sync -v
```

16 项通过。真实本地裸仓库覆盖 version 999、布尔值冒充版本、损坏配置记录、分叉本地提交。拒绝时 HEAD 等于最后有效本地提交；修正远端后同步成功，原本地提交仍为祖先。另通过实际 `serve` 自动同步入口：10 秒间隔，首次拒绝，发布修正后下一周期自动成功，没有人工 sync.request。

## 第三组：Session 扫描、锁与短事务

修改：`frontend.py`、`ipc.py`、`scheduler.py`。

finish/recover 复用一条释放路径：快照 owner/generation/token 与 Session 身份；持物理锁在写事务外完整验证 JSONL；短事务中重读 owner/generation/token/turn、核对进程与文件 dev/inode/size/mtime/ctime/mode/uid，提交后才释放锁。IPC 仅将 finish/recover 放到工作线程，各自使用独立 Store 连接，保留既有状态模型。异常退出仍 uncertain，不重发请求。

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_release_concurrency -v
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_native_recovery -v
```

慢扫描 fixture 通过真实 Unix IPC 验证 finish/recover：扫描不在写事务中，另一 task 的 memory.put/show 在 1 秒内完成；扫描期间 owner.lock 获取失败，SQLite COMMIT 回调仍观测到物理锁持有。受控 generation/token 更换不能被旧 owner 释放；扫描期间文件变化 fail-closed。真实 OMP/PTY 回归还覆盖 Ctrl+D 连续恢复、超过 16 MiB 的合法 Session、锁冲突、Guardian/OMP/launcher SIGKILL、setsid 后代清理及无请求重放。

## 第四组：多 Skill ZIP 本地直导

修改：`skill_import.py`、`customization.py`、`cli.py`、`bundled/paveway-skill-import.mjs`、`bundled/paveway-management.mjs`。

新菜单在 `/paveway skills`：**Import standard SKILL.md / ZIP · local**。逐项解析标准 frontmatter 的 name/description，正文原样保存；展示多选、同名替换、启停状态及正文预览。最终确认后单次 revision CAS、一次原子配置写入；版本变化或任意项不合法则整批失败。GPT 分析/编辑草稿入口保留。

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_skill_import -v
bun test tests/paveway-skill-import.test.mjs
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest -v \
  test_native_management.NativeManagementTests.test_local_multi_skill_zip_selects_two_with_conflict_and_no_model_request \
  test_native_management.NativeManagementTests.test_local_multi_skill_zip_select_all_then_cancel_writes_nothing \
  test_native_management.NativeManagementTests.test_skill_file_import_analyzes_reviews_and_saves_without_chat_history
```

通过：CLI 八项全选一次 revision、同名未确认拒绝、旧 revision 拒绝且零部分写入；真实 Native 菜单八项选二、替换保留停用状态、UTF-8/CRLF 正文一致、全选后取消无写入，直导零模型请求。JS 另覆盖切换启停、拒绝确认和保存冲突。`tests/multi_skill_fixture.py` 明确为合成八项包，未冒充实际 Paveway-missing-skills.zip；用户已要求先不等原包。

直导不安装 references/scripts 等额外资源，确认时提示，保持正文原样；需要整理资源引用时使用既有 GPT 分析入口。源与单项保存限额见 [Native UI](../native-ui.md)。

## 总回归

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests
bun test tests/*.test.mjs
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_native_omp test_native_management test_native_recovery -v
PAVEWAY_TEST_SYSTEMD=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_notifications_dbus -v
git diff --check
```

真实 Native 覆盖 46 项：首次全跑 45 项通过；原弹窗失败提示 fixture 只伪造 notify-send 退出码，补齐就绪探测 fixture 与明确拒绝信息后，该项单独重跑通过。真实通知协议行为由独立私有 D-Bus 测试验证，不由这个 UI fixture 代替。Bun 全跑 142 项，130 通过、12 跳过、0 失败；私有 D-Bus 含真实临时 systemd 单元 5 项全部通过。

最终普通 Python 发现测试运行 521 项，423 通过、98 按现有 opt-in/环境条件跳过，0 失败（159.980 秒）。Git 单组 16 项、notifications/release-concurrency/skill-import 联合 24 项也单独通过。`git diff --check` 通过；临时通知服务没有残留 loaded unit 或 unit file。

CI 已增加 D-Bus 依赖安装及两项真实 Native 直导用例，systemd 用户单元测试保持显式 opt-in，不能静默声称 GitHub runner 有用户管理器。
