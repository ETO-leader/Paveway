# 多设备 Git 用户数据同步（0.11.1）

`/sync` 管理一个独立的用户数据 Git 仓库。daemon 首次启动时自动建立 `state_dir/user-data-git`，默认每 60 秒归档；同一 Runtime 的全部 workflow/task 与GPT对话共用这个仓库，配置仍按 workflow 区分。没有 remote 时只维护本地 Git，不执行 fetch/push。已有仓库、设备身份、remote 和暂停状态在重启后保留。daemon 运行时会定期保存本地提交，并在设有 remote 时自动 fetch、merge、push；UI 退出后继续工作。不会采用本项目源码仓库的 origin，也不会自动创建远端仓库。

## 同步范围

| 数据 | 行为 |
| --- | --- |
| UI 自定义系统提示词、User Skill、启停状态 | 跨设备共享，单一新版本自动应用；并发修改保留双方并提示选择 |
| 已结束 run 的用户输入、最终回复、状态与安全错误摘要 | 按设备归档，可搜索、查看；保留 failed/uncertain 含义 |
| 原生 OMP 会话中的 user/assistant 文字 | 在可验证且未运行的会话中导出；保留分支 id/parent_id，按设备查询 |
| toolResult、工具调用参数、thinking、图片/二进制 | 不进入同步文字档案 |
| API key、service.env、OMP 登录/profile 认证文件 | 不复制 |
| 活跃 SQLite、执行队列、lease、socket、进程状态 | 不同步、不导入、不在另一设备重播 |
| workflow 的模型选择与思考等级（0.16.0） | 随自定义设置同步；provider/凭证仍由各设备配置 |
| 本机 config.toml 的路径、默认模型/工具权限、systemd 配置 | 每设备独立管理，不用远端路径覆盖本机 |
| coach/workflow 模板代码、Tijian 题库、L2 记忆笔记 | 不属于此用户配置/历史同步，仍由各自存储与备份管理 |

此处“用户配置”指 [UI 自定义设置](customization.md)，不包括整份机器配置。文字历史可能保留用户原文中的文件路径或敏感文本；它们仅作为历史资料，不当作另一设备的可执行配置。同步不提供加密，建议使用你控制的私有仓库。删除当前提示词/Skill 不会清除 Git 历史或已有备份。

## 在 UI 设置

重开新版 UI，输入 `/sync`，用方向键选择“配置 / 移除 remote”，或直接输入 `/sync remote`。填写 SSH/HTTPS 地址后保存；清空字段（Ctrl+A、Ctrl+K）再按 Enter 可移除 remote，继续保留本地仓库及所有提交。修改 remote 不改变暂停开关，已暂停时用 `/sync resume` 恢复。保存后无需重启 daemon，约 5 秒内热加载；已开始的同步会先完成，再应用新设置。

默认不需要初始化向导。`/sync setup` 提供高级设置，依次填写：

1. 专用本地 Git 数据目录，默认 `state_dir/user-data-git`。daemon 已初始化后保持这个目录；若在 daemon 首次启动前手动设置其他位置，必须使用独立空目录，不能使用源码目录或手动 clone 的其他 Git 仓库。
2. SSH/HTTPS remote；留空表示只做本地 Git 保存。支持绝对路径的本地 bare 仓库用于本地测试。不要填写带 token、密码、查询参数的 URL；每设备使用自己的 SSH key/Git credential helper。
3. 分支，默认 `main`。
4. 间隔秒数，默认 60，允许 10–86400。

首次使用空目录，之后修改 remote 或间隔时保留同一个目录，不必清空。现有同步身份不切换本地仓库路径或分支；另一套仓库/分支应使用独立本地 runtime 配置。

Enter 下一项/保存，Ctrl+J 换行，Esc 取消。设置在运行中的 daemon 内热加载，约每 5 秒检查本地设置变化；首次配置、修改或恢复启用会尽快启动一次同步，之后按间隔执行。升级程序本身后，先在无活动回合时重启 daemon 一次。

可用操作：

