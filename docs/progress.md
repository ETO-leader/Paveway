# 實作進度

## 2026-10-17（Asia/Singapore）— M0

倉庫起始只有 README、LICENSE、專案介紹與工程 Skill，沒有應用程式或測試。既有未追蹤的 `.codex/`、`documents-for-agents/` 保留。

本機探查：Linux、Python 3.12.3、systemd 255；`omp --version` 為 `omp/18.10.4`。本機 `omp --help` 有 `--mode`、`--cwd`、`--session-dir`、`--continue`、`--resume`、`--no-ui`、`--profile`。只驗證 CLI 能力，未測試模型認證或真正 RPC 回合。

參考上游 [CLI 文件](https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md) 與 [RPC 文件](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md)；上游 main 可能與安裝版本不同。M2 必須以實測確認 protocol ready、request id、prompt_result 和 session_settled，不把 ack 當完成。

M0 已交付可安裝 Python 套件、init/doctor、配置驗證、SQLite v1 migration、時間與 ID 函式、外部 workflow loader、架構與操作契約。測試使用標準庫 unittest，不需要模型金鑰。

本機驗證：wheel 建置與安裝成功；`python -m unittest discover -s tests -v` 共 28 項通過（包括 migration 故障回滾、未知 schema 保留、私密權限、模板路徑限制及 CLI 子進程）。在工作區 `.runtime/` 執行 init/doctor，DB 為 schema v1、WAL、quick_check ok，OMP 能力探測 PASS；未安裝 workflow、RPC 未驗證、daemon 未實作均回 WARN。沒有執行真實模型回合、RPC 故障注入或 systemd 部署測試。已加入 Python 3.11/3.12/3.13 的 CI 定義；本機只驗證 3.12.3，尚未觀察遠端 CI 結果。

下一步 M1：先完成 flock 與 SQLite queue 交易 API，再加入有界 Unix IPC、scheduler、fake OMP RPC 和獨立 daemon 測試。M1 驗收應含結束 CLI 後事件仍執行、重啟保留未到期事件、20 個同時到期事件串行，以及 dispatch 邊界崩潰後不重播 uncertain。

仍待 M2–M4：真實 OMP 會話接續、完整故障注入／backoff／熔斷、人工恢復、systemd 部署。正式 OI GPT workflow 和 Tijian MCP 接口另行接入。

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

完整重現命令及依賴見 `docs/tijian.md`。目前不驗證完整 Desktop UI／運行中 Desktop projection routing、真實 OJ 連線、需 Desktop 執行器的向量重算；這些依賴由上游提供，適配器在不可用時保留真實錯誤。正式 OI GPT策略、L2/L3 記憶與 M4 仍未實作。

## 2026-10-18（Asia/Singapore）— 整個專案運行回歸

依使用者要求，先不處理記憶功能，重新建置／安裝目前 0.5.0 套件並執行全部本機 opt-in 測試。新增 `test_project_cli_daemon_model_tijian_and_persisted_completion`：從範例產生全新配置，排程 CLI 退出後由獨立 daemon 執行真實 OMP → MCP → Tijian 原碼 CLI，核對事件 attempt=1、ack/completed/settled/exit=0 及成功落盤；停止 daemon 後，再以原 session 查歷史，確認唯一成功工具結果與真正題目資料，沒有再次送 prompt。

本輪 **104 項測試，298.446 秒：103 通過、1 跳過，無失敗**；skip 為未另行安裝的正式 Tijian 發行版，原碼 CLI 的 6 項整合均已執行。獨立真實 `verify-omp --with-model` 四項檢查全部 PASS，包含透過 daemon 的同 task 兩次喚醒接續與跨 task 隔離。依賴、SQLite integrity／WAL、權限與 CLI 檢查通過。

既有 `.runtime/config.toml` 的 doctor 回 ok=true，但沒有安裝 workflow、daemon 離線；本輪是在隔離配置中實際啟動服務驗證，不代表這份既有配置已完成部署。未變更 Runtime 業務程式或記憶功能。完整範圍、重現命令、產物位置與環境限制見 [運行測試報告](runtime-testing.md)。

## 2026-10-18（Asia/Singapore）— M4 與初版GPT入口，0.6.0

完成 `service.py` 的 user unit render/install/status/logs/start/stop/restart/enable/disable/uninstall。以絕對 venv Python／config、PATH 快照生成 unit，先 systemd-analyze verify 再發佈；安裝不啟動或啟用。限制操作當前 config 的托管 unit，不覆寫其他服務。含 control-group 清理、停止預算、啟動限制及重啟策略，不自動改 linger。實機測試修正 WorkingDirectory 不使用 ExecStart 引號語法的問題後，特殊路徑與完整生命週期通過。

新增 package-data 的 coach/example 模板及 `workflow create/list/show/connect-tijian`；安裝後為可編輯外部 workflow，教學策略不進 Scheduler。初版 coach 支援安排、進度與復盤、區分提示/題解使用；Tijian 不可用可用輸入降級。自動跟進預設關閉，明確 opt-in 最多建立一個 after_turn 事件；不能以模型文字冒充已排程。

新增 `ask [--wait]`、`events wait` 和 `runs output`。立即請求 at=now 使用穩定 fingerprint，重送相同 scope/key 幂等；等待超時不取消。回合完全 settled 後只查本輪最終 assistant 文字，最多 16 KiB UTF-8，與正常退出後的成功狀態原子保存，不記錄正文日誌。local-only 不借用舊文字；讀取時收到持久化/協議錯誤仍 uncertain。修正 IPC 將 RuntimeContractError 作 ValueError 重新包裝的問題，conflict 等業務碼得以保留。

README 已完整重寫，包含環境、初次設定、GPT請求與回覆、systemd 部署/停用、workflow、Tijian、全部配置、排程/查詢/恢復、備份升級、測試及限制。另提供三份 coach JSON 輸入示例，更新 deployment/API/architecture/operations 等文檔。記憶依使用者要求暫緩；未將完整 Tijian GUI/OJ/向量 executor 的環境驗收說成完成。

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

新增標準庫 curses `tui.py` 與 `ui --config PATH [--workflow ID] [--task ID]`。八頁提供概覽/診斷、GPT對話、事件安排與取消、記憶增改刪查、歷史/原文分頁、workflow 選擇/建立、服務安裝啟停/日誌及恢復查詢。所有操作由背景執行緒呼叫公開 CLI 子進程，不直接開 DB 或發模型 prompt，不更改既有 API/config/DB/workflow 版本。

正文以 0600 私密臨時檔傳入、清理後回收；argv 不用 shell，連字符開頭值以明確 option=value 傳遞。模型/工具輸出過濾終端控制符，中文依顯示寬度折行，支援多行及縮放。工作執行中介面可操作或退出；q/Ctrl+C 恢復終端，只清理界面自有 CLI 子進程，不停止獨立 daemon。提交逾時保留原 scope/payload/key，以 v 冪等核對，未確認前不產生新 key；版本衝突不盲目覆蓋。服務停止與刪除/取消需明確確認，恢復頁不直接重播 uncertain。

