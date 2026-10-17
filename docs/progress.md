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
