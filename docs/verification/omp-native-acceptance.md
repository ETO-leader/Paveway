# OMP 原生整合验收：0.21.0

日期：2026-10-30。任务基线由 `38d5136` fast-forward 至 `b6013df4827c4ff54651619ed8d869fd5b379cf1`，分支 main；开始时工作区干净。实际环境：WSL2 Linux、Python 3.12.3、Bun 1.4.3、OMP **18.10.4**、Chrome 142.0.7444.175（`/opt/google/chrome/chrome`）。没有改动 OMP 或 Tijian 上游，也没有部署或重启正式用户服务。

## 范围与决策

用户明确回复「取消 10k limit」，取代实施契约中要求固定 10k Guard 的 D4/C3 门槛。对话与压缩采用 OMP 原生策略，记忆取消 token 限制但保留 64KiB IPC 字节边界。旧 Guard/探针仅作为历史资料，不加载到默认前后台路径；不能把过去的辅助推理旁路调查写成“已实现全模型请求守卫”。

| 决策 | 本次采用与验证边界 |
| --- | --- |
| D1/D2 | 独立物理前后台 Session。真实前台在线时后台 RPC 成功；拒绝第二 owner，锁随受管进程继承；不提供共享文件接管。 |
| D3 | 前台只有生命周期观测，agent_end 不代表严格 settled；前台 after_turn_settled 关闭，旧后台 after-turn 契约保留。 |
| D4 | 按用户新指令移除固定预算要求；仍通过 trusted-extension、nonce、入口摘要、真实子 PID 和 Session 路径登记控制启动输入准入。摘要只覆盖入口文件，不声称整个依赖图被远程证明。 |
| D5 | 采用实际支持的纯文本 SYSTEM 兼容分支，原生工具／Skill／Rule 动态展开；前后台同一 Composer。保留 append/replace 原文，不叠加互斥参数。 |
| D6 | 持久 Inbox，created/offered/明确 acknowledged 分离；没有自动 sendMessage、交付推断或重播。成功但无消息的 agent_wake 可以沉默；明确 reminder 始终先持久化。 |
| D7 | 登记原生来源、只读分支文字索引和终态用户 Git 投影；实际长文本、原生 /new、顺序恢复、独立 Git 和第二设备读取均验证。归档有既有 16MiB 文件／1MiB 行界限，超限保留来源并拒绝，不宣称完整同步。 |

## 分阶段交付

| 阶段 | 实现提交 | 交付 |
| --- | --- | --- |
| M0 | `08c266a`、`8d3ae6b` | 记录实际辅助推理旁路、模板兼容与用户取消预算后的契约变更；历史反例仍保留。 |
| M1 | `8ff2b92` | 受管原生 TUI、私密启动信息、输入准入与独立 Session。 |
| M2 | `4fdfe87` | owner generation／锁／PID 身份、显式恢复、分页只读原生归档。 |
| M3 | `127cbff` | 原生唤醒 AgentTool／Slash、Runtime 时钟与幂等、持久 Inbox。 |
| M4 | `7e24d41` | 统一 Prompt 快照、Persona／Profile 隔离、新对话激活、状态 CAS。 |
| M5 | `4abdf71` | 原生管理菜单、私密编辑与遮罩登录、受控新旧 UI 往返、Memory／历史工具、原生 MCP 配置路径修正。 |
| M6 | 本发布提交，基于 `4abdf71` | 原生能力与故障验收、完整回归、默认入口切换与回退、0.21.0 打包、完整 README。 |

没有数据库 schema migration：继续使用 schema2 和现有版本化 runtime_metadata 控制工作记录；旧 queue/run 的完成语义不变。Persona／coach 为私密设置可选字段，首次写入前保存旧配置备份。旧渲染器只由 legacy 路径进入，保留用于回退，不参与原生聊天渲染。

## 实测结果与复现

