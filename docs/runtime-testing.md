# 整個專案運行測試（2026-10-18，Asia/Singapore）

下方原始記錄屬於 0.5.0；0.6.0 的 M4／coach、0.7.0 記憶、0.8.0 終端 UI 與最新 0.9.0 對話式 UI 結果見本文末段。舊版「記憶暫緩」等敘述保留為歷史記錄。

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

Tijian 使用上游原樣 CLI／application crates 的驗證入口；完整 Desktop UI、運行中 Desktop projection routing、外部 OJ 抓取及需 Desktop executor 的向量計算仍未驗收。沒有另行安裝的正式 Tijian 發行版時，其獨立 opt-in 測試會跳過，原碼 CLI 的讀寫／MCP／模型測試仍會實際執行。正式 OI GPT workflow 及 M4 長期服務部署尚未實作，不屬於本輪已通過的功能。

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

## 0.9.0：對話式 UI 與操作競態修復

2026-10-19（Asia/Singapore）。從安裝 wheel 跑 UI 專項 **18 項、11.917 秒全部通過**（`/tmp/paveway-ui09-tests.log`），包含真實 PTY 新增/編輯中文多行筆記、版本 2 內容驗證、排程及確認取消，沒有只對畫面字串模擬成功。連續操作實際揭露保存確認吞下一命令和正文拼接問題，修正後通過。選單 Enter 一次執行、普通 q/數字、bracketed paste、縮放、終端恢復及退出後 daemon 繼續也以 pyte 解析的實際終端格子驗證。

其後補強表單往返保留答案，重新建置安裝 wheel 再跑最終主回歸。真實模型定向 **1 項、11.367 秒通過**（`/tmp/paveway-ui09-final-real.log`），使用獨立 workspace/state/profile，驗證新 UI controller 經 CLI/daemon/OMP 收到並保存有隨機識別碼的真實回覆。此用例不宣稱真實模型 PTY 串流驗收；PTY 操作與真實模型路徑分別測試。

```bash
.venv/bin/python -m pip install '.[tijian,test]'
.venv/bin/python -m unittest discover -s tests -v
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_tui_omp.py -v
```

最終安裝包無 opt-in 主回歸 **147 項，119.460 秒：133 通過、14 跳過，無失敗**（`/tmp/paveway-ui09-final-regression.log`），包含最終 UI 的 18 項。skip 為需要指定模型、systemd 或 Tijian 真實環境的 opt-in；其中 UI 真實模型另行通過。

本次未重跑 0.7.0 的全部 Tijian 原碼/模型/systemd opt-in；其結果保留歷史。以真實 OMP PTY 人工檢視交互安排，並以新 UI PTY 的終端格子快照核對底部編輯區和 slash 選單。UI 使用 prompt_toolkit，pyte 僅用於測試；未安裝 `.[test]` 時 PTY 格子驗收明確 skip。未啟用預設服務或修改 linger。

## 2026-10-20：systemd 憑證與模型失敗提示

最終 wheel 無 opt-in 主回歸 **152 項，140.787 秒：138 通過、14 跳過、無失敗**（`/tmp/paveway-fixes-regression.log`）。含 UI 19 項，新增 auth-error 的完整 fake OMP/daemon 持久化測試：保存 agent_auth_failed 和安全 401 摘要、attempt 1、settled 完成、無最終回覆，DB 回傳／journal 不含假的 provider 金鑰或 token；UI 到達和重開歷史均顯示原因。安全摘要分類涵蓋 401/403/404/408/429/5xx/timeout/aborted 及未知錯誤。服務捕獲測試檢查選定變量、0600、合併保留、拒絕不安全檔案／缺失變量，以及 unit 驗證失敗時不保存憑證。

另以 `OICOACH_TEST_SYSTEMD=1` 定向跑 **2 項、16.530 秒全部通過**（`/tmp/paveway-fixes-systemd.log`）。真正安裝 UUID unit，從環境檔讀入含空格/引號/百分號/美元符/反斜線的測試值，直接核對 `/proc/PID/environ`，同時驗證特殊路徑、SIGKILL 後 queue 恢復、restart 和清理。測試用 unit 均已移除。

實際 enabled 的預設服務已安裝新 unit、明確捕獲 OPENAI_API_KEY 並在無活動回合時重啟，確認 daemon 值與 shell 相符，只輸出比較結果、不輸出值。同一 daemon 的新測試任務成功回覆並保存 **CONNECTION_OK**（event `e675777f-4186-4e2f-94dc-54b35b646c31`，attempt 1）；新憑證載入前的獨立連線檢查保存了安全的 HTTP 401 摘要（event `e5458268-c8d9-41e6-8eaa-c84c7cbc5944`）。經公開 CLI 恢復此任務到 UI controller，實際斷言錯誤原因與成功回覆均顯示。本次不重播原始 `test` 事件，也未回填其已丟失的錯誤文字。