安裝包無 opt-in 主回歸 **138 項，127.383 秒：125 通過、13 跳過，無失敗**；之後新增真實模型 UI 用例定向 **1 項、15.748 秒通過**，再補強 UTF-8 locale 邊界後重跑 UI 專項 **11 項、5.662 秒通過**。實際 PTY 測到中文輸入、縮放、q 退出、termios 恢復及退出後 daemon 完成原事件；真實模型另驗 UI controller 到 CLI/daemon/OMP/最終回覆與畫面資料。時間點與輸出檔見 [運行報告](runtime-testing.md)。

README 與 [終端 UI 說明](terminal-ui.md) 補齊啟動、首次部署、按鍵、分頁、生命週期與限制。目前顯示最終回覆，沒有 token/tool 即時串流；UI 不自動開啟 systemd/linger，不接管或替換 OMP 自身的 TUI。

## 2026-10-19（Asia/Singapore）— 0.9.0 對話式 UI 重做

依使用者反饋重現操作不執行／混亂問題，實際啟動本機 OMP 18.10.4 觀察其終端安排，改用 prompt_toolkit 常駐底部輸入框、上方對話、slash 補全及方向鍵選擇器。Enter 發送或執行選中命令，Tab 只補全，Ctrl+J/Alt+Enter 換行，數字/q 均為普通文字；移除數字切頁和 Ctrl+D 提交表單。`ui_client.py` 保留公開 CLI 子進程傳輸，UI 不直接讀寫 DB。版本升至 0.9.0，API/config/workflow/DB 版本不變。

真實 PTY 連續新增/修改筆記、排程及取消測出保存確認會清除後續命令，以及最後一欄正文與下一命令拼接的競態。現在提交即清空輸入，保存確認只還原未被修改的原草稿；忙碌時一條 slash 命令可排隊，狀態可見、可取消，確認後執行並檢查原 scope/generation。修正預填編輯器光標回到開頭、表單回上一項後丟失已填值；保存失敗保留答案。背景回覆可在編輯表單時收取，時間順序顯示，不覆蓋新草稿。

UI 專項 18 項通過（11.917 秒）；真實模型定向驗收 1 項通過（11.367 秒），涵蓋 UI controller → CLI → daemon → OMP → 最終回覆與畫面資料。實際 PTY 另驗中文、多行貼上、slash Enter 一次執行、方向鍵選環境、縮放、termios 恢復和退出後任務繼續；pyte 僅為測試依賴，CI 同步安裝。最終完整回歸見 [運行報告](runtime-testing.md)。README 和終端操作文件已重寫，沒有啟用預設服務或修改 linger。目前仍顯示最終回覆，沒有 token/tool 即時流。

最終安裝包無 opt-in 主回歸 **147 項，119.460 秒：133 通過、14 跳過，無失敗**（`/tmp/paveway-ui09-final-regression.log`），包含最終 UI 的 18 項。skip 為需要指定模型、systemd 或 Tijian 真實環境的 opt-in；其中 UI 真實模型另行通過。 文件連結、shell/TOML 示例、pip check 和 git diff --check 通過。

## 2026-10-20（Asia/Singapore）— systemd 憑證與模型錯誤顯示

使用者回報 test01/training 的 `test` 失敗，查明 OMP 回傳 HTTP 401，user service 沒有繼承終端 OPENAI_API_KEY；原有 Runtime 把 prompt_result.error 丟棄，UI 只能顯示無最終回覆。新增 `service install --capture-env NAME`（可重複），明確捕獲選定變量至配置旁 0600 的受管理環境檔，unit 僅載入路徑，不含秘密；普通安裝保留既有檔案，捕獲合併／更新，缺失變量、非法內容或不安全檔案拒絕修改。加入 gitignore，無全域 manager 環境或 OMP 認證變更。實測 systemd EnvironmentFile 是原樣路徑而非 ExecStart 引號參數，已用含空格/引號/百分號/美元符的真實 unit 和值驗證。

OMP 適配器按已安裝 18.10.4 的 prompt_result.error/httpStatus 分類失敗，只持久化受控摘要，不將任意供應商回應或請求寫入 DB／journal。使用既有 event/run 錯誤欄位，無 DB/API 版本變動；對話、歷史恢復／詳情、事件清單顯示原因與操作提示。401、403、404、408/timeout、429、5xx 和中止有區分，未知保留通用失敗。仍等待 settled 並正常回收，不自動重播。

已更新實際 enabled user service 並重啟，核對 daemon 的憑證與 shell 完全相同（只輸出比對結果）。同一 systemd daemon 新測試任務成功保存 `CONNECTION_OK`（event e675777f-4186-4e2f-94dc-54b35b646c31，attempt 1），前一失敗連線檢查保存安全 401 摘要；經真實 CLI 讀回 history 到 UI controller，確認錯誤和成功回覆都可見。原 test 失敗記錄未修改、未重播。最終測試結果見 [運行報告](runtime-testing.md)。

最終主回歸 **152 項、140.787 秒：138 通過、14 跳過、無失敗**；另真實 systemd 定向 **2 項、16.530 秒通過**。UI 19 項、公開接口與文件檢查通過；已部署服務 enabled/active，實際模型 CONNECTION_OK。詳細命令與驗收邊界見運行報告。

## 2026-10-20（Asia/Singapore）— 0.10.0 UI 自定义系统提示词与用户 Skill

新增 `/prompt`（查看、编辑 append/replace、确认恢复默认）及 `/skills`（方向键管理、新增、查看、编辑、启停、确认删除）。使用现有底部多行输入区及公开 CLI 子进程，保存冲突保留草稿。新增 `workflow customize`，以当前 revision 和文件锁做 CAS、0600 原子保存，按 workflow 隔离；缺省不改变原有行为，不修改 OMP 全局配置、coach 模板或 manifest 版本。

`customization.py` 在派发时加载设置，OMP 启动前生成当前 run 的不可变私密快照，使用原生 --append-system-prompt/--system-prompt 及 --config 的 skills.customDirectories 加载。系统提示不混入普通事件 payload，Skill 生成原生 SKILL.md 由模型按描述发现/按需读取；`skills_enabled=false` 不加载，`tools_enabled=false` 时原生 OMP 不提供 Skill 读取目录，UI 提示限制。用户设置版本不改变既有会话绑定，下一轮可继续同一 task；旧历史及快照不抹除，已开始回合不受编辑影响。

真实 PTY 已完成多行提示词编辑、Skill 新增/编辑及预填追加、派发并核对快照、停用、恢复默认、确认删除。另验证并发保存的 conflict 保留草稿、其他 workflow 隔离及同 task 下一轮配置更新。真实 OMP 验收通过 append/replace 系统提示及原生 Skill 元数据；真实模型实际读 Skill 正文，回覆两个分别只存在于系统提示/Skill 正文的随机识别码，再用更新系统提示恢复同一 session，回覆新识别码。定向原生验收 **2 项、65.902 秒通过**（`/tmp/paveway-custom-real-acceptance.log`），未将指令文件冒充普通用户消息。最终完整回归见 [运行报告](runtime-testing.md)。README、新增 [自定义说明](customization.md) 和接口/架构/UI 文档已同步。

