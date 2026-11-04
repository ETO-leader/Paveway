# 定时任务到期桌面提醒

0.22.0 增加用户明确提醒（native wakeup 的 `intent=reminder`）到期时的桌面弹窗。它由独立 `paveway-notifications.service` 用户服务读取已有持久事件，不依赖前台 UI 或模型队列。UI 关闭、后台 GPT 正在长时间执行或暂时不可用时，仍可按时提醒。内部 `agent_wake` 可以静默，不强制弹窗。

## 启用与检查

使用与一键启动器相同的配置路径：

```bash
oicoach-runtime notifications enable --backend auto --config /absolute/config.toml
oicoach-runtime notifications install --now --config /absolute/config.toml
oicoach-runtime notifications test --config /absolute/config.toml
oicoach-runtime notifications status --config /absolute/config.toml --json
```

原生 UI 的 `/paveway notifications` 提供 status、test popup、enable、隐藏正文启用和 disable。菜单的 enable 会同时安装并启用受管服务。新菜单需重新进入 UI 加载；命令行和独立服务可以立即使用，不必结束当前 GPT 会话或重启 Runtime。

`install` 先经 systemd-analyze 验证再发布，单独使用不会启动；`--now` 明确 enable/start。已有受管 unit 更新使用 `--replace`，其他配置／未拥有的同名 unit 拒绝覆盖。新安装默认关闭弹窗，启用时记录边界；不为启用前已过期的事件补发历史弹窗。启用后到期的既有未来提醒正常生效。

## 环境适配

- **Ubuntu 桌面**：使用 `notify-send` 和当前用户的 freedesktop Notifications D-Bus 服务。命令缺少时安装 `libnotify-bin`；必须有可用的图形桌面通知服务。用户服务缺少 DBus 环境变量时探测当前用户 `/run/user/UID/bus`。
- **WSL**：auto 优先 Windows PowerShell 的 WinRT Toast，使用已有 Windows PowerShell 桌面应用身份；不安装 PowerShell 模块、不改 Windows 注册表、不要求管理员权限。用户服务没有临时 `WSL_INTEROP` 变量时，在 WSL 系统目录中寻找有效的系统互操作 socket。需要启用 Windows 互操作及可执行的 `powershell.exe`。
- **显式覆盖**：enable/test 可用 `--backend linux` 或 `--backend windows`。有完整 Linux 桌面的 WSL 用户可以选择 linux；只有 WSLg 的环境不能仅凭存在 DISPLAY 就断言有 Linux 桌面通知服务器。

通知内容作为数据传入：Linux 使用 argv 且转义正文标记；Windows 使用固定脚本、JSON stdin 和 XML 文本节点，不把正文拼入 PowerShell 代码或命令行。独立服务不加载模型凭据环境文件，通知子进程仅接收桌面所需环境变量。

实现接口参考：[freedesktop Desktop Notifications](https://specifications.freedesktop.org/notification/latest-single/)、[Microsoft ToastNotificationManager](https://learn.microsoft.com/en-us/uwp/api/windows.ui.notifications.toastnotificationmanager)、[Windows 桌面应用通知身份](https://learn.microsoft.com/en-us/windows/win32/shell/enable-desktop-toast-with-appusermodelid)。

## 关闭、正文隐私与日志

```bash
# 弹窗仅显示通用到期提示，不显示任务正文。
oicoach-runtime notifications enable --hide-body --config /absolute/config.toml
# 停止发送新弹窗，保留排程和 Inbox。
oicoach-runtime notifications disable --config /absolute/config.toml
# 管理独立服务，不影响主 Runtime。
oicoach-runtime service status --name paveway-notifications.service --config /absolute/config.toml
oicoach-runtime service logs --name paveway-notifications.service --config /absolute/config.toml
oicoach-runtime service disable --now --name paveway-notifications.service --config /absolute/config.toml
```

设置位于私密 state_dir/desktop-notifications.json，默认正文显示任务提示的短预览。不能将私密配置和通知状态加入源码 Git。它是设备本地设置，不借源码 origin 同步。

服务退出／异常重启按事件与到期时间去重；同一到期时间不重复发送。用户明确重新安排到另一个时间可产生新的到期通知；取消或尚未到期时不弹窗。通知意图和 Inbox 提醒先在同一事务持久化，再请求系统显示。外部调用中断／结果未知时不盲目重复，status 会保留 unknown；失败时 Inbox 仍保存提醒，模型执行不受影响。

系统接受请求不等于用户看见／读过；通知权限、勿扰模式或系统策略可能只让消息进入通知中心。status 的 accepted 只代表接口接受，unavailable/unknown 会给出固定诊断码，日志不输出消息正文。弹窗不会将 Inbox 标为 acknowledged，也不会重发或调用模型。Windows/WSL 关机、休眠或实例关闭期间不能触发，Ubuntu 无桌面会话时不能保证弹窗。

## 验证范围

本轮 Windows 路径在真实 WSL 环境测试，桌面显示由用户确认；另以隔离配置、真实 systemd 用户服务和两秒计时任务验证到期后 Windows 接受弹窗，没有前台 UI、Runtime dispatcher 或模型调用。临时验证服务已卸载，不在正式任务队列留下测试事件。

当前 WSLg 环境没有 `org.freedesktop.Notifications` 桌面服务，因此 Ubuntu 原生桌面的屏幕效果未实机验收。Linux 路径已用 notify-send 进程 fixture 验证数据转义、环境隔离、到期行为、取消／重排和跨进程去重；不把这些测试说成已验证 GNOME 画面。
