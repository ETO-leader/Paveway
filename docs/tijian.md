# Tijian 適配 v1

## 核對的上游

來源為 [zjpanhongxuan/Tijian](https://github.com/zjpanhongxuan/Tijian)，本輪以 SSH 讀取，固定檢查 commit `b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f`（workspace 0.10.0）。依據：

- [command AST、完整 help、JSON 輸入及結果序列化](https://github.com/zjpanhongxuan/Tijian/blob/b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f/crates/tijian-command/src/lib.rs)。
- [CLI 執行與輸出](https://github.com/zjpanhongxuan/Tijian/blob/b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f/crates/tijian-app/src/main.rs)、[運行中 Desktop 的 mutation routing](https://github.com/zjpanhongxuan/Tijian/blob/b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f/crates/tijian-app/src/cli_control.rs)。
- [真正的 application 命令實作](https://github.com/zjpanhongxuan/Tijian/blob/b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f/crates/tijian-usecase/src/command.rs)。

此版本沒有原生 MCP server。Paveway 提供 stdio MCP → JSON CLI bridge；不讀寫 Tijian SQLite、不重作抓取器、不透過測試 Control 修改 UI。存在 Desktop 時，沿用 Tijian CLI 的 Execute／WaitApplied 路徑；沒有 Desktop 時，使用上游允許的 CLI 直接執行路徑。`problem vector compute` 的實際計算依賴運行中的 Desktop executor。

實作位於 `src/oicoach_runtime/integrations/tijian_catalog.py`（映射及 schema）、`tijian.py`（JSON 進程適配）、`tijian_mcp.py`（官方 MCP SDK）。只有 MCP server 需要可選依賴 `mcp>=1.28,<2`；CLI 適配與 Runtime 核心仍只用標準庫。

## 已映射功能

MCP 工具名為 `tijian_<operation>`；CLI `tijian call` 可接受帶或不帶 `tijian_` 前綴的名稱。共 26 個操作，含 help，並將有檔案副作用的 article export 從 article show 分開。

| 領域 | operation | 上游功能／限制 |
| --- | --- | --- |
| 能力 | `help` | 取得安裝版本的命令參考 |
| 題庫讀取 | `problem_get`, `problem_list`, `problem_stats` | 題目、題面與持久 metadata，篩選及統計；get 保留標籤、歷史、譯文、向量和未知新增欄位 |
| 收錄／抓取 | `problem_add`, `problem_refetch` | 批次題號／URL；refetch 可指定全部、OJ、僅題面。實際 OJ／網路能力由 Tijian 決定 |
| 狀態／評分 | `problem_status`, `problem_edit`, `problem_remove`, `history_import` | 單／多題狀態、難度／品質 0..10、刪題、帶本地日期時間的歷史記錄 |
| 搜尋／向量 | `search`, `problem_vector_compute` | search 是本地 quick_search；向量強制重算使用 Tijian 設定的模型及 Desktop executor |
| 每日計畫 | `plan_add`, `plan_remove`, `plan_target`, `plan_show`, `plan_carry`, `plan_clear` | 計畫題目、目標、日期、未完工作順延；遵循 Tijian 的業務語義 |
| 日報 | `daily_show` | 可指定起訖日期的訓練統計 |
| 備份 | `backup_export`, `backup_import`, `backup_verify` | native／Markdown 匯出、備份匯入與校驗 |
| 題單／文章 | `article_import`, `article_list`, `article_show`, `article_export` | Markdown 題單匯入、清單、按 ID 讀取及 Markdown 檔案匯出 |

上游桌面還有翻譯／語義原題搜尋、遠端提交與提交同步、競賽匯入、筆記資料夾／附件、Obsidian 交換等能力，但上述 JSON CLI 未對外提供其操作入口。本適配器不宣稱這些 GUI-only 功能已接入。已有譯文／向量可透過 problem_get 讀取，並不等於能由此觸發翻譯或語義搜尋。

狀態沿用 `unseen/无`、`explored/胡`、`attempted/错`、`solved/过`。計畫目標為 `explore/胡`、`solve/写`。不把 explored 當成「看過題解」或獨立 AC；目前上游 CLI 沒有單獨的「看題解」欄位。做題歷史和每日計畫也不會建立、取消或修改 Runtime wake event。

## 配置與使用

先安裝 Tijian，使 `tijian` 可執行；或指定其絕對路徑。Paveway 不下載憑證、不修改 Tijian profile、不自動登入。上游的設定、認證、資料及 `TIJIAN_RUNTIME_MODE` 等環境仍由 Tijian 管理；不要把測試 harness profile 和正式資料混用。

```bash
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/oicoach-runtime tijian tools --json
```

0.13.0 提供先验收后启用的配置命令：

```bash
.venv/bin/oicoach-runtime tijian setup --executable /absolute/path/to/tijian \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime workflow connect-tijian test01 --config "$PWD/.runtime/config.toml"
```

setup 检查可执行文件、JSON CLI help 及三项实际数据读取；只有全部成功，才启用并原子保存 Runtime 配置。其余机器配置按原值保留，配置文件仍为 0600；失败不保存，配置若在检查期间变化则报 conflict。可指定 `--read-only` / `--no-read-only`，省略时保留原设置。该命令可能触发 Tijian 正常的数据库初始化，但不会添加测试题或业务记录。配置保存不会中断活动请求；在空闲时重启 daemon 才会更新其 envelope，下一轮 OMP 启动时加载 workspace 的 MCP 设置。`.mcp.json` 包含本机路径，应放在本地忽略范围，不上传机器配置。

在既有 Runtime TOML 也可手动加入以下設定；舊配置可省略整個表，預設停用：

```toml
[tijian]
enabled = true
executable = "/absolute/path/to/tijian"
timeout_seconds = 120
max_output_bytes = 8388608
read_only = false
```

`read_only=true` 禁止業務寫入與檔案匯出，MCP discovery 只列讀取工具，手動呼叫隱藏寫入工具也會拒絕（包含 dry_run）。它是適配層的操作限制，不是對上游 schema 初始化或檔案存取的 OS sandbox。

```bash
# help/protocol 核對，不呼叫模型；with-data 另讀題庫統計、當日計畫及題單清單。
.venv/bin/oicoach-runtime tijian doctor --config "$PWD/.runtime/config.toml" --with-data --json
.venv/bin/oicoach-runtime tijian call problem_get --arguments '{"problem":"P1001"}' \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime tijian call plan_show --arguments '{"date":"2026-10-18"}' \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime tijian call problem_status \
  --arguments '{"problems":["P1001"],"status":"explored","dry_run":true}' \
  --config "$PWD/.runtime/config.toml" --json
```

`--arguments-file PATH` 可代替內聯 JSON，以免 shell escaping 及敏感資料留在命令列。實際參數以 `tijian tools` 的 inputSchema 為準；拒絕未知欄位、錯誤型別、空清單、非有限評分及 flag-like token。批次最多 100 個項目；映射後 JSON 請求最多 64 KiB，數字 limit 最大 10000。字串不能以 `-` 開頭或包含控制字元；含 `-` 的相對檔名可改用絕對路徑。refetch/vector 必須選擇 `problems` 或 `all=true`，`oj` 只可搭配 all。

## OMP MCP 接入

`tijian mcp-config --config ABS_CONFIG` 輸出 `mcpServers.tijian` 設定，包含目前 Python 的絕對路徑及明確 Runtime config。將其合併到所需 workflow workspace 的 `.mcp.json`；不要覆寫既有 servers。OMP 18.10.4 的專案 MCP discovery 支援此檔案（已核對本機 OMP 原始碼並做模型呼叫測試）。`tijian mcp` 啟動 stdio server，stdout 只含 MCP protocol；無須先啟動 Runtime daemon。

亦可建立全新的整合驗證目錄，腳本拒絕既有目的路徑，不修改全域配置、不啟動模型／服務：

```bash
.venv/bin/python examples/prepare_tijian.py --directory "$PWD/.runtime-tijian" \
  --tijian-executable /absolute/path/to/tijian
.venv/bin/oicoach-runtime tijian doctor --config "$PWD/.runtime-tijian/config.toml" --with-data --json

# 終端 A
.venv/bin/oicoach-runtime serve --config "$PWD/.runtime-tijian/config.toml"

# 終端 B：使用既有 OMP 模型認證，讀已收錄的 P1001；可用 payload-file 更換題號。
.venv/bin/oicoach-runtime schedule --config "$PWD/.runtime-tijian/config.toml" \
  --workflow tijian --task integration --at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --reason 'Tijian read integration' --dedupe-key tijian-read-1
.venv/bin/oicoach-runtime events list --config "$PWD/.runtime-tijian/config.toml" --json
.venv/bin/oicoach-runtime status --config "$PWD/.runtime-tijian/config.toml" --json
```

此範例預設 read_only，僅為接口驗證模板，沒有正式GPT策略；查無題目時不自動補抓。模型回合完成不代表 Tijian 查詢成功，應核對工具結果。啟用 Tijian 時，Runtime envelope 會附 CLI prefix/config 及 `availability=configured_not_verified`，Agent 可由 `tools/doctor/call` 使用同一接口；沒有依賴 PATH 恰好包含 venv。僅配置並不表示服務或題庫已可用。

## 結果、故障與恢復

結果保留原始 `message/changed/dryRun/data` 及額外欄位。`changed=true,dryRun=true` 可能只是預計變更；匯出檔案時上游也可能回 `changed=false`，因此不能拿 changed 判斷所有副作用。批次上游可部分成功，需核對 message/data 及實際題庫。

同一 MCP server 內的呼叫串行；多個 bridge/CLI 進程仍由 Tijian 自己的 canonical mutation path 處理。所有請求使用 argv 陣列及 JSON stdin，沒有 shell 拼接。獨立排空 stderr，不回傳或保存原始診斷；stdout 有大小上限，進程及自有程序組於逾時／取消時清理。沿用 Linux parent-death guardian，bridge 本身被 SIGKILL 時也會停止自有 Tijian 進程與同組後代；不接收或沿用不存在於 bridge 的 Runtime 鎖描述符。成功的資料內容本身可能包含題面、歷史及使用者資料，會交給呼叫者／模型，並非內容匿名化服務。

`problem_remove`、`plan_clear`、`backup_import/export`、`article_export` 必須 `confirm=true`，或用 `dry_run=true` 預演；只有上游真的需要 `--yes` 的刪題／清計畫才加入此旗標。

找不到執行檔、停用、唯讀或輸入錯誤為 `not_started`。寫入進程已啟動後發生非零退出、逾時、無效結果、輸出超限或傳輸錯誤，保守回 `outcome=uncertain`；讀取／dry-run 失敗為 `no_business_mutation`。沒有自動重試或事務回滾承諾；取消 MCP call 後也要核對可能已完成的寫入。先用 get/list/plan_show/daily_show 檢查實際資料，再決定是否重新操作。這與 Runtime event 的 uncertain 狀態是不同層次：模型成功收到工具錯誤時，Runtime run 仍可能正常完成。

## 測試與驗證邊界

```bash
.venv/bin/python -m pip install '.[tijian]'
.venv/bin/python -m unittest discover -s tests -p test_tijian.py -v

# 會呼叫一次真實 OMP 模型，但 Tijian 後端是 fake fixture；只修改暫存 workspace。
OICOACH_TEST_TIJIAN_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_tijian.py -v

# 使用真正 Tijian 和現有本地題庫，只核對協議及讀取，不做業務寫入。
OICOACH_TEST_REAL_TIJIAN=/absolute/path/to/tijian \
  .venv/bin/python -m unittest discover -s tests -p test_tijian.py -v
```

已驗證所有操作映射、狀態 roundtrip、metadata 保留、dry-run、參數防注入、讀取限制、錯誤去敏、逾時／取消／父程序死亡清理、無重送、官方 SDK stdio discovery/call，以及真實 OMP → MCP → fixture CLI 的模型路徑。CI 安裝 extra，因此 SDK 測試會執行；沒有模型或 Tijian 的 opt-in 測試明確 skip。

本機原先沒有 Rust；本輪將 Rust 1.99.1 安裝於 `/tmp/paveway-rust-toolchain`，未改 PATH／shell profile。Tijian 原始碼、建置快取與隔離資料也在 `/tmp`。Linux 缺少 GTK／WebKit 開發依賴，未建完整 Desktop。改用 `examples/build_tijian_source_cli.py` 從固定 revision 直接擷取**未修改的 run_cli 實作**，包含真正的 cli_control 模組，連結原始 tijian-core/command/usecase/control；不重寫命令、不使用 Python fixture 代替業務層。此產物是驗證入口，不是上游正式發行版或 Desktop executor。上游 checkout 沒有改動。

對該原碼 CLI 在全新 harness profile 實測：真實 SQLite 的狀態／評分／歷史、計畫增刪及順延、日期日報、本地搜尋、題單匯入／讀取／檔案匯出、ZIP 備份／校驗／刪題後還原；dry-run 沒有持久化狀態。種子題目透過上游 UseCases::insert_problem 建立，沒有直接改 DB。官方 SDK MCP 可對真正 CLI 寫狀態並讀回題面；真實 OMP 模型亦已透過 MCP 讀到同一原碼題庫，並核對歷史中實際 toolResult。模型測試停用 OMP 內建工具／extensions，等待 MCP 工具出現後才派發，不以模型回覆 DONE 當成工具成功。

原碼驗證可重現：需可用 Rust/cargo，clone 以你有權限的 HTTPS 或 SSH 地址進行。以下目的路徑必須是新的：

```bash
git clone git@github.com:zjpanhongxuan/Tijian.git /tmp/paveway-tijian-reference
git -C /tmp/paveway-tijian-reference checkout b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f
CARGO_TARGET_DIR=/tmp/paveway-tijian-build .venv/bin/python examples/build_tijian_source_cli.py \
  --source /tmp/paveway-tijian-reference --output /tmp/paveway-tijian-source-cli

OICOACH_TEST_TIJIAN_SOURCE_CLI=/tmp/paveway-tijian-build/debug/tijian-source-cli \
OICOACH_TEST_TIJIAN_SOURCE_SEED=/tmp/paveway-tijian-build/debug/tijian-source-seed \
OICOACH_TEST_TIJIAN_OMP_CONFIG="$PWD/.runtime/config.toml" \
  .venv/bin/python -m unittest discover -s tests -p test_tijian_source.py -v
```

省略最後一個 OMP 環境變數時跳過真實模型測試，其餘原碼測試不需要模型憑證。seeder 強制要求 harness mode；測試建立全新的 profile 並清理，不碰使用者題庫。建置工具只建立臨時 Rust project，記錄 provenance；沒有修改或安裝上游程式。若用自訂 cargo 安裝位置，額外傳 --cargo ABS_CARGO 並提供對應 CARGO_HOME/RUSTUP_HOME。

上游非桌面測試：command/control/core 通過；usecase 首次平行執行有一項 `successful_upsert_returns_semantic_index_change` 失敗（ok=0，預期 1），未修改上游，該測試單獨重跑與整個 usecase 串行重跑均通過。不可將首次失敗刪去或宣稱預設平行全套無條件通過。仍未驗證完整 Desktop UI、運行中 Desktop 的 projection routing、需 Desktop 的向量重算、真實外部 OJ／模型供應商；網路受限和缺 Desktop 的路徑只驗證會報錯／逐項失敗，不冒充成功抓取。


## 0.13.0 本机接通与持久构建

本次从上游实际拉取并核对 HEAD 仍为 b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f。Linux 使用 `examples/build_tijian_source_cli.py` 构建未修改的上游 run_cli/cli_control 入口，连接真实 core/command/usecase/control，不是 Python 模拟题库；没有链接 Desktop UI，也不是上游正式发行包。正式 Desktop 执行器专属能力仍受前述限制。

本机产物放在项目 `.runtime/tijian-build/debug/tijian-source-cli`，源码构建工程放在 `.runtime/tijian-cli-source`，不依赖会被清理的 `/tmp`。有 Rust/cargo 的环境可使用以下持久输出（SOURCE 是上述固定版本的 Tijian checkout，输出工程目录需尚未存在）：

```bash
CARGO_TARGET_DIR="$PWD/.runtime/tijian-build" \
  .venv/bin/python examples/build_tijian_source_cli.py \
  --source /absolute/Tijian --output "$PWD/.runtime/tijian-cli-source"
.venv/bin/oicoach-runtime tijian setup \
  --executable "$PWD/.runtime/tijian-build/debug/tijian-source-cli" \
  --config "$PWD/.runtime/config.toml" --json
.venv/bin/oicoach-runtime workflow connect-tijian test01 --config "$PWD/.runtime/config.toml"
```

新安装首次 setup 会先执行上游本地 search，以 canonical 路径初始化数据库/schema，再验证纯读取；这是因为上游 stats/plan/article 直接以只读方式打开，数据库不存在时会失败。不会执行 seeder，不添加问题、计划或训练记录。普通 doctor --with-data 仍只作读取验证，不自动初始化数据库。

本机 test01 已启用 CLI 与 workspace MCP，重启空闲 daemon 后生效。真实正常 profile 的 MCP 返回 26 项工具，题库统计 total=0；已有题库需另行导入或选择正确的数据环境。源数据测试的 P1001/P1002 只存在于临时 harness profile，不属于用户题库。默认启用 OMP 内置工具时，MCP 可挂载为 xd:// 设备，不一定作为顶层 dumpTools 项显示；已核对实际系统上下文包含 Tijian，并使用官方 MCP SDK 成功查询正式空库。


## 2026-10-21 离线题面核查

重新 fetch 上游后，origin/main 仍为 `b3ca3f92ef757e1eefa6f71d7f55e60abbefc01f`。已核对完整远端 Git tree 的 493 个文件，而不只检查 sparse checkout 的可见目录：没有随仓库提供的题库 DB、题面 ZIP/PDF 或 `网页示例/` 目录。唯一受版本控制的 HTML 是 UI 的 article_toc_sync 测试页，不是离线题库。

源码确实引用作者本机保存的题面，例如 `tijian-core/src/infra/fetchers/qoj.rs` 的 QOJ 11106 / Add One 3，以及 AtCoder 的 Sum of Average；这些 include_str! 位于可选 fixtures 测试。`tijian-core/Cargo.toml` 第 34–37 行明确说明第三方网页保存在故意 git-ignore 的根目录 `网页示例/`，干净 checkout 不带这些文件。上游 .gitignore 同时排除该目录、`*_files/`、普通 HTML 及 DB 文件。因此“源码引用离线样本”不能解释为“git clone 自带可查询题面库”。

Tijian 的已收录题面会持久化在用户数据目录的 tijian.db（problems.statement_md），译文也可缓存；Desktop import executor 另有单次执行过程的内存抓取去重缓存。这些数据不随源码拉取。当前 Paveway 所接本机题库，经既有 problem_stats 实测 total=0，problem_list(has_statement=true) 返回空；适配器没有漏读已存在的题面。要读取实际离线题目，需要已有题库/正式备份，或另行取得作者未提交的网页样本再解析；不能将测试 seeder 的占位题面当成真实 OJ 题库。本次未导入、抓取或修改用户题库。
