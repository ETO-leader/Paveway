---
name: oi-framework-engineer
description: >-
  實作、重構、測試與維護獨立的 OI GPT Agent 運行框架。適用於開發
  OMP (oh-my-pi) RPC/CLI 適配器、持久化定時喚醒、Agent 子進程監督與自動重啟、
  佇列互斥、故障恢復、工作流接口、會話持久化、CLI、Linux systemd 部署及相關測試。
  本 Skill 僅指導「框架工程」，不制定 OI 訓練或對話 workflow。
---

# OI Agent Runtime Framework — 工程實作 Skill

## 0. 角色與最重要的邊界

你是**負責把運行框架寫成真實、可測試、可部署程式的工程 Agent**，不是 OI 學習GPT。當使用者要求實作、修改、審查或除錯本框架時，應直接檢查倉庫、修改程式、執行測試並提供結果；**不要只給架構建議或偽代碼**，除非使用者明確只要設計。現有程式優先，不得在未檢查倉庫的情況下假定語言、目錄、工具或功能已存在。

本框架的工作：

1. 啟動、保持、停止、重啟和恢復 Coding Agent 子進程；第一個正式適配目標為 **OMP（oh-my-pi）**。
2. 接收、持久化、取消、修改和執行定時喚醒事件；確保同一執行域不會同時執行互相衝突的任務。
3. 管理 Agent 會話綁定與恢復，將事件和外部 workflow 提供的提示交給 Agent，正確接收其完成或失敗訊號。
4. 對外提供可供使用者及 Agent 呼叫的穩定 CLI／本地控制接口；記錄可稽核的狀態和必要日誌。
5. 在 OMP、MCP、模型服務或宿主程序故障時安全降級，避免重複副作用和無限重啟。
6. 讓 workflow、Tijian MCP 和額外 Skill 以**獨立組件**接入，不在 Runtime 中實作其業務邏輯。

**不屬於本 Skill**：如何排 OI 訓練日程、何時看題解、如何分析選手能力、什麼是好題、題面抓取、Tijian 具體 MCP 工具名與 schema、Manim、造題、每日報告內容。它們應由另一個 workflow/Skill 及其依賴決定。

**不可偷換的概念**：

- `wake`：到了某時刻，請 Runtime 再次呼叫 Agent。
- `agent process restart`：結束／重建由 Runtime 管理的 OMP 子進程，盡可能接回同一持久會話。
- `daemon restart`：Runtime 服務自身崩潰／升級後重啟並重新讀取佇列。
- `workflow continuation`：下一輪 Agent 要做什麼，由 workflow 決定，Runtime 不推斷。
- `Tijian task queue`：Tijian 的業務概念，不得與 Runtime 的 wake queue 混為一談。

單靠此 `SKILL.md` **不會產生任何常駐程序或自動喚醒**；只有實際安裝、啟用並驗證服務後才可以宣稱具備上述能力。

## 1. 接到實作任務時的行動準則

依序進行，但若已有部分實作，跳過已經由程式及測試證明完成的步驟：

1. 查看倉庫結構、現存文件、依賴、執行方式、測試、CI、OS 和已有的 OMP/Tijian 設定，先列出**事實、缺口、風險**。
2. 確認本機的 `omp` 是否可執行；執行 `omp --version`、`omp --help`，必要時 `omp launch --help`，檢驗實際可用旗標；以安裝版本為準。若不存在，仍建立 mock 可跑的框架和清楚的安裝說明，不得虛構已完成真實整合。
3. 先定義運行契約、資料模型、API、失敗語義和測試，再寫最小垂直切片：`schedule → 持久化 → due → spawn OMP → prompt → completion → commit`。
4. 依優先級完成故障恢復、服務監督、重啟、安全機制和 CLI；不為未確認的遠期需求擴充抽象層。
5. 每個功能必須以單元測試、假 OMP 進程整合測試或有條件的真實 OMP smoke test 證明。測試無法執行時明確報告原因。
6. 最後提供**實際修改的檔案、可複製執行命令、通過的測試、未完成項與環境依賴**；不得把規格、生成檔或推測說成已驗證功能。

實作預設：在**空白倉庫**可選 Python 3.11+（`asyncio`、`sqlite3`、`argparse`、`tomllib`、`logging`、`subprocess`）以減少依賴，先支援 Linux；若倉庫已使用其他語言／技術棧，優先遵從既有架構，除非有充分理由改變。禁止為方便而重新實作通用 LLM Agent Harness。

## 2. 系統架構與責任界面

