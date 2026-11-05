# Windows Terminal Bubble 兼容修复计划

目标：修复 256 色色相、终端默认背景和可配置圆帽，不重设计布局或修改 OMP／业务协议。

1. `paveway-statusline.mjs`：保留 OMP `theme.getColorMode()` 的权威结果；无宿主结果时识别 Windows Terminal／COLORTERM／TERMINAL，禁色优先。不能仅凭 WSL 强制 True Color。
2. `ansi256()`：固定语义颜色使用校准表；通用量化按实际 `[0,95,135,175,215,255]` 找最近色阶，不允许有明显色相的颜色被灰阶吞没。波浪沿用现有计算，256 色仅使用经校准的青色亮度阶梯。
3. `palette()`：`null` 表示默认背景并生成 SGR 49；圆帽外围、分隔、左右对齐、极窄填充都改用默认背景。局部仅关闭自身设置的颜色／粗体，不使用 SGR 0 清除无关样式。
4. 圆帽保留现有 Nerd／Unicode／ASCII，增加 `PAVEWAY_STATUSLINE_CAPS` 显式选择；列宽检查仅用于预算，不能推断字体像素兼容。
5. Bun 测试解码 49 和真实 256 RGB，断言色相、默认背景、样式边界、模式优先级、字体配置及现有行为。
6. PTY 保留 True Color，新增独立的真实 256、Unicode、NO_COLOR、80/100/120/160、波浪与静止场景。新证据使用独立目录，默认背景按黑色重绘，明确不是 Windows Terminal 实机像素证据。
7. 调查可达 Windows Terminal 字体／内置 Powerline／行高与真实窗口；仅对隔离 probe 取图，不修改用户设置、不安装字体。如果不能完成字体配置对照，明确列为未验证，不用 Linux 字体重绘替代。
8. 完成八项架构自审，保留原始图和失败／修正记录，交付命令、真实结果及限制。

保持：Wakeup/Inbox 条件显示与异常优先、模型/Workflow 优先级、Context 数据/量化/波浪公式/motion=false/Compaction、单行位置、OMP 18.16.0 集成、输入框与 Transcript/Scrollback/Session 语义。不提交、推送或清理用户文件。