```text
/sync                     方向键菜单
/sync remote              配置或移除 remote，保留仓库、身份及启停开关
/sync setup               高级设置：专用仓库、remote、分支与间隔
/sync status              查看启停、最近同步结果与冲突
/sync now                 请求 daemon 立即后台同步，完成后 UI 显示结果
/sync pause               暂停自动同步，不删除本地提交或历史
/sync resume              恢复自动同步
/sync conflicts           选择冲突版本，或查看尚未绑定的远端配置
/sync bind coach          为远端配置选择本机 workspace，建立本地 coach workflow
/sync history [QUERY]     搜索所有已拉取设备的历史
/sync messages DEVICE SESSION [QUERY]  查询某设备的文字原文（所有分支）
/sync next                当前同步历史/原文的下一页
/sync prev                上一页
```

`/sync now` 是立即返回的后台请求；不会把网络等待占在模型队列里。同步本身需要 daemon，首次部署可用前景 serve 或已安装的 systemd user 服务；配置 Git 不会暗中安装/启动服务。暂停不会中断已经开始的 Git 操作，操作锁忙时可等本轮结束后重试。

## 第二台设备

在第二台设备安装同版本程序，完成自己的 init、OMP 认证与 daemon 启动，然后用 `/sync remote` 填写相同 remote。每台设备自动创建自己的本地仓库；默认分支为 main，需自定义分支时在首次 daemon 启动前使用 CLI setup。

每个设备会生成自己的 device_id，不要将 `state_dir/git-sync/settings.json` 或整份活动 state 复制到另一台机器冒充同一设备。数据仓库由程序 init/fetch 管理，不需要手动 clone。设备自己的仓库路径、remote、device_id、开关和间隔保存在该私密 settings.json，不进入共享 Git 内容。

若该 workflow ID 已存在且自定义部分尚未修改，远端单一版本会自动应用，保存为本机自己的 revision；本机 workspace、权限与模型凭证保持独立。

若 workflow 不存在，`/sync conflicts` 会显示“待绑定”。输入 `/sync bind ID`，填写本机存在的 workspace 绝对路径，建立本地 coach 模板，下一次同步再应用远端 prompt/Skill。你也可先手动创建同 ID 的自定义 workflow。不会复制另一设备的绝对目录或安装它的可执行文件。

远端历史始终在 `/sync history` 查询，与本机 `/history` 的执行记录分开。不会把归档 run 变成本机事件，也不冒充 OMP 的跨设备 `--resume`；在另一设备训练时使用它自己的 task/session，需要时查阅归档文本。不同设备同名 task 的历史按 device_id 分开保留。

## 并发修改和冲突

共享配置以内容寻址的版本记录保存，每个版本带父版本。单一新版本可自动应用；两台设备分别从同一旧版本编辑时，会形成两个 head，自动同步保留双方，继续同步历史，同时让每台设备保留当前本机配置。

用 `/sync conflicts` 选择 workflow，再选择“保留本机”或某一设备版本，输入 `yes` 确认。解决结果是引用双方父版本的新记录，不删除旧版；下次自动同步或 `/sync now` 将它推送给其他设备。待绑定条目也在此菜单中，但不会擅自选择本机目录。

配置导入沿用自定义设置的文件锁/CAS。若远端应用恰逢 UI 编辑，保存会提示 conflict 并保留草稿，Esc 后重开、核对最新内容再保存。已开始的 OMP 回合保留其快照；导入的新配置从下一次派发起生效。

历史按设备目录保存，常规多设备写入不改同一 run 文件。Git push 遇到远端前进会有限次数 fetch/merge 后重试，不 force push、不 reset、不删除用户数据。真正的 Git 层冲突或意外文件会停止该轮并保留工作树，需检查数据后重试，不会自动丢弃任一侧。

## 离线和边界

本地配置/历史先产生 Git 提交，再尝试网络操作。网络/认证失败写入同步状态，保持本地提交供下轮重试，不改变模型请求结果、退避策略或执行队列。Git 以非交互模式运行，单命令有超时、整轮 Git 操作有时间预算；不会在 UI 等待 SSH 密码或询问自动重播任务。