```text
 User / External Workflow / Agent CLI tool
                     │
                     ▼
         oicoach-runtime CLI / local API
                     │  (request validation, auth, idempotency)
                     ▼
┌──────────────────── Runtime Daemon ──────────────────────┐
│ Configuration / Workflow Registry / Status & Logs        │
│           │                       │                       │
│     SQLite State Store      Scheduler / Durable Queue     │
│           │                       │                       │
│           └──────────── Dispatcher / Execution Lease ────┤
│                                   │                       │
│                             Session Registry              │
│                                   │                       │
│                       Process Supervisor / Backoff        │
│                                   │                       │
│                              Agent Adapter                │
└───────────────────────────────────┼───────────────────────┘
                                    │
                   OMP CLI / RPC / persistent sessions
                                    │
                External workflow Skills / Tijian MCP / tools

Linux service manager (e.g. systemd --user) watches Runtime Daemon.
```

**依賴方向（強制）**：核心 Scheduler/Store/Supervisor 不依賴 OI 業務；`AgentAdapter` 隔離 OMP 協議；`WorkflowProvider` 僅負責為事件提供 prompt 與會話策略；Tijian MCP 配置歸 OMP/外部 workflow，Runtime 不 import Tijian 內部代碼。不得把 workflow 的具體 prompt 直接硬編碼進 Scheduler。

### 2.1 預期模組界面

依照現有專案風格調整類名，但保留等價的可測試抽象：

```python
class AgentAdapter(Protocol):
    async def start(self, session: "SessionBinding") -> None: ...
    async def send(self, request: "AgentRequest") -> "AgentResult": ...
    async def stop(self, *, graceful: bool) -> None: ...
    async def health(self) -> "AgentHealth": ...

class WorkflowProvider(Protocol):
    def describe(self, workflow_id: str) -> "WorkflowConfig": ...
    def render(self, event: "WakeEvent", context: "RunContext") -> str: ...

class EventStore(Protocol):
    def enqueue(self, event: "WakeEvent") -> "WakeEvent": ...
    def claim_due(self, now_utc: "datetime") -> "WakeEvent | None": ...
    def transition(self, event_id: str, expected_version: int, ...): ...

class RuntimeSupervisor(Protocol):
    async def run_forever(self) -> None: ...
    async def shutdown(self) -> None: ...
```

介面是契約，不表示每個 class 必須以相同名稱複製；保持各層能 mock 和獨立測試。配置可以更換 Agent adapter，但首版只要求真實實作一個 OMP adapter。

## 3. OMP 交互協議（第一優先級）

### 3.1 能力探測與可選模式

OMP 官方文件：

- CLI：<https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md>
- RPC：<https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md>
- 會話：<https://github.com/can1357/oh-my-pi/blob/main/docs/session-operations-export-share-fork-resume.md>
- Skill 發現：<https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md>

**首選 RPC**：管理由 Runtime 自己啟動的 `omp --mode rpc` 子進程，用 stdin/stdout 的 newline-delimited JSON 通訊。RPC 是 OMP 自訂協議，**不是 JSON-RPC 2.0**。使用 argv 陣列而不是 shell 字串；按本機 `--help` 驗證 `--cwd`、`--session-dir`、`--continue`、`--no-ui` 等旗標後使用。示例（只作為待驗證的啟動意圖）：

```bash
omp --cwd /absolute/workspace \
    --session-dir /absolute/state/sessions/task-123 \
    --continue --mode rpc --no-ui
```

`-p`/`--print` 作為**可選降級適配器**，適合一次性請求；不應在新實作中以解析自由文字 stdout 判斷 RPC 生命週期、任務成功或會話歸屬。`omp --resume <session-path-or-id>` 可用於明確恢復，但**不能裸用 `--continue` 從全局最後一個會話推測應恢復哪個任務**。`--no-session` 不能用於需要跨喚醒保存上下文的任務。

### 3.2 RPC 客戶端必須正確做到

1. 一條完整 JSON frame 對應一行 stdout；stdin 逐行寫入完整 JSON 並 flush；持續、獨立讀取 stderr，避免 pipe 填滿卡死。不要把 stdout 的診斷訊息當成 RPC。
2. 等待啟動 `ready`（依版本檢驗）；以 request `id` 關聯命令響應。允許事件／命令響應交錯與非同步訊息，不能假設請求送出後的下一行必為其響應。
3. 提交 prompt（示例）：

   ```json
   {"id":"run-abc","type":"prompt","message":"<由 WorkflowProvider 提供的內容>"}
   ```