最终安装包主回归 **162 项、152.133 秒：146 通过、16 跳过、无失败**（`/tmp/paveway-custom-regression.log`），其中 UI 21 项；skip 为模型、Tijian 或 systemd 的 opt-in，原生自定义两项另行实际通过。 文件链接、shell/TOML 示例、pip check 和 git diff --check 通过。

本机已安装 0.10.0，在确认无活动回合后重启默认 user service，核对 enabled/active；test01 的自定义设置仍为默认 revision 0，未替用户填入示例提示词或 Skill，原模型凭证保持不变。使用中的旧 UI 需要关闭重开以加载新命令。

## 2026-10-20（Asia/Singapore）— 0.11.0 Git 用户配置与历史同步

实现独立 `git_sync.py`、`sync` CLI 和 `/sync` 方向键菜单/设置向导。使用专用用户数据 Git 仓库，原生 git init/fetch/merge/commit/push；不复用源码 origin，不强推。运行中的 daemon 热加载私密同步设置，在独立后台线程周期运行；UI 可立即请求并轮询结果，退出 UI 后仍运行。Git/网络错误只记同步状态，不进入 Agent 熔断或阻塞训练。

按 workflow 同步 UI prompt/User Skill；内容寻址版本图记录父版本，单 head 自动按 CAS 导入，多 head 保留本机和远端版本并要求明确选择，不做覆盖式合并。新设备缺少 workflow 时待绑定本机 workspace，使用本地模板与权限，不复制他机路径。按 device_id 归档终态 run 输入/最终回复/安全错误及经验证的 user/assistant 文字原文；过滤 thinking、工具调用与 toolResult，保留 all_branches 身份。活动队列、SQLite、凭证/服务环境文件、原生可执行会话和 L2 笔记不导入其他设备。原文活动检查同时考虑导出事务后新开始的回合，变化/不完整档案保留前次导出。

双设备实际 bare remote 专项最初 8 项通过（17.419 秒），补强读取校验后 9 项通过（21.098 秒）。两 daemon 自动同步/失败不阻塞模型专项 2 项、12.326 秒通过。UI 22 项、27.151 秒通过，含实际 PTY 填写 remote、后台 push、查询同步历史与暂停。主回归首轮 173 项、175.003 秒：157 通过、16 跳过；最终安装包补强后结果见 [运行报告](runtime-testing.md)。测试未依赖互联网 Git 服务，不声称已验证未指定的 SSH/HTTPS remote。

本机已创建私密独立 Git 仓库并启用本地自动保存；daemon 在空闲时升级至 0.11.0，保持 enabled/active。后台实际归档 4 个终态 run、2 份原生文字会话，生成 2 个 Git 提交；未追踪 SQLite、机器 config、凭证文件，并核对当前模型 key 未出现在导出 JSON 中。未收到用户的数据 remote，因此保持 remote 空，不联网推送、不借用代码仓库 origin。README、[Git 同步说明](git-sync.md)、接口、架构和 UI 操作文件已更新。

最终安装包完整回归 **174 项、178.887 秒：158 通过、16 跳过、无失败**（`/tmp/paveway-sync-final-regression.log`）。包括同步专项 9 项、两 daemon 自动同步/故障隔离、UI 22 项和所有既有无 opt-in 流程；需要真实模型/Tijian/systemd 环境的 opt-in 仍明确跳过，本轮未冒充重新执行全部旧版实机验收。 文档链接、shell/TOML 示例、pip check 和 git diff --check 通过。


## 2026-10-20（Asia/Singapore）— 0.11.1 默认本地 Git 与 UI remote 入口

按用户补充要求，daemon 初次启动自动建立 `state_dir/user-data-git`，同一 Runtime 内所有GPT、workflow/task 共用一个独立仓库；无需先填写 remote。无 remote 时只产生本地提交，不 fetch/push，也不采用源码仓库 origin。已有配置、设备身份、提交与暂停开关不被初始化覆盖，初始化失败独立记录并退避。

新增 `/sync remote` 单字段 UI 表单和 `sync remote --remote URL` CLI。可添加、修改或清空远端；清空后保留本地历史继续归档。修改 remote 保留仓库、分支、设备身份、间隔和暂停状态。`/sync` 菜单显示仅本地/远端模式，并明确所有GPT对话共用仓库。README、同步说明、UI、接口、架构和运维文档同步更新。

新增实际本地 Git 默认初始化、多个 workflow 共用归档、无网络操作、初始化幂等、远端修改/移除保留身份/提交/暂停，以及无 daemon 配置入口测试；扩展真实 PTY 测试，验证清空 remote 后没有 origin 且保留暂停状态。故障隔离测试在 daemon 启动前配置无效 remote，避免把首次初始化持锁的时序混入网络失败注入。


完整安装包回归 **178 项、195.150 秒：162 通过、16 跳过、无失败**（`/tmp/paveway-default-git-regression.log`），命令 `.venv/bin/python -m unittest discover -s tests -v`。跳过项仍为真实模型/Tijian/systemd 的 opt-in；本次双设备远端以实际本地 bare Git 仓库验证，未连接未配置的互联网远端。依赖检查、编译、文档本地链接和 `git diff --check` 通过。

本机已安装 0.11.1，并在确认 active_runs 为空后重启 user service。服务 enabled/active，daemon running；核对原同步设置、设备身份与仓库路径完全一致，remote 仍为空且 Git 没有 origin。重启后自动归档成功，保留现有 4 条终态 run，未上传远端。旧 UI 需关闭重开以加载 `/sync remote`。


## 2026-10-20（Asia/Singapore）— 0.12.0 三类 Skill 同时加载

用户 Skill 分为 OI Introduce（oi-introduce）、user information（user-information）、personal skill（personal-skill）。`/skills` 按分类管理，新增时选择分类；`/skills category NAME` 或菜单“移动分类”调整已有项目，保留正文与启停状态。CLI skill-put 增加可选 --category；省略时新建默认 personal-skill、编辑保留原分类。旧记录缺字段时兼容解释，不改写内容寻址记录或已有历史。

每轮不可变快照将三类已启用 Skill 正文同时加入真实系统上下文，按三类及名称稳定排序；append/replace 均加载，停用项与全局禁用除外，工具关闭不影响正文。原生 SKILL.md 与目录发现保留。分类经既有 CAS 原子保存与 Git 配置同步，不改 DB/schema，不填入未经用户提供的个人资料。

定向单元/Git/终端测试 **41 项、62.682 秒通过**（`/tmp/paveway-skills-targeted.log`）。实际安装的 OMP RPC get_state.systemPrompt 验收 **1 项、3.833 秒通过**（`/tmp/paveway-skills-native.log`），验证 append/replace 中三类完整正文同时存在；该原生验收没有调用模型。完整回归另验证扩展 PTY 中分类移动与快照不变。


完整安装包回归 **180 项、214.227 秒：164 通过、16 跳过、无失败**（`/tmp/paveway-skills-regression.log`），命令 `.venv/bin/python -m unittest discover -s tests -v`。包括扩展真实 PTY 分类选择/移动、正文同时入系统快照、移动后旧快照不变，以及既有 Git、记忆、故障恢复测试。16 项为模型/Tijian/systemd opt-in，实际 OMP 三类原生加载验收另行通过，未将其冒充模型调用。依赖、编译、文档链接与 git diff --check 通过。

