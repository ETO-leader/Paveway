# 操作與恢復

## M1 操作

依 README 安裝後執行 `init` 和 `doctor --json`。`init` 建立 0700 的 state/sessions 目錄、0600 配置與 DB；拒絕已存在但不私密的 state 目錄，避免自行改動使用者目錄權限。workflow root 可為既有可信唯讀目錄。

初始化可重入。若在寫出配置後建立 state/DB 失敗，保留配置供修復，再執行同一 `init --config PATH` 即可；不能加覆寫路徑旗標。檢查報告不輸出完整 OMP help/stderr、prompt 或憑證。

`doctor` 執行可信 OMP executable 的 `--version/--help`（各最多 10 秒），讀取 DB、配置、workflow 並查詢 daemon 狀態；不執行模型回合、不登入、不查認證、不改全域 OMP 配置。OMP 未安裝時報 FAIL，daemon 離線回 WARN；持久化初始化不依賴 OMP/MCP。

`serve --config PATH` 是前景常駐服務。另開終端用相同配置 schedule/status/events/runs/recover；排程 CLI 結束不影響 daemon。關閉 serve 所在終端可能停止服務，長期常駐的 systemd 方案仍待 M4。

SIGTERM/Ctrl-C 停止接受新請求，取消當前派發並嘗試 abort、關閉 stdin、限時等待，最後終止自有程序組。已記錄 intent 的中斷回合保留 uncertain。guardian 使用 Linux parent-death signal 及繼承 flock，daemon SIGKILL 後清理其程序組；OMP 自身退出後也清理仍留在同程序組的工具子程序。這不是任意脫離程序組的程序或遠端工具的副作用撤銷；正式 service cgroup 邊界留待 M4。

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

啟動前失敗在 M1 直接 blocked，不會自動 crash loop；可修復配置後 `recover retry EVENT_ID --config PATH`。若該會話先前 uncertain，仍須顯式確認風險。backoff/circuit breaker、自動重啟與 agent restart 尚待 M3。日誌只輸出結構化 ID、轉移和錯誤碼，stderr 持續排空但不保存原文，避免洩漏金鑰或私人內容；詳細失敗分類和受控 debug 留待後續完善。