`doctor` ok、預設服務 enabled/active、憑證檔 0600 且 Git 忽略、pip check、git diff --check、文件連結和 shell/TOML 示例均通過。本次未重跑全部 Tijian/其他模型 opt-in；真實模型驗收是上述 systemd daemon 的實際新請求，無 token/tool 串流宣稱。

## 0.10.0：自定义系统提示词与用户 Skill

真实 OMP 定向验收 **2 项、65.902 秒通过**（`/tmp/paveway-custom-real-acceptance.log`）。首项不调用模型，通过 get_state.systemPrompt 核对 append、replace 及 OMP 原生发现的 Skill 名称/描述；保持工具读取能力，正文不预先灌入系统提示词。第二项先在系统提示和 Skill 正文分别生成随机识别码，用户请求不包含识别码，核对模型实际使用工具并在最终回覆包含两者；随后替换系统提示、停用 Skill、以同一 session --resume 再回合，核对新系统提示不含旧系统标识且最终回复为新识别码。隔离临时 workspace/session，不改全域 OMP 配置。

```bash
.venv/bin/python -m pip install '.[tijian,test]'
.venv/bin/python -m unittest discover -s tests -v
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_customization_omp.py -v
```

新增本地单元覆盖 workflow 范围、私密权限、无模板插值、Skill 启停/删除和 reset 保留 Skill、并发 CAS、超限/非法名字/symlink 拒绝、旧快照不可变和禁用技能不载入。UI 真正 PTY 按键新增/编辑系统提示与 Skill，直接通过 CLI 验证保存内容、派发后的快照，再停用/恢复默认/删除；另测 CLI 冲突保留 UI 草稿。Runtime 用同一 task 两轮确认绑定保持、快照各自保存配置。真实 systemd 凭证修复与先前全部 Tijian/systemd 结果仍列在历史段落，不冒充本轮全部重跑。

最终安装包主回归 **162 项、152.133 秒：146 通过、16 跳过、无失败**（`/tmp/paveway-custom-regression.log`），其中 UI 21 项；skip 为模型、Tijian 或 systemd 的 opt-in，原生自定义两项另行实际通过。 文件链接、shell/TOML 示例、pip check 和 git diff --check 通过。

本机已安装 0.10.0，在确认无活动回合后重启默认 user service，核对 enabled/active；test01 的自定义设置仍为默认 revision 0，未替用户填入示例提示词或 Skill，原模型凭证保持不变。使用中的旧 UI 需要关闭重开以加载新命令。

## 0.11.0：Git 用户配置与历史多设备同步

用实际本地 bare remote、两个独立设备目录验证双向配置/历史传输、并发编辑保留版本及显式解决、未知 workflow 待绑定、离线提交/重试、异常远端文件在 checkout 前拒绝、归档不导入队列、不复制 DB/凭证。原生文字档案另验证 user/assistant 全文字与分支关系，过滤 thinking、工具调用和 toolResult，不完整档案保留之前导出。Git 数据专项补强后 **9 项、21.098 秒全部通过**（`/tmp/paveway-sync-validated.log`）。

实际两 daemon 通过配置自动完成同步，无 UI、没有手动调用 cycle；模型请求成功后在第二设备查询到原 run，第二设备 events 仍为空。另测 remote 不存在时同步记录 error，但前景训练仍 succeeded，daemon/agent 正常。定向 **2 项、12.326 秒通过**（`/tmp/paveway-sync-auto.log`）。

UI **22 项、27.151 秒通过**（`/tmp/paveway-sync-ui-final.log`），其中实际 PTY 建立测试 remote、填写配置、发起训练、请求后台同步、等待 push、查看跨设备历史及 pause；验证 Git bare 分支真实存在归档文件，非仅渲染菜单。首轮主回归 **173 项、175.003 秒：157 通过、16 跳过，无失败**（`/tmp/paveway-sync-regression.log`）；其后新增异常历史读取验证并补强活动会话重新检查，最终重新安装后结果列于后段。

本机实际 user service 保持 enabled/active，独立本地数据仓库后台保存 4 个终态 run、2 份原生文字会话，2 个提交，仓库权限 0700。核对当前模型 key 未出现在导出 JSON，Git 跟踪列表无 SQLite/config.toml/service.env/auth 文件。用户尚未指定数据 remote，所以未对互联网仓库 push/pull；本机只自动本地 commit。没有更改代码仓库 origin 或 OMP 登录。

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s tests -p test_git_sync.py -v
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_runtime.RuntimeTests.test_automatic_two_device_git_sync_without_ui_or_queue_import \
  test_runtime.RuntimeTests.test_git_sync_failure_does_not_block_foreground_training -v
