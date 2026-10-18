# 整個專案運行測試（2026-10-18，Asia/Singapore）

下方原始記錄屬於 0.5.0；0.6.0 的 M4／coach、0.7.0 記憶與最新 0.8.0 終端 UI 結果見本文末段。舊版「記憶暫緩」等敘述保留為歷史記錄。

本輪測試目前已實作的 Runtime 與 Tijian 適配，不新增或測試 L2/L3 記憶功能。測試從重新建置、安裝的 0.5.0 套件執行；環境為 Linux、Python 3.12.3、OMP 18.10.4、MCP SDK 1.30.0，以及固定 Tijian commit `b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f` 的原碼 CLI 驗證入口。

## 測試範圍

| 路徑 | 驗證內容 |
| --- | --- |
| 安裝／診斷 | wheel 安裝、依賴一致性、CLI、配置／私密權限、SQLite integrity/WAL |
| 排程與持久化 | 到期／after_turn、去重與 CAS、取消／重排、交易回滾、20 個事件串行 |
| RPC 與恢復 | ack／completed／settled、provider error、逾時、前後置 crash、子進程清理、退避／熔斷、人工重啟 |
| systemd | 暫時性 user unit 內強制終止 daemon，驗證 queue 恢復及 uncertain 不重播，最後停止回收 unit |
| 真實 OMP | 三個模型回合，驗證同 task 兩次喚醒接續及跨 task 隔離 |
| Tijian | 26 個接口映射、官方 SDK MCP discovery/call、原碼業務層的狀態／歷史／計畫／日報／題單／備份還原 |
| 全鏈路 | 獨立 CLI → 排程 → daemon → 真實 OMP 模型 → MCP → 真正 Tijian 原碼 CLI → completed/settled → SQLite 成功落盤 |

新增的 `test_project_cli_daemon_model_tijian_and_persisted_completion` 先建立獨立 workflow／workspace／Runtime state 和 Tijian harness profile。排程 CLI 退出後，由另一個 daemon 執行稍後事件；核對 attempt=1、ack、completed、settled 及 agent exit=0。停止 daemon 後，重新開啟已落盤的同一 session，只查歷史，不送新 prompt，確認存在唯一成功的 Tijian 工具結果及正確題目資料。這項檢查避免只憑模型回覆 DONE 或 Runtime succeeded 就推斷外部接口成功。

## 重現命令

先依 [Tijian 說明](tijian.md) 建置上游原碼 CLI／seeder。下列命令會發送小型真實模型回合，並建立、強制終止及回收測試用 systemd user unit，不安裝長期服務：

```bash
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/python -m pip check

OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_SYSTEMD=1 \
OICOACH_TEST_TIJIAN_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_TIJIAN_SOURCE_CLI=/tmp/paveway-tijian-build/debug/tijian-source-cli \
OICOACH_TEST_TIJIAN_SOURCE_SEED=/tmp/paveway-tijian-build/debug/tijian-source-seed \
  .venv/bin/python -m unittest discover -s tests -v

.venv/bin/oicoach-runtime verify-omp --config "$PWD/.runtime/config.toml" --with-model --json
.venv/bin/oicoach-runtime doctor --config "$PWD/.runtime/config.toml" --json
git diff --check
```

## 本輪結果

從重新安裝的 0.5.0 套件執行 **104 項測試，298.446 秒：103 通過、1 跳過，無失敗**。唯一跳過項目是另行安裝的正式 Tijian 發行版 CLI 檢查；上游原碼 CLI 的 6 項整合（含真實 daemon 全鏈路）及兩種直接 MCP 模型路徑均已執行。systemd 故障恢復、子進程清理、去重／CAS、串行排程與 uncertain 不重播亦通過。

獨立 `verify-omp --with-model` 的 rpc_transport、model_selected、same_task_resume、task_isolation 全部 PASS。`pip check`、`git diff --check` 通過；Tijian 原碼仍為上述 SHA，checkout 無修改。本輪沒有修改 Runtime 業務程式或記憶功能，只補充全鏈路回歸與測試報告。