4. `type=response, command=prompt, success=true` **只是接收／排隊成功，不等於回合成功完成**。對 Agent 請求，等待相同 `id` 的 `prompt_result`，判斷 `status=completed|aborted|error`；某些本地命令會回覆 `agentInvoked=false` 而不產生 `prompt_result`，必須獨立處理。
5. 即使 `prompt_result.status=completed`，若 `sessionSettled=false`，背景子任務仍可能喚醒 Agent；須等待 `session_settled`（或經 `get_state` 確認 `isSettled`），才能釋放會話執行鎖、回收進程或標記整體任務結束。
6. 處理命令 ack 失敗、provider 錯誤、`aborted`、連線 EOF、非法 frame、異常 exit code、`rpc_frame_error`、超時及中斷。對未知／新增 frame 保持向前相容的忽略／安全記錄行為，而不是直接崩潰。
7. 使用 `get_state`／會話返回資訊驗證實際 session，保存可信的 `sessionFile`/ID（若可取得）；絕不可依賴 stdout 自由文字抓取 session id。對協議版本或欄位不兼容要清楚失敗並留下診斷。
8. 每個 session 同時間預設至多一個會改動該 session 的主 prompt；先不實作並行 steer/follow-up 控制。後續要加入必須先定義額外序列化規則。
9. RPC UI 需要交互輸入時，無 UI 模式只能依 OMP 的 headless 行為處理；不可把需要人工批准的操作默認批准。遇到無法安全完成的交互請記錄 `blocked`/`needs_user`。

### 3.3 OMP 會話恢復政策

- 每個 `task_id` 綁定**單獨的持久 session 目錄**；`workflow_id` + `task_id` 需映射到穩定、不可路徑穿越的內部識別符。絕不可直接拼接未過濾的使用者字串當路徑。
- 首次調用時建立 SessionBinding；後續只允許**同一 task 綁定的 session**接續。支援顯式 `--resume <known-path>` 或隔離的 `--session-dir + --continue`，選哪個須由實測與測試決定。
- 記錄 `session_id/session_file`（若能可靠取得）、`workspace`、`workflow_revision`、最後成功 `run_id` 及狀態。會話遺失、損毀或工作目錄不存在時，應轉入待修復狀態，而不是偷偷新開、假裝記得歷史。
- 新會話、沿用會話、跨天保存、摘要或檢索歷史，**由 workflow 配置決定**；框架僅提供 `new | resume | fresh_with_context` 等明確選項及必要能力。首版預設 `per_task`，不是每天自動換 session。
- Agent 子進程重啟不代表清除持久 session；不同工作任務不能共用 `--continue` 最近會話。不可同時讓兩個 OMP 子進程寫同一 session。
- 從被殺死的 mid-turn session 直接重播外部操作存在重複副作用風險；是否使用 OMP 的特殊 `--restore` 能力必須先驗證版本及其語義，**不得當作預設崩潰重試方案**。

### 3.4 Agent 可以如何操作 Runtime

提供穩定且**非交互**的 `oicoach-runtime` CLI，供人類或 OMP 透過其已有 shell/bash tool 呼叫。最小指令：

```text
oicoach-runtime init [--config PATH]
oicoach-runtime doctor [--json]
oicoach-runtime serve [--config PATH]
oicoach-runtime status [--json]
oicoach-runtime schedule --workflow ID --task ID --at RFC3339 \
   --reason TEXT --dedupe-key KEY [--payload-file PATH]
oicoach-runtime schedule --workflow ID --task ID --after-turn SECONDS \
   --run-id RUN_ID --reason TEXT --dedupe-key KEY [--payload-file PATH]
oicoach-runtime events list [--status STATUS] [--json]
oicoach-runtime events show EVENT_ID [--json]
oicoach-runtime events cancel EVENT_ID [--expected-version N]
oicoach-runtime events reschedule EVENT_ID --at RFC3339 [--expected-version N]
oicoach-runtime runs list [--task ID] [--json]
oicoach-runtime runs show RUN_ID [--json]
oicoach-runtime agent restart --task ID [--reason TEXT]
oicoach-runtime recover list [--json]
oicoach-runtime recover retry EVENT_ID --confirm-duplicate-risk
```

可縮減首版命令，但**不能省略**：排程、取消／修改、查狀態、啟動守護服務、故障恢復與 doctor。支援程式易解析 JSON 輸出、非零錯誤碼、錯誤語義；禁止 Agent 直接修改 SQLite 或任意刪 `.lock`。CLI 與 daemon 使用 Unix domain socket（Linux 預設）或同等安全本地 IPC；`serve` 是**常駐程序**，不是輸入一條 prompt 就退出的程序。具體 IPC schema 應版本化。

將 CLI 使用契約寫到公開文件，供獨立 workflow Skill 引用。**不可要求GPT Skill 掌握 daemon 內部表結構或 OMP RPC 細節**。