```

最终安装包完整回归 **174 项、178.887 秒：158 通过、16 跳过、无失败**（`/tmp/paveway-sync-final-regression.log`）。包括同步专项 9 项、两 daemon 自动同步/故障隔离、UI 22 项和所有既有无 opt-in 流程；需要真实模型/Tijian/systemd 环境的 opt-in 仍明确跳过，本轮未冒充重新执行全部旧版实机验收。 文档链接、shell/TOML 示例、pip check 和 git diff --check 通过。


## 0.11.1：默认本地 Git 与 remote 配置

新增默认归档、多GPT共用仓库、无 fetch/push、幂等保留已有设置、修改/清空 remote 保留身份/提交/暂停等真实 Git 验证；扩展 PTY 验证 `/sync remote` 清空远端。两 daemon 自动同步和故障隔离仍通过。


完整安装包回归 **178 项、195.150 秒：162 通过、16 跳过、无失败**（`/tmp/paveway-default-git-regression.log`），命令 `.venv/bin/python -m unittest discover -s tests -v`。跳过项仍为真实模型/Tijian/systemd 的 opt-in；本次双设备远端以实际本地 bare Git 仓库验证，未连接未配置的互联网远端。依赖检查、编译、文档本地链接和 `git diff --check` 通过。

本机已安装 0.11.1，并在确认 active_runs 为空后重启 user service。服务 enabled/active，daemon running；核对原同步设置、设备身份与仓库路径完全一致，remote 仍为空且 Git 没有 origin。重启后自动归档成功，保留现有 4 条终态 run，未上传远端。旧 UI 需关闭重开以加载 `/sync remote`。


## 0.12.0：三类 Skill 同时加载

分类与兼容专项验证 OI Introduce、user information、personal skill；默认旧分类不改写数据，CLI 编辑保留分类，修改类别保留启停状态，append/replace 同时包含三类正文，停用不加载。单元/Git/UI 定向 41 项、62.682 秒通过（`/tmp/paveway-skills-targeted.log`）。实际 OMP RPC systemPrompt 验收 1 项、3.833 秒通过（`/tmp/paveway-skills-native.log`），不调用模型。完整回归还覆盖最终扩展的 PTY 移动分类操作。


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

## 2026-10-21 — 0.14.0 系统时区与倒计时

基础定向测试 **20 项全部通过，26.695 秒**（`/tmp/paveway-countdown-targeted.log`）：

```bash
.venv/bin/python -m unittest tests.test_store tests.test_tui.TimerDisplayTests tests.test_runtime.RuntimeTests.test_upcoming_cli_reports_due_time_and_filters_scope -v
```

覆盖 scoped upcoming 查询、100 项边界/最近触发排序、不返回 payload、终态排除、after-turn 待锚定/锚定后 UTC、CLI 到 daemon 的过滤，以及系统 TZ 下冬夏不同偏移、倒数递减/过期等待、错误恢复及作用域切换。随后增加历史消息时间恢复、刷新期间发送恰好一次和表单确认避让测试。

首次 UI 回归发现取消确认和后台 timer 读取竞争，导致确认未送出；单独重现后修复为表单/选择器打开时暂缓后台队列查询。最终安装 **0.14.0** 后，完整 UI 加 after-turn 集成测试 **32 项全部通过，54.223 秒**（`/tmp/paveway-countdown-ui-final.log`）：

```bash
.venv/bin/python -m unittest tests.test_tui tests.test_runtime.RuntimeTests.test_after_turn_anchor_checked_and_armed_after_settled -v
```

实际 PTY 验证秒级倒数变化、到时自动执行、执行中状态、完成后移除、输入草稿保留、仅一个 run 及终端恢复；原有自定义 prompt/Skill、Git 同步、笔记增改、排程取消、缩放及退出重进恢复均通过。测试使用隔离 fake OMP/临时 daemon，不创建真实用户定时任务，不调用模型。本轮定向套件合计覆盖 **49 个不同测试**，未将历史完整主回归或模型验收算作本轮重跑。

本机确认 active_runs 为空后重启用户服务，公开 events.upcoming 实查 test01/training 返回空队列，daemon running。系统时区实查为 +08；UTC 2026-10-21T00:00:00Z 在 UI 格式化为 2026-10-21 08:00:00 +08。保留现有配置、会话、Tijian 与本地 Git 设置，旧 UI 需退出重开以加载新界面。pip check、compileall 与 git diff --check 通过。

## 2026-10-21 — 0.15.0 Skill 按需读取

安装 0.15.0 后，自定义设置与完整 UI 定向 **38 项通过，58.498 秒**（`/tmp/paveway-lazy-skills-targeted.log`）：

```bash
.venv/bin/python -m unittest tests.test_customization tests.test_tui -v
```

核对 append/replace 的 system.md 只保留自定义系统正文，三类原生 Skill 文件完整可读、分类描述可发现、停用项不生成、旧分类兼容、空自定义系统提示无需另造索引提示、原始描述不改写、快照及 CAS 保持不变。真实 PTY 验证分类管理、正文修改和原生文件快照，既有倒计时、笔记、Git 与会话恢复流程均通过。

实际 OMP 18.10.4 无模型验收 **2 项通过，7.117 秒**（`/tmp/paveway-lazy-skills-native-final.log`）：

```bash
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" .venv/bin/python -m unittest \
  tests.test_customization_omp.RealCustomizationTests.test_native_system_prompt_append_replace_and_skill_discovery_without_model \
  tests.test_customization_omp.RealCustomizationTests.test_disabled_tools_or_skills_do_not_inject_a_fallback_body -v