本机已安装 0.12.0，在确认没有活动回合后重启 user service，核对 enabled/active、daemon running。原有自定义配置和 Git 设置保持一致，remote 仍为空；没有填入示例个人资料或替用户创建 Skill 正文。重开 UI 后 `/skills` 即显示三个分类，保存的启用内容从下次派发开始同时加载。


## 2026-10-20（Asia/Singapore）— 0.12.1 Skill 用途描述上限

按用户要求，将 Skill 用途描述的后端 UTF-8 字节上限与 UI 表单上限由 1024 调至 65536（64 KiB），并在表单标签及自定义文档中标明。独立临时配置验证 65536 字节中文描述可保存，65537 字节拒绝且不覆盖原内容；实际 OMP RPC get_state 确认完整描述与正文进入系统上下文，无模型调用。

现有自定义与 UI 回归 **29 项、36.499 秒全部通过**（`/tmp/paveway-skill-limit-tests.log`）；pip check、git diff --check 通过。已安装 0.12.1，在确认无活动回合后重启本机 user service，daemon running/服务 active，Git 设置保持原样。旧 UI 需重开以采用新的输入上限。


## 2026-10-20（Asia/Singapore）— 0.12.2 UI 恢复运行中请求与自动核对

调查用户的 QOJ 查询：事件 ef8f760c-3e6f-4e16-a056-91a5cbea49d9 于 15:33:00.715 UTC 创建，15:35:39.502 完成，实际约 159 秒。原生会话记录显示多轮 read/eval 工具与模型往返，最终回复在 settled 时已保存；回复说明 QOJ 登录限制和浏览器检查超时。本次未重跑网络请求，无法仅据已关闭 UI 的记录断言其当时未显示的唯一原因。

确认并修复 UI 恢复漏洞：恢复到运行中的 run 时，旧逻辑立即将其标记 seen，展示没有最终回复的占位，不设置 pending，后续不追踪。现在继续轮询至终态，仅对完成 run 去重；用户输入按 event 去重。等待时显示已用时间及最终回复显示方式；空闲时每 15 秒检查当前 task 最近 5 条 run，自动补上后台/其他窗口回复，避免仅靠重开触发恢复。只读核对，不重新 ask，不修改草稿。

UI 测试 **24 项、31.959 秒通过**（`/tmp/paveway-refresh-tests.log`），含新增运行中恢复、自动发现后台回复及不重复显示单元验证；实际 PTY 扩展覆盖退出后 daemon 仍执行、重进恢复等待、完成后无需手动刷新出现回复并保留草稿，原事件 attempt=1、run 数仍为 1。安装包已更新至 0.12.2；实际本机 QOJ 原回复也通过新版 UI client 恢复，核对没有创建新 run。此轮仅更新 UI，无需重启 daemon，正在打开的旧 UI 需重开一次以加载代码。依赖检查、git diff --check 通过。


## 2026-10-20（Asia/Singapore）— 0.13.0 Tijian 实际启用

用户反馈 test01 返回 tijian_disabled。检查确认旧 Runtime 配置关闭、没有已安装的 Tijian 执行文件，之前 /tmp 验收构建已不在。本次重新拉取上游，HEAD 与现有固定契约同为 b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f；将 Rust/cargo 与真实源码 CLI 构建放入 gitignored .runtime 持久目录，未修改全局 shell 配置或上游代码。

新增 `tijian setup --executable PATH [--read-only|--no-read-only]`：核对可执行文件、JSON help、本地搜索初始化、统计、计划及题单读取，成功后原子更新私密 Runtime 配置；失败保留原配置，检查期间并发更改报 conflict。只通过上游 canonical API 初始化 schema，不直接修改 SQLite、不插入测试题。工作目录 .mcp.json 保存本机路径，加入项目根忽略规则。新安装 stats 等纯读取不能创建数据库的问题经真实源码重现后由 setup 初始化流程处理。

旧适配与新 setup/部署定向 28 项、32.052 秒：26 通过、2 外部 opt-in 跳过（`/tmp/paveway-tijian-validated-tests.log`），含新增真正空 profile 初始化验证。真实源码库专项 6 项、52.586 秒：4 通过、2 模型项跳过（`/tmp/paveway-tijian-real-source-tests.log`），验证状态/历史/评分/计划/日报、题单/备份/恢复、MCP 调用及外部依赖失败语义。另执行真实模型完整项目链路 1 项、56.320 秒通过（`/tmp/paveway-tijian-model-project.log`）：CLI 退出后独立 daemon → OMP → MCP → 真正源码题库 → 持久完成，核对唯一真实工具返回、attempt=1。无向用户题库添加 fixtures。

本机 test01 已启用，doctor --with-data 全 PASS，官方 MCP SDK 实际发现 26 项工具并查询正常 profile 的 total=0。进一步检查 OMP 默认内置工具模式下 Tijian 出现在系统上下文和 xd:// 挂载设备；仅查看顶层 dumpTools 不能判断 MCP 不可用，禁用内置工具的对照则直接显示 26 项 MCP 工具。原始顶层工具等待探针因此超时，随后根据安装版本源码明确其为原生呈现方式，而非 MCP 断连。

确认无 active_runs 后将本机服务更新至 0.13.0，服务 active、daemon running，Tijian enabled；原 prompt/User Skill/Git 设置保持不变。现在是上游源码 CLI 接入，不代表安装或验收完整 Desktop；远程提交/提交同步等 GUI-only 功能仍没有新增 CLI 入口。README、接口与 Tijian 文档已更新。私密 Runtime 配置、模型凭证、题库、.mcp.json、Rust 工具链与二进制均不进入源码提交。


## 2026-10-21（Asia/Singapore）— 0.13.1 自然语言单次定时授权

用户反馈普通 UI 请求“定時任務測試：請你設計一個 10min 的定時任務”仅得到方案并要求手填 payload。实际事件只有 message/mode，旧 coach 模板将 auto_followup=true 与 next_check_seconds 写成所有后续排程的必要条件，导致明确请求也被拒；该事件确实没有创建子任务。

修正外部 coach 策略：明确的一次性定时/提醒或给定时长的定时测试即授权该次排程，不要求 JSON 字段；示例 10min 为 600 秒。明确仅设计/不要执行时禁止排程且优先于结构化 opt-in；没有明确单次请求时的主动跟进仍须 opt-in。继续使用 after_turn、本回合 run 锚点、稳定 dedupe-key 和下轮 auto_followup=false；不在 Scheduler/CLI 加自然语言猜测或自动重放旧请求。

基础安装/after_turn 断言 10 项、6.923 秒通过（`/tmp/paveway-timer-focused.log`）。新增实际模型验收 1 项、68.444 秒通过（`/tmp/paveway-timer-natural-language.log`）：以普通 ask 和用户原句、无额外 payload 标志，验证恰好一个 600 秒 after_turn 子任务，等待父回合 settled 后为 queued/attempt=0，due-parent completed 精确为 600 秒；另一次明确“仅设计、不排程”即使带 auto_followup=true 仍不创建任务。此验收不等待 10 分钟触发，结束后销毁隔离队列，不给用户正式对话补建提醒。