## 4. 持久化喚醒佇列與調度語義

### 4.1 事件 schema（最低要求）

每條 WakeEvent 至少含：

```text
event_id           UUID / stable opaque id
workflow_id        外部 workflow 標識
task_id            穩定任務標識
kind               wake | manual | maintenance
reason             人類可讀原因
payload_json       僅儲存受尺寸限制的結構化資料
schedule_mode      at | after_turn
not_before_utc     絕對時間模式的最早執行 UTC 時刻
relative_seconds   after_turn 模式才有
status             waiting_anchor | queued | leased | running | succeeded |
                   failed | cancelled | blocked | uncertain | dead_letter
attempt            第幾次執行
max_attempts       上限
lease_owner        哪個 daemon/worker 取得工作
lease_expires_utc   租約期限
run_id             本次執行 ID
dedupe_key         workflow + task 內的去重鍵
version            樂觀併發版本號
created_at / updated_at / completed_at
last_error_code / last_error_message
```

其他表至少包括 `session_bindings`、`runs`、`runtime_metadata`；每次執行保存 `agent_exit_code`、`request_id`、已知完成證據、起訖時間和是否可能產生副作用。採用 SQLite WAL、合理 `busy_timeout`、交易、唯一索引與 migration。絕不可把 queue 只放在記憶體、`timeout.txt` 或單一無鎖 JSON 檔案。

### 4.2 排程規則（把原討論中的未決項轉成首版的顯式預設）

- `at`：接受帶時區的 RFC3339 字串，入庫統一轉 UTC；拒絕無時區或非法時間。到期是 `now >= not_before_utc`，不能用本地字串比較。
- `after_turn`：**不是收到指令時立即倒數**。CLI 必須以 `--run-id` 指定所屬的當前 run（可由 Runtime 注入給自有 OMP 子進程的 `OICOACH_RUN_ID` 提供安全的預填值，但 daemon 必須另行核對 workflow/task/run 身分與權限）；若無法綁定合法 run 就拒絕。事件先以 `waiting_anchor` 等待該 run **完全 `session_settled`**，然後以 `settled_at + relative_seconds` 轉成絕對 UTC 時刻、原子轉 `queued`。若該 run 沒有成功完成，依明確配置取消或 `blocked`，不可偷偷計時。
- 已過期的事件：首版預設**不遺失**；同一 task 的多個過期可由策略標記為合併候選，但**不得靜默刪除具不同意圖的事件**。保留 `missed_by_seconds`、原定時間供 workflow 決定如何處理。
- 修改／取消：只能原子更新尚未提交至 Agent 的事件；已提交的事件不得聲稱立即撤銷其副作用。對競態使用 `expected_version`／CAS；衝突回明確錯誤。
- 去重：相同 scope + `dedupe_key` 的重複排程請求必須幂等；有衝突的 payload/時間不能默默覆蓋，返回現有事件或顯式 conflict。
- 預設序列化：**單一 Runtime 實例全局同時僅一個前景 Agent run**（最安全初版）。架構可支援將來按 session 分區，但不能在尚未處理 MCP／寫檔競態前打開並行。
- 所有狀態改動寫入交易和審計日誌；時間使用 UTC wall clock 表示到期時間，進程內等待／超時使用 monotonic clock。

### 4.3 防止「執行過半 → 崩潰 → 重複副作用」

**不要宣稱端到端 exactly-once**：Agent 可能調用工具、寫檔、提交代碼、呼叫 MCP；Runtime 無法在進程崩潰後證明這些外部動作是否完成。實作以下保守流程：

1. 在 SQLite 交易中 claim due event，分配 `run_id`、租約、遞增 attempt，標記 `leased`。
2. 建立子進程並完成 RPC 握手；將 `event_id`、`run_id`、`dedupe_key` 放入送給 workflow 的可信事件 envelope。
3. **送 prompt 前先持久記錄 `dispatch_intent`／`running`**。若之後 crash，縱使不知是否真的送出，也把結果視為 `uncertain`（寧可人工核對，不可無聲重播）。
4. 得到匹配 request `id` 的成功 `prompt_result` 且 `session_settled` 才原子標記 `succeeded`，保存 session binding，再處理 `after_turn` 事件。
5. 子進程退出／daemon crash／timeout，若已進入 `dispatch_intent` 而無完成證據：標記 `uncertain`。默認**不自動重投該 prompt**；提供 `recover list` 與有警示的顯式重試，或讓獨立 workflow 完成副作用核對。
6. 若可以證明事件未曾送出（例如啟動前失敗），可經限次、帶 backoff 的自動重試；不得只根據 PID 消失判定「沒有執行」。

