# 實作進度

## 2026-10-17（Asia/Singapore）— M0

倉庫起始只有 README、LICENSE、專案介紹與工程 Skill，沒有應用程式或測試。既有未追蹤的 `.codex/`、`documents-for-agents/` 保留。

本機探查：Linux、Python 3.12.3、systemd 255；`omp --version` 為 `omp/18.10.4`。本機 `omp --help` 有 `--mode`、`--cwd`、`--session-dir`、`--continue`、`--resume`、`--no-ui`、`--profile`。只驗證 CLI 能力，未測試模型認證或真正 RPC 回合。

參考上游 [CLI 文件](https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md) 與 [RPC 文件](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md)；上游 main 可能與安裝版本不同。M2 必須以實測確認 protocol ready、request id、prompt_result 和 session_settled，不把 ack 當完成。

M0 已交付可安裝 Python 套件、init/doctor、配置驗證、SQLite v1 migration、時間與 ID 函式、外部 workflow loader、架構與操作契約。測試使用標準庫 unittest，不需要模型金鑰。

本機驗證：wheel 建置與安裝成功；`python -m unittest discover -s tests -v` 共 28 項通過（包括 migration 故障回滾、未知 schema 保留、私密權限、模板路徑限制及 CLI 子進程）。在工作區 `.runtime/` 執行 init/doctor，DB 為 schema v1、WAL、quick_check ok，OMP 能力探測 PASS；未安裝 workflow、RPC 未驗證、daemon 未實作均回 WARN。沒有執行真實模型回合、RPC 故障注入或 systemd 部署測試。已加入 Python 3.11/3.12/3.13 的 CI 定義；本機只驗證 3.12.3，尚未觀察遠端 CI 結果。

下一步 M1：先完成 flock 與 SQLite queue 交易 API，再加入有界 Unix IPC、scheduler、fake OMP RPC 和獨立 daemon 測試。M1 驗收應含結束 CLI 後事件仍執行、重啟保留未到期事件、20 個同時到期事件串行，以及 dispatch 邊界崩潰後不重播 uncertain。

仍待 M2–M4：真實 OMP 會話接續、完整故障注入／backoff／熔斷、人工恢復、systemd 部署。正式 OI 教練 workflow 和 Tijian MCP 接口另行接入。

## 2026-10-17（Asia/Singapore）— M1

套件版本 0.2.0，沿用 SQLite schema v1。已實作：

- `store.py`：持久 at/after_turn、scope 去重、請求 fingerprint、CAS 取消／重排、串行 claim、run 證據與 audit、session 綁定、基本 reconcile 及人工重試。
- `ipc.py`、`cli.py`：0600 Unix socket、SO_PEERCRED、128 KiB 有界版本化接口；serve/status/schedule/events/runs/recover 命令與分頁。
- `scheduler.py`、`adapters/omp_rpc.py`：workflow envelope、RPC v1、request ID 匹配、ack 與 completion 分離、settled、local-only completion、獨立超時及 stderr drain。子進程取得目前配置與 run ID，並可用 envelope 提供的 Python CLI argv 呼叫相同 daemon。
- `processes.py`、`child.py`：state/socket flock、Linux parent-death guardian、自有程序組清理；新 daemon 不覆寫 live socket 或一般檔案。guardian 的 PID/啟動身份存於 run，exit code 反映 guardian 所監督的 OMP 退出結果；因程序組終止時可能是 signal exit。
- `tests/fake_omp.py`、`workflows/example`、`examples/prepare_smoke.py`：明確標記測試用途的可重現示範，無需模型或 MCP。

驗證：

```text
.venv/bin/python -m pip install .
.venv/bin/python -m unittest discover -s tests -v
Ran 57 tests in 55.735s — OK
git diff --check — 通過
```

以上測試從已建置安裝的 wheel 執行；本機 Python 3.12.3，尚未取得遠端 CI 或其他 Python 版本的執行結果。包含 20 個同時到期事件串行、同 task 續聊／跨 task 隔離、CLI 結束後執行、daemon 正常重啟與 SIGKILL、OMP 在 ack 前／後與 settled 前崩潰、工具子程序殘留清理、非法／過大 frame、stderr flood、local-only、provider error、去重/CAS／多 client 競態、交易 rollback 與 dispatch commit 邊界。