本机 test01 的相同旧规则已原子替换，私密旧版备份保存在 .runtime/backups；保留 workflow 身份以继续已有会话，原 prompt/User Skill 不变，下一次派发读取新规则，无需重启 daemon。README 与 coach 操作说明已更新，安装包 0.13.1。依赖检查与 git diff --check 通过。

## 2026-10-21 — 0.14.0 系统时区时间戳与倒计时

终端顶栏增加当前时间，消息和历史详情显示原始创建/完成时间，直接转换为系统时区并保留日期对应的夏令时，不增加时区配置。存储时间仍为 UTC。

新增只读 events.upcoming / events upcoming --workflow ID [--task ID]，返回最多 100 项当前作用域待执行/执行中的摘要，不含 payload。UI 每五秒读取，面板展示前三项并随重绘更新剩余时间；/timers 打开详情选择器。未锚定 after-turn 显示待起算，到时未领取显示等待派发，启动/执行状态依据 daemon，完成/取消后移除。读取失败与过期快照明确标记，不把零倒计时作为执行成功。

修复刷新与输入的竞争：普通消息在队列读取期间提交会等待该读取完成再发送一次；表单/选择器期间暂停后台队列读取，避免阻塞保存或取消确认，倒计时继续更新。升级无需数据库迁移，不创建额外用户定时任务。验证结果见 runtime-testing.md。

## 2026-10-21 — 0.15.0 Skill 按需读取

移除每回合将已启用 Skill 全文拼进 system.md 的逻辑，三类正文仍保存为本轮私密 SKILL.md，经 OMP 原生目录发现；原生索引只展示名称、带分类标签的描述与 skill:// 读取指引，由 Agent 选择适用项再读正文。自定义 append/replace 系统提示保持原文；原始用户设置和 Git 同步格式不变，无迁移。停用 Skill 不注册，工具停用时不把正文作为替代注入。UI 操作说明与 README 已同步更新。

自定义设置/UI 38 项通过，实际 OMP 原生发现与工具/Skill 停用验收 2 项通过。模型端到端验收因当前模型服务返回回合错误未通过，未声称模型已读取正文。本机已安装 0.15.0，在无 active run 时重启 daemon；配置与用户自定义文件哈希核对不变。已有会话历史不清除，需要全新上下文可使用 /new。详细验证与限制见 runtime-testing.md。

## 2026-10-21 — 0.16.0 模型与思考等级选择

新增 /select-model 和 /select-thinking-hardness：原生 OMP 模型目录、筛选/方向键选择、直接输入、当前值标记及 default 恢复继承。切换明确模型将等级设为 auto，再按模型支持的元数据选择等级。设置按 workflow 保存，所有 task 下一次派发读取；运行中回合保持原参数，同一会话续聊时传递新选择。

新增可选 inference 用户配置、CAS 写入与 Git 归档/导入，旧记录读取不补写字段或改动内容哈希；运行时 agent.thinking 可选，未设时保持 OMP 原行为。模型目录经公开 CLI 查询，保留 executable/profile/workspace，不修改 OMP 全局配置或认证文件。目录中非推理模型的 thinking=null 按无显式等级处理；命令失败不覆盖设置。文档与测试范围见 runtime-testing.md。

最终安装包定向回归 86 项通过（104.652 秒），实际 OMP 模型/high/low 参数验收 1 项通过（5.379 秒），不调用模型服务。本机空闲重启至 0.16.0，公开目录查询成功、70 个模型，配置及自定义文件哈希不变。重开 UI 即可选择；测试命令和实测边界见 runtime-testing.md。

## 2026-10-21 — 0.17.0 启动代理同步与检查

确认原卡住回合的 systemd/OMP 进程未继承终端代理，官方 Codex 连线 ECONNREFUSED 后 OMP 持续重试。新增 UI/serve/服务启动代理同步与 TCP 检查、公开 proxy sync/status 和 /proxy；保存标准大小写代理及 PI_PROXY_PROVIDER 到私密服务环境文件，锁定合并以保留 API key。每次启动 OMP 或查询模型目录读取最新代理，后续同步无需重启 daemon，已运行进程保持原环境。代理内容不进入公开报告或 Git。

代理/服务/配置/完整 UI 与下一回合热加载回归 77 项通过（81.752 秒）；隔离真实模型调用 openai-codex/gpt-6-luna、low 返回 PROXY_OK。已升级本机服务至 0.17.0，核对进程取得六个标准代理变量、API key 未变，原卡住回合保存为 uncertain，未自动重放。用户配置和 Skill 哈希保持不变；可 /new 后重新发送，或人工核对后恢复原 uncertain 事件。细节见 runtime-testing.md。

## 2026-10-21 — 0.18.0 GPT 称谓与 Git 资料稽核

界面和项目文字统一 GPT，event.md、个人 Skill、自定义设置及历史原文保留。每轮系统提示添加 GPT 命名约定，在默认设置、append/replace 及工具/Skill 停用时生效；不修改 manifest/session 身份。新增 AGENTS.md 和私密数据忽略规则。

回归 57 项中 56 通过、1 条件略过（78.777 秒），最终定向 12 项通过（18.814 秒），真实 Luna 在保留旧 event.md 首行角色描述的测试中以 GPT 自述，无被禁止职称。本机空闲升级至 0.18.0，event.md 与用户设置哈希不变。

Git 稽核覆盖源代码全部可达历史 187 个 blob 及工作区，私密路径、已知正文、凭证扫描无命中。但工程 Skill、默认模板及规划文件确实已在源码远端；个人 Skill 与对话也确实已进入独立本机资料 Git，目前无 remote。未 commit/push 或清理历史，详见 privacy-audit.md。


## 0.19.0 — 记忆维护与独立 10k 上下文预算

用户授权更新 event.md：默认模板及本机 test01 均加入重要信息及时维护、关键进展和每轮结束前检查的要求，已有提示已私密备份。本机 workflow revision 保持原绑定，个人自定义 prompt/Skill 不变。记忆快照从事件移至系统 prompt，以 o200k_base 将完整 JSON 限于 10,000 tokens；事件只携带预算及位置。独立 OMP context hook 将每次请求的对话副本限制为 10,000 tokens，按完整轮次/工具交换裁剪，极大单次输入保留首尾并告知模型；磁盘历史及记忆总库不删。必需守卫未就绪时在派发前拒绝，不静默失效。

验证：69 个不同用例通过，包括记忆/迁移 7、定制 8、Runtime 42（旧 prompt 精确断言修正后定向通过）、OMP 验证 6、原生定制 2、真实模型记忆 1、上下文窗口 3。真实模型在超大输入中看不到被裁掉的随机识别码，但完整识别码仍保留于原生 session。原生 OMP 18.10.4；Python 新增 tiktoken，离线先预热编码表。不是整个模型 API 请求的 20k 总预算，系统/工具定义与 provider 封装另计。

## 2026-10-30 — 0.20.0 原生 Tijian MCP 接入、提示词重构与启动流程优化