所有新原生测试使用**实际 OMP、PTY、扩展、工具、进程与本地 IPC**，模型响应来自隔离 HTTP fixture；HOME/XDG/认证配置隔离，没有付费模型请求或用户私人凭据。MCP 使用本地 stdio fixture；Browser 使用新建私密 Profile 访问 loopback 页面，不接管用户浏览器。

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests
PAVEWAY_TEST_NATIVE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_native_omp test_native_management test_native_capabilities test_native_recovery -v
```

| 检查 | 结果与日志 |
| --- | --- |
| 最终完整基础回归 | **303 项：270 通过、33 条件跳过，97.610 秒**；`/tmp/paveway-final-regression.log`。跳过项包含原生 opt-in、真实模型或本机依赖条件；不计为已验收。 |
| 原生端到端统一验收 | **18 项全部通过，65.800 秒**；`/tmp/paveway-native-acceptance-final.log`。聊天／长文本、在线后台并行隔离、/new 与 resume、Persona、真实 AgentTool 排程、UI 退出后提醒、管理编辑、登录遮罩、Profile 切换、旧 UI 往返、Browser、Subagent、MCP、Memory 与故障恢复。 |
| 最终默认入口与第二设备归档 | **2 项通过，7.161 秒**；`/tmp/paveway-native-rollout.log`。从另一工作目录启动默认一键入口、退出保留 daemon；真实原生会话导出后经本地 bare remote 在第二隔离设备发现并读取。 |
| 提醒／静默语义与旧 setup 更新 | **19 项通过，0.639 秒**；`/tmp/paveway-final-fixes.log`；独立 Inbox 4 项通过，`/tmp/paveway-inbox-quiet.log`。不将空白自主唤醒强制包装成已通知，明确提醒仍留存。 |
| 最终 Skill 菜单回归 | **1 项通过，2.880 秒**；`/tmp/paveway-native-skill-final.log`。编辑现有 Skill 默认保留原分类，正文不成为聊天。 |
| 最终 transport 诊断回归 | **11 项：10 通过、1 opt-in 跳过，2.358 秒**；`/tmp/paveway-smoke-final.log`。清理已取消 Guard 的旧错误文案，不再误报仍需预算握手。 |
| 构建 | `pip wheel . --no-deps` 生成 `oicoach_runtime-0.21.0-py3-none-any.whl`；`/tmp/paveway-package-build.log`。校验包含四个原生 mjs 模块和内置 Tijian Skill，不含运行数据。 |

故障测试证明：daemon 重启后活前台保留 uncertain，显式 recover 与第二 owner 均拒绝；父进程在模型回合中被 SIGKILL 后，受管 guardian/OMP 退出，重启 daemon 后必须显式恢复，普通 resume 不新增模型请求、不自动恢复原 prompt。已有持久 Session 和原文保留。全新 Session 在首个助手回复前可能尚未落盘，不能保证强杀时保留未落盘输入；缺文件恢复给出明确拒绝并允许 --new。

## 发现的失败及修正

- 本机 OMP 的 `ui.input({secret:true})` 忽略遮罩选项。改用宿主 masked Input，并在接受输入前用固定假值检验；真实 PTY／隔离文件扫描不包含测试密码，取消不保存。
- OMP 将子 Agent 的 session/agent 事件送入同一扩展宿主。主登记和受管工具现在只接受 `ctx.agent.kind=main`；真实 task 工具可完成，不再误杀主 TUI。
- 原管理 UI 往返最初导入了不存在的 run_ui，已改为实际入口；测试的恢复等待条件也须包含新 owner 已 idle，不能仅凭增加 generation 就发输入。
- 真实 Browser 冷启动在 10 秒边界偶发超时，曾导致统一测试失败，未把失败计为通过。隔离 Chrome 加禁用扩展／后台网络、DOM-ready 导航，Browser open 明确 30 秒边界、eval 40 秒、测试总等待 50 秒；最终统一验收通过，独立复验 4.681 秒通过。仍有超时诊断，不重试真实操作。
- 旧根 `.mcp.json` 不是本机原生发现位置。显式配置改为 `.omp/mcp.json` 并保留旧文件；真实 MCP 工具经 OMP `xd://` 设备调用，不能要求它一定出现在 provider function 列表。旧 setup 测试的三个根路径假设相应更新，原冲突／symlink 断言保留。
- 首次不隔离 wheel 构建缺少 setuptools，未计作成功；使用标准隔离构建后通过。

## 回退与未验收项

**回退已实测**：原生 `/paveway legacy` 正常退出并释放 owner 后进入旧 UI，退出旧 UI 恢复同一原生 Conversation，daemon 保持 running。`./paveway.sh --legacy-ui` 或 `PAVEWAY_UI=legacy ./paveway.sh` 可选旧入口；后台 RPC 保留。切换 UI 不等于降级二进制，后者需先停服务并恢复私密设置备份；不删除原始数据，不自动重播 uncertain。

**未测试／不作承诺**：真实付费模型质量及费用、Windows→WSL Tijian 原生进程与 Desktop 数据库共享、真实 VJudge 网络登录、外部手机／桌面通知。Chrome 不存在的环境会明确跳过 Browser fixture，不自动下载。没有声称前台严格 settled、共享物理 Session 双向切换、全模型请求预算守卫或跨设备原始 Session 写回。

源码审查确认本次变更不包含运行配置／customization／Session／数据库／凭据文件，event.md 未改写；新增原生 MCP 路径加入忽略规则。专用用户数据 Git 仅在隔离 fixture 使用本地 remote 测试，没有更改或上传正式个人数据仓库。
