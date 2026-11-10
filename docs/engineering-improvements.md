# 唤醒、诊断与记忆交接

本轮更新沿用现有 SQLite schema、去重键、CAS、会话所有权与恢复命令，没有新增业务队列或迁移用户笔记。

## 原子 Native 唤醒

`wakeup.schedule` 在同一个写事务内重新认证 owner、校验原生配置快照、写入事件、审计和冻结配置。任一步失败整笔回滚；同语义、同去重键重试只返回原事件，保留第一次成功保存的快照。修改参数仍产生 conflict，修改时间或取消仍需事件版本。

历史事件缺少快照时返回 `native_wakeup_snapshot_missing`，不拿当前 Prompt/Skills 重建历史配置、不发送模型请求。先检查事件，尚未执行时可取消，再按明确授权创建新请求。blocked/uncertain 事件应先核对执行证据和副作用，不自动重播。

## 诊断与恢复

`oicoach-runtime status --json` 保留原字段，增加 `diagnostics`。最近最多 100 个失败、阻塞或结果不明事件按 binding 聚合；未绑定事件按 workflow/task 聚合。每项保留 event/run ID、状态、错误码、dispatch intent、结果是否未知和下一步指引，不合并或删除原事件。

诊断区分 configuration、binding、host_startup、model_authentication、memory、persistence、dispatch/pre_dispatch。输出不包含事件正文、Memory 正文、Provider 原始错误、认证或 owner token。`automatic_replay: false` 表示诊断本身不触发重播，不改变既有派发前启动退避机制。

前台仍需验证进程身份和物理锁后使用 `frontend recover`；后台先查 `recover list`、事件和 Run 证据，再决定显式恢复。需要继续工作时可以另开 task，保留旧会话。

## Memory 写入与故障

Native `paveway_memory_put` 新增可选 `expires_at` 和 `source_event_id`，与 CLI、后台共用 `Memory.put()`。过期时间需带时区且在未来；事件来源按原有 scope 校验。来源关联不等于证明用户说过某句话或提交已核验。

新调用应显式指定 `scope: "profile"` 或 `scope: "task"`；旧调用仍默认 profile。Task 笔记使用逻辑 task，Native 唤醒的独立物理 Session 不改变其作用域。更新使用版本 CAS，删除保留墓碑。更改、过期和删除在下一轮顶层请求准备时增量刷新，不改写系统前缀或磁盘历史。

Recall 新增 `availability: available|empty|expired` 和过期计数。界面区分没有有效笔记、过期和服务不可用。加载失败标记 `memory_recall_failed`，投影失败标记 `memory_projection_failed`。RPC 传递固定协议诊断：派发前读取失败阻塞且没有 dispatch intent；派发后出错保留未知执行结果。

原生 UI 提供显式、仅当前进程有效的降级命令：

```text
/paveway-memory-context status
/paveway-memory-context off
/paveway-memory-context on
```

等待当前活动结束再操作。`off` 从下一轮撤回旧 Memory 上下文，不再召回，数据库笔记保留；请手动补充当前状态。`on` 从下一轮重新读取。该入口不绕过 Runtime 所有权或模型认证，Runtime 整体离线时仍不能启动受管会话。默认加载失败导致 UI 退出时，修复后重开；服务可用而需无记忆续聊时，可在重开后的首次提问前显式关闭。

## 训练交接验收

`test_training_checkpoint_reads_latest_task_note_after_native_exit` 使用锁定宿主、真实 PTY、隔离 Runtime 和本地 HTTP Provider，执行：CLI 双题计划 v1 → Native 工具更新 task 笔记 v2（含来源、有效期、用户报告用时、提示许可）→ 工具创建单次授权检查点 → UI 正常退出 → 后台到期读取 v2 → CLI 复盘 v3。检查点 reason 保存 ID 和授权范围，事件、Inbox 结果和已读状态分别核对。

这是工具和生命周期验收，不是实际选手训练效果或模型自主判断能力验收。完成状态标为用户报告，待核验题不宣称独立通过。实际训练仍由外部 workflow 根据当前计划区分换题、仍在推进、确认完成或未知；Runtime 不硬编码 OI 决策。

跨设备 Memory 导出、父版本合并和墓碑冲突是独立任务，本轮未把笔记加入用户数据 Git。

## 验证与升级

CI 保留 Python 3.11/3.12/3.13 基础矩阵，新增 Linux native 作业。固定宿主安装、前置条件、Bun 与原生 HTTP/PTY 验收分步骤执行；安装失败直接失败，缺少必需前置条件不记为通过。实际 GitHub 作业以远端结果为准。

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_wakeup test_memory test_prompt_cache_contract test_diagnostics -v
python3 packaging/omp/install.py
PATH="$PWD/packaging/omp:$PATH" PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests \
  .venv/bin/python -m unittest test_native_omp test_native_management test_native_recovery -v
```

没有 schema 迁移；旧笔记和旧调用继续可读。加载新代码前，正常退出原生 UI、等待后台活动结束，再重启 Runtime 并重开界面。不强行回收活动或不确定 owner。回退时保留数据并恢复上一版程序；冻结快照沿用原格式。