另以安裝的 CLI 在 `.runtime-smoke/` 排程約 10 秒後事件；發送 CLI 已退出，獨立 daemon 仍完成 `7ebfaf6e-1ce3-46c7-a65f-19073daec495`，run `0249f388-35ff-4db2-98e3-46e33c3609c8` 保存 ack、completed、settled，最終 succeeded、attempt=1、exit_code=0。示範 daemon 隨後 SIGTERM 正常退出，沒有啟用常駐系統服務。此為 fake OMP 驗證，不是真實模型驗證。

恢復政策：intent 後狀態不明不自動重播；列 uncertain 並要求 `recover retry --confirm-duplicate-risk`。啟動前失敗與前置 stale lease 直接 blocked；M1 不自動重試或 crash loop。舊 run 和 audit 不刪除，重試受 max_attempts 限制。

下一步 M2：在獨立測試 workspace/profile 核對真實 OMP 握手、會話持久化與連續兩次喚醒；模型憑證可用時才做模型 smoke，否則如實標記。M3 尚需 backoff、circuit breaker、自動重啟、agent restart 與更廣的故障注入；M4 尚需 systemd user unit 與部署驗證。沒有變更既有 OMP profile、使用者會話、認證或 linger 政策。

## 2026-10-18（Asia/Singapore）— M2

套件版本 0.3.0，維持 config/schema/API v1 相容。新增 `verify-omp [--with-model]`、`smoke.py` 與 opt-in 真實 OMP 測試。transport 模式實際檢查 RPC ready/get_state/get_messages、settled 與 session 身分，不呼叫模型；模型模式在獨立暫存 workspace/state 啟動 daemon，派發 task A 兩次及 task B 一次，驗證隨機 token 續聊和歷史隔離，結束時清理。輸出只含檢查結果，不含模型文字或憑證。

`[agent]` 支援可選 model selector 及 tools/extensions/skills/rules 四個布林開關，預設保留原行為；verify-omp 將四者停用。沿用選定的 executable/model/profile 與既有認證，不改使用者配置或既有 session，也不自動登入。

本機真實 OMP 18.10.4、Python 3.12.3 驗證通過：

- RPC v1 握手、關聯式狀態與歷史查詢。
- 透過獨立 daemon 執行真實模型回合，模型 completed、session settled 且進程正常退出。
- 同 task 兩次喚醒保持 session file/ID，第二次正確回覆第一輪的隨機 token。
- 不同 task 使用獨立 session file/ID，第二個 task 的歷史沒有第一個 task 的 token。

實測修正：OMP 在初輪前會回報尚未存在的 session 路徑；對於沒有任何 dispatch intent 的前置失敗，人工 retry 可清除這種暫定 binding，但不清除曾可能執行過 prompt 的會話。成功判定延後至 settled、session 已落盤且 OMP 正常退出；持久化 error notice 或非零 disposal exit 保留 uncertain，不啟動 after_turn。RPC 診斷新增受控錯誤碼，不輸出原始 stderr/provider message。

guardian 改為 Linux child subreaper，收養並清理自有工具後代，避免掃描整機 /proc 導致清理成本受其他程序數量影響。整合測試的正常收尾改用 1 秒 grace；另外以 50ms 的非合作 Agent 測試驗證仍會有界強制終止。多 client 測試為持久寫入工作量設定獨立 turn budget，並保留「釋放前仍 running」及全部狀態／串行斷言；專用 timeout 測試仍保留短截止時間。

重現命令：

```bash
.venv/bin/python -m pip install .
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" .venv/bin/python -m unittest discover -s tests -v
.venv/bin/oicoach-runtime verify-omp --config "$PWD/.runtime/config.toml" --with-model --json
```

模型模式會發送三個小型模型回合；未提供 --with-model 時會明確 SKIP 模型驗證。CI 沒有本機 OMP 配置時跳過 opt-in real transport，其餘 fake 流程不需要 API key。其他 OMP／Python 版本與遠端 CI 尚未在本輪實測。

最終驗證結果：0.3.0 wheel 建置／安裝成功；從已安裝套件執行 **64 項測試，63.516 秒，全部通過、無跳過**（本機顯式提供真實 OMP 配置）。獨立 `verify-omp --with-model` 的 rpc_transport、model_selected、same_task_resume、task_isolation 均 PASS。`git diff --check` 通過。測試 daemon 與暫存資料皆已清理，沒有安裝或啟用 systemd 服務。

