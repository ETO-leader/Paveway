# 事件與 run 狀態機 v1

狀態：M1 已實作下列基本轉移；自動重試、backoff 和 dead_letter 策略仍待 M3。

## 事件

| 原狀態 | 目標 | 條件 |
| --- | --- | --- |
| 新請求 | `queued` | `at` 使用帶已知 offset 的 RFC3339 時刻 |
| 新請求 | `waiting_anchor` | `after_turn` 必須綁定同 workflow/task 的合法 active run |
| `waiting_anchor` | `queued` | anchor 成功且完全 settled；到期時間 = settled_at + relative_seconds |
| `waiting_anchor` | `blocked` | anchor 失敗或不確定，不啟動倒數 |
| `queued` | `leased` | 原子 claim、建立 run、增加 attempt，保存 owner/lease |
| `leased` | `running` | 送 prompt 前先 commit dispatch intent |
| `running` | `succeeded` | 同 request ID 的 completed prompt_result 加 settled 證據 |
| `running` | `failed` | 明確 error/aborted result 且 settled；保留副作用資訊 |
| `running` | `uncertain` | 送出後 EOF、timeout、crash，或完成證據不完整 |
| `leased` | `blocked` | 啟動、workflow、session 驗證失敗，或 daemon 中斷；未記錄 intent |
| `waiting_anchor/queued` | `cancelled` | CAS 版本匹配、尚未 claim |
| `uncertain` | `queued` | 人工核對副作用後明確接受重複風險；新增 run，不抹除舊 run |
| `failed/blocked` | `queued` | 人工 retry；必須有到期時間、尚未達 max_attempts |

修改只允許未 claim 事件且必須檢查版本。已 leased 的事件先保守拒絕修改，避免將 DB 更新誤稱為撤銷 Agent 副作用。取消與重排都保存 audit transition。

同 scope 的 dedupe key 唯一；相同請求 fingerprint 返回既有事件，時間、reason 或 payload 不同則 conflict。取消後也不回收 dedupe key。保留原始 fingerprint，不能因重排而讓同一原始請求被當成新事件。

逾期事件保留，按 `(not_before_utc, created_at, event_id)` 排序；不靜默刪除。workflow envelope 包含原定時間與 `missed_by_seconds`。

## Run 與恢復

每個 attempt 擁有獨立 `run_id/request_id`，保存 dispatch intent、ack、prompt result、settled、退出碼、程序歸屬和可能副作用。

只有 ack 不能成功；只有 completed、沒有 settled 也不能成功。adapter 要求相同 request ID，忽略其他 ID 的完成事件；SQL 也對 succeeded run 要求 completed result 與非空 settled_at。本地命令 `response.data.agentInvoked=false` 使用獨立完成路徑，再以 get_state 確認 settled。

啟動 daemon 時，在持有 state 及 socket 排他鎖後 reconcile：未送出的 lease 轉 blocked，曾記錄 intent 的 run 轉 uncertain。lease 過期或 PID 消失都不是未執行證據。guardian 繼承兩把 flock，等待 OMP 及自有程序組清理後才釋放；parent-death signal 在 daemon 被 SIGKILL 時終止自有程序組。新 daemon 不能與仍持鎖的舊 guardian 同時啟動。

Runtime 不提供端到端 exactly-once。會話 uncertain 時，後續事件也不能偷偷沿用；人工恢復該會話必須接受重複副作用風險。需要 UI 的 frame、協議錯誤或 timeout 若發生在 intent 後，保守列 uncertain 並出現在 recover list。不得直接修改 SQLite 重試。