```

通过 RPC get_state.systemPrompt 确认 append/replace 的三类名称与分类描述存在、正文随机标识不在提示中、停用项不发现；另确认工具或 Skill 被停用时不注入替代正文。该验收不调用模型。

模型读取端到端用例已增加：正文随机 token 不在系统提示或用户请求中，必须出现在实际 toolResult 与最终答复，无关 Skill token 不可出现在工具结果，再恢复同一会话确认新系统提示生效。本次尝试失败：OMP 在第一轮返回 error（安全分类 agent_turn_failed）；隔离诊断重试仍失败，当前默认模型为 oi-relay/gpt-6.1-sol。日志为 `/tmp/paveway-lazy-skills-native.log`、`/tmp/paveway-lazy-skills-model-diagnostic.log`，未记录凭证或供应商原始错误。故本轮未完成模型实际读取正文与续聊的验收，不能将原生目录验证冒充该项成功。

在无活动回合时重启本机用户服务，核对 installed_version=0.15.0、daemon running、active_runs=0，配置和各 workflow customization.json 哈希前后一致。不改用户 Skill 内容、提示词、会话身份或全局 OMP 配置；无需数据库迁移。旧历史保留，旧 UI 需退出重开。pip check、compileall、git diff --check 通过。

## 2026-10-21 — 0.16.0 模型与思考等级

安装最终 wheel 后，定向回归 **86 项全部通过，104.652 秒**（`/tmp/paveway-model-selection-regression.log`）：

```bash
.venv/bin/python -m unittest tests.test_model_selection.ModelSelectionTests tests.test_foundation \
  tests.test_customization tests.test_git_sync tests.test_tui \
  tests.test_runtime.RuntimeTests.test_model_settings_apply_next_dispatch_and_resume_the_existing_session \
  tests.test_runtime.RuntimeTests.test_customization_applies_next_dispatch_without_rebinding_existing_task -v
```

覆盖旧设置默认继承、模型/等级独立保存、无效选择拒绝/CAS、不改原提示词/Skill、配置 thinking 校验、OMP 目录失败与超时不改设置/不暴露原输出、双设备模型设置同步、原记录兼容。模型目录兼容实机非推理模型的 thinking=null。实际 PTY 输入 /select-model，方向键选择 fake/coach，筛选 high、发送请求核对 fake OMP 的 argv，再恢复默认；原有全部 UI 流程均通过。另在一个运行中的 hold 回合修改模型与等级，核对该回合参数不变、下一回合使用新参数且恢复同一 session/binding。

实际 OMP 18.10.4 参数验收 **1 项通过，5.379 秒**（`/tmp/paveway-model-selection-native-final.log`）：

```bash
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest tests.test_model_selection.NativeModelSelectionTests -v
```

使用隔离临时 workflow/workspace/session，读取实际模型目录，选 oi-relay/gpt-6.1-sol，经公开配置生成 --model 和 --thinking，分别启动 high/low，RPC get_state 核对实际 provider/id 与 thinkingLevel。只验证本机 OMP 接受参数，不调用模型服务；未将它称为模型供应商可用性或生成回复验收。

本机在 active_runs 为空时重启用户服务，已安装 0.16.0、daemon running。UI 所用公开 CLI 对 test01 读取模型目录成功，共 70 个模型；现有配置与所有 customization.json 的哈希保持一致，未预设模型/思考等级或修改凭证。旧 UI 需退出重开；以后更换设置从下一次派发开始生效，不必反复重启 daemon。无需 DB 或 manifest 迁移；pip check、compileall 和 git diff --check 通过。

## 2026-10-21 — 0.17.0 启动代理同步与检查

实际诊断确认 20:24 启动的 OMP 未取得终端代理，20:38/20:52 记录 ECONNREFUSED 后继续请求。新增自动代理同步与启动 TCP 检查，使用原有私密 config.service.env，保留已捕获 API key，环境文件锁定/原子合并；每次 OMP spawn 与模型目录查询读取最新代理，不自动重播任何历史请求。

初步服务/模型设置 14 项通过（7.065 秒，`/tmp/paveway-proxy-initial.log`）。最终代理、服务、配置、完整 UI 和 daemon 不重启的代理热加载定向 **77 项全部通过，81.752 秒**（`/tmp/paveway-proxy-regression.log`）：

```bash
.venv/bin/python -m unittest tests.test_proxy tests.test_setup tests.test_foundation tests.test_tui \
  tests.test_runtime.RuntimeTests.test_proxy_updates_apply_to_next_omp_without_daemon_restart -v
