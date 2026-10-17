# CLI 與本地 API 契約 v1

## M1 可用命令

```text
oicoach-runtime --version
oicoach-runtime init [--config PATH] [--state-dir ABS_PATH] [--workflow-root ABS_PATH] [--json]
oicoach-runtime doctor [--config PATH] [--json]
oicoach-runtime serve [--config PATH]
oicoach-runtime status [--config PATH] [--json]
oicoach-runtime schedule --workflow ID --task ID --at RFC3339 --reason TEXT --dedupe-key KEY [--payload-file PATH]
oicoach-runtime schedule --workflow ID --task ID --after-turn SECONDS --run-id RUN_ID --reason TEXT --dedupe-key KEY
oicoach-runtime events list [--status STATUS] [--offset N]
oicoach-runtime events show EVENT_ID
oicoach-runtime events cancel EVENT_ID --expected-version N
oicoach-runtime events reschedule EVENT_ID --expected-version N --at RFC3339
oicoach-runtime runs list [--task ID] [--offset N]
oicoach-runtime runs show RUN_ID
oicoach-runtime recover list [--offset N]
oicoach-runtime recover retry EVENT_ID [--confirm-duplicate-risk]
```

所有葉命令接受 `--config/--json`，放在最後一層子命令後，例如 `events list --config PATH --json`。`doctor` 可選文字或 JSON，其他控制命令始終輸出 JSON；`serve` 輸出 JSONL 狀態，不是一問一答命令。`init/doctor` 不要求 daemon；其他查詢和寫入必須經同一配置的 daemon。

配置優先順序為 `--config`、`OICOACH_CONFIG`、XDG 預設。Runtime 向自有子進程注入 `OICOACH_CONFIG`、`OICOACH_RUN_ID`；後者可作 `--after-turn` 的 run ID 預設，daemon 仍驗證 scope 和 running 狀態。TOML 路徑必須絕對，不做 shell 展開。

退出碼：0 表示命令成功或診斷無 FAIL（可有 WARN）；1 表示操作或診斷失敗；2 表示 argparse 用法錯誤。操作失敗的 `--json` 錯誤寫 stderr；診斷成功執行但檢查 FAIL 的報告仍寫 stdout。用法錯誤目前是 argparse 文字，不宣稱支援 JSON。

成功 JSON 包含 `api_version: 1`。錯誤格式：

```json
{"api_version":1,"error":{"code":"invalid_config","message":"Only config schema_version = 1 is supported"}}
```

錯誤碼包含 `invalid_input/invalid_config/storage_error/invalid_workflow/io_error/conflict/not_found/daemon_unavailable/internal_error`。控制命令成功輸出 `{"api_version":1,"result":...}`；`init/doctor` 維持既有報告結構。`doctor` 回傳 `milestone/ok/checks`，每項檢查為 `PASS/WARN/FAIL`。

## 排程與恢復語義

`at` 和 `after_turn` 二選一；RFC3339 必須有已知時區 offset，入庫統一 UTC。payload 為 JSON object、最大 64 KiB，禁止 NaN/Infinity；reason 1–4096 字元，dedupe key 1–256 字元。去重 scope 為 workflow/task，完全相同請求返回原事件，衝突回 conflict。

`cancel/reschedule` 必須提供正整數 expected_version，僅接受 queued/waiting_anchor；reschedule 明確轉成 at 模式。`after_turn` 綁定同 scope 的 running run，在成功 settled 後起算，失敗則 blocked。

events/runs list 每頁最多 100 筆、可用 offset（0–1000000）翻頁；events list 不含 payload，show 才含詳細資料。recover list 每種狀態最多 100 筆，offset 分別套用於各狀態。頁面以建立時間及 ID 排序；並行寫入時不是快照，精確查詢請使用 ID。

recover list 包含 failed/blocked/uncertain/dead_letter。retry 保留舊 run，只恢復有到期時間且 attempt 尚未達 max_attempts 的事件；uncertain 事件或會話必須顯式 `--confirm-duplicate-risk`。沒有成功 anchor 的相對事件不能偷改成立即執行，請人工核對後另建 at 事件。

status 包含 daemon、agent_health、全部狀態計數、active_runs、最近錯誤碼和上次成功時間。`agent restart`、自動 backoff、circuit breaker 和 service-manager 安裝仍未實作。

## IPC v1

Unix domain socket，控制資料夾 0700、socket 0600，透過 SO_PEERCRED 檢查同 UID。每個連線一個 JSONL 請求／回應、各限 128 KiB，讀取 10 秒、寫入 5 秒超時，同時最多處理 64 個 client。daemon 不接受從 payload 指定執行檔、OMP flags 或模板路徑。

```json
{"api_version":1,"id":"client-request-id","command":"schedule","arguments":{}}
```

回應保留 `api_version/id`，成功使用 `result`，失敗使用 `error.code/message`。拒絕未知版本、未知欄位及不符參數。無法解析的 frame 可能回 `id:null`。ID 使用 ASCII `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`。

IPC command 為 `status/schedule/events.list/events.show/events.cancel/events.reschedule/runs.list/runs.show/recover.list/recover.retry`；arguments 使用 snake_case。控制 CLI 是穩定的外部入口，workflow 不需要知道資料表或 OMP 協議。
