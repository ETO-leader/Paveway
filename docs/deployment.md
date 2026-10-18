# Linux systemd user 部署（M4，0.6.0）

需要可用的 user manager、systemctl、systemd-analyze 和 journalctl。先安裝套件、init、workflow create，再執行 doctor／verify-omp。venv、配置、workspace 和 state 放在可持久使用的絕對路徑。

```bash
oicoach-runtime service render --config /absolute/config.toml
oicoach-runtime service install --config /absolute/config.toml
oicoach-runtime service enable --now --config /absolute/config.toml
oicoach-runtime service status --config /absolute/config.toml --json
oicoach-runtime status --config /absolute/config.toml --json
oicoach-runtime service logs --config /absolute/config.toml --lines 50 --json
```

install 在 temporary unit 上執行 `systemd-analyze --user verify`，成功才原子發佈並 daemon-reload，**不啟用、不啟動**。輸出具體後續命令；顯式 enable --now 才啟用及啟動。已有相同 unit/config 可重入；不同內容需 --replace。未擁有的同名 unit、symlink 或其他 config 的 unit 不覆寫、不控制。

預設 oicoach-runtime.service，所有 service 命令可 --name CUSTOM.service。安裝目錄為 XDG_CONFIG_HOME/systemd/user，預設 ~/.config/systemd/user，需與 manager 搜尋環境一致。--working-directory 預設配置所在目錄；特殊空格、引號、百分號及美元符路徑已實測，WorkingDirectory 拒絕反斜線／末尾空白，可另選正常目錄。

生成內容包括絕對 venv Python ExecStart、WorkingDirectory、PATH 快照、PYTHONUNBUFFERED=1、UMask=0077、Restart=on-failure/RestartSec=3、Unit 區段的 StartLimitIntervalSec=60/StartLimitBurst=5、TimeoutStopSec=60、KillMode=control-group、journal 輸出。保留 venv symlink，不 resolve 成系統 Python。`packaging/systemd/oicoach-runtime.service` 是含佔位符的示例；正式使用生成器。

PATH 是安裝時快照；工具位置改變後更新 unit 再手動 restart。API key 不會複製進 unit，使用 OMP 已有認證或使用者管理的服務環境；新終端 env 不自動進入已啟動的 manager。沒有設定可能破壞 Agent/MCP 的私有 /tmp 或目錄隔離；工具權限由 OMP／使用者管理。

```bash
oicoach-runtime service stop --config /absolute/config.toml
oicoach-runtime service start --config /absolute/config.toml
oicoach-runtime service restart --config /absolute/config.toml
oicoach-runtime service disable --config /absolute/config.toml
oicoach-runtime service disable --now --config /absolute/config.toml
oicoach-runtime service uninstall --config /absolute/config.toml
```

disable 不帶 --now 不停止服務；uninstall disable --now、刪 unit 並 reload，保留 DB/session/workflow。--replace 不重啟活動服務。先停止同 state/socket 的前景 serve，避免 flock 衝突。重啟活動回合會留下 uncertain，須核對副作用，不重播 prompt。

doctor 查預設 unit 的 config marker／ActiveState；自訂名稱用 service status --name。Unit active 不等於模型或業務成功。啟動頻率限制需先修復原因，再可 `systemctl --user reset-failed UNIT` 後 start。

不自動設定 linger。需要登出後持續運行時由使用者自行選擇 `loginctl enable-linger USER`。沒有 manager 時可前景 serve，不能宣稱已開機／登出常駐。

## 實測

```bash
OICOACH_TEST_SYSTEMD=1 .venv/bin/python -m unittest discover -s tests -p test_systemd.py -v
```

包含 transient --collect unit 的兩次 SIGKILL／uncertain 不重播，以及真正安裝 UUID unit 的特殊路徑、enable/start、SIGKILL 後 queue 恢復、restart、journal、disable/uninstall、DB 保留。finally 清理 unit、enable symlink 與程序，不啟用預設服務／linger。CI 預設跳過實機 service 測試；單元測試仍驗證安裝順序、失敗不發佈、拒絕覆寫與輸入校驗。
