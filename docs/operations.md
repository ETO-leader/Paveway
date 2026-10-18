# 操作與恢復

0.8.0 可用 `oicoach-runtime ui --config PATH` 互動操作既有 CLI；不選 workflow 時從清單選擇。q/Ctrl+C 只退出 UI，不停止獨立 daemon 或取消已入庫事件。服務停止需在服務頁明確確認；unknown/uncertain 的副作用核對規則不變。詳見 [終端 UI](terminal-ui.md)。

0.7.0 的长期笔记通过 `memory put/show/list/delete/recall` 管理，历史通过 `history search/show/messages` 查阅，详见 [记忆说明](memory.md)。DB v1→v2 在 init/serve 中原子迁移，doctor 只读；备份需包括 DB 和 sessions。删除/过期笔记不会删除历史会话、旧回复或备份；关闭 workflow.memory_enabled 只停止新回合注入，不能撤回旧上下文。

## M4 操作

依 README 安裝後執行 `init` 和 `doctor --json`。`init` 建立 0700 的 state/sessions 目錄、0600 配置與 DB；拒絕已存在但不私密的 state 目錄，避免自行改動使用者目錄權限。workflow root 可為既有可信唯讀目錄。

初始化可重入。若在寫出配置後建立 state/DB 失敗，保留配置供修復，再執行同一 `init --config PATH` 即可；不能加覆寫路徑旗標。檢查報告不輸出完整 OMP help/stderr、prompt 或憑證。

`doctor` 執行可信 OMP executable 的 `--version/--help`（各最多 10 秒），讀取 DB、配置、workflow 並查詢 daemon 狀態；不執行模型回合、不登入、不查認證、不改全域 OMP 配置。OMP 未安裝時報 FAIL，daemon 離線回 WARN；持久化初始化不依賴 OMP/MCP。

`verify-omp --config PATH --json` 才會實際建立受限的 OMP RPC 進程，檢查 ready、get_state、get_messages。加 `--with-model` 則另外啟動暫存 daemon，發送三個模型回合、驗證續聊與 task 隔離；使用既有認證，無認證或模型錯誤回 FAIL，不自動登入或重播。測試結束刪除暫存 workspace/state/session，輸出不包含原始 prompt、模型文字或憑證。

`serve --config PATH` 是前景常駐服務。另開終端用相同配置 ask/schedule/status/events/runs/recover；排程 CLI 結束不影響 daemon。關閉 serve 所在終端可能停止服務，長期常駐使用已實作的 `service install`、`service enable --now`，見 [部署](deployment.md)。

SIGTERM/Ctrl-C 停止接受新請求，取消當前派發並嘗試 abort、關閉 stdin、限時等待，最後終止自有程序組。已記錄 intent 的中斷回合保留 uncertain。guardian 使用 Linux parent-death signal 及繼承 flock，daemon SIGKILL 後清理其程序組；OMP 自身退出後也清理同組工具。正式 unit 另以 KillMode=control-group 管理服務後代；這不是對遠端工具副作用的撤銷或對能自行逃離管理邊界的程序的隔離保證。

鎖檔永久保留，不根據檔案存在判斷服務存活，也不要手動刪除。若新 daemon 提示仍有 guardian 持鎖，等自有程序組清理後重試；不根據 PID 檔殺進程。socket 若是非 socket 檔案或有 live listener，serve 拒絕覆寫。

資料庫不存在、schema 不相容或損壞時不覆寫；保留檔案，修正配置或另外建立新的私密 state 目錄。請勿刪掉原檔來假裝恢復成功。

## 備份

使用 SQLite backup API 取得一致快照；daemon 運行時不能只複製主 DB 而漏掉 WAL。以下腳本需要自己填入實際路徑，目的檔必須不存在：

```python
import os
import sqlite3
from contextlib import closing
from pathlib import Path

source = Path("/absolute/state/runtime.sqlite3")
destination = Path("/absolute/private-backup/runtime.sqlite3")
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
```

## uncertain 處理

