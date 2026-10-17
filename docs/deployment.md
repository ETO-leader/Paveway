# 部署狀態

M1 可以安裝 CLI，透過 `serve --config PATH` 前景運行獨立 daemon，再由另一個 CLI 發送持久事件。已用 fake OMP 驗證 CLI 結束後仍執行及 daemon 重啟保留 queue；尚未安裝或啟用任何系統服務。

後續 M4 將提供 `packaging/systemd/oicoach-runtime.service`，包含絕對路徑 ExecStart、WorkingDirectory、Restart=on-failure、RestartSec、TimeoutStopSec、KillMode=control-group；StartLimitIntervalSec/StartLimitBurst 位於 Unit 區段。啟用前使用目標機的 `systemd-analyze verify` 驗證，並先顯示實際安裝及啟用命令。

Runtime shutdown 會有界地結束自有 Agent 或保存 uncertain，再釋放 lock/socket。daemon SIGKILL 使用 Linux guardian 清理自有程序組，重啟後保守 reconcile。此階段 daemon 崩潰後仍需人工重新執行 serve，沒有服務管理器自動拉起。

不自動設定 `loginctl enable-linger`、修改 OMP profile、既有會話或模型憑證。linger 是否需要由使用者選擇；本輪沒有執行任何系統登入／開機政策變更。請先用 README 的 fake fixture 驗證環境，真實 OMP 的模型回合尚待 M2。