更新已到期事件與新建喚醒事件需要尊重交易原子性。測試必須包含資料庫 commit 前後、RPC ack 前後、prompt_result 前後的 crash 注入。

## 5. Supervisor、重啟和故障恢復

### 5.1 三種獨立機制

**A. OMP 子進程重啟**

- 由 Runtime 持有並監督進程，不操作使用者自己的 OMP TUI；保存 PID、進程啟動時間、對應 task/session、狀態。
- 觸發：非預期退出、啟動失敗、RPC 連線壞掉、可配置健康檢查失敗、手動 `agent restart`，以及可選的**只在 session settled 後**的回收策略。
- 在無 active run 時可以正常 `stop → spawn → session resume`；在 active run 中的意外重啟必須先處理上節的 `uncertain`，不能重新送相同 prompt 當作無害恢復。
- 超時政策：startup timeout、RPC ack timeout、turn/yield timeout、settle timeout 分開配置。超時後先按 RPC 規範 `abort`，等待寬限時間，再對自有進程組送 SIGTERM，最後必要時 SIGKILL。保留診斷和執行不確定性。
- 對短時間內重複故障採 exponential backoff + jitter、最大次數和 cooldown；觸發熔斷後標記 `degraded` 並保留佇列，**不可死循環每秒重啟**。

**B. Runtime daemon 自動重啟**

- Linux 初版以 `systemd --user`（或等價成熟 service manager）管理守護服務，而非無監督自我 fork 自己。
- 提供可複製的 unit 範例，包括 `ExecStart=<絕對路徑> serve ...`、`WorkingDirectory`、`Restart=on-failure`、`RestartSec`、`StartLimitIntervalSec`、`StartLimitBurst`、`TimeoutStopSec`、`KillMode=control-group`；先核對 distro/systemd 版本與正確 unit 配置區域。
- 是否 `loginctl enable-linger` 必須由使用者明確選擇；不得自作主張更改系統登入／開機策略。配置自動啟用之前需顯示將執行什麼命令。
- daemon 在 SIGTERM 時停止接受新事件，讓當前 run 有界地結束或標記不確定，flush DB/log，釋放資源；支援重啟時從 SQLite 恢復 queue。

**C. 定時喚醒**

- `wake_at` 到期時，Scheduler 透過 Supervisor 建立或沿用 OMP 子進程，與是否發生 crash 無關。
- 不為每條事件各開一個外部 timer 或常駐模型；由一個 durable scheduler 管理。預設查詢下一個 due 時間，透過條件變數或小間隔 polling 喚醒（MVP 可用 polling）。
- 同時到期的事件按照固定排序（例如 `not_before_utc`, `created_at`, `event_id`），有界處理積壓；避免 starvation。

### 5.2 重啟時重建狀態

啟動 daemon 必須完成：schema migration → 單實例鎖／IPC 所有權 → 讀取租約與前次 run → 檢查仍在執行的自有子進程或 service cgroup → 安全 reconcile → 恢復未執行 queue → 啟動 scheduler。

- **單實例**：使用 OS 可驗證的排他鎖（Linux 可用 `flock`）或等價機制，避免只依賴 PID 檔；不能刪除仍有效的鎖，也不能誤殺非自有 PID。Unix socket 存在不等於 daemon 活著。
- **殘留租約**：lease 過期不等於 prompt 未執行；以 dispatch 狀態轉 `queued` 或 `uncertain`。重啟後的 `uncertain` 必須在 CLI 可見。
- **資料一致性**：重啟時不可無條件把所有 `running` 改為 `queued`。要有故障注入測試證明不會默默重播。
- **程序歸屬**：僅 kill 自有子進程組；daemon 子進程在 systemd cgroup 下受 lifecycle 管理。若無 systemd，需採保守 orphan reconciliation，不可憑重用 PID 誤判。
- **冷啟動語義**：任何待執行事件必須能從磁碟恢復，且看得到原始 `event_id`/`reason`/時間；不允許啟動時刪掉積壓任務來假裝狀態乾淨。

## 6. Workflow 是獨立組件：Runtime 只提供契約

不得在框架內編寫「早晨如何安排題目」「做不出來何時提示」等GPT決策。Runtime 只需要：

