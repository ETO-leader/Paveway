# 独立 coach workflow v1

0.7.0 的GPT模板在 `src/oicoach_runtime/bundled/coach.md`，通过 package data 安装；`workflow create ID --workspace ABS --template coach` 将其复制为可编辑的外部 manifest/event.md，默认 memory_enabled=true。Scheduler 不 import 教学规则，仍只渲染受限 `${event_json}` 并执行事件。

使用 `ask --workflow ID --task TASK --mode plan|checkin|review --message TEXT [--wait]` 提交请求；或 schedule 的 payload-file 提供结构化数据。示例见 `examples/coach-plan.json`、`coach-checkin.json`、`coach-review.json`。payload 由模板理解，不是 Runtime 的新增业务 schema。

模板要求依据明确的目标、时区、时间窗口、休息与进度安排/调整/复盘；区分口胡、题解/提示使用、编码未过和独立通过。缺失信息时说明限制，不伪造题面、提交、比赛成绩或已完成操作。没有 Tijian 也可使用用户输入工作；Tijian 的具体 schema 通过工具发现，不在GPT提示中复制全部 API。

用户通过普通对话明确要求创建一次性定时任务或提醒，即可授权该次排程，不要求手填 payload 字段；例如“定时任务测试：请你设计一个 10min 的定时任务”会尝试创建一次 600 秒后的测试检查。明确“仅设计、不执行/不排程”则不调用 schedule，其优先级高于结构化 opt-in。没有明确的单次请求时，GPT主动跟进仍默认关闭，只有用户 opt-in `auto_followup=true` 并提供有限非负 `next_check_seconds` 才尝试一次。后续事件应关闭自动跟进，使用稳定 dedupe-key，检查排程返回再声明已安排。工具不可用时明确报告，不能声称未来会醒来。Runtime 对父 run 的 settled 成功和 queue 持久化负责，不假设模型一定调用工具。

只有明确用户反馈/指令才修改 Tijian 状态和计划，不猜测 AC 或题解使用状态，不自动重试 outcome=uncertain 的写入。不能因为回合 succeeded 就认定业务成功或训练有效。

最终可读结果放在 assistant 回复中。Runtime 在 completed/settled 后查询本轮最终文本，正常退出后与成功状态同交易保存有界回复；`runs output` 或 `ask --wait` 可读取。无推送通知；用户需主动查询或等待。旧版本 run、local-only 或不支持查询的 OMP 可能没有输出；不以别的回合文本代替。

模板要求在关键进展及每轮结束前通过 memory CLI 及时维护有证据的偏好/事实/决定/任务摘要，不必等待用户要求记住。系统 prompt 的记忆快照及最近对话各自最多 10,000 tokens，原始历史保留，旧内容按需检索。区分事实与 hypothesis。读到旧记忆时以本轮明确更正优先，冲突重读；仅真实工具成功后声称保存。共享偏好用于跨 task，训练要点默认限定 task，历史细节按需检索。旧模板需手动合并记忆段并升版，详见 [记忆接口](memory.md)。

模板是初版可运行策略，不保证模型建议质量或覆盖所有 OI 教学场景。真实验收在隔离 workspace、关闭外部工具时执行 plan → checkin → review 三个同 task 回合；另有真实模型用工具保存笔记、重启后由关闭工具的全新 task 读取注入记忆的验收。验证运行行为，不是学习效果评测。

0.13.1 修正普通 UI 对话无法满足旧 payload 门槛的问题。延迟从当前回合完全 settled 后起算（after_turn）；每次最多建立一个后续事件，后续 payload.auto_followup=false，稳定 dedupe-key 防重复。既有 coach 外部模板需合并定时规则；仅升级软件包不会覆盖用户自定义 workflow 文件。