送 prompt 前先 commit intent；其後 crash/timeout、缺少完整 completion + settled 證據時保留 uncertain。先使用 `recover list`、`events show`、`runs show` 核對 OMP session、外部 MCP 與檔案副作用。確認能承擔重複後才執行：

```text
oicoach-runtime recover retry EVENT_ID --confirm-duplicate-risk --config PATH --json
```

不得直接把 running/uncertain 改成 queued 或只因 PID 消失就重播。`recover retry` 保留舊 run，新 attempt 另建 run；不提供 exactly-once 保證。若綁定的 session_file 遺失、ID 不符、workspace/revision 改變，事件 blocked，不默默新開會話。

啟動前的傳輸／進程失敗可在 max_attempts 內自動重試，耗盡進入 dead_letter；永久 workflow/session 錯誤仍 blocked。從未 dispatch 的暫定 session 若尚未落盤，可清除其 file/ID；曾經 dispatch 的 session 遺失則仍 blocked。模型完成之後的持久化錯誤或非零退出也會列 uncertain，不啟動 after_turn 倒數。修復 blocked 事件後可人工 recover retry；dead_letter 若已耗盡預算，必須核對後建立新的 dedupe key，不能藉手動控制無限重設 attempt。

## 退避與熔斷

全域啟動失敗和已派發 run 的執行／provider 失敗都更新故障策略；已派發 prompt 不會因此重播。策略、run 結果、事件狀態和 audit 原子持久化，daemon 重啟保留 cooldown。預設參數如下，可在既有 [restart] 增補：

```toml
max_failures_per_window = 5 # 1–1000
window_seconds = 300
backoff_initial_seconds = 2
backoff_max_seconds = 120
cooldown_seconds = 60
jitter_ratio = 0.2 # 0–1；向上加入最多 20%，但不超過 backoff_max
```

連續失敗退避約為 initial × 2^(n−1)，上限 max；時間窗內達上限，或半開 probe 再失敗，則重新冷卻。下一次啟動取退避與冷卻較晚者。策略使用 UTC durable deadline，程序內等待與超時使用 monotonic。status 可查 next_start_utc/open_until_utc；等待期間控制 socket 持續可用。

## 手動 agent restart

```text
oicoach-runtime agent restart --task TASK --workflow WORKFLOW --reason "operator restart" --config PATH --json
oicoach-runtime agent show RESTART_ID --config PATH --json
```

此架構每次 wake 建新進程，restart 會中止該 task 的 active run，並以同一 session 啟動新進程做 ready/get_state 檢查後回收，不發送 prompt。當前 prompt 被中斷則 uncertain，仍須人工核對；其他 task 不被中斷，控制工作等其完成後串行執行。回覆 requested 只表示持久接收，使用 agent show 查看最終 succeeded/failed/interrupted。

修復啟動環境後，可顯式加 --reset-circuit，僅成功 probe 才清除 supervisor 冷卻與計數；這不是模型／provider 健康驗證，也不重設事件 attempt 或清除 uncertain。restart job 跨 daemon crash 保留，未完成者標 interrupted。受管 Agent 用 Linux pidfd 發信號；OMP root 與 guardian 均有 parent-death 保護，Runtime subreaper 會收養並清理同組工具，避免 guardian 先退出但工具仍持有 RPC pipe。

daemon 自動拉起由 `service` 安裝的 systemd unit 提供，包含 Restart=on-failure、啟動頻率限制及 control-group 清理。日誌只輸出結構化 ID、轉移和錯誤碼，stderr 持續排空但不保存原文；更完整的受控 debug 留待後續完善。

0.6.0 新增 `ask --wait`、`events wait` 和 `runs output`。等待逾時不取消事件；重送同 scope/key 的 ask 會返回原事件，不同內容回 conflict。最終 assistant 回覆以最多 16 KiB UTF-8 保存在私密 DB metadata，不寫入日誌；截斷明確標記。local-only、舊 run 或未取得本輪文字時回 unavailable。輸出不是額外的成功證據；若查詢期間收到 session-persistence error，仍保留 uncertain。