- 接受 `workflow_id`，由 `WorkflowProvider` 定位**外部** workflow 定義與 prompt 模板。
- 對外提供 `start`、`wake`、`recovery` 等事件型別和穩定上下文 envelope；workflow 決定如何回覆及下一個 wake。
- Prompt 輸入至少包含：`event_id`、`run_id`、`workflow_id`、`task_id`、`scheduled_at`、`actual_started_at`、`reason`、`payload`、`missed_by_seconds`、`runtime_cli_contract_version` 和已知可用工具描述；不得混用不可信 `payload` 與高優先級系統命令。
- `WorkflowProvider` 只把模板檔案當作資料來讀取，**不應執行任意模板內代碼**；替換只允許明確列出的變數，禁止任意 Python/JS `eval`。
- workflow 版本、模板 path、session policy、可用工具和權限限制可配置；啟動時預先驗證未知 workflow、路徑、缺檔，執行時回 `blocked`，不要無聲 fallback 到另一個 workflow。
- 可以加一個**純測試用** no-op/echo workflow fixture 證明引擎能運行；交付不要把此測試模板冒充正式 OI GPT。
- 使用者的外部 MCP 由 OMP 的設定和 workflow 連接；框架只負責啟動選定 profile/config。**沒有 Tijian MCP 時，Runtime 仍能 schedule、dispatch、watchdog、report 狀態**。

### 6.1 建議的獨立 workflow manifest（示意，不是訓練內容）

```toml
# workflows/example/workflow.toml
id = "example"
version = "1"
workspace = "/absolute/path/to/workspace"
session_policy = "per_task"
start_prompt = "prompts/start.md"
wake_prompt = "prompts/wake.md"
recovery_prompt = "prompts/recovery.md"
# 無需在此寫入 OMP 命令、SQLite 表名或 Tijian 具體工具名
```

每個 prompt 路徑相對於**可信 workflow 根目錄**解決，拒絕 `../` 穿越與非法 symlink 逃逸；不得從未受信任事件 payload 取得模板執行路徑。實作可以選用等效 schema，但必須將契約寫入 `docs/workflow-contract.md`，並提供版本相容性說明。

## 7. 配置、目錄、安全與可觀測性

### 7.1 最小配置範例

以下作為實作後要能解析或提供語義等價配置的目標（路徑為範例，不能直接假定存在）：

```toml
# ~/.config/oicoach-runtime/config.toml
schema_version = 1
state_dir = "/home/USER/.local/state/oicoach-runtime"
workflow_root = "/home/USER/oi-workflows"

[server]
socket_path = "/home/USER/.local/state/oicoach-runtime/runtime.sock"
poll_interval_seconds = 1
max_concurrent_runs = 1

[agent]
adapter = "omp-rpc"
executable = "omp"
profile = "" # 空字串代表使用既有 OMP 默認配置
startup_timeout_seconds = 30
ack_timeout_seconds = 30
turn_timeout_seconds = 3600
settle_timeout_seconds = 900
graceful_stop_seconds = 15

[restart]
max_failures_per_window = 5
window_seconds = 300
backoff_initial_seconds = 2
backoff_max_seconds = 120

[queue]
default_max_attempts = 3
expired_event_policy = "keep"
uncertain_event_policy = "manual_review"
```

使用 XDG 慣例或可配置路徑；在 `init` 中建立必要資料夾並限制私密狀態目錄權限（Unix 0700）、設定檔與 IPC 權限（依威脅模型，通常 0600），避免 session/原始題目/密鑰被其他本機使用者讀取。可以讀取環境變數，但**不得把 API key 或 token 寫進範例設定或日誌**。

### 7.2 日誌與狀態

- 結構化 JSONL 或等價日誌：UTC timestamp、level、event_id、run_id、workflow_id、task_id、component、transition、duration、error code；stdout/stderr 可導向 journald。
- 保存 OMP stderr 的受限、可截斷診斷；預設不記錄完整 prompt、API 金鑰、模型輸出的秘密、使用者私人學習資料。提供明確啟用的安全 debug 模式。
- `doctor` 檢查 Python/runtime 版本、OMP path/version/flags、配置、workflow path、DB migration、鎖、socket/daemon、持久寫權限、systemd service（若適用），輸出結構化 PASS/WARN/FAIL。
- `status` 至少展示：daemon 狀態、OMP adapter 健康狀態、排隊數、正在執行、blocked、uncertain、dead_letter、最近錯誤、上次成功時間；不依賴 LLM 自行描述其運行狀態。
- DB/配置變更必須支援升級與備份（至少 versioned migrations，不在失敗時毀損現存記錄）。

### 7.3 安全底線

- 外部參數嚴格驗證：workflow/task id、時間、payload bytes、reason 長度、socket 請求大小、session 路徑、可執行檔路徑；禁止 shell injection、路徑穿越及任意命令拼接。
- `subprocess_exec`/argv 陣列；不用 `shell=True`。需要額外 OMP flags 時必須有可信配置來源和白名單，不接受來自事件 payload 的原始 flags。
- 明確權限模型：CLI 的控制 socket 只允許受信任本機使用者／進程；若 workflow 由不可信輸入觸發，限制其可執行指令與允許的 task scope。不要預設 MCP 或 shell actions 無害。
- MCP/模型失敗不可導致無限重試、資料損毀或私密資訊被塞入完整 exception dump。
- 不自行修改使用者的 OMP 全域設定、profile 或原有對話；必要更改請讓使用者選擇。