根据上游 Tijian 原生 stdio MCP Server（`tijian-mcp`，基线 `90e2c75`）发布，Paveway 完成原生接入并彻底移除旧 Python 桥接器：

1. **移除旧 Python 桥接器**：删除 `tijian_catalog.py`（工具映射与 Schema）、`tijian_mcp.py`（Python MCP server）及 `tijian.py`（JSON CLI 适配器）。从 `config.py` 中删除 `TijianConfig`，从调度器 `available_tools` 注入中移除旧 CLI 前缀与命令，从 `pyproject.toml` 移除可选依赖 `.[tijian]`。Paveway 不再在 Python 中自行定义或组装 17 个工具的 Schema，所有工具定义、输入输出契约及权限约束均以运行中原生 `tijian-mcp` 实际暴露的 `tools/list` 为准。
2. **原生 MCP 配置与冲突防御**：保留并重构 `workflow connect-tijian` 命令，直接写入 workflow workspace 的 `.mcp.json`。支持默认推荐的 `--read-only` 与显式的 `--read-write`（参数严格互斥）。增加 `disabledServers` 冲突防御：若已有配置的禁用列表包含 `tijian`，明确拒绝并报错 `ConflictError`，原配置文件字节完全不变；保留其他已禁用服务与第三方 MCP Server，配置过程具备原子性、幂等性与 0600 权限保护。
3. **VJudge 认证独立解耦**：将 VJudge HTTPS 登录会话管理从旧 `tijian` 命令组解耦为独立的 `vjudge auth [status|test|login|cookie|clear]` CLI 命令，终端 UI 同步支持 `/vjudge` 与 `/tijian` 双入口。保留用户名/密码隐藏输入与私密文件导入机制，会话文件保存在与 Tijian 兼容的共享 `vjudge_session.json` 中。
4. **重写 coach.md 与 Skill/MCP 文档注入**：
   - 重构 `bundled/coach.md`：清除所有旧 CLI 调用协议（`tijian tools`、`tijian call`、`argv_prefix`、`problem_add/refetch` 及 JSON CLI 返回字段假设），保留教学策略、训练安排原则，以及公开赛题研究的停止条件（最多 6 次检索、3 类赛事各取 1 题）。
   - 更新内置 `paveway-tijian` Skill：作为业务编排层指导 Agent 能力选择，明确标注文档、待办与分类目录的原生 MCP 尚未实现；严格区分同步题面确保（`tijian_problem_ensure`）与异步 AI Ensure（译文/摘要 `tijian_problem_content_ensure`、向量 `tijian_problem_embedding_ensure`）及 Job 跟踪机制。
   - 重写 `docs/tijian.md`：详细阐述原生 stdio MCP 架构、`tijian-mcp` 构建获取、Paveway 配置流程、只读/读写权限、WSL2 与 Windows Desktop 数据边界以及 VJudge 会话管理。
5. **首次启动流程重构（`paveway.sh`）**：
   - 移除自动生成默认 `gpt` Workflow 的硬编码行为。
   - 首次启动无 Workflow 时在交互终端主动提示输入名称（ID），严格经 `validate_id()` 校验，空输入要求重新输入，遇到 EOF/Ctrl+C 安全退出。
   - 支持通过 `./paveway.sh --workflow NAME` 非交互指定；已有 Workflow 时直接进入现有选择流程，已有旧版 `gpt` 保持原样不被破坏。
6. **修复 OMP 18.13.3 Context Guard 就绪时序不兼容**：
   - 针对 OMP 18.13.3 真实环境中 RPC `ready` 早于扩展 `paveway_context_ready` 发送的时序，重构 `OmpRpc.ready()`，分别独立追踪两项就绪信号，消除顺序假设；严格校验 Token 限制（10000）与协议版本。
   - 修复 `context-window.mjs` 在 Bun 单文件打包环境中的模块导入路径（增加直接 `@oh-my-pi/pi-agent-core` 导入回退）。
   - 增强 `smoke.py verify-omp` 诊断错误分类，细化 `agent_context_guard_missing`、`agent_context_guard_invalid`、`agent_rpc_ready_missing`、`agent_rpc_timeout` 等安全诊断。
   - 扩充 `fake_omp.py` 与 `test_omp_verification.py`，全面覆盖两种 Ready 顺序及各种故障模式；真实 OMP 18.13.3 的 `verify-omp` 无模型诊断通过。

测试与 CI：新增 `test_native_mcp.py` 与 `test_paveway_script.py`，更新 `test_setup.py`、`test_vjudge_auth.py`、`test_runtime.py`、`test_foundation.py`。修复 GitHub Actions CI 中多 Python 版本的并发锁与错误处理问题，全量测试在 Python 3.11、3.12、3.13 矩阵均全部通过。

**完成了首次与 Tijian 的实测联动。**

## 2026-10-30 — OMP 原生重构 M0 门槛

依据用户提供的 Implementation Contract／Native Refactor Plan，从 `main` 的 `35cf9a911c9df90598f664d215ea74cf29f6faa9` 开始验证，安装版本为 OMP 18.12.6。旧基线 266 项运行完成：252 通过、14 跳过，214.220 秒；原生 RPC transport、append/replace、Skill 发现与 `skill://` 正文读取、禁用权限 4 项通过。

新增 `examples/verify_omp_m0.py`，在临时 HOME／XDG／workspace／Session 内运行真实 OMP RPC 和原生 PTY；provider 仅为本机 HTTP fixture，不使用凭据或付费模型。实际观察到 context Hook 抛错后，RPC 与原生 TUI 仍发送请求并显示回复；显式指定不存在的扩展也没有阻止推理。正常 Guard 对照能裁剪副本且保留 Session 原文。保留探针运行 33.54 秒，退出码 2：`d4=BLOCKED`、`controls_passed=true`。

**按 D4 停止 M1–M6，不新增或默认启用受管原生 TUI。** 未修改 Runtime／队列／schema／用户 Prompt、event.md、用户数据或上游 OMP；旧 TUI、CLI、RPC 保持原状。原生 Skill 测试删除上游截断索引中“必须出现完整描述”的措辞断言，保留真正的发现、权限与全文读取检查。

完整 D1–D7 状态、失败样例、源码证据、复现命令、回退边界和最小上游 API 缺口见 [M0 兼容性记录](verification/omp-m0-compatibility.md)。没有把 loopback fixture 视为真实模型、Browser、Subagent、Tijian 或 Windows→WSL 验收；未通过的交接、settled、投递与新历史同步能力不开放。

最终同一全量回归运行 186.130 秒：250 通过、14 跳过、1 failure、1 error，**不是全绿**。未修改的重启测试在预定 0.6 秒到期前未能保证停机，事件被领取后以 `blocked/daemon_shutdown` 结束；另一项旧 UI PTY 测试在 pyte 渲染空字符单元时抛出 `IndexError`。保留失败日志与具体位置，不重跑掩盖、不修改这些断言；完整 M0 验收也未通过。此次交付是可复现调查与安全停止记录，不是完成了原生重构的发布。

## 2026-10-30 — 0.20.1：M0 基线修复与已加载 Guard fail-stop

从 `68e7efe8066e360fb87bb8a2765a94f2632a44e7` 继续处理，不修改 OMP 18.12.6 上游。版本更新为 0.20.1；schema、旧队列解释、用户配置及 Session 不变。

