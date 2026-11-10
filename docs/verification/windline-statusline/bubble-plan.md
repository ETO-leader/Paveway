# Independent Bubble 改造计划

**目标：** 在已有 OMP 集成上统一左右胶囊展示，保留 Context 与 Runtime 语义。

**架构：** `paveway-statusline.mjs` 内使用一个轻量 Bubble 原语处理圆帽、颜色、内边距和宽度。先按信息优先级分配完整胶囊，再升级文字与容量条；最终不对带圆帽的整行执行截断。

**技术：** 现有 JavaScript/Bun renderer；现有隔离 OMP PTY、WideScreen 终端单元记录及图片重绘。

## 边界

- 不修改宿主、Transcript、Scrollback、输入框、Alternate Screen 策略。
- 不修改 Wakeup/Inbox 协议、所有权、通知、持久化、Context 统计和动画算法。
- 不更新 OMP，不引入 Windline 依赖，不提交或清理用户文件。

## 实施

1. `src/oicoach_runtime/bundled/paveway-statusline.mjs`：保留导出接口、统计和 `contextMeterCells`；替换连接色带和右侧纯文字为共同的胶囊渲染。Nerd 使用 ``/``，Unicode 使用 `◖`/`◗`，ASCII 使用括号。
2. 宽度包含两端圆帽、内边距和胶囊间隔；异常/非零 Inbox、模型、Context 优先。先省略身份细节、Workflow、Cost 等次要内容；极窄宽度不输出半个圆帽。
3. Wakeup `none` 和 Inbox 0 隐藏；倒计时/Running 显示；不可用或未知保留醒目提示。仅消费现有结构化状态。
4. `tests/paveway-statusline.test.mjs`：覆盖独立圆帽的终端颜色、80/100/120/160 列预算、Unicode/ASCII/256/无色降级、状态条件及异常优先级，保留动画不变量。
5. `tests/test_native_statusline.py`：真实隔离 Runtime/模型/OMP 场景生成 PTY ANSI 和终端单元；截图保留旧版，新增 `bubble-*`，逐张审视后修正。
6. `docs/verification/windline-statusline/`：记录实际命令与结果、截图来源、旧版对比、修正和已知限制。

参考：[bubble.lua](https://github.com/windwp/windline.nvim/blob/master/lua/wlsample/bubble.lua) 的 `basic.right` 与 [helpers.lua](https://github.com/windwp/windline.nvim/blob/master/lua/windline/helpers.lua) 的成对 rounded separators。原有验收记录把它概括成连续 Powerline 连接，不能作为本次设计依据。