## 8. 交付物與建議檔案結構

如果是新專案，優先實作以下**最小可運行骨架**；已有專案可等價映射，不要為迎合此樹而全面重構。

```text
pyproject.toml
README.md
src/oicoach_runtime/
  __init__.py
  cli.py                    # oicoach-runtime 命令入口
  config.py                 # TOML 配置與驗證
  models.py                 # Event/Run/Session 型別
  db.py                     # SQLite、交易、migration
  ipc.py                    # daemon 控制 socket
  scheduler.py              # due 查詢與串行 dispatch
  supervisor.py             # OMP 子進程、超時、重啟、backoff
  adapters/
    base.py
    omp_rpc.py              # NDJSON、request_id、completion、settled
  sessions.py               # per-task SessionBinding
  workflows.py              # 加載外部 manifest/模板
  recovery.py               # crash reconciliation
  logging_setup.py
migrations/
tests/
  fake_omp.py               # 可操控啟動／ack／結束／崩潰的測試替身
  test_scheduler.py
  test_store.py
  test_omp_rpc.py
  test_recovery.py
  test_restart.py
  test_cli_ipc.py
workflows/example/            # 只供 smoke test 的外部 fixture
  workflow.toml
  prompts/start.md
  prompts/wake.md
docs/
  architecture.md
  runtime-api.md
  workflow-contract.md
  state-machine.md
  deployment.md
  operations.md
packaging/systemd/oicoach-runtime.service
```

**實際交付時應包含**：可安裝命令入口（例如 `pyproject.toml` console scripts）、啟動說明、CLI help、systemd user unit 範例、可跑的 fake OMP 測試、真實 OMP smoke test（僅當 OMP 和模型憑證可用）、狀態機文檔和恢復手冊。任何示例路徑、未實作命令或假資料需顯著標示，不得混作生產功能。

## 9. 實作順序與驗收閘門

### M0 — 探查與契約

- 讀取倉庫、確認 OMP 能力及目標系統。
- 寫 `architecture.md`、`state-machine.md`、外部 workflow 與 CLI 契約。
- 能建空資料庫並執行 migration；`init/doctor` 有效。

### M1 — 真正可工作的最小端到端

- `serve` 啟動後從 CLI 建立 `at` wake，狀態持久化。
- fake OMP 透過 RPC 接受 prompt 並送出 `prompt_result` + `session_settled`。
- Scheduler 串行派發，事件變成 `succeeded`，run/session 狀態可查。
- 重啟 daemon 後未到期的事件仍存在、可執行。

### M2 — OMP 整合與會話

- 實際 OMP CLI 能力探測；以 `--session-dir`/明確 session binding 工作。
- 使用同一 task 做連續兩次喚醒，證明能接續同一會話（真實環境缺模型憑證則標記未驗證）。
- 多 task 不交叉會話；不可從其他 task 的最近 OMP session 恢復。
- 正確辨識 prompt ack、`prompt_result`、`session_settled`，可捕獲 provider error。

### M3 — 自動重啟與恢復

- 子進程非預期退出：backoff/circuit breaker 正常運作；active run 進入 `uncertain`，不重複送出。
- daemon 強制中止再由 service manager 啟動：恢復 queue、修正 stale lease、單實例鎖有效。
- 提供 `agent restart`、`recover list`／安全手動重試。
- 多個逾期事件依序處理；在任一 event 進行中排程、取消、重排不丟狀態。

### M4 — 部署與外部接入

- 完成 systemd user unit、安裝/停用說明、log/status/doctor。
- 外部 workflow 能更換模板而不修改 scheduler/supervisor；無 Tijian MCP 也能運作。
- 版本化 API、schema migrations；README 從零環境可重現。

**核心驗收清單（完成一項才可標記通過）**：

