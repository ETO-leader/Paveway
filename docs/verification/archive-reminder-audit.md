# Session 归档可观察性与 Reminder / Agent Wake 审计

日期：2026-11-10。HEAD `436785a`，工作区保留上一轮尚未提交的四组改动。本次不 commit/push，不改真实用户会话，不重启正式服务。

## 已修复与本次缺口

上一轮已经解耦 Native 生命周期验证与 16 MiB 文本导出上限，并验证 Ctrl+D／恢复、写事务外扫描、提交时持锁；Inbox 也已支持独立到期投递、按 event+due 去重和改期保留旧已读记录。本次保留这些机制。

两个剩余问题均成立：

1. `export_history()` 捕获导出异常后 continue，`cycle()` 只报告 Run 数和 Git 结果。活动、变化、过大或不支持格式的 Session 都不可见。真实触发条件是存在这些 Session；即使 Git push 成功，部分会话文字仍未归档。
2. `/wakeup` 固定创建 intent=reminder，但 claim_due/dispatch_event 对两种 intent 没有分流。普通提醒也可能启动模型，或者因为缺少模型快照被 blocked；它也要等待模型执行槽位。

修改前针对性复现日志：`/tmp/paveway-intents-archive-before.log`。两项用例分别得到 native_wakeup_snapshot_missing／blocked 和缺失 session_archives 字段。测试使用隔离 Runtime、SQLite 与裸 Git 仓库，没有私人数据。

## 最小实现

### Git Sync

修改 `git_sync.py`、`errors.py`、`bundled/paveway-management.mjs`、`tui.py`。

- export_history 返回原有 Run 数及轻量 session_archives 汇总：exported、skipped、deferred、attention、reasons。
- active/changed 为正常暂缓；size_limit、entry_limit、missing、unavailable、unsafe、invalid_or_unsupported、owner_unreleased 为需关注原因。只输出固定原因码与计数，没有正文、凭据和敏感路径。
- 失败后也重新核对 owner/活动 Run，避免一次正常启动竞态被当作损坏；跳过时不覆盖旧有效归档。
- cycle 在本机已有 status.json 中保存统计，Git 后续网络失败也保留本次已完成的导出统计。state=ok 与 remote_synced 仍表示同步结果，不改成全量备份成功。
- Native Sync Status 显示成功、暂缓、需关注及“不完整”；旧 TUI 的状态与同步完成标题也标明不完整。
- 不改变 Git 文件格式、离线提交、合并／冲突或 push 语义。16 MiB 仍只约束文字导出；生命周期 validate_session 不使用该总大小上限。

### Reminder / Agent Wake

修改 `bundled/paveway-wakeup.mjs`、`wakeup.py`、`store.py`、`inbox.py`、`scheduler.py`、`bundled/core.md`；通知 worker 继续使用既有 reminder 筛选及 occurrence 去重。

- `/reminder` 固定创建 reminder；`/wakeup` 固定创建 agent_wake，没有旧提醒别名。共用解析、工具、队列、作用域、owner/generation、CAS 与去重。
- list 按 intent 在查询 limit 前过滤，并返回 version/intent；cancel/reschedule 传 intent，服务端拒绝跨类型操作。工具仍只有原 paveway_wakeup_* 一组，可选 intent 过滤／类型保护。
- Runtime 在现有 schedule_loop 中调用 project_due(complete=True)。单个事务写 Inbox 并将 queued reminder 标为 succeeded，保存完成时间与 reminder_delivered 审计；不创建 Run、租约、Session 或 Agent 完成证据。
- claim_due 从模型队列领取时排除这种纯提醒。长 Agent Run 等待循环也照常结算提醒，不受模型熔断或通知失败影响。
- 独立通知 worker 只做幂等 Inbox 投递／弹窗，不承担事件模型执行或 Runtime 结算；它可以早于、晚于或并发于 Runtime，均复用同一 occurrence。
- reminder 不依赖模型快照；agent_wake 的固定配置、Workflow、Memory、物理绑定、RPC 结果与 settled 证据完全保留。用户说“提醒我”默认 reminder，明确未来 AI 工作才 agent_wake，授权／opt-in 与禁止无限循环保留。
- 旧 queued reminder 按实际 intent 走本地结算；已完成事件不重放。旧 uncertain 模型执行仍需人工核查，不自动结算或重发。没有 native_wakeup 或 intent 的普通 schedule/after_turn 保持原契约。

