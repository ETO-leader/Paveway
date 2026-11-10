# 原生 OMP 退出与恢复审查

本次只审查 Native 生命周期，不改真实用户会话、不自动恢复或重放、不提交或推送。保留 released / starting / idle / active / uncertain 模型。

## 结论与复现

| 最初怀疑 | 结论 | 确定性证据 |
| --- | --- | --- |
| OMP 错误继承 owner.lock | 成立 | 真实锁定 OMP 的 `/proc/<pid>/fd` 中发现同一锁 inode；guardian 也持有它。不能据此声称所有工具后代都会继承该 FD。 |
| 锁冲突污染旧会话 | 成立 | 对正常 released 会话占用物理锁，再走 `_run_once`，失败后的 generation、配置键和状态发生改变；修复后真实 `paveway.sh` 失败不改变旧记录。 |
| 16 MiB 归档限制阻止退出和恢复 | 成立 | 17 MiB 合法 JSONL 的 finish(0) 返回 uncertain；原 recover 使用同一完整导出函数。 |
| 进程组清理漏掉后代 | 对改变 session/group 的工具成立 | 真实 Linux guardian 启动 `setsid()` 工具后正常退出，原清理保留工具；修复后的真实 OMP bash 用例覆盖 Ctrl+D 和 guardian SIGKILL。 |

普通无后代、未超限会话的 Ctrl+D 原有回归能够通过；没有把这些条件性缺陷描述为每次退出必然失败。

## 最小修复

- `native_terminal.py`、`child.py`：通信 FD 与锁 FD 分离。guardian 保留锁，OMP 只接收 attestation 通信 FD；后台 worker 不接收锁 FD。正常退出先等 OMP、guardian 与后代回收，再释放 launcher 的锁并提交 finish。
- `native.py`、`frontend.py`：复用同一 session-directory 计算，launcher 先取得物理锁，再提交 begin。拿不到锁不会创建新 generation，也不调用 finish(None)。begin 和 finish 仍由 generation/token 约束，旧 token 不能释放新 owner。
- `child.py`：专用 guardian 和 Native CLI 都是 subreaper，其直接或被收养的子进程属于本次进程树，即使 setsid 也按 pidfd 清理、waitpid 回收。共享 Runtime 仍使用原有显式 group 过滤；不扫描或终止系统无关进程。超时失败抛出异常，不向 launcher 所处的终端进程组广播 SIGKILL。
- guardian 接到 parent-death 的 SIGTERM 时，先用既有清理函数回收自己的子树，再非零退出；不先杀死自己而丢失收养关系。真实 launcher SIGKILL 用例同样验证了 detached 工具被回收。
- `frontend.py`、`git_sync.py`：把流式文件安全验证和有界文本导出分开。finish/recover 验证路径、普通文件、UID、Session ID/cwd/version、完整 JSONL、条目身份，以及校验前后 size/mtime/inode。扫描不积累整份消息列表，不再受 16 MiB 总导出限制；文本归档保留该限制。
- finish(0) 在提交 released 前重新取得真实物理锁，并校验文件。未写入任何回合的新空 Session 继续允许正常退出；已恢复或已使用的 Session 丢失时保留原标识并 fail-closed。新增 `session_required` 是该连续性义务的一位证据，不是新的生命周期状态。

## 诊断与边界

`frontend show` 保留退出码，增加 `release_diagnostic`，区分 clean_exit、锁不可用、Session 缺失、身份不符、JSONL 不完整、条目超限、文件改变、未知或非零进程退出。`native_finish_failed` 包含具体安全验证错误码。大文件可 released，同时 `history_export.status=size_limit`，表示只读文字导出未执行，不代表会话损坏。

仍保留每条 JSONL 1 MiB 的校验内存边界，超限返回 `native_session_entry_limit`；没有通过取消所有限额来掩盖问题。完整流式校验仍为 O(file size)，极大文件的耗时受存储速度及现有 IPC 超时影响。非零、SIGKILL、Runtime 失联或清理失败不冒充正常退出；需要显式检查后恢复。

## 验证入口

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_frontend test_child_lifecycle test_git_sync test_native_entry -v
PATH="$PWD/packaging/omp:$PATH" PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests \
  .venv/bin/python -m unittest test_native_recovery -v
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests
```

原生回归使用仓库锁定 OMP/Bun、隔离 Runtime、真实 PTY 和本地 HTTP Provider，不使用真实用户数据。覆盖 Ctrl+D → 立即再次运行 launcher → 同一 Conversation/Session 连续三轮；锁冲突后原记录不变；17 MiB 合法会话两次恢复与 Ctrl+D；异常终止无重放；setsid 工具在正常和 guardian SIGKILL 后均消失，无关对照进程仍存活。CI 原生作业纳入整个 recovery 套件。

最终结果：Python 全量 488 项，402 通过、86 条件跳过（140.864 秒，`/tmp/paveway-lifecycle-python-final.log`）；完整真实 Native recovery 16 项通过（100.575 秒，`/tmp/paveway-lifecycle-native-complete.log`）；补充 17 MiB Session 在真实 OMP SIGKILL 后显式 recover、再启动同一 Session 通过（13.651 秒，`/tmp/paveway-lifecycle-native-large-recover.log`）。此前定向 31 项通过。初始复现日志为 `/tmp/paveway-lifecycle-repro-lock.log`、`/tmp/paveway-lifecycle-repro-fd.log`、`/tmp/paveway-lifecycle-repro-unit.log` 和 `/tmp/paveway-lifecycle-repro-descendant.log`。

全量复验发现兼容性测试的全目录不变性快照与其隔离 daemon 的首次 Git 自动同步竞争。仅在该测试启动前暂停 fixture 的 Git writer，保留全部字节级断言，未修改 Git 同步业务逻辑。真实服务和真实会话没有重启或恢复；代码尚需在正常退出、任务空闲后按部署流程加载。