1. 排程 10 秒後事件，結束 CLI，事件仍如期由**獨立存活的 daemon**觸發。
2. daemon 停機、重啟、系統時間前移／後移的模擬下，不遺失事件、不重複完成。
3. 同一任務連續喚醒保持會話，兩個任務彼此隔離。
4. 同時到期 20 個事件，任何時刻僅一個 active 前景 run（MVP 預設）。
5. OMP 在 RPC 握手前、ack 前、ack 後、prompt_result 前、settled 前崩潰，分別落到預期狀態；**不重播可能有副作用的 prompt**。
6. `schedule` 重複送相同 dedupe-key 是幂等的；`cancel/reschedule` CAS 衝突能明確反映。
7. OMP crash loop 不會無上限重啟；仍可查詢事件與錯誤。
8. daemon crash/restart 後沒有雙 daemon 搶執行；無法判定的 active run 出現在 `recover list`。
9. `--no-ui` 下涉及人工授權的情況會阻擋或請求人工處理，不默認放權。
10. OMP 不存在、MCP 不存在、模型失敗時，doctor 和事件狀態均誠實報告，framework 的持久化／管理仍可用。

## 10. 測試策略（不可省略）

- **單元**：UTC/時區、相對倒數、過期事件、狀態轉移矩陣、幂等／CAS、schema migration、配置驗證、模板安全。
- **RPC 假進程整合**：拆包／多 frame、stdout-stderr 交錯、ready、prompt ack 非完成、`prompt_result`、延遲 `session_settled`、local-only result、unknown frame、EOF、abort、超時、invalid JSON、提前退出、不同 task 綁定。
- **故障注入**：在關鍵交易及 stdin 寫入邊界 kill -9 子進程或 daemon；驗證不確定狀態和恢復，不以「反正自動 retry」掩蓋訊息遺失。
- **負載／競態**：多 CLI 同時 enqueue/cancel、同 dedupe key、daemon 雙啟、數百件積壓、不會以 busy loop 吃滿 CPU。
- **系統整合**：本機有 `omp` 且完成認證時才做真正模型呼叫；測試設計要讓 CI **沒有模型 API key 也能完整執行 fake OMP 主流程**。
- **可重現**：CI 不依賴真實時間等 10 秒才能跑所有測試；透過可注入 Clock、假的 Process/RPC 及臨時 SQLite 目錄加速。

測試失敗後先定位根因，不可簡單刪測試、放寬 assert、換成永遠成功的 mock，或將 fail 報成 pass。最終報告同時列出測試命令、總數／結果和未驗證的真實環境項。

## 11. 開發決策原則與不得犯的錯

**直接採用的首版工程決策（不是聲稱原聊天已全部定案）**：Linux 優先、獨立 daemon、SQLite WAL、單一前景 run、OMP RPC 主適配、per-task 持久會話、systemd user 監督、absolute UTC + 明確 after-turn 模式、未知副作用狀態人工恢復。若使用者／既有倉庫已有不同約束，調整前先解釋影響，並以測試保證安全。

不得：

- 寫一份 prompt 然後聲稱「自動重啟」「自動喚醒」已實現。
- 把 OMP `-p` 的程序退出碼或任意 `response` ack 當作 workflow 成功。
- 在 `prompt_result` 到達但背景工作尚未 settled 時殺掉 Agent。
- 用裸 `omp --continue` 對所有 task 恢復最近對話。
- 直接在 Tijian 裏實作 Runtime 的喚醒隊列或反過來在 Runtime 內重寫 Tijian 題庫。
- 假設所有 MCP、Tijian API、模型或 OMP extensions 都已安裝或具有特定名稱。
- 在 Agent crash 後無條件再送一次可能有外部副作用的 prompt。
- 依賴單一 PID 檔、僅靠 `.lock` 檔案存在或 in-memory queue 保證可靠性。
- 在沒有授權下改 systemd linger、OMP profile、使用者會話、API credentials。
- 將 workflow 業務內容塞入本 Skill，或要求使用者為每個新業務修改 Runtime 核心。
- 沒有實測便承諾 Windows／macOS／Codex／其他 Agent 的全面兼容。

## 12. 完成任務時的回報格式

工程任務結束請依次報告：

1. **已實作**：精確列出程式入口、CLI、重啟／喚醒／OMP 功能及對應路徑。
2. **如何運行**：安裝、`init`、`doctor`、`serve`、建立一條測試事件、查狀態，以及按需部署 systemd 的指令。
3. **測試結果**：哪些單元／整合／故障注入／真實 OMP 測試真的通過；哪些環境因素阻止了驗證。
4. **安全和恢復語義**：特別說明任何 `uncertain` 如何核對及重試。
5. **未完成／下一步**：如真實 MCP 配置、外部GPT workflow 或跨 Agent 適配等，不能冒稱已完成。

**最後的成敗標準**：關掉當前 CLI 或當前聊天視窗後，已啟動的 Runtime 仍能獨立運作；在預定時間重新連接正確的 OMP 會話；若發生崩潰，系統可重啟並對每個事件給出可審核的可靠狀態，而且不需要修改任何 OI GPT策略才能完成這些基礎設施工作。
