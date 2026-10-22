# CLI 與本地 API 契約 v1

## M4 可用命令

`ui [--workflow ID] [--task ID] [--config PATH]` 在 0.9.0 改为对话记录＋常驻输入框、slash 命令与方向键选择。Enter 发送、Ctrl+J 换行，数字/q 不再是导航键。它不接受 --json，不增加 daemon API 或改动 DB 版本，业务仍经 CLI 子进程调用，见 [终端 UI](terminal-ui.md)。

0.7.0 新增 `memory put/show/list/delete/recall` 与 `history search/show/messages`，均经 daemon 控制 socket，沿用 API v1。完整参数、scope/CAS/有效期、分页/截断及错误语义见 [记忆与历史接口](memory.md#ipc-契约)。数据库独立升至 v2，不改变请求 envelope。

```text
oicoach-runtime --version
oicoach-runtime init [--config PATH] [--state-dir ABS_PATH] [--workflow-root ABS_PATH] [--json]
oicoach-runtime doctor [--config PATH] [--json]
oicoach-runtime verify-omp [--config PATH] [--with-model] [--json]
oicoach-runtime serve [--config PATH]
oicoach-runtime status [--config PATH] [--json]
oicoach-runtime schedule --workflow ID --task ID --at RFC3339 --reason TEXT --dedupe-key KEY [--payload-file PATH]
oicoach-runtime schedule --workflow ID --task ID --after-turn SECONDS --run-id RUN_ID --reason TEXT --dedupe-key KEY
oicoach-runtime events list [--status STATUS] [--offset N]
oicoach-runtime events upcoming --workflow ID [--task ID]
oicoach-runtime events show EVENT_ID
oicoach-runtime events cancel EVENT_ID --expected-version N
oicoach-runtime events reschedule EVENT_ID --expected-version N --at RFC3339
oicoach-runtime runs list [--task ID] [--offset N]
oicoach-runtime runs show RUN_ID
oicoach-runtime recover list [--offset N]
oicoach-runtime recover retry EVENT_ID [--confirm-duplicate-risk]
oicoach-runtime agent restart --task ID [--workflow ID] [--reason TEXT] [--reset-circuit]
oicoach-runtime agent show RESTART_ID
```

所有葉命令接受 `--config/--json`，放在最後一層子命令後，例如 `events list --config PATH --json`。`doctor` 可選文字或 JSON，其他控制命令始終輸出 JSON；`serve` 輸出 JSONL 狀態，不是一問一答命令。`init/doctor` 不要求 daemon；其他查詢和寫入必須經同一配置的 daemon。

`verify-omp` 也不依賴既有 daemon，輸出頂層 `api_version/milestone/ok/checks`，退出碼 0 表示選定範圍通過、1 表示失敗。預設僅 transport 檢查，model_roundtrip 明確 SKIP；`--with-model` 發送三個小型模型回合，檢查同 task 續聊與另一 task 隔離。使用暫存私密 workspace、runtime、queue、session 及自有 daemon，最後清理；沿用已選 executable/model/profile 和認證，不修改原配置或既有會話。

`[agent]` 新增可選 `model = ""` 和 `tools_enabled/extensions_enabled/skills_enabled/rules_enabled`（預設皆 true）；轉換成已知 argv，禁止原始 shell flags。verify-omp 始終將四種能力設為 false，模型 selector 可在 TOML 指定。舊配置可省略新欄位，schema_version 仍為 1。

0.16.0 新增本地 CLI `workflow models ID`，查詢所選 OMP executable/profile 的模型目錄與當前 workflow 的 inference 設定，不呼叫模型。`workflow customize inference ID --expected-revision N [--model SELECTOR] [--thinking LEVEL]` 至少提供一項，以原子寫入/CAS 保存；空串恢復繼承。show 的頂層 inference 回傳套用本機預設後的 selector/level，settings.inference 為可選的原始 workflow 覆蓋；缺欄位的舊設定仍按原文讀取。`[agent].thinking` 可選，預設空串，支援 off/minimal/low/medium/high/xhigh/max/auto；非空 workflow 覆蓋優先，下一次 spawn 轉為 --model/--thinking，執行中的回合不變。這些入口不新增 daemon mutation API 或資料庫遷移，UI 入口為 /select-model 和 /select-thinking-hardness，詳見 [自訂設定](customization.md)。

0.17.0 新增本地 `proxy sync/status`（支援 --config/--json），sync 合併目前終端的代理變數到私密服務環境檔，status 只讀取與檢查。報告只含 mode、變數名、代理 TCP 可達性、NO_PROXY=* 標記及生效範圍，不含網址或憑證；不新增 daemon API 或 Git 同步欄位。UI 啟動及 /proxy 執行 sync，serve 與服務啟動亦同步檢查，每次新建 OMP/查模型目錄讀取最新保存值。缺少的變數保留保存值，顯式空串清除此項，已運行 OMP 不變。詳見 [部署代理說明](deployment.md)。

配置優先順序為 `--config`、`OICOACH_CONFIG`、XDG 預設。Runtime 向自有子進程注入 `OICOACH_CONFIG`、`OICOACH_RUN_ID`；後者可作 `--after-turn` 的 run ID 預設，daemon 仍驗證 scope 和 running 狀態。TOML 路徑必須絕對，不做 shell 展開。

退出碼：0 表示命令成功或診斷無 FAIL（可有 WARN）；1 表示操作或診斷失敗；2 表示 argparse 用法錯誤。操作失敗的 `--json` 錯誤寫 stderr；診斷成功執行但檢查 FAIL 的報告仍寫 stdout。用法錯誤目前是 argparse 文字，不宣稱支援 JSON。

成功 JSON 包含 `api_version: 1`。錯誤格式：

```json
{"api_version":1,"error":{"code":"invalid_config","message":"Only config schema_version = 1 is supported"}}
```

錯誤碼包含 `invalid_input/invalid_config/storage_error/invalid_workflow/io_error/conflict/not_found/daemon_unavailable/internal_error`。控制命令成功輸出 `{"api_version":1,"result":...}`；`init/doctor` 維持既有報告結構。`doctor` 回傳 `milestone/ok/checks`，每項檢查為 `PASS/WARN/FAIL`。

## 排程與恢復語義

`at` 和 `after_turn` 二選一；RFC3339 必須有已知時區 offset，入庫統一 UTC。payload 為 JSON object、最大 64 KiB，禁止 NaN/Infinity；reason 1–4096 字元，dedupe key 1–256 字元。去重 scope 為 workflow/task，完全相同請求返回原事件，衝突回 conflict。

`cancel/reschedule` 必須提供正整數 expected_version，僅接受 queued/waiting_anchor；reschedule 明確轉成 at 模式。`after_turn` 綁定同 scope 的 running run，在成功 settled 後起算，失敗則 blocked。

events/runs list 每頁最多 100 筆、可用 offset（0–1000000）翻頁；events list 不含 payload，show 才含詳細資料。recover list 每種狀態最多 100 筆，offset 分別套用於各狀態。頁面以建立時間及 ID 排序；並行寫入時不是快照，精確查詢請使用 ID。

0.14.0 新增只讀 `events.upcoming`（CLI `events upcoming`），必填 workflow_id、可選 task_id。返回 queued/waiting_anchor/leased/running 的最多 100 筆摘要，按觸發 UTC、建立時間、ID 排序，未錨定事件排後；包含 event_id/workflow_id/task_id/status/not_before_utc/schedule_mode/relative_seconds/anchor_run_id/created_at/reason/run_id，不含 payload。UI 使用它恢復倒數，不修改排程、資料庫版本或既有 list 語義。

recover list 包含 failed/blocked/uncertain/dead_letter。retry 保留舊 run，只恢復有到期時間且 attempt 尚未達 max_attempts 的事件；uncertain 事件或會話必須顯式 `--confirm-duplicate-risk`。沒有成功 anchor 的相對事件不能偷改成立即執行，請人工核對後另建 at 事件。

status 包含 daemon、agent_health、全部狀態計數、active_runs、最近錯誤碼、上次成功時間、supervisor 與 last_restart。supervisor 提供 closed/backoff/open/half_open、連續／時間窗故障數、下次可啟動時間與冷卻期限。service-manager 部署另透過 service 入口管理。

0.6.0 新增以下入口，服務管理細節見 [部署文件](deployment.md)：

```text
workflow create ID --workspace ABS [--template coach|example]
workflow list | show ID | connect-tijian ID
ask --workflow ID --task ID (--message TEXT | --message-file PATH)
    [--mode plan|checkin|review] [--dedupe-key KEY] [--wait] [--timeout SECONDS]
events wait EVENT_ID [--timeout SECONDS]
runs output RUN_ID
service render|install [--working-directory ABS] [--name UNIT]
service install [--replace] [--capture-env NAME ...]
service status|logs|start|stop|restart|enable|disable|uninstall [--name UNIT]
```

皆接受 --config/--json。render 普通輸出為 unit 文字，runs output／ask --wait 普通輸出為回覆；其他成功結果為 JSON。enable/disable 可加 --now，logs 可 --lines 1..1000；詳見 help。安裝不啟用，控制只操作相同 config marker 的私有托管 unit。

`ask` 等價於 payload.mode/message 的 immediate schedule。`at="now"` 由 Store 時鐘轉為 UTC，fingerprint 使用穩定的 now 語義；同 key 相同 immediate 請求幂等，不同輸入 conflict。reschedule 亦可使用 now。等待預算必須有限且大於 0、不超過 86400 秒；逾時回 wait_timeout，不取消事件。ask --wait --json 回 result.event/output，終態非 succeeded 的退出碼為 1。

`runs.output` 是新增 IPC command，arguments={run_id}；回 status=available|pending|unavailable、text、truncated。available text 最多 16 KiB UTF-8，僅保存成功 run 本輪最終 assistant 回覆。不支持的可選文字查詢不假裝有回覆；session/transport 錯誤不被 presentation fallback 吞掉。舊 run 無輸出 metadata 時回 unavailable。輸出與成功提交同交易，不新增資料表或刪除舊 schema v1 記錄。

agent restart 是非同步控制，先持久回覆 requested job 與 restart_id；agent show 或 status 才表示其 running/succeeded/failed/interrupted 結果。相同 task 的 active run 會中斷，其他 task 的 run 不被中斷，restart 等其完成後串行執行。同時只接受一個 pending restart；task ID 跨 workflow 重複時必須提供 --workflow。

restart 以已綁定 session 執行新進程的 ready/get_state 檢查後回收，不發送 prompt；prompt_replayed 恆為 false。中斷的 active prompt 保留 uncertain，不因 restart 成功而自動恢復。--reset-circuit 只有在新進程檢查且正常退出後清除 supervisor 計數／冷卻，不重設事件 attempt，不會重播 failed/uncertain/dead_letter。daemon 中斷的控制工作標為 interrupted，不自動重做。

## Tijian 進程接口

0.5.0 增加獨立進程接口 `tijian tools`、`tijian doctor [--with-data]`、`tijian call NAME [--arguments JSON | --arguments-file PATH]`、`tijian mcp-config`、`tijian mcp`，皆接受 --config；不經 Runtime socket，不需要 daemon 已啟動。一般命令可 --json，成功 stdout／錯誤 stderr；mcp 的 stdout 僅供協議。tools 輸出完整 inputSchema，call 回 api_version/result，錯誤額外包含 outcome。mcp-config 直接回 mcpServers 設定物件。詳見 [Tijian 契約](tijian.md)。

## IPC v1

Unix domain socket，控制資料夾 0700、socket 0600，透過 SO_PEERCRED 檢查同 UID。每個連線一個 JSONL 請求／回應、各限 128 KiB，讀取 10 秒、寫入 5 秒超時，同時最多處理 64 個 client。daemon 不接受從 payload 指定執行檔、OMP flags 或模板路徑。

```json
{"api_version":1,"id":"client-request-id","command":"schedule","arguments":{}}
```

回應保留 `api_version/id`，成功使用 `result`，失敗使用 `error.code/message`。拒絕未知版本、未知欄位及不符參數。無法解析的 frame 可能回 `id:null`。ID 使用 ASCII `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`。

IPC command 為 `status/schedule/events.list/events.upcoming/events.show/events.cancel/events.reschedule/runs.list/runs.show/runs.output/recover.list/recover.retry/agent.restart/agent.show`；arguments 使用 snake_case。agent.restart 接受 task_id、可選 workflow_id/reason/reset_circuit；agent.show 接受 restart_id。ask/wait 是 CLI 的組合操作，不新增另一個 mutation path。CLI 保留 server 的 conflict 等結構化錯誤碼，不把業務錯誤重新包成 daemon_unavailable。

模型回合失敗會在既有 event.last_error_code/last_error_message 與 run.error_code/error_message 保存安全分類摘要（例如 agent_auth_failed／HTTP 401），不儲存供應商原始請求、金鑰或錯誤回應。認證、權限、模型不存在、限流、超時、5xx 及中止有各自分類，其他保留 agent_turn_failed。原有 schema/API 版本不變，仍不自動重播已派送請求。

0.10.0 新增本地公开 CLI `workflow customize show/prompt/skill-put/skill-enable/skill-disable/skill-delete ID`，写入要求当前 `--expected-revision`，正文用 `--content-file`，由 UI `/prompt`、`/skills` 调用。不增加 daemon IPC 命令或更改数据库版本；详见 [自定义接口与生效规则](customization.md)。

0.11.0 新增 `sync setup/remote/status/now/run/pause/resume/conflicts/resolve/bind/history/show/messages`，管理独立用户数据仓库。仅 `sync now` 使用新增 daemon IPC `sync.request`（无参数，返回 requested/previous_completion），唤醒独立后台同步器；其他命令读写本地专用同步数据。无需更改现有 config/DB schema；完整参数和分支/冲突语义见 [Git 同步](git-sync.md)。

0.11.1 的 daemon 默认初始化一个 Runtime 共用的本地数据 Git 仓库；缺少 remote 时不会联网。`sync remote --remote URL` 设置远端，`--remote ''` 移除远端，均保留设备身份、提交、分支、间隔与 enabled 状态。已有设置不会被自动初始化覆盖。

0.12.0 的 `workflow customize skill-put ID --category oi-introduce|user-information|personal-skill` 指定用户 Skill 分类；可省略，新建默认 personal-skill，编辑保留原分类。旧记录无 category 字段仍有效。分类通过既有 CAS 保存与 Git 同步，下次派发时所有启用类别的正文同时加入系统上下文。

0.13.0 新增 `tijian setup --executable PATH [--read-only|--no-read-only]`，验证协议及数据读取后原子更新私密 Runtime 配置；诊断失败不保存，配置并发变化报 conflict。不重启 daemon、不触发业务写入；MCP 绑定仍通过 `workflow connect-tijian ID`。


## 会话阻挡诊断（0.19.1）

派发前的会话绑定检查分别返回 `session_uncertain`、`session_blocked`、`session_workflow_changed`、`session_file_missing`，继承原有 ConflictError 类。事件和 run 的既有 error_message 字段保存固定、无路径或正文的操作指引；不将任意异常原文写入公共结果。dedupe/CAS 等其他冲突仍为 conflict，无 schema 迁移。

`session_uncertain` 表示该 task 有结果未确认的旧会话，不是新请求发生模型或 Tijian 错误。新请求在 dispatch_intent 之前 blocked，不自动派发或重播；普通重启 daemon/agent 不会清除该状态。UI 输入 `/new`（可附新 task ID）可在保留原会话、原 uncertain 事件和历史的情况下继续新对话。要恢复旧事件，应先 `/recover` 核对外部副作用，再由用户明确选择带重复风险确认的恢复操作。不得自动设置旧会话为 ready。


活动事件的 `events.show` 可附可选 `progress` 对象：run_id、phase（starting/tool/model）、tool_calls、started_at、last_activity_at，以及已分类的 tool 名称。仅在当前进程管理的活动 run 上返回；不写入数据库、历史或 Git 归档，终态/重启后不保留。工具名只保留 read/write/bash/todo/glob/grep，其余归为 external-tool，不含参数、结果或原始动态工具名。缺省该字段时客户端保持兼容。