- 修复重启回归的 0.6 秒到期竞态：先检查未来事件跨重启保持 queued/due/version 且没有 Run，再用真实 CAS reschedule 明确放行，验证只执行一次。没有增加等待超时或接受 blocked。
- 独立 `中\rA` 已复现 pyte 0.8.2 的宽字符续格缺陷。测试专用 WideScreen 在写入、部分擦除和移位时处理旧字形全体，保留严格显示检查；旧 UI PTY 与原生探针共用，不改应用内容或渲染器。
- Paveway Context Guard 初始化／注册和同步裁剪异常时，先写不含原始异常内容的固定诊断，再同步自我 SIGKILL；10k 算法与正常 RPC ready 不变。实测发现 OMP factory 会把 process.exit/reallyExit 转成可吞掉的异常，因此不能用 exit(78) 充当启动失败的保证。Tokenizer 仅从宿主解析，避免隔离 wrapper 的 Bun 自动安装。

原先两项失败场景 2 项通过（8.124 秒）；宽字符边界 4 项通过；Guard 原生预算、配对、原文与故障边界 3 项通过。**完整回归 271 项，257 通过、14 跳过，158.516 秒，无 failure/error**，日志 `artifact://29`。随后增强的 factory exit 拦截回归 1 项通过（0.483 秒）；实际 CLI 输出 `0.20.1`。

真实 OMP RPC／原生 PTY 的 Guard 注册及 context 错误共 4 个场景，均退出 -9、provider 请求为 0，无成功回复。原生 TUI 运行时 stderr 被 OMP 重定向到日志，探针按隔离目录中的该 PID 日志核验，不冒充已显示给用户。正常 Guard 仍裁剪副本且保留 Session 原文，4 项原生 Prompt／Skill／transport 检查通过。保留探针运行 48.14 秒，`guard-failure-boundary.fail_closed=true`、`controls_passed=true`；日志 `artifact://30`。