結束後確認測試 daemon／OMP 進程及 transient systemd unit 已清理，沒有留下常駐測試服務。

本機完整輸出保存於 `/tmp/paveway-project-running-tests.log`；真實 OMP 檢查結果為 `/tmp/paveway-project-real-omp.json`，既有配置診斷為 `/tmp/paveway-project-doctor.json`。這些是本機測試產物，不是遠端 CI 結果。

## 配置與驗收邊界

本輪 `.runtime/config.toml` 的 doctor 回 `ok=true`，資料庫、權限與 OMP CLI 檢查通過，但有「沒有安裝 workflow」「daemon 離線」WARN。這是既有配置的實際部署狀態；本輪運行驗證使用臨時配置啟動獨立 daemon，不將測試通過說成現有配置已常駐部署。doctor 的 CLI 能力探測本身也不代表模型已驗證，真實模型結果另由 verify-omp 提供。

Tijian 使用上游原樣 CLI／application crates 的驗證入口；完整 Desktop UI、運行中 Desktop projection routing、外部 OJ 抓取及需 Desktop executor 的向量計算仍未驗收。沒有另行安裝的正式 Tijian 發行版時，其獨立 opt-in 測試會跳過，原碼 CLI 的讀寫／MCP／模型測試仍會實際執行。正式 OI 教練 workflow 及 M4 長期服務部署尚未實作，不屬於本輪已通過的功能。

## 0.6.0 後續驗證：M4 與 coach

重新建置／安裝 0.6.0 wheel，以前述全部 opt-in 參數執行 **118 項測試，325.843 秒：117 通過、1 跳過，無失敗**。唯一 skip 仍為另行安裝的正式 Tijian 發行版 CLI 檢查，原碼 CLI 整合均執行。完整輸出 `/tmp/paveway-m4-tests.log`。

新增實際安裝的 UUID user unit 測試：含空格／引號／百分號／美元符路徑、verify、enable/start、SIGKILL 自動重啟後 queue 完成、restart、journal、disable/uninstall 與 DB 保留。另通過 workflow 安裝與 MCP 合併保護、ask 即時幂等／等待、逾時不取消、回覆長度／私密性／local-only、查回覆時 late persistence notice 仍 uncertain，以及 IPC conflict 錯誤碼保留。

真實模型透過內置 coach 處理兩道題與休息窗口，回覆含正確題目標識和驗收 token；可查到本輪最終文字且不進日誌，重送同 key 沒有第二個 run。此為產品路徑驗收，不是 OI 教學品質保證。原有 CLI → daemon → OMP → MCP → Tijian 全鏈路仍通過。

獨立 verify-omp --with-model 四項全 PASS，結果 `/tmp/paveway-m4-real-omp.json`。README CLI walkthrough 另實際執行 init、workflow create/list/show/connect-tijian、service render 及 systemd-analyze 校驗，全部通過。pip check、git diff --check 通過。測試結束後沒有保留 UUID unit／預設服務啟用／linger 變更。

全量回歸後，僅擴充 coach 驗收用例為同一 task 的 plan/checkin/review 三回合，未改產品程式；定向重跑 **1 項，58.016 秒，通過**（`/tmp/paveway-m4-coach-three-modes.log`）。後兩回合使用真正 ask --wait 讀回覆，核對各自 token、題目、run 數量及初輪回覆未被覆蓋。README 本地連結、shell 語法、TOML、三份 JSON 示例另行核對通過。

## 0.7.0：本地記憶與完整回歸

重新建置安裝 0.7.0，使用 README 的全部 opt-in 命令執行 **128 項，391.692 秒：127 通過、1 跳過，無失敗**，輸出 `/tmp/paveway-memory-all.log`。唯一 skip 為未另行安裝的正式 Tijian CLI，原碼 CLI/seeder、MCP、真實模型及兩項 systemd 測試均執行。產品程式在本次完整回歸期間未改動。