下一步 M3：安全的啟動前重試、backoff/circuit breaker、自動重啟與 agent restart。M4 的 systemd 部署、正式 OI workflow/Tijian 接入，以及 L2 長期筆記／L3 歷史檢索仍未完成；本輪的續聊驗證只證明 L1 session 上下文可延續。

## 2026-10-18（Asia/Singapore）— M3

套件版本 0.4.0，沿用 SQLite schema/config/API v1。核心新增 `supervisor.py` 與 Store 的原子策略持久化；啟動階段未有 dispatch intent 的傳輸／進程失敗，可在 max_attempts 內重試，每次保留獨立 failed run，耗盡後事件 dead_letter。原定 due time、dedupe fingerprint 不變；workflow、session 身分與路徑錯誤不進入自動重試。已派發 prompt 的未知結果始終 uncertain，不因 backoff 或 daemon 重啟而重送。

全域 backoff 採指數上升、上限及可配置 jitter。故障時間窗達上限則 circuit open，持久 next_start_utc/open_until_utc；冷卻後串行放行一個半開 probe，成功清空策略，失敗重新冷卻。provider error 與執行中 crash 也可增加故障計數，但不重播其事件。策略、run 結果、事件狀態和 audit 同交易；CLI 在冷卻期間仍可查詢／排程／CAS 更新。新增 cooldown_seconds/jitter_ratio，舊 TOML 可省略。

新增 `agent restart --task ID [--workflow ID] [--reset-circuit]` 與 `agent show RESTART_ID`。控制工作 requested/running/succeeded/failed/interrupted 持久化；相同 task 的 active run 中斷，其他 task 等待完成後再串行 probe。fresh process 以原 session 檢查 ready/get_state 後回收，不送 prompt，中斷事件仍保留 uncertain。明確 reset 且 probe 成功才清除 supervisor 計數／冷卻，不重設事件 attempt，不清除 uncertain。未完成的控制工作在 daemon reconcile 時 interrupted，不自動重做。

監督補強：adapter 用 pidfd 對自有 guardian 發信號，獨立監控其 returncode，避免 orphan 持有 pipe 而漏判退出。guardian 與 Runtime 皆使用 subreaper；OMP root 具有 guardian-death 保護並繼承 flock，guardian 被 SIGKILL 時也會停止 OMP 並收養清理同組工具。停止流程共享 cancellation-shielded cleanup task，保留有界 abort/EOF/TERM/KILL 及清理，不因外層取消而提早關閉 pidfd。

已在本機真實 systemd 255 user manager 用 transient --collect unit 驗證兩次 daemon SIGKILL：未到期 queue 自動恢復完成；執行中 prompt 重啟後 uncertain、attempt=1，不重播，後續健康事件正常。使用 fake OMP，測試 finally 停止並回收 UUID unit，沒有安裝長期服務，也沒有設定 enable/linger。本輪真實 OMP 模型三回合回歸的 transport、同 task 續聊與跨 task 隔離亦 PASS。

重現完整本機驗證（第二個環境變數會建立、終止及回收暫時性 user unit）：

```bash
.venv/bin/python -m pip install .
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" OICOACH_TEST_SYSTEMD=1 \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/oicoach-runtime verify-omp --config "$PWD/.runtime/config.toml" --with-model --json
```

最終驗證結果：0.4.0 wheel 建置／安裝成功；從已安裝套件執行 **81 項測試，93.800 秒，全部通過、無跳過**（本機顯式啟用真實 OMP transport 與 transient systemd 驗證）。獨立真實模型三回合驗證亦通過；包含取消 stop 等待者時 cleanup 仍會完成的回歸測試。

CI 未設 opt-in 變數時跳過真實 OMP transport 與 systemd，fake／單元主流程仍無需模型憑證。下一步 M4 為正式 systemd user unit、安裝／停用及部署診斷；正式 OI workflow/Tijian 接入、L2/L3 記憶仍未實作。

## 2026-10-18（Asia/Singapore）— Tijian 適配，0.5.0

按使用者要求，先處理 Tijian，M4 正式部署繼續後置。以 SSH 拉取 `zjpanhongxuan/Tijian`，核對 commit `b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f`。此版本沒有原生 MCP，已有 JSON CLI。新建 `integrations/tijian_catalog.py`、`tijian.py`、`tijian_mcp.py`，映射 26 個操作，涵蓋題面與 metadata、題庫管理、狀態／歷史、難度／品質、搜尋、向量重算入口、每日計畫、日報、題單及備份。GUI-only 業務未虛構成 API，完整清單及限制見 `docs/tijian.md`。

