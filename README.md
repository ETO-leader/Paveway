# Paveway
Paveway =  Paveway is An adVanced Efficient Workflow for informAtics olYmpiads

獨立 OI 教練運行框架，優先使用現有 OMP；教練策略、Tijian MCP 和其他 Skill 由外部組件提供。

目前完成 **M1：可運行的持久排程流程**。提供獨立 daemon、Unix socket CLI、SQLite 佇列、串行 OMP RPC 派發、per-task 會話綁定及保守恢復。端到端、會話與崩潰測試使用 fake OMP；真實 OMP／模型整合、完整自動重啟策略和 systemd 部署仍待 M2–M4。

需要 Linux、Python 3.11+。Python 執行期沒有第三方依賴；建置使用 setuptools。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/oicoach-runtime --help

# 在工作區內試跑，避免變更既有個人配置。
.venv/bin/oicoach-runtime init --config "$PWD/.runtime/config.toml" \
  --state-dir "$PWD/.runtime/state" --workflow-root "$PWD/.runtime/workflows"
.venv/bin/oicoach-runtime doctor --config "$PWD/.runtime/config.toml" --json

PYTHONPATH=src python3 -m unittest discover -s tests -v
```

不指定 `--config` 時使用 `$XDG_CONFIG_HOME/oicoach-runtime/config.toml`，預設狀態存於 `$XDG_STATE_HOME/oicoach-runtime`（未設 XDG 時分別使用 `~/.config`、`~/.local/state`）。`init` 可重複執行，不覆寫既有配置；已有配置時請直接編輯 TOML，不能用 `init` 旗標重設路徑。

`doctor` 的 FAIL 代表檢查失敗，退出碼為 1；WARN 表示尚未配置或尚未驗證。探測到 OMP 旗標不代表真實模型回合已驗證。

可用以下測試 fixture 跑完整流程，無需模型金鑰或 MCP。`prepare_smoke.py` 要求新的配置路徑，不覆寫現有配置；建立的是 fake OMP 與 echo workflow，**不是正式 OI 教練**。

```bash
.venv/bin/python examples/prepare_smoke.py --config "$PWD/.runtime-smoke/config.toml"

# 終端 A：保持 daemon 運行；它獨立於發送排程的 CLI。
.venv/bin/oicoach-runtime serve --config "$PWD/.runtime-smoke/config.toml"
```

```bash
# 終端 B：CLI 結束後，由終端 A 的 daemon 繼續執行事件。
.venv/bin/oicoach-runtime schedule --config "$PWD/.runtime-smoke/config.toml" \
  --workflow example --task demo --at "$(date -u -d '+10 seconds' +%Y-%m-%dT%H:%M:%SZ)" \
  --reason 'smoke test' --dedupe-key demo-1
.venv/bin/oicoach-runtime status --config "$PWD/.runtime-smoke/config.toml" --json
.venv/bin/oicoach-runtime events list --config "$PWD/.runtime-smoke/config.toml" --json
.venv/bin/oicoach-runtime runs list --config "$PWD/.runtime-smoke/config.toml" --json
```

`serve` 保持前景運行，Ctrl-C/SIGTERM 可關閉；目前尚無系統服務自動拉起。若 prompt 已送出但完成證據不明，事件列為 `uncertain`，不自動重播；先核對副作用，再使用 `recover retry EVENT_ID --confirm-duplicate-risk`。啟動前失敗列為 `blocked`，此階段也不自動重試。

文件：

- [架構與里程碑](docs/architecture.md)
- [狀態機及恢復語義](docs/state-machine.md)
- [CLI/API 契約](docs/runtime-api.md)
- [外部 workflow 契約](docs/workflow-contract.md)
- [操作與恢復手冊](docs/operations.md)
- [部署狀態](docs/deployment.md)
- [本次環境探查與進度](docs/progress.md)