没有新增数据库表、任务系统、迁移层、重试队列或服务，直接在现有事务与调度边界完成分流，符合 HERO。终态编辑仍复用 Store.modify：已成功提醒可改期，Agent Wake 不放宽原终态限制。

## 回归与证据

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_wakeup test_notifications test_runtime test_git_sync \
  test_store test_frontend test_release_concurrency -v
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -v
bun test tests/*.test.mjs
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_native_omp test_native_recovery \
  test_native_management.NativeManagementTests.test_due_inbox_notice_preserves_draft_and_does_not_ack_or_replay \
  test_native_capabilities.NativeCapabilityTests.test_training_checkpoint_reads_latest_task_note_after_native_exit -v
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_native_management.NativeManagementTests.test_sync_status_shows_incomplete_history_without_model_request -v
git diff --check
```

实际验证：

- Git 实际裸仓库：先导出小 Session，再扩展为有效 >16 MiB JSONL；释放、恢复可用，size_limit 明确计数，旧备份字节不变，其他配置成功推送到另一设备。活动 owner 仅 deferred；不支持版本计 attention；扫描中真实追加文件计 changed，不覆盖旧备份。
- Runtime 实际 CLI/IPC 与可观察 fake_omp 子进程：纯 reminder 到期、重启、重复提交后只有一条 Inbox、无 Run，fake-starts 文件不存在（启动次数零）。另有普通 intent=reminder 但无 native_wakeup 的任务，仍真正启动一次 Agent。
- 真实通知 worker 进程先投递，Runtime 后结算；下一事件两进程并发处理，最终两事件均 succeeded、两条 Inbox、两次通知、Run 数零。未启用桌面时同样完成；通知失败仍保留 Inbox。
- 本地事务故障注入：Inbox 写入与完成之间失败，二者一起回滚；再次处理只写一次，不留下永久 running。
- JS：两命令的相对时间、RFC3339、系统时区 HH:mm，列表过滤及取消／重排类型参数。
- 真实 OMP/PTY＋本地 provider：/reminder 到期不创建后台 Session；/wakeup 确实后台调用并在 UI 退出／daemon 重启后完成，固定快照与 Memory scope 保留。取消/CAS/旧 generation 与异常未知结果不盲目重试回归保留。
- 真实 Native Sync Status 菜单读取 CLI 状态并显示“不完整”和 size_limit，无模型请求。

全量 Python 526 项：427 通过、99 按既有环境／opt-in 跳过，0 失败（161.314 秒）；随后追加的普通 intent 兼容用例单独通过。Bun 143 项：131 通过、12 跳过、0 失败。早期核心组 116 项、追加事务／worker 组 24 项、归档原因／权限组 10 项均通过。真实 OMP 27 项全部通过，Sync Status UI 单项通过。日志分别位于 `/tmp/paveway-intents-full.log`、`/tmp/paveway-intents-bun-final.log`、`/tmp/paveway-intents-core.log`、`/tmp/paveway-intents-atomic-worker.log`、`/tmp/paveway-archive-reasons-final.log`、`/tmp/paveway-intents-native.log`、`/tmp/paveway-archive-native-ui.log`。

## 明确限制

归档统计是本次本机候选 Session 的计数，不列出正文或路径，不意味着远端所有设备的备份清单。跳过不会自动分片、压缩或无限重试大型归档；下一次普通同步按原机制再观察。历史 valid 导出保留为旧版本，不能声称是最新完整备份。

本轮没有 GNOME 实屏／注销登录复验，也未调用付费模型；使用真实 OMP 与本地 loopback provider 验证执行链。此前独立 D-Bus 与桌面验收边界见 [通知说明](../desktop-notifications.md)。部署时旧 daemon 不会热加载 Python：应在空闲时由用户重启 Runtime，再重新进入 UI；不会为升级重放 blocked/uncertain 事件。