新增 `[tijian]` 可選配置（舊配置預設停用），以及 `tijian tools/doctor/call/mcp-config/mcp`。JSON CLI 適配無第三方依賴，stdio MCP 使用官方 SDK extra `mcp>=1.28,<2`；CI 安裝 extra 執行真正 SDK discovery/call。參數型別/schema、大小及 flag-like token 均驗證；原始 envelope 和新增 metadata 欄位保留。read_only 可限制業務寫入／檔案匯出；破壞操作顯式 confirm 或 dry_run。寫入進程啟動後的未知結果為 outcome=uncertain，不自動重送；stderr 不回傳原文。

Tijian 子進程沿用 parent-death guardian，避免 MCP bridge 被 SIGKILL 後原生工具脫離 Runtime 程序組繼續存活。新增取消、逾時、bridge SIGKILL、輸出超限、stderr flood 和無重送回歸。Runtime envelope 提供 configured_not_verified 的 CLI 發現入口；`examples/prepare_tijian.py` 生成全新、預設唯讀的 workflow/workspace/.mcp.json，拒絕覆寫既有目錄，不修改全域 OMP。

回應使用者要求，另在 `/tmp` 安裝 Rust 1.99.1（未修改 shell/PATH），從原碼執行 command/control/core/usecase 測試。Linux 缺 GTK／WebKit 開發依賴，未建完整 Desktop；新增 `examples/build_tijian_source_cli.py`，使用原樣 run_cli／cli_control，連結真正上游 crates，建出驗證用 CLI 與要求 harness mode 的種子資料入口。上游 checkout 保持乾淨；沒有重寫其業務規則或直接改 DB。

真實上游 SQLite/usecase 整合已驗證狀態／評分／歷史、計畫／日報、搜尋、題單匯出匯入、ZIP 備份校驗與刪題後還原、dry-run 不改狀態，以及 SDK MCP 寫入後讀回。真實 OMP 模型透過 MCP 讀到原碼題庫，並檢查實際 toolResult。模型測試停用內建工具／extensions，等待 Tijian MCP 工具可見，再使用明確工具名；不只檢查模型完成訊號。

上游 command/control/core 合計 432 passed、1 ignored；首次 usecase 平行測試有一項向量 upsert 失敗（ok=0，預期 1），未修改上游。該測試獨立重跑通過，整個 usecase 串行重跑 70 passed，抽出的原有 CLI／routing 測試另 8 passed。沒有將首次失敗抹去或宣稱平行全套無條件通過。

最終 0.5.0 wheel 建置／安裝成功。從已安裝套件執行 **103 項測試，198.014 秒：102 通過、1 跳過，無失敗**。真實 OMP transport、兩種 MCP 模型路徑、transient systemd 故障恢復與 5 項真實原碼整合全部執行；唯一 skip 是另行安裝的正式 Tijian 執行檔驗證（本輪使用原碼 CLI 驗證入口）。`pip check` 及 `git diff --check` 通過，上游 checkout 沒有修改。

```bash
.venv/bin/python -m pip install '.[tijian]'
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_SYSTEMD=1 \
OICOACH_TEST_TIJIAN_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_TIJIAN_SOURCE_CLI=/tmp/paveway-tijian-build/debug/tijian-source-cli \
OICOACH_TEST_TIJIAN_SOURCE_SEED=/tmp/paveway-tijian-build/debug/tijian-source-seed \
  .venv/bin/python -m unittest discover -s tests -v
```

完整重現命令及依賴見 `docs/tijian.md`。目前不驗證完整 Desktop UI／運行中 Desktop projection routing、真實 OJ 連線、需 Desktop 執行器的向量重算；這些依賴由上游提供，適配器在不可用時保留真實錯誤。正式 OI 教練策略、L2/L3 記憶與 M4 仍未實作。

## 2026-10-18（Asia/Singapore）— 整個專案運行回歸

依使用者要求，先不處理記憶功能，重新建置／安裝目前 0.5.0 套件並執行全部本機 opt-in 測試。新增 `test_project_cli_daemon_model_tijian_and_persisted_completion`：從範例產生全新配置，排程 CLI 退出後由獨立 daemon 執行真實 OMP → MCP → Tijian 原碼 CLI，核對事件 attempt=1、ack/completed/settled/exit=0 及成功落盤；停止 daemon 後，再以原 session 查歷史，確認唯一成功工具結果與真正題目資料，沒有再次送 prompt。