**完整 D4 仍 BLOCKED，探针按设计退出 2**：扩展缺失的 raw OMP 仍能推理；同一子进程的受管启动握手、输入准入及完整来源矩阵尚未实现/验证。已加载 Guard 的错误边界修复不等于 M1 通过。explicit-only 固定扩展在源码上具备继续验证的路径，不把“只能等上游”当成唯一方案；当前仍保留旧入口，关闭原生受管入口，不开放前台 settled 锚点／共享 Session／自动投递。详细证据与风险见 [M0 记录的 0.20.1 小节](verification/omp-m0-compatibility.md#0201-阻碍修复第二轮)。

## 2026-10-30 — M0 第三轮：实际子进程准入与辅助推理旁路

从 `7b39dcfb03a44d13fce79b802441d242e29b9883` 继续，应用版本保持 0.20.1，未改 OMP 18.12.6 上游、默认入口或正式用户数据。新增 `examples/verify_omp_admission.py` 和 `examples/omp_admission.mjs`，提供真实原生 PTY、私有继承 socket、源码摘要／nonce／PID／Session 身份握手及逐次 context 授权。Guard 抽出同步就绪回调接口，默认 RPC ready 与 10k 算法不变；回归增加就绪回调抛错时的 fail-stop 检查。

实测本机 `--trusted-extension` 会拒绝缺失入口、缺失依赖与 factory 异常；不能把普通 `--extension` 的结果泛化到此模式。输入只在同一子进程最终 Session 准入后转送，TUI stdout 不再承载探针的 RPC ready JSON。启动自动推理来源由隔离 profile 与固定 argv 限制，不把 UI 的普通 Enter 禁用标志当作全局权限门控。

**13 个场景按预期观测完成，44.73 秒**，日志 `artifact://36`：正常对话 1 次授权／1 次模型请求；入口/依赖/factory 错误、摘要/nonce/版本错配、无握手、拒绝授权、IPC 断开、IPC 不回应和 Guard 错误均没有模型请求。无握手与 IPC 不回应由明确的外部 watchdog 终止，不冒充已加载、settled 或完成。

**发现并保留新阻碍：原生 `/rename` 产生第二个模型请求，但 context 授权仍只有一次。** 该场景即使 `--no-title`、固定角色和 `title.icons=boring` 仍成立。故探针 `cases_passed=true` 表示包括旁路反例在内的观测符合预期；`provider_bypass_observed=true`、`full_d4=BLOCKED`，退出码 2，不是安全验收通过。

完整回归 **271 项，257 通过、14 跳过，132.408 秒，无失败**，日志 `artifact://37`；隔离环境中原生 RPC transport、append/replace、Skill 全文读取及权限 4 项也通过。所有模型请求仅到本地 fixture，没有真实模型费用，未验收 Browser/Tijian 或 Windows→WSL。

当前按 D4 保留旧 TUI/CLI/RPC，不安装尚不能覆盖辅助推理的应用启动器。要保留完整原生能力，需要让标题及其他 side-request 路径也经过受信 mandatory 请求边界；不能只补启动握手、拦截命令字符串或静默删掉原生能力来达标。最小上游接口建议、未实测的 compaction/tree/live/child 路径、回退边界与完整矩阵见 [M0 验证记录](verification/omp-m0-compatibility.md) 第三轮小节。
## 2026-10-30 — M0 WSL2 复验、手动压缩与系统模板

已 fast-forward 到 `b6013df4827c4ff54651619ed8d869fd5b379cf1`，按两份重构设计／实施契约继续；实际环境为 WSL2、Python 3.12.3、OMP **18.10.4**，与上轮 18.12.6 分别记录。安装新版项目依赖后，旧基线 271 项中 257 通过、14 跳过；新增检查后的最终回归 **272 项，257 通过、15 跳过，106.207 秒，无失败**。

扩展原生准入探针，加入 `/compact soft` 超长中文反例、版本记录、bracketed paste 和完整原文保留检查。普通请求已裁掉的中段标记重新进入压缩辅助请求：1 次 context 授权却产生 3 次模型请求；`/rename` 旁路在本机也复现。短输入不能压缩、尾部空白被 composer 清理的两次无效／失败实验已保留说明，未删除断言冒充通过。

新增真实 OMP 模板展开的无模型验证：动态工具／Skill 索引、禁用权限、正文按需读取和身份替换。隔离原生检查共 5 项通过；不等于统一 Prompt Composer／Persona 迁移已实现。完整 M0 的 Guard 四种故障均无模型请求且 fail-stop，但辅助路径仍使完整 D4 BLOCKED。

依据契约 D4 保留旧入口，M1–M6 不提前验收；未修改生产 schema、用户数据、OMP／Tijian 上游或默认启动器。Windows Tijian MCP、真实模型、Browser／Subagent 未测试。复现命令、日志、失败样例与回退边界见 [本机复验记录](verification/omp-m0-wsl-2026-10-30.md)。


## 2026-10-30 — M0 范围变更：取消固定 10k 限制

用户在原生重构期间明确回复「取消 10k limit」，取代先前固定 Guard 门槛。后台不再加载预算扩展，只验证原生 RPC ready/protocol；prompt 不再宣称 10k 裁剪，记忆快照去掉 token 上限，仅保留 64 KiB IPC 字节边界。用户自定义 prompt、Skill、既存 event.md 和历史原文均未改写。旧预算算法仅保留历史探针，不进入默认路径。

D1/D2 采用独立物理前后台 Session；D3 不开放前台严格 settled 锚点；D6 采用持久 Inbox、不自动注入；D7 原生索引必须完成才切默认入口。这些分支不变。另修复启动器遗漏 PyYAML 检查造成旧环境升级失败的实际问题。定向验证 41 项，40 通过、1 opt-in 跳过（6.375 秒），包含真实 fake-daemon 完成与旧 CLI/UI 启动。无 schema／正式数据变化。后续逐阶段新增显式原生入口。

## 2026-10-30 — M1 显式原生入口

新增 `oicoach-runtime native --workflow ID [--task ID]`、原生 OMP 薄扩展及透明 PTY supervisor。精确 trusted-extension、私密配置、nonce／源码摘要／实际 guardian 子 PID／Session 目录核验完成后才转送用户输入。提供只读 `/paveway status` 和 `/coach`，无自研聊天渲染器；当前每次建立独立前台目录，不接管后台 Session，旧 ui/CLI/RPC 保留默认。

`PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_native_omp -v`：真实 OMP 18.10.4、原生 PTY 与本地 provider fixture，1 项通过（2.889 秒）。超长输入中段确实进入 provider，原文保存；native /exit 为 0，daemon 仍 running，无旧 Guard ready 输出。没有使用私人模型凭据，Browser／Subagent／真实 Tijian 未验收。M2 的注册／恢复／归档与 M3 的工具尚未在本提交实现，故不切默认入口。

## 2026-10-30 — M2 前台登记、恢复与只读归档

前台所有权以版本化 `frontend:v1:` 记录存于现有 SQLite runtime_metadata（沿用项目控制工作记录方式，无 schema 迁移）。新增 generation／owner capability 校验、guardian 与 OMP PID/启动身份、原生目录继承 flock、生命周期观测及显式恢复。agent_end 只记 observed_loop_only，不作为 settled；daemon 重启转 uncertain，心跳不释放所有权，活进程或锁阻止接管。不占背景 Run 槽，不共享前后台物理 Session。

原生入口支持 `--conversation`／`--new`，默认接续当前 workflow/task 最近的已释放对话；原生 /new 在受管目录中重新登记并提升 generation。`frontend list/show/messages/recover` 提供诊断、分页文字检索和显式恢复；原文和 parent_id／分支来源保留。已退出的登记 Session 以现有文字过滤政策进入独立用户 Git，未配置 remote 仍只本地维护，不归档 capability、启动配置或原始工具日志。

验证：4 项所有权／CAS／活进程／锁／unknown 不重播单元通过；真实 OMP 本地 fixture 验证长文本、TUI 在线时后台 RPC 独立执行、第二 owner 拒绝、顺序恢复同一前台 Session 及独立 Git 导出，合计 6 项通过（8.901 秒）；另原生 /new 的双来源归档／generation 场景 1 项通过（2.804 秒）。最初 PTY 大写入阻塞已定位为测试端不同时读屏，改用非阻塞双向驱动；未改应用成功判断。没有正式用户数据库／Session 迁移，也没有把前台 loop end 标为严格完成。

## 2026-10-30 — M3 同源唤醒与持久收件箱

新增 `paveway_wakeup_schedule/list/cancel/reschedule` 原生 AgentTool、`/wakeup in 10m TEXT`／`at HH:mm TEXT`／list／cancel／reschedule，以及原生 `/inbox [page]`、CLI `inbox list/show/acknowledge`。接口按已登记 owner/generation 绑定作用域，after_now 使用 Runtime 时钟及语义 fingerprint 保证重试幂等，取消／重排保留 CAS。需明确 opt-in 字段，默认不会循环跟进；前台 after_turn_settled 明确拒绝，未静默降级，旧后台 CLI after-turn 不变。

持久记录区分提醒与 Run 结果、created／offered／用户明确 acknowledged；列表、通知、工具调用返回均不代表 Run 完成或用户已读。明确提醒在领取到期事件时先持久化，模型失败也不删除；后台终态另投影结果，未知不重播。选择 D6 的 Inbox 分支，不自动 sendMessage 注入前台，也没有外部系统推送承诺。所有记录使用现有 SQLite 版本化 metadata，无 schema 迁移。

`PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_wakeup test_native_omp -v`：8 项通过（19.149 秒），包括语义去重、scope/CAS/旧 generation、拒绝无 opt-in／前台 settled、收件箱持久化与不伪造完成，以及真实 OMP 经 AgentTool 调到同一 Runtime、Slash 提醒在 UI 退出后运行。真实的是 OMP／工具 loop／PTY／IPC，模型仅本地 fixture，没有付费模型或私有凭据。

## 2026-10-30 — M4 统一 Prompt 与独立 GPT Persona

新增单一 `prompt_composer.py`，前台与 RPC 共用不可变 system.md／settings.json／prompt-manifest.json，记录 Composer 版本、旧 append/replace 模式、配置／Persona／系统摘要。两种旧模式原文保留，均经 OMP 原生纯文本 SYSTEM 路径加载（Skill/Rule 动态展开由 OMP 完成），不再继承默认 Coding Assistant；不叠加互斥 template/plain 参数。选择已实测的纯文本兼容路径，保留用户文本中的模板符号。共享原生 Skill 目录探针，避免新快照覆盖既有 OMP 目录。

新增 `coach list/create/show/persona`、`native --coach ID`。原 Workflow 隐式映射默认 GPT Profile；新 Profile 引用源 Workflow 模板并分配私有 runtime workflow，以隔离配置、Skill、Memory 与 Session。同一流程可创建多个 Profile，不隐式复制另一 Profile 的私人 Prompt／Skill／笔记；Tijian workspace 仍可共享。Persona 扩展到现有 CAS 设置及独立用户 Git 的配置记录；首次改写前保存私密原配置备份。通用动态状态通过 `coach state show/put` 复用私有 Memory 的版本化 fact 存储，不引入情绪策略或自动改写 Persona。

Persona 改动要求新对话；旧对话续接被明确拒绝，原文、配置及历史仍在。原生唤醒保留创建时受信配置快照，并使用各 Conversation 的独立后台绑定；即使之后编辑 Persona，已授权的唤醒也不会读到另一版本或另一 Profile 的配置。旧后台 task 的 Persona 变化同样拒绝静默换身份，需新 task。

验证：Profile／隔离／CAS／私密回退／Git 配置、原有自定义设置、真实原生 Persona／唤醒／恢复，共 **23 项通过（23.426 秒）**；另原生 transport／动态模板／append-replace／Skill 读取及权限 5 项通过（不调用真实模型）。Persona 实机测试先误取了异步标题请求，已按 Paveway Core 识别主对话请求；未删去身份隔离断言，也未禁用原生标题功能。日志 `/tmp/paveway-native-m4-complete.log` 与 `/tmp/paveway-native-m4-prompt.log`。新字段只写用户数据，无源码私人信息、正式用户迁移或 schema 升级。