```

覆盖大小写归一、PI_PROXY_PROVIDER、缺省保留/空值清除、API key 保留、并发 24 次环境更新不丢项、0600/不安全文件拒绝、代理 URL/认证信息不进报告、端口可达/不可达与 DNS 总等待边界。实际 PTY 启动后核对代理已保存、API key 未变、报告无 URL/密码，发送请求核对 fake OMP 取得代理，再执行 /proxy；另在同一 daemon 的两次派发之间更新代理，核对第二次 spawn 使用新值、恢复同一 session，daemon PID 未变。

本机 `proxy sync` 报告代理 TCP 可达、bypass_all=false。隔离临时 workspace/session 的真实 OMP 调用 **openai-codex/gpt-6-luna、low**，禁用工具/扩展/Skill/规则，仅请求一次固定文字，得到 completed 和准确回复 **PROXY_OK**（`/tmp/paveway-proxy-native-model.log`）。这项确实调用了模型，不把单纯 TCP 探测称作模型连线成功；未触发原GPT请求、Tijian 写入或新增定时任务。

安装最终 0.17.0 后，为替换保留旧环境的卡住进程，重启受管用户服务。核对 daemon running、主 PID 改变、HTTP_PROXY/HTTPS_PROXY/NO_PROXY 及小写别名均已存在；API key 与旧 daemon 值相同，config.toml 和各 customization.json 哈希不变。原 run d7e8a4c3-2ba8-4124-8a1f-35083a16c3ca 已保存为 uncertain，active_runs=0，没有自动重新发送。旧 task 的 uncertain binding 仍需人工核对恢复；用户可 /new 后重新发送，不应声称原 task 可直接盲重试。无需 DB/manifest 迁移，pip check、compileall、git diff --check 通过。

## 2026-10-21 — 0.18.0 GPT 命名与 Git 稽核

界面、文档与工程说明统一 GPT，既有 event.md 保持原字节。默认 revision=0、append/replace、工具/Skill 停用均附加系统命名约定；Skill 正文仍按需读取。

安装包回归 **57 项：56 通过、1 条件略过，78.777 秒**（/tmp/paveway-gpt-regression.log）：

```bash
.venv/bin/python -m unittest tests.test_customization tests.test_tui tests.test_omp_verification tests.test_setup tests.test_runtime.RuntimeTests.test_customization_applies_next_dispatch_without_rebinding_existing_task -v
```

最终定向 **12 项全通过，18.814 秒**（/tmp/paveway-gpt-final-targeted.log）：

```bash
OICOACH_TEST_REAL_OMP_CONFIG="$PWD/.runtime/config.toml" .venv/bin/python -m unittest tests.test_customization tests.test_customization_omp.RealCustomizationTests.test_native_system_prompt_append_replace_and_skill_discovery_without_model tests.test_customization_omp.RealCustomizationTests.test_disabled_tools_or_skills_do_not_inject_a_fallback_body tests.test_tui.TerminalIntegrationTests.test_pty_slash_completion_arrow_selector_and_reply_keep_next_draft tests.test_runtime.RuntimeTests.test_customization_applies_next_dispatch_without_rebinding_existing_task -v
```

覆盖默认命名指令、原设置/event.md 不变、快照不可变、原生系统规则与 Skill 按需加载、原 session 下一轮生效，以及实际 PTY 的 GPT 回复标题和草稿保留。原生 systemPrompt 支持分块列表，按渲染内容检查规则。

另在隔离 workspace/session 真实调用 openai-codex/gpt-6-luna：未传入个人 Skill 或自定义提示，以原 event.md 首行角色描述要求自述，最终以 GPT 自称且无被禁止职称（/tmp/paveway-gpt-identity-model-final.log）。该最终行为验证通过，不将仅有命名指令的存在检查称为模型行为验收。

源代码远端 fetch/refs 核对后，187 个可达历史 blob 与工作区的已知私密数据/凭证检查无命中。工程 Skill/默认模板/规划文件已在源码远端，个人 Skill/对话已在独立本机资料 Git，目前没有 remote，见 privacy-audit.md。未 commit/push。两份既有 event.md 哈希未变；本机无活动回合时升级至 0.18.0，daemon running，用户设置和 event.md 哈希仍一致。旧 UI 需重开，pip check、compileall、git diff --check 通过。


## 0.19.0 记忆与对话预算验收

共 69 个不同用例通过：记忆/迁移 7、定制 8、Runtime 42、OMP 验证 6、原生定制 2、真实记忆 1、上下文窗口 3。Runtime 首轮 41 项中只有旧系统提示精确断言不含新增预算说明，更新断言后该项通过，另增缺失守卫不派发用例通过。

- `/tmp/paveway-context-memory.log`：7 通过（6.426 秒）；新增“大于旧 8 KiB 但不超过 10k tokens”断言亦在 final-checks 中重验。
- `/tmp/paveway-context-customization.log`：8 通过（3.977 秒）。
- `/tmp/paveway-context-runtime.log`：40 通过、1 旧断言失败；`/tmp/paveway-context-runtime-final.log` 修正后 1 通过（2.333 秒）；`/tmp/paveway-context-guard.log` 新增 1 通过（2.250 秒）。守卫缺失时 dispatch_intent 始终为空，不发送业务 prompt。
- `/tmp/paveway-context-native.log`：原生 append/replace 及按需 Skill 目录 1 通过（4.939 秒）。
- `/tmp/paveway-context-final-checks.log`：8 通过（10.174 秒），包括 6 OMP 验证、原生工具/Skill 停用权限、记忆边界。
- `/tmp/paveway-context-memory-real.log`：真实模型写入、daemon 重启、全新 task 无工具读回 1 通过（26.737 秒）。
- `/tmp/paveway-context-model-window.log`：3 通过（5.499 秒）。原生 tokenizer 测试 7 类消息，覆盖完整旧轮次、大型工具结果/参数、中文/emoji 超大输入、工具配对和输入不变；真实模型只看到裁剪后的超大输入，回复 MISSING，而随机识别码仍在完整会话文件中。

真实模型验收均使用隔离 workspace/state/session 和当前 workflow 的模型选择，不使用默认余额不足的 relay，不写本机训练记录。模型预算按 o200k_base JSON 计数，不声称不同 provider 的最终计费相同。原文历史保留，不将 prompt 要求等同于模型每次必然写入成功。


## 0.19.1 会话冲突诊断

`PYTHONPATH=tests .venv/bin/python -m unittest -v test_store test_runtime.RuntimeTests.test_uncertain_session_explains_new_task_without_replaying_old_request test_runtime.RuntimeTests.test_successful_explicit_reset_leaves_uncertain_event_unreplayed test_runtime.RuntimeTests.test_crash_boundaries_and_explicit_provider_failure test_tui.TimerDisplayTests.test_legacy_and_typed_session_conflicts_explain_how_to_continue`：20 项通过（29.879 秒），日志 `/tmp/paveway-session-conflict.log`。

实际回归场景：前一个已派发请求崩溃后保留 uncertain；同 task 的新请求在派发前被 session_uncertain 阻挡，事件和 run 保存固定安全中文指引且无路径/正文泄露；新 task 可以正常执行；旧文件字节、旧 uncertain 状态不变，无旧 prompt 重播。另验证旧版无说明的 blocked/conflict 界面也给出 /new 和 /recover。没有迁移或修改用户旧错误记录，没有自动清除会话保护。


## 0.19.1 工具循环与实时进度修正

对话预算改为计数角色、content、toolCallId/toolName/isError 的 JSON，排除本地 usage/details/耗时等 metadata，避免有效工具证据被过早裁掉；完整会话不改写。上下文说明提醒不要因旧交换省略而重建已完成计划。当时 workflow 要求空库/样本不足结束并说明缺失数据；该规则过强，现已由下文的自动补齐规则取代。实际权限或网络限制阻止继续时，todo 的不可完成项目标为 blocked/abandoned，不能反复 init。

`/tmp/paveway-loop-progress.log`：17 项通过（7.252 秒），包括原生 tokenizer/metadata 大对象回归、UI 进度/草稿、Runtime 实际 RPC 工具进度和无命令正文泄漏、会话保护与 OMP 清理/续接。`/tmp/paveway-loop-native.log`：原生 OMP append/replace/Skill 权限 2 项通过（6.911 秒）。进度状态栏最终定向 1 项通过（`/tmp/paveway-loop-ui-final.log`）。

真实 OMP + 真正 Tijian source CLI 的隔离空库验收（`/tmp/paveway-empty-library-smoke.log`）：使用相同赛事风格分析请求、启用原生工具及 MCP，4 次工具操作后 completed 且 settled，最终说明缺少题目数据。harness 禁止外部 OJ 访问，未抓取或修改用户题库，未将本次模型操作当成实质命题风格分析成功。此结果不保证所有长任务绝不会循环。

新增可选 events.show.progress 仅在内存维护；UI 显示阶段、调用次数和最近活动时间，不将工具开始/有活动误判为最终成功，也不自动重播中断任务。


## 2026-10-23 Tijian 自动补齐与上下文证据保留

此前为终止空库循环而加入的“空库立即结束”规则过强，已撤回。新流程默认本地优先，样本不足时自动发现可信赛题 ID/URL、少量首次收录，再从 Tijian 读回 statementMd 分析；用户明确只用现有数据或禁止新增、以及 read_only 仍优先。检索最多 6 次，有可靠候选即先收录，不为凑齐目录反复搜索。有 shell 时优先既有 JSON CLI，避免无必要的 eval SDK 探索。小样本需明确覆盖缺口。

上下文裁剪增加最多 1,500 tokens 的已省略工具结果原文片段，计入同一 10k 对话预算；原始调用线索和结果片段只作历史数据，不含 assistant 隐藏思考，不持久化到另一套缓存。原生 tokenizer 测试覆盖长工具轮次的候选线索保留、消息不变、工具配对及上限。

- 真正 OJ 首次收录→保存→不可达代理下仍能读取/重复 add：1 项通过，16.961 秒，`/tmp/paveway-tijian-network-acceptance.log`。使用全新 XDG 用户数据目录，不修改正式题库。
- Tijian 适配回归：16 通过、2 条件略过，5.660 秒，`/tmp/paveway-tijian-supplement-regression.log`。
- 上下文及自定义 prompt/Skill 回归：10 通过，4.392 秒，`/tmp/paveway-tijian-context-final.log`。
- 完整真实模型验收的第一次尝试仍过度检索，触及测试专用 60 次工具调用上限；第二次已自动收录并读取 5 道真实题面，但在 210 秒测试时限内未结束。分别保存为 `/tmp/paveway-tijian-supplement-first-attempt.log`、`/tmp/paveway-tijian-supplement-second-attempt.log`；不能将这两次报告为端到端成功。随后收紧发现阶段、改为先入库后扩展、优先稳定 CLI，继续验证。


## 2026-10-24 剩余验收与工具证据裁剪

远端已有 53fbb77；本机 HEAD 的空对象从远端重新获取后，git fsck 未见可达对象损坏，工作区文件保留。之前的 /tmp 模型验收日志未留存，不能据此补报成功。

补上可重复的 opt-in 模型验收 `RealSourceTests.test_model_collects_missing_statements_and_finishes_report`：隔离正常 Tijian 数据目录，按所选 workflow 的模型和思考级别执行原始赛事分析请求；要求 completed、settled、实际收录、真正 problem_get 非空题面结果和引用已收录题号的最终报告。不会加载私人 Skill/系统提示，不连接正式 Runtime socket，不写正式题库。

仅移动静态规则到 system 仍不足以通过完整模型验收：本轮第一次尝试读到了真实题面，但 300 秒内未完成（`/tmp/paveway-model-report-before-evidence-fix.log`），不能计作成功。进一步修正最新工具交换超限时被整组丢弃的问题：保留调用/结果配对，在副本中缩短结果文本并标注首尾片段，仍遵守同一 10k 上限、原始会话不变。另提供 `tijian tools --name OP`（可重复）按需读取 schema，旧的完整目录输出和 MCP discovery 保持兼容。

已复验：核心 43 项中 42 通过、1 条件略过（`/tmp/paveway-core-final.log`）；最终上下文/目录/模板/界面状态回归 34 项中 31 通过、3 条件略过（`/tmp/paveway-final-regressions.log`）；真实终端自定义 prompt/Skill 派发和退出重入两项通过；Runtime 安全进度与 uncertain 隔离两项通过（`/tmp/paveway-runtime-resume.log`）；真实 OJ 缓存读取一项通过，23.564 秒（`/tmp/paveway-network-resume.log`）。最初大组测试的两个过时界面断言已修正并定向重验，没有将原始失败报告为全通过。

完整模型验收命令（需要真正 Tijian source CLI、OMP 认证及可用网络）：

```bash
OICOACH_TEST_TIJIAN_SOURCE_CLI="$PWD/.runtime/tijian-build/debug/tijian-source-cli" \
OICOACH_TEST_TIJIAN_SOURCE_SEED="$PWD/.runtime/tijian-build/debug/tijian-source-seed" \
OICOACH_TEST_TIJIAN_NETWORK=1 \
OICOACH_TEST_TIJIAN_OMP_CONFIG="$PWD/.runtime/config.toml" \
OICOACH_TEST_WORKFLOW=test01 \
.venv/bin/python -m unittest tests.test_tijian_source.RealSourceTests.test_model_collects_missing_statements_and_finishes_report -v
```

第二次完整模型验收虽然减少了前期目录读取，仍在扩展年份和反复读题过程中超时，不能计作成功。workflow 因此将默认首轮收敛为每类赛事 1 题、最多 3 题，逐题保存简短公开证据摘要到临时目录，之后立即生成初步报告并标明样本局限；仅明确要求完整范围时继续分批扩展。

收敛样本后的真实模型在 225.053 秒内 completed 且 settled，并生成三类赛事样本的完整公开分析；但随后 todo 收尾短句成为 native get_last_assistant_text，导致最终文字长度断言失败（`/tmp/paveway-model-report-bounded.log`）。这次没有计作端到端通过。Runtime 已改为有界收集本次 prompt 的 assistant message_end 可见文字，保留完整报告及后续补充，排除 thinking/工具内容；继续执行可选查询，以免吞掉持久化错误。回归覆盖报告后短补充、16 KiB 多字节上限、跨回合隔离及原有持久化失败语义。

最终完整真实验收通过（`/tmp/paveway-model-report-output-final.log`）：1 项，223.679 秒；正常模式的隔离空库收录 3 题，实际读回 P10217、P10786、P11362 的非空题面，completed 且 settled，取得 3,288 字、引用已收录题号的公开回复。该项不是全部近年赛事的统计验收，也不保证所有模型/网络条件下都能在同样耗时内完成。

最后完整 Runtime 回归 46 项全部通过，115.519 秒（`/tmp/paveway-runtime-final.log`）。额外定向检查确认原生可见文字聚合保持 16 KiB 上限、不包含 thinking/工具正文且不跨回合复用。依赖检查、compileall、git diff --check 通过。源代码可达历史 232 个 blob 及最终跟踪工作区的已知私密值检查无命中，未发现运行时资料路径入库；个人资料 Git 没有 remote，不随源码 push。


## 2026-10-26 VJudge 登录集成

新增 Paveway `/tijian` 选单及 `tijian auth status/test/login/cookie/clear` CLI。登录采用与上游桌面端相同的 VJudge HTTPS 协议，成功验证后才原子写入原生 Cookie 会话文件；密码不持久保存，不提供新的密码绑定/自动续期。Tijian 上游工作区没有修改。UI 密码与 Cookie 以隐藏字段输入，CLI 通过 0600 临时文件接收；登录数据不加入模型对话、命令参数或 Paveway Git 归档。

验证结果：

- UI、认证、Tijian 适配回归共 69 项，67 通过、2 条件略过，81.438 秒（`/tmp/paveway-vjudge-regression.log`）。包含真实 PTY 的隐藏输入、Esc 不提交、草稿保留及现有界面操作。
- 最终认证定向 14 项全部通过，5.167 秒（`/tmp/paveway-vjudge-security-final.log`），覆盖 HTTPS 表单与 Cookie 合并、验证后保存、失败保留旧会话、并发变更不覆盖、跨域/降级重定向阻止、私密临时文件、无命令参数或错误正文泄露、输入注入拒绝、NO_PROXY 与不支持的代理类型，以及未配置时不伪称已联网检查。
- 真正 VJudge 网络使用明确无效的测试 Cookie，返回 `vjudge_not_logged_in`，没有创建会话文件。未使用私人真实账号，不能据此宣称真实账号登录或 CF1A 抓取成功。
- bash 语法、compileall、pip check、git diff --check 通过。会话与账号文件名加入源码 Git 忽略规则；独立用户数据仓库仍按自身 remote 配置管理。


## 2026-10-27 终端滚动修正

原界面关闭 mouse_support，原生 ScrollbarMargin 只负责绘制，无法点击/拖动。现启用鼠标事件，使用独立的交互滚动条和只读 TranscriptWindow；按折行后的显示行计算视口，滚动不切换输入焦点，Ctrl+Home/End 跳转首尾，PageUp/Down 按实际窗口高度翻页。移除新回复及后台恢复时无条件开启 tail-follow 的行为，阅读历史时保持位置。

`/tmp/paveway-scroll-regression.log`：完整 UI 回归 39 项全部通过，69.297 秒。新增真实 PTY 测试发送 SGR 滚轮、轨道点击、滑块拖动、移出滑块到正文后的拖动、Ctrl+End 与 PageUp 事件，验证中文单段折行仍可滚动、输入焦点及草稿保持，且不额外提交模型请求。另验证新回复到达时阅读模式不被重置。原登录隐藏输入、选择器、终端缩放与退出重入回归均通过。compileall、pip check、git diff --check 通过。重开 UI 生效，不需要重启 daemon。