本輪 **104 項測試，298.446 秒：103 通過、1 跳過，無失敗**；skip 為未另行安裝的正式 Tijian 發行版，原碼 CLI 的 6 項整合均已執行。獨立真實 `verify-omp --with-model` 四項檢查全部 PASS，包含透過 daemon 的同 task 兩次喚醒接續與跨 task 隔離。依賴、SQLite integrity／WAL、權限與 CLI 檢查通過。

既有 `.runtime/config.toml` 的 doctor 回 ok=true，但沒有安裝 workflow、daemon 離線；本輪是在隔離配置中實際啟動服務驗證，不代表這份既有配置已完成部署。未變更 Runtime 業務程式或記憶功能。完整範圍、重現命令、產物位置與環境限制見 [運行測試報告](runtime-testing.md)。

## 2026-10-18（Asia/Singapore）— M4 與初版教練入口，0.6.0

完成 `service.py` 的 user unit render/install/status/logs/start/stop/restart/enable/disable/uninstall。以絕對 venv Python／config、PATH 快照生成 unit，先 systemd-analyze verify 再發佈；安裝不啟動或啟用。限制操作當前 config 的托管 unit，不覆寫其他服務。含 control-group 清理、停止預算、啟動限制及重啟策略，不自動改 linger。實機測試修正 WorkingDirectory 不使用 ExecStart 引號語法的問題後，特殊路徑與完整生命週期通過。

新增 package-data 的 coach/example 模板及 `workflow create/list/show/connect-tijian`；安裝後為可編輯外部 workflow，教學策略不進 Scheduler。初版 coach 支援安排、進度與復盤、區分提示/題解使用；Tijian 不可用可用輸入降級。自動跟進預設關閉，明確 opt-in 最多建立一個 after_turn 事件；不能以模型文字冒充已排程。

新增 `ask [--wait]`、`events wait` 和 `runs output`。立即請求 at=now 使用穩定 fingerprint，重送相同 scope/key 幂等；等待超時不取消。回合完全 settled 後只查本輪最終 assistant 文字，最多 16 KiB UTF-8，與正常退出後的成功狀態原子保存，不記錄正文日誌。local-only 不借用舊文字；讀取時收到持久化/協議錯誤仍 uncertain。修正 IPC 將 RuntimeContractError 作 ValueError 重新包裝的問題，conflict 等業務碼得以保留。

README 已完整重寫，包含環境、初次設定、教練請求與回覆、systemd 部署/停用、workflow、Tijian、全部配置、排程/查詢/恢復、備份升級、測試及限制。另提供三份 coach JSON 輸入示例，更新 deployment/API/architecture/operations 等文檔。記憶依使用者要求暫緩；未將完整 Tijian GUI/OJ/向量 executor 的環境驗收說成完成。