原生会话只接受匹配绑定身份的自有 JSONL，最大 16 MiB，单行最大 1 MiB；活动、变化中、不完整、越界或不支持的档案不覆盖此前有效导出，run 的输入/最终回复仍可归档。文字查询每页 20 条，单条显示最多 4096 字符并标明截断；完整已导出文字仍在 Git 数据文件中。档案保留所有分支，不能当作单一当前执行路径或隐藏推理。

只接受专用仓库格式和受限的数据路径/普通文件，拉取后不会 checkout 或执行 .gitattributes、hook、submodule 或 symlink；Git hooks 在自动操作时关闭。没有复制任意目录或认证文件，也不从代码仓库的 origin 猜测数据发布位置。remote 若在仓库外被改动，需重新 review/setup 才继续。

## CLI

```bash
# daemon 自动创建默认本地仓库；手动添加已创建且可访问的数据远端。
oicoach-runtime sync remote --remote git@example.com:user/private-paveway-data.git \
  --config /absolute/config.toml

# 移除 remote，保留本地提交与自动归档。
oicoach-runtime sync remote --remote '' --config /absolute/config.toml

oicoach-runtime sync now --config /absolute/config.toml --json
oicoach-runtime sync status --config /absolute/config.toml --json
oicoach-runtime sync history --query '动态规划' --offset 0 --config /absolute/config.toml --json
oicoach-runtime sync show --device DEVICE_ID --run RUN_ID --config /absolute/config.toml --json
oicoach-runtime sync messages --device DEVICE_ID --session BINDING_ID --offset 0 --config /absolute/config.toml --json
oicoach-runtime sync conflicts --config /absolute/config.toml --json
oicoach-runtime sync bind --workflow coach --workspace /absolute/workspace --config /absolute/config.toml
oicoach-runtime sync resolve --workflow coach --keep-local --config /absolute/config.toml
# 或 resolve --workflow coach --head HEAD_SHA256，选择 conflicts 返回的当前版本。
oicoach-runtime sync pause --config /absolute/config.toml
oicoach-runtime sync resume --config /absolute/config.toml
```

无需 daemon 的维护命令是 `sync run`：直接执行一轮，失败返回非零；与后台同步互斥。日常 UI/CLI 用 `sync now` 请求 daemon，避免阻塞调用端。没有凭证时 Git 会明确失败，不自动选择其他 remote。

## 仓库格式

```text
format.json
profiles/WORKFLOW/SHA256.json               prompt / User Skill 版本图
devices/DEVICE_ID/runs/RUN_ID.json           已结束回合的可查询摘要与回复
devices/DEVICE_ID/sessions/BINDING_ID.json   经过过滤的 user/assistant 文字原文
```

Git 数据格式为独立 v1，设置 JSON 也独立于 Runtime 的 config/DB schema。程序不将这些文件恢复进活动 SQLite；本地同步状态、基线和互斥锁在 `state_dir/git-sync`。仓库与本地状态目录 0700，程序写入的文件 0600；备份可保留数据仓库，跨设备新安装应重新生成本地同步身份。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p test_git_sync.py -v
.venv/bin/python -m unittest discover -s tests -p test_tui.py -v
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_runtime.RuntimeTests.test_automatic_two_device_git_sync_without_ui_or_queue_import \
  test_runtime.RuntimeTests.test_git_sync_failure_does_not_block_foreground_training -v
```

测试以两个独立设备目录、两个 daemon 和实际本地 bare remote 验证双向同步、原生文字过滤、离线提交与重试、配置冲突/解决、新设备绑定、队列隔离和异常远端拒绝。PTY 实际填写 remote、提交训练请求、后台 push、查看同步历史及暂停。SSH/HTTPS 的实际访问需要用户指定远端与自己的认证，不把本地 bare 验收称为已验证某个互联网仓库。

0.12.0 的三类 Skill 分类与正文一起同步；旧记录省略 category 时按 personal skill 解释，读取不改写记录和历史哈希。使用分类的设备应统一升级到 0.12.0 或更新版本，旧程序会拒绝无法识别的额外字段并保留本地数据。