新增 7 項記憶/遷移單元、2 項 Runtime 記憶整合及 1 項真實模型用例。涵蓋中文/字面搜尋、workflow/task scope、來源事件限制、有效期、8 KiB 注入預算、私密性、CAS 競態、刪除墓碑、原生 JSONL 的所有分支、分頁/截斷、symlink/異常檔案拒絕與遷移回滾。真實模型實際呼叫 CLI 保存帶隨機識別碼的共享偏好；daemon 重啟後，以獨立 task、禁用工具的新 OMP 會話讀出注入內容，再查歷史和原文，不寫入 daemon 日誌。定向真實驗收另通過 1 項、80.332 秒（`/tmp/paveway-memory-real.log`）。

在產品程式未變的情況下補強測試：v1 遷移前後逐表比較 events/runs/session_bindings/metadata/audit；筆記工具寫成功後立即 crash，run=uncertain、筆記存在且重啟不重播。定向重跑記憶單元 **7 項、6.908 秒通過**（`/tmp/paveway-memory-final-unit.log`），Runtime 記憶用例 **2 項、6.320 秒通過**（`/tmp/paveway-memory-crash.log`）。這兩項測試補強發生在全量程序已載入測試後，因此另列定向結果，不冒稱全量當時已包含補強斷言。

無 opt-in 的主回歸也通過 **128 項，120.542 秒：115 通過、13 跳過**（`/tmp/paveway-memory-regression.log`）。README/記憶文檔相對檔案連結、bash/TOML 語法、wheel 中 002 migration 和新版 coach 資源、pip check、git diff --check 均驗證。既有全鏈路與故障語義仍通過；正式 Desktop/OJ/向量 executor 的既有環境限制不變。

## 0.8.0：終端 UI

從已安裝 0.8.0 wheel 跑無 opt-in 主回歸：**138 項，127.383 秒，125 通過、13 跳過，無失敗**（`/tmp/paveway-ui-regression.log`）。其中 UI 專項當時 10 項；其他 skip 仍為需要本機模型/systemd/正式 Tijian 或原碼 CLI 環境的 opt-in，沒有聲稱本次重跑 0.7.0 全部實機整合。

隨後新增獨立 opt-in `test_tui_omp.py`，用 UI controller → CLI 子進程 → daemon → 真實 OMP 模型 → 保存回覆 → UI 輪詢及繪製的路徑驗收，**1 項、15.748 秒通過**（`/tmp/paveway-ui-real.log`）。核對 attempt=1、隨機識別碼、實際最終回覆、私密內容不進 daemon 日誌，以及關閉 client 後 daemon 仍存活。使用隔離 state/workspace，四種外部能力關閉，不修改既有模型配置或對話。

另補強非 UTF-8 locale 的明確拒絕與測試；重建安裝後，UI 專項 **11 項、5.662 秒全部通過**（`/tmp/paveway-ui-final-tests.log`）。覆蓋中文多行/光標/取消/幫助、終端控制符過濾、來源/屬性/CAS 保留、CLI 錯誤與 doctor FAIL 顯示、原文分頁、不明提交同 key 核對及阻止新 key、防過期頁面結果、字面量 shell 字串與連字符參數、非 TTY/JSON/關閉 client 拒絕。PTY 實際輸入中文、縮小/放大終端、q 退出並核對 termios ECHO/ICANON 恢復；界面退出後釋放 fake Agent 繼續執行，事件成功且 attempt=1。

真實模型與 locale 補強發生在上述完整程序已載入測試後，所以另列定向結果，不把完整 138 項結果說成包含後增測試。既有 CLI/API/DB/調度語義未因 UI 改動；本次未人工視覺驗收、未提供 token/tool 即時流，也未更改預設服務啟用或 linger。