最終從新安裝的 wheel 跑 **118 項測試，325.843 秒：117 通過、1 跳過，無失敗**；skip 為獨立正式 Tijian 發行版，真實原碼、模型與兩種 systemd 測試均已執行。獨立真實三回合 verify-omp 四項 PASS。README CLI walkthrough、pip check、git diff --check 通過；測試 unit 和程序已回收，未啟用預設服務或 linger。更細驗收範圍見 [運行報告](runtime-testing.md#060-後續驗證m4-與-coach)。

其後將既有 coach 用例擴充為同一 task 的 plan/checkin/review 三回合，產品程式未變；定向重跑 1 項、58.016 秒通過，確認後兩輪 ask --wait、三個 run 的回覆和初輪快照保留。README 連結、shell/TOML 語法與 JSON 示例均校驗通過，最終 wheel 已以完整 README 重新建置安裝。

## 2026-10-18（Asia/Singapore）— 0.7.0 本地記憶

依使用者新要求完成 L2/L3，不再暫緩。先檢查本機 OMP 18.10.4 的原生 memory-backend/mnemopi 契約，確認其後端、bank、cwd 與模型設定獨立；不接管全域設定或重做 Agent。L1 沿用現有 per-task resume；新增 `memory.py` 提供標準庫 SQLite scoped 筆記與唯讀原文檢索，不引入向量服務。

DB schema 1→2 新增 memory_notes，init/serve 原子遷移、doctor 只讀；配置/manifest/API 仍為 v1。新增 `memory put/show/list/delete/recall` 與 `history search/show/messages`，沿用私密同 UID socket，所有筆記寫入和 audit 同交易。筆記有 workflow/task scope、kind、pinned、來源事件、有效期、版本 CAS，刪除清空內容但保留版本墓碑防舊請求復活。歷史搜尋既有輸入/回覆、保留 failed/uncertain；原生 JSONL 只讀校驗 header/path/ownership，明確列出所有分支的有界文字，排除 thinking/image。

workflow manifest 可選 memory_enabled，舊設定省略為 false，新 coach 預設 true。Scheduler 只注入最多 8 KiB 有效筆記及通用工具入口，不寫教學/摘要規則。外部 coach 模板處理明確偏好/更正/忘記與有證據的訓練摘要，區分 hypothesis 與事實，版本衝突重讀，只有實際工具成功才聲稱保存。舊 workflow 不自動覆蓋；啟用及升版/新 task 方法已寫入完整 README 與 [記憶說明](memory.md)。

真實模型定向驗收 1 項、80.332 秒通過：模型呼叫 CLI 保存含隨機識別碼的共享偏好，重啟 daemon 後，全新 task 在禁用工具下讀回注入內容；原文查閱亦通過。新增單元 7 項及 fake OMP 記憶用例 2 項通過，後續補強遷移逐表保留與「筆記保存後 crash、run uncertain、重啟不重播」並定向重跑通過。完整安裝包回歸結果見 [運行報告](runtime-testing.md)。

刪除筆記不刪歷史會話/備份；當前功能是本地字面檢索，非語義/向量檢索、全歷史自動蒸餾或跨設備同步。沒有修改 OMP 全域設定、啟用預設 systemd 服務或 linger。

最終安裝包全量 **128 項，391.692 秒：127 通過、1 跳過，無失敗**，唯一 skip 仍為正式 Tijian 發行版獨立檢查。真實 coach 三模式、記憶跨 task、Tijian 原碼/模型/MCP 全鏈路及 systemd 均通過。無 opt-in 主回歸 115 通過、13 跳過；補強後遷移 7 項及 Runtime 記憶 2 項定向重跑通過。README 已同步 0.7.0 的完整操作、升級和限制，具體命令與輸出檔見運行報告。

## 2026-10-18（Asia/Singapore）— 0.8.0 終端 UI

新增標準庫 curses `tui.py` 與 `ui --config PATH [--workflow ID] [--task ID]`。八頁提供概覽/診斷、教練對話、事件安排與取消、記憶增改刪查、歷史/原文分頁、workflow 選擇/建立、服務安裝啟停/日誌及恢復查詢。所有操作由背景執行緒呼叫公開 CLI 子進程，不直接開 DB 或發模型 prompt，不更改既有 API/config/DB/workflow 版本。

正文以 0600 私密臨時檔傳入、清理後回收；argv 不用 shell，連字符開頭值以明確 option=value 傳遞。模型/工具輸出過濾終端控制符，中文依顯示寬度折行，支援多行及縮放。工作執行中介面可操作或退出；q/Ctrl+C 恢復終端，只清理界面自有 CLI 子進程，不停止獨立 daemon。提交逾時保留原 scope/payload/key，以 v 冪等核對，未確認前不產生新 key；版本衝突不盲目覆蓋。服務停止與刪除/取消需明確確認，恢復頁不直接重播 uncertain。

安裝包無 opt-in 主回歸 **138 項，127.383 秒：125 通過、13 跳過，無失敗**；之後新增真實模型 UI 用例定向 **1 項、15.748 秒通過**，再補強 UTF-8 locale 邊界後重跑 UI 專項 **11 項、5.662 秒通過**。實際 PTY 測到中文輸入、縮放、q 退出、termios 恢復及退出後 daemon 完成原事件；真實模型另驗 UI controller 到 CLI/daemon/OMP/最終回覆與畫面資料。時間點與輸出檔見 [運行報告](runtime-testing.md)。

README 與 [終端 UI 說明](terminal-ui.md) 補齊啟動、首次部署、按鍵、分頁、生命週期與限制。目前顯示最終回覆，沒有 token/tool 即時串流；UI 不自動開啟 systemd/linger，不接管或替換 OMP 自身的 TUI。
