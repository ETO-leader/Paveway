"""Conversation-first terminal UI: persistent composer, slash commands and selectors."""

import asyncio
import json
import locale
import math
import os
import queue
import shlex
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea, Frame

from . import __version__
from .customization import SKILL_CATEGORIES, THINKING_LEVELS, skill_category
from .errors import RuntimeContractError
from .models import parse_timestamp, validate_id
from .ui_client import CliClient, CliFailure

TERMINAL = {"succeeded", "failed", "blocked", "uncertain", "dead_letter", "cancelled"}
COMMANDS = {
    "/help": "操作说明", "/workflow": "选择或创建 workflow", "/task": "切换任务 ID",
    "/new": "新任务", "/mode": "安排 / 反馈 / 复盘", "/events": "查看 / 取消事件",
    "/schedule": "安排定时请求", "/memory": "查看 / 编辑长期笔记", "/history": "历史与会话原文",
    "/prompt": "自定义当前 workflow 系统提示词", "/skills": "管理当前 workflow 用户 Skill",
    "/sync": "多设备 Git 配置与历史同步",
    "/timers": "查看当前任务的触发时间与倒计时",
    "/select-model": "选择当前GPT使用的模型",
    "/select-thinking-hardness": "选择模型思考等级",
    "/proxy": "同步代理并检查代理端口",
    "/status": "运行状态", "/service": "服务安装 / 启停 / 日志", "/recover": "查看待核对事件",
    "/doctor": "运行诊断", "/init": "初始化配置", "/refresh": "刷新当前视图",
    "/next": "下一页", "/prev": "上一页", "/retry-send": "用原标识核对上次提交",
    "/discard-send": "确认后放弃本地提交记录", "/quit": "退出界面，后台任务继续",
}
HELP = """直接在底部输入，Enter 发送；Ctrl+J / Alt+Enter 换行。
输入 / 显示命令，Tab 补全，↑↓ 选择，Enter 确认，Esc 返回。
普通数字、q、r、s 等始终是输入文字，不是全局操作键。
Ctrl+C 清空草稿 / 取消选择；空输入连续两次 Ctrl+C 或 Ctrl+D 退出。
PageUp/PageDown 滚动记录，Ctrl+L 重绘；草稿保留在本次界面内存。

/workflow [ID]                  选择 workflow
/workflow create [ID PATH]      创建 coach（PATH 是已存在的绝对目录）
/task ID · /new [ID]            接续 / 创建任务
/mode [plan|checkin|review]      训练安排 / 进度反馈 / 复盘
/prompt [show|edit|reset]       查看 / 编辑 / 恢复系统提示词
/skills [add [CATEGORY]|category NAME|edit NAME|show NAME|enable NAME|disable NAME|delete NAME]  三类 Skill
/sync [remote|setup|now|status|pause|resume|conflicts|history]  多设备 Git 同步
/events · /events cancel ID     查看事件 / 确认后取消
/schedule                      输入执行时间及请求
/timers                        当前任务定时队列（系统时区）
/select-model [provider/model]  选择模型；切换时思考等级恢复 auto
/select-thinking-hardness [LEVEL]  选择思考等级；default 恢复继承
/proxy                         同步终端代理并检查端口
/memory [shared|task]           共享 / 当前 task 笔记
/memory add [KEY]               创建笔记
/memory edit KEY · forget KEY   修改 / 确认后删除
/history [QUERY]               查当前 workflow 历史
/history messages TASK [QUERY]  查指定任务原文（所有分支）
/next · /prev                  翻当前查询页
/status · /doctor · /init       运行状态 / 诊断 / 初始化
/service [status|install|start|stop|logs]  停止需确认，不自动启用服务
/recover                       只查看，不自动重播 uncertain
/retry-send                    用同一正文和 key 核对提交，不重跑已有事件
/discard-send                  放弃本地记录，不取消可能已入库的事件
/quit                          不停止 daemon 或取消已提交任务

回覆为已保存的最终文本，暂无 token/tool 实时流。"""


def visible(value):
    return "".join(c if c == "\n" or not unicodedata.category(c).startswith("C") else " " for c in str(value))


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def timestamp_text(value):
    try:
        return parse_timestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except RuntimeContractError:
        return "时间未知"


def duration_text(seconds):
    seconds = max(0, math.ceil(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天 " if days else "") + f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def message_heading(role, stamp):
    return role + " · " + timestamp_text(stamp)


def reply_text(event, output):
    text = (output or {}).get("text") or ""
    if event["status"] != "succeeded":
        code = event.get("last_error_code")
        reason = event.get("last_error_message")
        detail = " · ".join(part for part in (code, reason) if part)
        text = (text + "\n" if text else "") + (detail or "请求未成功完成；/events 查看详情")
        text += "\n未自动重发；修复原因后再提交新请求。"
    return text or "没有已保存的最终回复；/events 查看执行状态"


class SlashCompleter(Completer):
    def __init__(self, enabled=lambda: True):
        self.enabled = enabled

    def get_completions(self, document, complete_event):
        prefix = document.text_before_cursor
        if not self.enabled() or "\n" in prefix or not prefix.startswith("/"):
            return
        choices = COMMANDS
        for command, options in (("/mode ", ("plan", "checkin", "review")),
                                 ("/service ", ("status", "install", "start", "stop", "logs")),
                                 ("/prompt ", ("show", "edit", "reset")),
                                 ("/skills ", ("add", "show", "edit", "category", "enable", "disable", "delete", *SKILL_CATEGORIES)),
                                 ("/sync ", ("remote", "setup", "now", "status", "pause", "resume", "conflicts", "history", "next", "prev", "bind", "messages")),
                                 ("/memory ", ("shared", "task", "add", "edit", "forget"))):
            if prefix.startswith(command):
                choices = {command + option: "" for option in options}
                break
        for command, description in choices.items():
            if command.startswith(prefix):
                yield Completion(command, start_position=-len(prefix), display_meta=description)


class TerminalUI:
    def __init__(self, client, workflow=None, task="training", *, input=None, output=None):
        self.client, self.workflow, self.task = client, workflow, task
        self.mode = "plan"
        self.shared = True
        self.chat, self.system, self.seen = {}, [], set()
        self.announced = set()
        self.busy, self.notice = "", "输入请求，或用 /workflow 选择训练环境"
        self.pending, self.last_submission = None, None
        self.last_poll, self.last_interrupt = 0, 0
        self.last_restore, self.pending_since = 0, None
        self.timers, self.timer_error, self.last_timer_poll, self.timers_observed = None, None, 0, None
        self.results, self.worker = queue.Queue(), None
        self.picker, self.wizard, self.view = None, None, None
        self.deferred = None
        self.sync_pending, self.sync_last_poll, self.sync_history_query, self.sync_history_offset = None, 0, "", 0
        self.sync_archive = None
        self.generation, self.restore_pending = 0, False
        self.follow_tail = True
        self.app = None
        self.input, self.output = input, output
        self.editor = TextArea(multiline=True, height=Dimension(min=1, max=6),
            prompt="❯ ", completer=SlashCompleter(lambda: not self.picker and not self.wizard), history=InMemoryHistory(),
            complete_while_typing=True, wrap_lines=True)
        self.transcript = TextArea(read_only=True, focusable=False, scrollbar=True, wrap_lines=True)
        self.editor.buffer.on_text_changed += self._draft_changed

    @property
    def scope(self):
        return self.workflow, self.task

    def _draft_changed(self, _):
        if self.picker:
            self.picker["selected"] = 0
        self.invalidate()

    def replace_draft(self, value):
        self.editor.buffer.document = Document(value, len(value))

    def invalidate(self):
        if self.app:
            self.app.invalidate()

    def info(self, title, body=""):
        entry = f"{title}\n{body}".rstrip()
        if not self.system or self.system[-1] != entry:
            self.system.append(entry)
            self.system = self.system[-60:]
            self.chat.setdefault(self.scope, []).append(message_heading("系统", datetime.now(timezone.utc).isoformat()) + "\n" + entry)
        self.notice = title
        self.draw()

    def draw(self):
        welcome = "Paveway · GPT\n直接输入开始对话；/workflow 选择环境，/help 查看命令。"
        records = self.chat.get(self.scope, [])
        content = welcome + "\n\n" + "\n\n".join(records)
        position = len(content) if self.follow_tail else min(self.transcript.buffer.cursor_position, len(content))
        self.transcript.buffer.set_document(Document(visible(content), position), bypass_readonly=True)
        self.invalidate()

    def submit(self, label, operation, done):
        if self.busy:
            self.notice = "正在读取状态，请稍后按 Enter；草稿已保留"
            return False
        self.busy = label
        def worker():
            try:
                value, error = operation(), None
            except (RuntimeContractError, OSError) as failure:
                value, error = None, failure
            except Exception:
                value, error = None, CliFailure("CLI 操作失败，没有自动重复写入")
            self.results.put((done, value, error))
        self.worker = threading.Thread(target=worker, name="paveway-ui-cli", daemon=True)
        self.worker.start()
        self.invalidate()
        return True

    def collect(self):
        try:
            done, value, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = ""
        try:
            if error:
                if self.wizard:
                    self.wizard["saving"] = False
                    if not self.editor.text:
                        field = self.wizard["steps"][self.wizard["index"]][0]
                        self.replace_draft(self.wizard["answers"][field])
                message = f"{getattr(error, 'code', 'io_error')}: {error}"
                if self.last_submission and not self.last_submission["confirmed"]:
                    message += "\n提交未确认：/retry-send 用原标识核对，/discard-send 显式放弃本地记录。"
                self.info("操作未完成", message)
            else:
                if self.wizard and self.wizard.get("saving"):
                    draft = self.wizard["draft"] if self.editor.text == self.wizard["submitted_text"] else self.editor.text
                    self.wizard = None
                    self.replace_draft(draft)
                done(value)
        except RuntimeContractError as failure:
            self.info("请检查输入", str(failure))
        self.drain_deferred()
        if self.restore_pending and not self.busy:
            self.restore_pending = False
            self.restore()
        self.draw()

    def defer_command(self, text):
        if self.deferred:
            self.notice = "已有一条等待执行的命令；当前输入已保留"
            return
        self.deferred = {"text": text, "scope": self.scope, "generation": self.generation}
        self.replace_draft(self.wizard["submitted_text"] if self.wizard and self.wizard.get("saving") else "")
        self.notice = "命令已排队，当前 CLI 操作结束后执行"

    def drain_deferred(self):
        if not self.deferred or self.busy or self.wizard or self.picker:
            return
        command, self.deferred = self.deferred, None
        if self.scope != command["scope"] or self.generation != command["generation"]:
            self.info("已取消等待命令", "任务已切换；没有在新任务中执行旧命令。")
            return
        try:
            self.execute(command["text"])
        except RuntimeContractError as error:
            self.info("等待命令未执行", str(error))
            if not self.editor.text:
                self.replace_draft(command["text"])

    def scope_args(self, task=True):
        if not self.workflow:
            raise RuntimeContractError("尚未选择 workflow；输入 /workflow 选择或创建")
        return ["--workflow", self.workflow] + (["--task", self.task] if task else [])

    def bootstrap(self):
        def loaded(report):
            from .proxy import summary
            self.info("代理启动检查", summary(report.pop("proxy")))
            workflows = report["workflows"]
            if self.workflow:
                if self.workflow not in {row["id"] for row in workflows}:
                    self.info("找不到所选 workflow", "输入 /workflow 选择或创建")
                else:
                    self.restore()
            elif len(workflows) == 1:
                self.choose_scope(workflows[0]["id"], self.task)
            elif workflows:
                self.workflow_picker(workflows)
            else:
                self.info("欢迎使用 Paveway", "尚无 workflow。输入 /workflow create 创建 coach；配置不存在时先 /init。\n对话需要独立 daemon：另开终端运行 serve，或 /service start 启动已安装服务。")
        def read():
            proxy = self.client.call("proxy", "sync")
            return {**self.client.call("workflow", "list"), "proxy": proxy}
        self.submit("读取训练环境", read, loaded)

    def choose_scope(self, workflow, task):
        validate_id(workflow)
        validate_id(task)
        self.workflow, self.task = workflow, task
        self.generation += 1
        self.system = []
        self.view = None
        self.follow_tail = True
        self.restore_pending = True
        self.timers, self.timer_error, self.last_timer_poll, self.timers_observed = None, None, 0, None
        self.notice = f"当前任务：{workflow}/{task}"
        self.draw()

    def track_pending(self, event):
        if not self.pending or self.pending["event_id"] != event["event_id"]:
            age = 0
            try:
                created = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
                age = max(0, (datetime.now(timezone.utc) - created).total_seconds())
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            self.pending_since = time.monotonic() - age
            self.last_poll = 0
        self.pending = event

    def restore(self, *, background=False):
        if not self.workflow:
            return
        args, scope, generation = self.scope_args(), self.scope, self.generation
        seen = set(self.seen)
        def read():
            rows = self.client.call("history", "search", *args)[:5]
            return [self.client.call("history", "show", row["run_id"]) for row in reversed(rows) if row["run_id"] not in seen]
        def loaded(records):
            if self.scope != scope or self.generation != generation:
                return
            messages = self.chat.setdefault(scope, [])
            changed = False
            for record in records:
                run, event, output = record["run"], record["event"], record["output"]
                if run["run_id"] in self.seen or (self.pending and event["event_id"] == self.pending["event_id"]):
                    continue
                if event["event_id"] not in self.announced:
                    self.announced.add(event["event_id"])
                    messages.append(message_heading("你", event.get("created_at")) + "\n" + event["payload"].get("message", event["reason"]))
                    changed = True
                if event["status"] in TERMINAL:
                    self.seen.add(run["run_id"])
                    messages.append(message_heading("GPT · " + run["status"], run.get("completed_at") or event.get("completed_at")) + "\n" + reply_text(event, output))
                    if output.get("truncated"):
                        messages.append("[回复截断，可用 /history messages 查询原文]")
                    changed = True
                elif self.pending is None:
                    self.track_pending(event)
            if not background or changed:
                self.notice = "已恢复最近对话；正在继续等待最终回复" if self.pending else "已恢复最近对话；可直接输入，/history 查询更早记录"
            if changed and background:
                self.follow_tail = True
        if not self.submit("恢复对话", read, loaded):
            self.restore_pending = True
        else:
            self.last_restore = time.monotonic()

    def reconcile_history(self):
        if (self.workflow and not self.busy and not self.pending and not self.restore_pending
                and not self.picker and not self.wizard and time.monotonic() - self.last_restore >= 15):
            self.restore(background=True)

    def send(self, message):
        args = self.scope_args()
        if self.pending:
            self.notice = "上一请求仍在执行；可以继续编辑草稿，完成后再按 Enter"
            return False
        if self.last_submission and not self.last_submission["confirmed"]:
            self.info("上次提交尚未确认", "请先 /retry-send 核对原请求，或 /discard-send 显式放弃本地记录。新草稿已保留。")
            return False
        if not message.strip() or len(message.encode("utf-8")) > 32768 or "\0" in message:
            raise RuntimeContractError("请求需为非空 UTF-8 文本，最多 32 KiB；草稿已保留")
        if self.busy:
            self.notice = "正在读取状态，请稍后发送；草稿已保留"
            return False
        self.last_submission = {"args": args, "scope": self.scope, "mode": self.mode, "message": message,
                                "key": "ui-" + uuid.uuid4().hex, "confirmed": False}
        self.chat.setdefault(self.scope, []).append(message_heading("你", datetime.now(timezone.utc).isoformat()) + "\n" + message)
        self.follow_tail = True
        return self.resubmit()

    def resubmit(self):
        if not self.last_submission:
            raise RuntimeContractError("本次界面没有提交记录；旧事件请用 /events 查询")
        if self.pending:
            raise RuntimeContractError("请求已经确认，正在执行；无需重新提交")
        submission = dict(self.last_submission)
        def queued(event):
            self.track_pending(event)
            self.announced.add(event["event_id"])
            self.last_submission["confirmed"] = True
            self.notice = "请求已保存，等待GPT回复 · " + event["event_id"][:8]
            self.last_poll = 0
        return self.submit("提交请求", lambda: self.client.call("ask", *submission["args"], "--mode", submission["mode"],
            "--dedupe-key", submission["key"], text_files={"--message-file": submission["message"]}), queued)

    def poll(self):
        if not self.pending or self.busy or time.monotonic() - self.last_poll < 2:
            return
        self.last_poll = time.monotonic()
        event_id = self.pending["event_id"]
        def read():
            event = self.client.call("events", "show", event_id)
            output = self.client.call("runs", "output", event["run_id"]) if event["status"] in TERMINAL and event["run_id"] else None
            return event, output
        def loaded(result):
            event, output = result
            self.pending = event
            if event["status"] in TERMINAL:
                scope = event["workflow_id"], event["task_id"]
                if event["run_id"]:
                    self.seen.add(event["run_id"])
                message = message_heading("GPT · " + event["status"], event.get("completed_at")) + "\n" + reply_text(event, output)
                if output and output.get("truncated"):
                    message += "\n[回复截断，可查询历史原文]"
                self.chat.setdefault(scope, []).append(message)
                self.pending = None
                self.pending_since = None
                self.last_timer_poll = 0
                self.notice = f"请求完成 · {event['status']} · {scope[0]}/{scope[1]}"
                self.follow_tail = True
        self.submit("检查回复", read, loaded)

    def timer_line(self, event, now=None):
        now = now or datetime.now(timezone.utc)
        label = event["event_id"][:8]
        reason = visible(event.get("reason", "定时任务")[:30]).replace("\n", " ")
        if event["status"] == "waiting_anchor":
            return label + " · 待起算 " + duration_text(event.get("relative_seconds") or 0) + "（本回合结束后） · " + reason
        due = event.get("not_before_utc")
        if event["status"] in ("leased", "running"):
            phase = "启动中" if event["status"] == "leased" else "执行中"
        else:
            try:
                remaining = (parse_timestamp(due) - now).total_seconds()
                phase = "剩余 " + duration_text(remaining) if remaining > 0 else "已到时 · 等待派发"
            except RuntimeContractError:
                phase = "触发时间未知"
        return label + " · " + phase + " · " + timestamp_text(due) + " · " + reason

    def timer_text(self):
        if self.timer_error:
            return "定时任务 · 状态读取失败，自动重试；旧倒计时不作为执行结果"
        if self.timers is None:
            return "定时任务 · 正在读取" if self.workflow else "定时任务 · 请先选择 workflow"
        stale = self.timers_observed is not None and time.monotonic() - self.timers_observed > 15
        if not self.timers:
            return "定时任务 · 当前 task 无待执行任务" + (" · 状态待刷新" if stale else "")
        title = "定时任务 · 系统时区 · /timers 查看（最多100项）" + (" · 状态待刷新" if stale else "")
        return title + "\n" + "\n".join(self.timer_line(event) for event in self.timers[:3])

    def poll_timers(self, *, force=False):
        if not self.workflow or self.busy or (not force and time.monotonic() - self.last_timer_poll < 5):
            return False
        if not force and (self.wizard or self.picker):
            return False
        scope, generation, args = self.scope, self.generation, self.scope_args()
        def read():
            try:
                return self.client.call("events", "upcoming", *args), None
            except (RuntimeContractError, OSError) as error:
                return None, str(error)
        def loaded(result):
            if self.scope != scope or self.generation != generation:
                return
            rows, self.timer_error = result
            if rows is not None:
                self.timers, self.timers_observed = rows, time.monotonic()
            if force:
                if self.timer_error:
                    self.info("定时任务状态不可用", self.timer_error)
                elif rows:
                    self.open_picker("定时任务 · 系统时区 · ↑↓ Enter 查看详情", [
                        (self.timer_line(event), lambda event=event: self.show_event(event["event_id"])) for event in rows])
                else:
                    self.info("暂无待执行定时任务", "当前 workflow/task 没有等待或正在执行的事件。")
        if self.submit("读取定时任务", read, loaded):
            self.last_timer_poll = time.monotonic()
            return True
        return False

    def open_picker(self, title, items):
        self.picker = {"title": title, "items": items, "selected": 0, "draft": self.editor.text}
        self.replace_draft("")
        self.editor.buffer.cancel_completion()
        self.invalidate()

    def filtered_items(self):
        if not self.picker:
            return []
        query = self.editor.text.casefold()
        return [item for item in self.picker["items"] if query in item[0].casefold()]

    def select(self):
        items = self.filtered_items()
        if not items:
            self.notice = "没有匹配项；Esc 返回或清空搜索"
            return False
        index = min(self.picker["selected"], len(items) - 1)
        callback = items[index][1]
        self.replace_draft(self.picker["draft"])
        self.picker = None
        callback()
        return True

    def workflow_picker(self, rows):
        items = [(f"{row['id']} · {row['workspace']}", lambda row=row: self.choose_scope(row["id"], self.task)) for row in rows]
        items.append(("＋ 创建 coach workflow", self.create_workflow))
        self.open_picker("选择 workflow · ↑↓ Enter · 输入可筛选 · Esc 返回", items)

    def start_wizard(self, title, steps, done):
        self.wizard = {"title": title, "steps": steps, "index": 0, "answers": {}, "done": done, "draft": self.editor.text}
        self.replace_draft(steps[0][2])
        self.editor.buffer.cancel_completion()
        self.invalidate()

    def confirm(self, title, done):
        def confirmed(answers):
            if answers["confirm"] != "yes":
                raise RuntimeContractError("输入 yes 确认，或 Esc 取消")
            return done()
        self.start_wizard(title, [("confirm", "输入 yes 确认；Esc 取消", "", 8)], confirmed)

    def accept_wizard(self):
        wizard = self.wizard
        if wizard.get("saving"):
            self.notice = "正在保存，尚未收到确认；Esc 可关闭表单，但不会取消已提交操作"
            return
        field, label, _, maximum = wizard["steps"][wizard["index"]]
        value = self.editor.text
        if len(value.encode("utf-8")) > maximum:
            raise RuntimeContractError(f"{label} 最多 {maximum} 字节；没有截断输入")
        wizard["answers"][field] = value
        if wizard["index"] + 1 < len(wizard["steps"]):
            wizard["index"] += 1
            next_field = wizard["steps"][wizard["index"]]
            self.replace_draft(wizard["answers"].get(next_field[0], next_field[2]))
        elif wizard["done"](dict(wizard["answers"])) is not False:
            if self.busy:
                wizard["saving"] = True
                self.replace_draft("")
                wizard["submitted_text"] = ""
            else:
                self.wizard = None
                self.replace_draft(wizard["draft"])
        self.invalidate()

    def saved(self, value):
        if isinstance(value, dict) and "event_id" in value:
            self.last_timer_poll = 0
        self.info("操作已保存", pretty(value))

    def custom_saved(self, value):
        settings = value["settings"]
        skill_notice = "三类已启用 Skill 提供名称与用途索引，正文由 Agent 按需读取。"
        if not value["skills_enabled"]:
            skill_notice = "注意：agent.skills_enabled=false；用户 Skill 已保存，但不会加载。"
        elif not value.get("tools_enabled", True):
            skill_notice += " 当前工具已停用；无法按需读取 Skill 正文，请先启用读取工具。"
        self.info("自定义设置已保存 · v" + str(settings["revision"]),
            "当前 workflow 的所有 task 下次派发请求时生效，无需重启 daemon；已开始的回合和旧历史不变。\n"
            + skill_notice)

    def select_inference(self, kind, args):
        self.scope_args()
        if len(args) > 1:
            raise RuntimeContractError("用法：/select-model [provider/model|default] 或 /select-thinking-hardness [LEVEL|default]")
        if kind == "thinking" and args and args[0] not in (*THINKING_LEVELS, "default"):
            raise RuntimeContractError("思考等级：default / " + " / ".join(THINKING_LEVELS))
        workflow, scope, generation = self.workflow, self.scope, self.generation
        def loaded(report):
            if self.scope != scope or self.generation != generation:
                return
            current = report["settings"].get("inference", {})
            effective = report["inference"]
            def save(value):
                if self.scope != scope or self.generation != generation:
                    return
                options = ["--" + kind, value]
                if kind == "model":
                    options += ["--thinking", "auto" if value else ""]
                def saved(value):
                    inference = value["inference"]
                    self.info("模型设置已保存 · v" + str(value["settings"]["revision"]),
                        "模型：" + (inference["model"] or "OMP 默认") + "\n思考等级：" + (inference["thinking"] or "OMP 默认")
                        + "\n当前 workflow 的所有 task 下次派发生效；正在执行的回合保持原设置。")
                return self.submit("保存模型设置", lambda: self.client.call("workflow", "customize", "inference", workflow,
                    "--expected-revision", str(report["settings"]["revision"]), *options), saved)
            if args:
                return save("" if args[0] == "default" else args[0])
            if kind == "model":
                choices = [("", "继承 Runtime / OMP 默认")]
                choices += [(model["selector"], model["selector"] + " · " + visible(model["name"])) for model in report["models"]]
                title = "选择模型 · 输入筛选 · ↑↓ Enter · 切换后 thinking=auto"
            else:
                model = next((model for model in report["models"] if model["selector"] == effective["model"]), None)
                levels = model["thinking"] if model is not None else list(THINKING_LEVELS)
                choices = [("", "继承 Runtime / OMP 默认"), ("auto", "auto · 由 OMP 自动决定")]
                choices += [(level, level) for level in THINKING_LEVELS if level in levels and level != "auto"]
                title = "选择思考等级 · " + (effective["model"] or "OMP 默认模型") + " · ↑↓ Enter"
            self.open_picker(title, [(("✓ " if value == current.get(kind, "") else "") + label,
                                      lambda value=value: save(value)) for value, label in choices])
        command = ("workflow", "customize", "show", workflow) if args else ("workflow", "models", workflow)
        return self.submit("读取模型设置", lambda: self.client.call(*command), loaded)

    def custom_command(self, kind, args):
        self.scope_args()
        workflow, scope, generation = self.workflow, self.scope, self.generation
        def loaded(report):
            if self.scope != scope or self.generation != generation:
                return
            settings = report["settings"]
            revision = settings["revision"]
            def save(operation, *extra, content=None):
                return self.submit("保存自定义设置", lambda: self.client.call("workflow", "customize", operation, workflow,
                    "--expected-revision", str(revision), *extra,
                    text_files={"--content-file": content} if content is not None else None), self.custom_saved)
            def prompt_edit():
                current = settings["system_prompt"]
                self.info("编辑系统提示词", "append 追加到 OMP 原生系统提示；replace 替换原生系统提示。两种模式都提供三类已启用 Skill 的索引，正文按需读取；coach 模板仍作为回合请求发送。\nCtrl+J 换行；内容按原文保存，不展开 $ 或执行命令。")
                def done(values):
                    if values["mode"] not in ("append", "replace"):
                        raise RuntimeContractError("模式只能是 append 或 replace；Shift+Tab 可返回修改")
                    return save("prompt", "--mode", values["mode"], content=values["content"])
                self.start_wizard("系统提示词", [("mode", "append 追加 / replace 替换", current["mode"], 16),
                    ("content", "系统提示词正文 · Ctrl+J 换行", current["text"], 65536)], done)
            def prompt_show():
                self.info("自定义系统提示词 · " + settings["system_prompt"]["mode"] + " · v" + str(revision),
                          settings["system_prompt"]["text"] or "使用 OMP 默认系统提示词，三类已启用 Skill 的正文按需读取；尚未追加自定义系统提示词。")
            def prompt_reset():
                self.confirm("恢复默认系统提示词（不删除 Skill）", lambda: save("prompt", "--mode", "append", content=""))
            def skill_edit(skill=None, category=None):
                if category is None:
                    if skill:
                        category = skill_category(skill)
                    else:
                        self.open_picker("选择 Skill 分类 · 正文按需读取", [
                            (label, lambda category=category: skill_edit(category=category)) for category, label in SKILL_CATEGORIES.items()])
                        return
                steps = [] if skill else [("name", "Skill 名称 · 小写字母/数字/短横线", "", 64)]
                steps += [("description", "用途描述 · 最多 64 KiB · 说明何时需要读取此 Skill", skill["description"] if skill else "", 65536),
                          ("content", "Skill Markdown 正文 · Ctrl+J 换行", skill["content"] if skill else "", 32768)]
                def done(values):
                    return save("skill-put", "--name", skill["name"] if skill else values["name"],
                                "--category", category, "--description", values["description"], content=values["content"])
                self.start_wizard(("编辑用户 Skill" if skill else "新增用户 Skill") + " · " + SKILL_CATEGORIES[category], steps, done)
            def skill_action(action, skill):
                if action == "show":
                    self.info("用户 Skill · " + SKILL_CATEGORIES[skill_category(skill)] + " · " + skill["name"] + (" · 启用" if skill["enabled"] else " · 停用"),
                              skill["description"] + "\n\n" + skill["content"])
                elif action == "edit":
                    skill_edit(skill)
                elif action == "category":
                    self.open_picker("移动 Skill 分类 · " + skill["name"], [
                        (label, lambda category=category: save("skill-put", "--name", skill["name"], "--category", category,
                            "--description", skill["description"], content=skill["content"])) for category, label in SKILL_CATEGORIES.items()])
                elif action == "delete":
                    self.confirm("删除用户 Skill " + skill["name"], lambda: save("skill-delete", "--name", skill["name"]))
                else:
                    save("skill-" + action, "--name", skill["name"])
            def skill_menu(skill):
                actions = ["show", "edit", "category", "disable" if skill["enabled"] else "enable", "delete"]
                labels = {"show": "查看正文", "edit": "编辑", "category": "移动分类", "disable": "停用", "enable": "启用", "delete": "删除"}
                self.open_picker("用户 Skill · " + skill["name"], [(labels[a], lambda a=a: skill_action(a, skill)) for a in actions])
            def category_menu(category):
                group = [s for s in settings["skills"] if skill_category(s) == category]
                self.open_picker(SKILL_CATEGORIES[category] + " · ↑↓ Enter", [("＋ 新增用户 Skill", lambda: skill_edit(category=category))] + [
                    (("● " if s["enabled"] else "○ ") + s["name"] + " · " + s["description"], lambda s=s: skill_menu(s)) for s in group])
            if kind == "prompt":
                if not args:
                    self.open_picker("系统提示词 · 当前 " + settings["system_prompt"]["mode"],
                        [("查看当前系统提示词", prompt_show), ("编辑系统提示词", prompt_edit), ("恢复 OMP 默认提示词", prompt_reset)])
                elif args == ["show"]:
                    prompt_show()
                elif args == ["edit"]:
                    prompt_edit()
                elif args == ["reset"]:
                    prompt_reset()
                else:
                    raise RuntimeContractError("用法：/prompt [show|edit|reset]")
            elif not args:
                self.info("用户 Skill · 当前 workflow", "OI Introduce、user information、personal skill 三类共同提供索引，正文按需读取。\n"
                    + ("下次请求生效。" if report["skills_enabled"] else "当前 agent.skills_enabled=false，不会加载。"))
                self.open_picker("用户 Skill 分类 · 按需读取 · ↑↓ Enter", [
                    (label + " · " + str(sum(skill_category(s) == category and s["enabled"] for s in settings["skills"])) + " 项启用",
                     lambda category=category: category_menu(category)) for category, label in SKILL_CATEGORIES.items()])
            elif len(args) == 1 and args[0] in SKILL_CATEGORIES:
                category_menu(args[0])
            elif args == ["add"]:
                skill_edit()
            elif len(args) == 2 and args[0] == "add" and args[1] in SKILL_CATEGORIES:
                skill_edit(category=args[1])
            elif len(args) == 2 and args[0] in ("show", "edit", "category", "enable", "disable", "delete"):
                skill = next((s for s in settings["skills"] if s["name"] == args[1]), None)
                if skill is None:
                    raise RuntimeContractError("没有此用户 Skill；/skills 查看或 /skills add 新建")
                skill_action(args[0], skill)
            else:
                raise RuntimeContractError("用法：/skills [oi-introduce|user-information|personal-skill|add [CATEGORY]|show NAME|edit NAME|category NAME|enable NAME|disable NAME|delete NAME]")
        return self.submit("读取自定义设置", lambda: self.client.call("workflow", "customize", "show", workflow), loaded)

    def sync_command(self, args):
        action = args[0] if args else "menu"
        if action in ("next", "prev") and self.sync_archive:
            self.sync_archive["offset"] = max(0, self.sync_archive["offset"] + (20 if action == "next" else -20))
            return self.sync_messages()
        if action in ("history", "next", "prev"):
            self.sync_archive = None
            if action == "history":
                self.sync_history_query, self.sync_history_offset = " ".join(args[1:]), 0
            else:
                self.sync_history_offset = max(0, self.sync_history_offset + (20 if action == "next" else -20))
            def loaded(page):
                self.info("跨设备历史 · 第 " + str(page["offset"] // 20 + 1) + " 页", "/sync next · /sync prev\n历史仅供查询，不会导入执行队列或接管另一设备的会话。")
                def show(row):
                    def details(report):
                        item = report["record"]
                        self.info("同步历史 · " + item["device_id"][:8] + " · " + item["status"],
                            "你\n" + str(item.get("user_message", "")) + "\n\nGPT\n" + (item.get("reply") or item.get("error_message") or item.get("error_code") or "没有最终回复")
                            + ("\n\n文本原文：/sync messages " + item["device_id"] + " " + item["binding_id"] if item.get("binding_id") else ""))
                    self.submit("读取同步历史", lambda: self.client.call("sync", "show", "--device", row["device_id"], "--run", row["run_id"]), details)
                if page["records"]:
                    self.open_picker("跨设备历史 · ↑↓ Enter", [(r["device_id"][:8] + " · " + r["workflow_id"] + "/" + r["task_id"] + " · " + r["status"] + " · " + r["snippet"][:70], lambda r=r: show(r)) for r in page["records"]])
                else:
                    self.info("没有同步历史", "同步完成后再查询，或修改搜索文字。")
            return self.submit("读取跨设备历史", lambda: self.client.call("sync", "history", "--query", self.sync_history_query, "--offset", str(self.sync_history_offset)), loaded)
        if action == "messages":
            if len(args) < 3:
                raise RuntimeContractError("用法：/sync messages DEVICE SESSION [QUERY]")
            self.sync_archive = {"device": args[1], "session": args[2], "query": " ".join(args[3:]), "offset": 0}
            return self.sync_messages()
        if action == "bind":
            if len(args) != 2:
                raise RuntimeContractError("用法：/sync bind WORKFLOW_ID，然后输入本机 workspace 绝对路径")
            def bound(value):
                self.info("本机 workflow 已建立", "下次同步会应用远端用户配置。新 workflow 使用本地 coach 模板；没有导入远端执行队列。")
            self.start_wizard("绑定远端配置到本机 coach", [("workspace", "本机已存在的 workspace 绝对路径", str(Path.cwd()), 4096)],
                lambda values: self.submit("绑定远端配置", lambda: self.client.call("sync", "bind", "--workflow", args[1], "--workspace", values["workspace"]), bound))
            return True
        if len(args) > 1:
            raise RuntimeContractError("此同步操作不接受额外参数")
        if action == "now":
            def requested(value):
                self.sync_pending = {"previous": value["previous_completion"]}
                self.sync_last_poll = 0
                self.info("Git 同步已请求", "后台保存、pull 与 push，不阻塞训练请求；完成后会显示结果。")
            return self.submit("请求 Git 同步", lambda: self.client.call("sync", "now"), requested)
        if action in ("pause", "resume"):
            return self.submit("Git 同步 " + action, lambda: self.client.call("sync", action), lambda value: self.info("Git 同步已暂停" if action == "pause" else "Git 自动同步已启用", pretty(value["last"])))
        if action == "conflicts":
            def loaded(report):
                if not report["conflicts"]:
                    self.info("没有配置冲突", "已绑定的单一远端版本会自动应用。")
                    return
                def choose(conflict):
                    workflow = conflict["workflow_id"]
                    if conflict["needs_binding"]:
                        self.info("远端配置尚未绑定 · " + workflow, "输入 /sync bind " + workflow + " 选择本机 workspace；不会采用另一设备的绝对路径。")
                        return
                    def resolve(head=None):
                        extra = ["--head", head] if head else ["--keep-local"]
                        self.confirm("解决配置冲突 · " + workflow, lambda: self.submit("保存冲突选择", lambda: self.client.call("sync", "resolve", "--workflow", workflow, *extra), lambda value: self.info("冲突选择已保存", "其他版本仍保留在 Git 历史中；/sync now 推送解决结果。")))
                    self.open_picker("选择保留的配置版本 · " + workflow,
                        [("保留当前本机配置", resolve)] + [(h["device_id"][:8] + " · " + h["prompt_preview"][:70] + " · " + ",".join(h["skill_names"]), lambda h=h: resolve(h["head"])) for h in conflict["heads"]])
                self.open_picker("配置冲突 / 待绑定 · ↑↓ Enter", [(c["workflow_id"] + (" · 待绑定" if c["needs_binding"] else " · 多个版本"), lambda c=c: choose(c)) for c in report["conflicts"]])
            return self.submit("读取配置冲突", lambda: self.client.call("sync", "conflicts"), loaded)
        if action not in ("setup", "remote", "status", "menu"):
            raise RuntimeContractError("用法：/sync [remote|setup|now|status|pause|resume|conflicts|history|bind ID]")
        def loaded(report):
            if action == "status":
                self.info("Git 同步状态", pretty(report))
            elif action == "remote":
                self.info("配置用户数据 remote", "全部GPT对话共用一个独立 Git 仓库。remote 留空只维护本地 Git；填写后按同步开关自动 pull/push。")
                def save_remote(values):
                    from .git_sync import validate_remote
                    validate_remote(values["remote"])
                    return self.submit("保存 remote", lambda: self.client.call("sync", "remote", "--remote", values["remote"]),
                        lambda result: self.info("remote 已保存", "仅本地 Git 保存，不上传。" if not values["remote"] else "remote 已配置；自动同步开关保持原样，/sync status 查看。"))
                self.start_wizard("用户数据 remote", [("remote", "SSH/HTTPS remote；清空后停止上传，不填写 token", (report["settings"] or {}).get("remote", ""), 4096)], save_remote)
            elif action == "menu":
                choices = [("配置 / 移除 remote", "remote"), ("仓库与同步间隔设置", "setup"), ("立即后台同步", "now"), ("查看同步状态", "status"),
                           ("跨设备历史", "history"), ("配置冲突 / 新设备绑定", "conflicts"),
                           ("暂停自动同步", "pause"), ("恢复自动同步", "resume")]
                mode = "远端同步" if (report["settings"] or {}).get("remote") else "仅本地 Git"
                self.open_picker("全部GPT对话共用 · " + mode + " · ↑↓ Enter", [(label, lambda cmd=cmd: self.sync_command([cmd])) for label, cmd in choices])
            else:
                values = report["settings"] or {}
                self.info("设置 Git 用户数据仓库", "仅同步自定义 prompt/Skill 和可查询历史；不上传 API key 文件或执行队列。remote 留空时仅本地 Git 保存。")
                def done(values):
                    from .git_sync import validate_remote
                    validate_remote(values["remote"])
                    try:
                        interval = int(values["interval"])
                    except ValueError:
                        raise RuntimeContractError("同步间隔需为 10–86400 的整数秒数") from None
                    return self.submit("保存 Git 同步设置", lambda: self.client.call("sync", "setup", "--repository", values["repository"], "--remote", values["remote"], "--branch", values["branch"], "--interval", str(interval)),
                        lambda result: self.info("Git 同步配置已保存", "daemon 运行时会自动保存并同步。新设备请使用自己的本地配置和工作目录。"))
                self.start_wizard("Git 用户数据同步", [("repository", "专用本地 Git 数据目录（空目录）", values.get("repository", report["default_repository"]), 4096),
                    ("remote", "SSH/HTTPS remote；留空仅本地，不填写 token", values.get("remote", ""), 4096),
                    ("branch", "同步分支", values.get("branch", "main"), 128),
                    ("interval", "同步间隔秒数 · 10–86400", str(values.get("interval_seconds", 60)), 8)], done)
        return self.submit("读取 Git 同步设置", lambda: self.client.call("sync", "status"), loaded)

    def sync_messages(self):
        view = dict(self.sync_archive)
        return self.submit("读取同步原文", lambda: self.client.call("sync", "messages", "--device", view["device"],
            "--session", view["session"], "--query", view["query"], "--offset", str(view["offset"])),
            lambda value: self.info("同步原文 · 所有分支 · /sync next · /sync prev", pretty(value)))

    def poll_sync(self):
        if self.sync_pending is None or self.busy or time.monotonic() - self.sync_last_poll < 2:
            return
        self.sync_last_poll = time.monotonic()
        def loaded(report):
            last = report["last"]
            if not report["settings"] or not report["settings"]["enabled"]:
                self.sync_pending = None
                self.info("Git 同步已暂停")
            elif last.get("state") != "syncing" and last.get("completed_at") != self.sync_pending["previous"]:
                self.sync_pending = None
                title = "Git 同步完成" if last.get("state") == "ok" else "Git 同步未完成"
                if last.get("conflict_count"):
                    title += " · 配置需选择版本"
                self.info(title, pretty(last) + ("\n/sync conflicts 处理冲突或绑定本机 workspace。" if last.get("conflict_count") else ""))
        self.submit("检查 Git 同步进度", lambda: self.client.call("sync", "status"), loaded)

    def create_workflow(self, identifier="", workspace=""):
        def done(values):
            validate_id(values["id"])
            def created(result):
                self.choose_scope(result["workflow"]["id"], self.task)
                self.info("workflow 已创建", "启动独立 daemon 后可直接输入训练请求。")
            return self.submit("创建 workflow", lambda: self.client.call("workflow", "create", values["id"],
                "--workspace", values["workspace"]), created)
        self.start_wizard("创建 coach workflow", [("id", "workflow ID", identifier or "coach", 128),
            ("workspace", "已存在的绝对 workspace 路径", workspace or str(Path.cwd()), 4096)], done)

    def memory_edit(self, key="", note=None):
        args = self.scope_args(not self.shared)
        steps = [] if note else [("key", "笔记 key（ASCII ID）", key, 128)]
        steps += [("title", "笔记标题", note["title"] if note else "", 256),
                  ("content", "正文 · Ctrl+J 换行，Enter 下一步", note["content"] if note else "", 4096),
                  ("kind", "fact / preference / summary / hypothesis", note["kind"] if note else "fact", 32),
                  ("pinned", "优先加载？yes / no", "yes" if note and note["pinned"] else "no", 8),
                  ("expiry", "有效期 RFC3339；留空长期", note.get("expires_at") or "" if note else "", 64)]
        def done(values):
            identifier = note["key"] if note else values["key"]
            validate_id(identifier)
            if values["kind"] not in ("fact", "preference", "summary", "hypothesis") or values["pinned"] not in ("yes", "no"):
                raise RuntimeContractError("笔记类型或 yes/no 无效；Esc 取消后重新编辑")
            extra = (["--pinned"] if values["pinned"] == "yes" else []) + (["--expires-at", values["expiry"]] if values["expiry"] else [])
            if note and note.get("source_event_id"):
                extra += ["--source-event", note["source_event_id"]]
            return self.submit("保存笔记", lambda: self.client.call("memory", "put", *args, "--key", identifier,
                "--title", values["title"], "--kind", values["kind"], "--expected-version", str(note["version"] if note else 0),
                *extra, text_files={"--content-file": values["content"]}), self.saved)
        self.start_wizard("编辑笔记 · " + ("workflow 共享" if self.shared else self.task), steps, done)

    def query_view(self, kind, query="", offset=0, task=None):
        scope, generation = self.scope, self.generation
        args = self.scope_args(False) if kind not in ("events", "recover") else []
        if kind == "memory":
            command = ["memory", "list", *self.scope_args(not self.shared), "--query", query]
        elif kind == "history":
            command = ["history", "search", *args, "--query", query]
        elif kind == "messages":
            validate_id(task)
            command = ["history", "messages", *args, "--task", task, "--query", query]
        else:
            command = ["events" if kind == "events" else "recover", "list"]
        view = {"kind": kind, "query": query, "offset": offset, "task": task}
        def loaded(value):
            if self.scope != scope or self.generation != generation:
                return
            self.view = view
            self.info(f"{kind} · 第 {offset // (100 if kind in ('events', 'recover') else 20) + 1} 页", "/next 下一页 · /prev 上一页")
            if kind == "messages":
                self.info("会话原文 · 所有分支，非当前执行路径", pretty(value))
                return
            items = []
            for row in value:
                if kind == "memory":
                    label = f"{row['key']} · v{row['version']} · {row['title']}"
                    callback = lambda row=row, args=list(self.scope_args(not self.shared)): self.submit("读取笔记",
                        lambda: self.client.call("memory", "show", *args, "--key", row["key"]),
                        lambda note: self.info("笔记 · " + note["key"], pretty(note) + "\n/memory edit KEY · /memory forget KEY"))
                elif kind == "history":
                    label = f"{row['task_id']} · {row['status']} · {row['snippet'][:70]}"
                    callback = lambda row=row: self.show_run(row["run_id"])
                else:
                    label = f"{row['event_id'][:8]} · {row['status']} · {row.get('task_id', '')}" + (" · " + row["last_error_code"] if row.get("last_error_code") else "")
                    callback = lambda row=row: self.show_event(row["event_id"])
                items.append((label, callback))
            if items:
                self.open_picker(kind + " · ↑↓ Enter · Esc 返回", items)
            else:
                self.info("没有匹配记录", "可以修改查询，或 /prev 返回上一页。")
        return self.submit("读取 " + kind, lambda: self.client.call(*command, "--offset", str(offset)), loaded)

    def show_run(self, run_id):
        def loaded(value):
            event, output = value["event"], value["output"]
            self.info(f"历史 · {event['workflow_id']}/{event['task_id']} · {value['run']['status']}",
                message_heading("你", event.get("created_at")) + "\n" + event["payload"].get("message", event["reason"])
                + "\n\n" + message_heading("GPT", value["run"].get("completed_at") or event.get("completed_at")) + "\n" + reply_text(event, output)
                + f"\n\n原文：/history messages {event['task_id']}\nrun_id={run_id}")
        return self.submit("读取历史", lambda: self.client.call("history", "show", run_id), loaded)

    def show_event(self, event_id):
        def read():
            event = self.client.call("events", "show", event_id)
            return {"event": event, "output": self.client.call("runs", "output", event["run_id"]) if event["run_id"] else None}
        return self.submit("读取事件", read, lambda value: self.info("事件详情", pretty(value)))

    def execute(self, text):
        if not text.startswith("/"):
            return self.send(text)
        try:
            parts = shlex.split(text)
        except ValueError:
            raise RuntimeContractError("命令引号未闭合；草稿已保留") from None
        name, args = parts[0], parts[1:]
        if name in ("/quit", "/help", "/status", "/doctor", "/init", "/refresh", "/next", "/prev", "/retry-send", "/discard-send", "/schedule", "/recover", "/timers", "/proxy") and args:
            raise RuntimeContractError("此命令不接受参数：" + name)
        if name == "/quit":
            if self.app:
                self.app.exit()
            return True
        if name == "/help":
            self.info("操作说明", HELP)
            return True
        if self.busy:
            self.notice = "正在读取状态，请稍后按 Enter；命令已保留"
            return False
        if name in ("/prompt", "/skills"):
            return self.custom_command(name[1:], args)
        if name in ("/select-model", "/select-thinking-hardness"):
            return self.select_inference("model" if name == "/select-model" else "thinking", args)
        if name == "/proxy":
            from .proxy import summary
            return self.submit("同步代理", lambda: self.client.call("proxy", "sync"),
                               lambda report: self.info("代理检查", summary(report)))
        if name == "/sync":
            return self.sync_command(args)
        if name == "/workflow":
            if args and args[0] == "create":
                if len(args) not in (1, 3):
                    raise RuntimeContractError("用法：/workflow create [ID \"绝对路径\"]")
                self.create_workflow(*args[1:])
            elif len(args) == 1:
                validate_id(args[0])
                return self.submit("选择 workflow", lambda: self.client.call("workflow", "show", args[0]),
                                   lambda value: self.choose_scope(value["workflow"]["id"], self.task))
            elif not args:
                return self.submit("读取 workflow", lambda: self.client.call("workflow", "list"), lambda report: self.workflow_picker(report["workflows"]))
            else:
                raise RuntimeContractError("用法：/workflow [ID]")
        elif name in ("/task", "/new"):
            if len(args) > 1 or (name == "/task" and not args):
                raise RuntimeContractError("用法：/task ID 或 /new [ID]")
            self.scope_args()
            self.choose_scope(self.workflow, args[0] if args else "training-" + uuid.uuid4().hex[:8])
        elif name == "/mode":
            if not args:
                self.open_picker("训练模式", [(label, lambda mode=mode: self.set_mode(mode)) for mode, label in
                    (("plan", "安排 plan"), ("checkin", "反馈 checkin"), ("review", "复盘 review"))])
            elif len(args) == 1:
                self.set_mode(args[0])
            else:
                raise RuntimeContractError("用法：/mode [plan|checkin|review]")
        elif name == "/memory":
            if not args or args[0] in ("shared", "task"):
                if args:
                    self.shared = args[0] == "shared"
                return self.query_view("memory", " ".join(args[1:]))
            operation, rest = args[0], args[1:]
            if operation == "add" and len(rest) <= 1:
                self.memory_edit(rest[0] if rest else "")
            elif operation in ("edit", "forget") and len(rest) == 1:
                key, scope = rest[0], self.scope
                validate_id(key)
                options = self.scope_args(not self.shared)
                def loaded(note):
                    if self.scope != scope:
                        return
                    if operation == "edit":
                        self.memory_edit(note=note)
                    else:
                        self.confirm("删除笔记 " + key + "（不删除旧会话/备份）", lambda: self.submit("删除笔记",
                            lambda: self.client.call("memory", "delete", *options, "--key", key,
                                "--expected-version", str(note["version"])), self.saved))
                return self.submit("读取笔记版本", lambda: self.client.call("memory", "show", *options, "--key", key), loaded)
            else:
                raise RuntimeContractError("用法：/memory [shared|task|add [KEY]|edit KEY|forget KEY]")
        elif name == "/history":
            if args and args[0] == "messages":
                if len(args) < 2:
                    raise RuntimeContractError("用法：/history messages TASK [QUERY]")
                return self.query_view("messages", " ".join(args[2:]), task=args[1])
            return self.query_view("history", " ".join(args))
        elif name == "/events" and args:
            if len(args) != 2 or args[0] != "cancel":
                raise RuntimeContractError("用法：/events 或 /events cancel EVENT_ID")
            validate_id(args[1])
            def loaded(event):
                self.confirm("取消事件 " + event["event_id"], lambda: self.submit("取消事件", lambda: self.client.call(
                    "events", "cancel", event["event_id"], "--expected-version", str(event["version"])), self.saved))
            return self.submit("读取事件版本", lambda: self.client.call("events", "show", args[1]), loaded)
        elif name in ("/events", "/recover"):
            return self.query_view("events" if name == "/events" else "recover")
        elif name in ("/next", "/prev"):
            if not self.view:
                raise RuntimeContractError("请先 /events、/memory 或 /history 查询")
            view = dict(self.view)
            step = 100 if view["kind"] in ("events", "recover") else 20
            view["offset"] = max(0, view["offset"] + (step if name == "/next" else -step))
            return self.query_view(**view)
        elif name == "/refresh":
            if self.view:
                return self.query_view(**self.view)
            self.restore()
        elif name == "/timers":
            return self.poll_timers(force=True)
        elif name == "/retry-send":
            return self.resubmit()
        elif name == "/discard-send":
            self.confirm("放弃本地提交记录，不取消可能已入库的事件", self.discard_submission)
        elif name == "/schedule":
            options, mode = self.scope_args(), self.mode
            def scheduled(values):
                return self.submit("安排请求", lambda: self.client.call("schedule", *options, "--at", values["at"],
                    "--reason", "UI scheduled request", "--dedupe-key", "ui-" + uuid.uuid4().hex,
                    text_files={"--payload-file": json.dumps({"mode": mode, "message": values["message"]}, ensure_ascii=False)}), self.saved)
            self.start_wizard("安排定时请求", [("at", "RFC3339（含时区）或 now", "now", 64),
                ("message", "请求内容 · Ctrl+J 换行", "", 32768)], scheduled)
        elif name == "/service":
            actions = ("status", "install", "start", "stop", "logs")
            if not args:
                self.open_picker("服务操作", [(action, lambda action=action: self.execute("/service " + action)) for action in actions])
            elif len(args) == 1 and args[0] in actions:
                action = args[0]
                def operate():
                    return self.submit("服务 " + action, lambda: self.client.call("service", action), self.saved)
                if action == "stop":
                    self.confirm("停止服务可能中断活动回合并产生 uncertain", operate)
                else:
                    return operate()
            else:
                raise RuntimeContractError("用法：/service [status|install|start|stop|logs]")
        elif name in ("/status", "/doctor", "/init"):
            return self.submit("读取 " + name, lambda: self.client.call(name[1:]), lambda value: self.info(name[1:], pretty(value)))
        else:
            raise RuntimeContractError("未知命令；输入 /help 查看。正文需要以 / 开头时可在前面加一个空格。")
        return True

    def set_mode(self, mode):
        if mode not in ("plan", "checkin", "review"):
            raise RuntimeContractError("模式为 plan / checkin / review")
        self.mode = mode
        self.notice = "训练模式：" + mode

    def discard_submission(self):
        self.last_submission = None
        self.info("仅放弃本地记录", "没有取消数据库中的事件，请用 /events 核对。")
        return True

    def accept(self):
        try:
            if self.wizard and self.wizard.get("saving") and self.editor.text in ("/help", "/quit"):
                command = self.editor.text
                self.replace_draft("")
                self.execute(command)
                return
            if self.busy and self.editor.text.startswith("/") and self.editor.text not in ("/help", "/quit") and (not self.wizard or self.wizard.get("saving")):
                self.defer_command(self.editor.text)
                return
            if self.busy == "读取定时任务" and not self.pending and not self.wizard and not self.picker and self.editor.text.strip():
                self.defer_command(self.editor.text)
                return
            if self.wizard:
                self.accept_wizard()
            elif self.picker and not self.editor.text.startswith("/"):
                self.select()
            else:
                text = self.editor.text
                if not text.strip():
                    return
                # Clear before dispatch so selectors/wizards can safely fill the same editor.
                self.editor.buffer.append_to_history()
                self.replace_draft("")
                if self.execute(text) is False:
                    self.replace_draft(text)
        except RuntimeContractError as error:
            if not self.wizard and 'text' in locals() and not self.editor.text:
                self.replace_draft(text)
            self.info("请检查输入", str(error))
        self.draw()

    def escape(self):
        if self.deferred:
            self.deferred = None
            self.notice = "已取消等待执行的界面命令；已提交操作不会撤回"
        if self.wizard:
            saving = self.wizard.get("saving")
            self.replace_draft(self.wizard["draft"])
            self.wizard = None
            if saving:
                self.notice = "已提交操作仍会继续，请核对保存结果"
        elif self.picker:
            self.replace_draft(self.picker["draft"])
            self.picker = None
        else:
            self.editor.buffer.cancel_completion()
        self.invalidate()

    def picker_text(self):
        if not self.picker:
            return []
        rows = self.filtered_items()
        selected = min(self.picker["selected"], max(0, len(rows) - 1))
        start = max(0, selected - 5)
        output = [("class:picker.title", visible(self.picker["title"]) + "\n")]
        for index, item in enumerate(rows[start:start + 7], start):
            output.append(("class:picker.selected" if index == selected else "class:picker", ("❯ " if index == selected else "  ") + visible(item[0]).replace("\n", " ") + "\n"))
        if not rows:
            output.append(("class:muted", "无匹配项；Esc 返回"))
        return output

    def composer_title(self):
        if self.wizard:
            field = self.wizard["steps"][self.wizard["index"]]
            return visible(self.wizard["title"] + " · " + field[1] + f" · {len(self.editor.text.encode('utf-8'))}/{field[3]} bytes")
        return "输入请求或 / 命令" if not self.picker else "筛选选项 · ↑↓ Enter 确认 · Esc 返回"

    def status_text(self):
        state = self.busy or self.notice
        if self.pending:
            state += " · 请求 " + self.pending["status"]
            elapsed = max(0, int(time.monotonic() - self.pending_since)) if self.pending_since is not None else 0
            phase = "等待模型或工具完成" if self.pending["status"] == "running" else "等待任务完成"
            state += f" · 已等待 {elapsed // 60}分{elapsed % 60:02d}秒 · {phase}，完成后显示回复"
        if self.deferred:
            state += " · " + self.deferred["text"].split()[0] + " 已排队"
        return visible(f"{self.workflow or '未选 workflow'} / {self.task}  ·  {self.mode}  ·  {state}").replace("\n", " ")

    def build(self):
        bindings = KeyBindings()
        @bindings.add("enter", eager=True)
        def enter(event):
            completion = self.editor.buffer.complete_state
            if completion and completion.current_completion:
                self.editor.buffer.apply_completion(completion.current_completion)
            self.accept()
        @bindings.add("c-j", eager=True)
        @bindings.add("escape", "enter", eager=True)
        def newline(event):
            self.editor.buffer.insert_text("\n")
        @bindings.add("escape", eager=False)
        def escape(event):
            self.escape()
        @bindings.add("tab", eager=True)
        def tab(event):
            if self.picker:
                self.picker["selected"] = min(self.picker["selected"] + 1, max(0, len(self.filtered_items()) - 1))
            elif self.wizard:
                self.accept()
            elif self.editor.buffer.complete_state:
                completion = self.editor.buffer.complete_state
                if completion.completions:
                    self.editor.buffer.apply_completion(completion.current_completion or completion.completions[0])
            else:
                self.editor.buffer.start_completion(select_first=True)
        for key, delta in (("up", -1), ("down", 1)):
            @bindings.add(key, filter=Condition(lambda: self.picker is not None), eager=True)
            def move(event, delta=delta):
                self.picker["selected"] = min(max(0, self.picker["selected"] + delta), max(0, len(self.filtered_items()) - 1))
        @bindings.add("c-c", eager=True)
        def interrupt(event):
            if self.picker or self.wizard:
                self.escape()
            elif self.editor.text:
                self.replace_draft("")
                self.notice = "草稿已清空；后台任务继续"
            elif self.deferred:
                self.deferred = None
                self.notice = "等待命令已取消；已提交任务继续"
            elif time.monotonic() - self.last_interrupt < 1.5:
                event.app.exit()
            else:
                self.notice = "再按一次 Ctrl+C 退出；不会停止后台任务"
                self.last_interrupt = time.monotonic()
        @bindings.add("c-d", filter=Condition(lambda: not self.editor.text), eager=True)
        def eof(event):
            if self.picker or self.wizard:
                self.escape()
            else:
                event.app.exit()
        for key, delta in (("pageup", -10), ("pagedown", 10)):
            @bindings.add(key, eager=True)
            def scroll(event, delta=delta):
                buffer = self.transcript.buffer
                row = max(0, min(buffer.document.line_count - 1, buffer.document.cursor_position_row + delta))
                buffer.cursor_position = buffer.document.translate_row_col_to_index(row, 0)
                self.follow_tail = row == buffer.document.line_count - 1
        @bindings.add("c-l", eager=True)
        def redraw(event):
            event.app.renderer.clear()
        @bindings.add("s-tab", eager=True)
        def mode(event):
            if self.wizard:
                wizard = self.wizard
                if not wizard.get("saving") and wizard["index"] > 0:
                    wizard["answers"][wizard["steps"][wizard["index"]][0]] = self.editor.text
                    wizard["index"] -= 1
                    field = wizard["steps"][wizard["index"]]
                    self.replace_draft(wizard["answers"].get(field[0], field[2]))
            else:
                modes = ("plan", "checkin", "review")
                self.set_mode(modes[(modes.index(self.mode) + 1) % 3])
        root = FloatContainer(HSplit([
            Window(FormattedTextControl(lambda: [("class:title", f"Paveway {__version__} · GPT · " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))]), height=1),
            self.transcript,
            Window(FormattedTextControl(self.timer_text), height=Dimension(min=1, max=4), style="class:muted"),
            ConditionalContainer(Window(FormattedTextControl(self.picker_text), height=Dimension(min=2, max=9)), Condition(lambda: self.picker is not None)),
            Window(FormattedTextControl(self.status_text), height=1, style="class:status"),
            Frame(self.editor, title=self.composer_title, style="class:composer"),
            Window(FormattedTextControl("Enter 发送 · Ctrl+J/Alt+Enter 换行 · / 命令 · Tab 补全 · Ctrl+D 空输入退出"), height=1, style="class:muted"),
        ]), floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1))])
        self.app = Application(layout=Layout(root, focused_element=self.editor), key_bindings=bindings, full_screen=True,
            input=self.input, output=self.output, mouse_support=False, style=Style.from_dict({
                "title": "bold #88c0d0", "status": "#a3be8c", "muted": "#888888", "composer": "#88c0d0",
                "picker.title": "bold #88c0d0", "picker.selected": "bg:#304050 #ffffff",
                "completion-menu.completion.current": "bg:#304050 #ffffff",
            }), min_redraw_interval=0.05)
        # Do not wait a second for unsupported cursor-position reports (tmux/PTY terminals).
        if hasattr(self.app.output, "enable_cpr"):
            self.app.output.enable_cpr = False
        self.app.ttimeoutlen = 0.05
        self.app.timeoutlen = 0.3
        self.draw()
        return self.app

    async def tick(self):
        while True:
            self.collect()
            self.drain_deferred()
            if not self.busy and self.restore_pending:
                self.restore_pending = False
                self.restore()
            self.poll()
            self.poll_sync()
            self.reconcile_history()
            self.poll_timers()
            self.invalidate()
            await asyncio.sleep(0.15)

    def run(self):
        app = self.build()
        def start():
            self.bootstrap()
            app.create_background_task(self.tick())
        try:
            app.run(pre_run=start)
        finally:
            self.client.close()
            if self.worker:
                self.worker.join(timeout=2)


def run(config_path, workflow=None, task="training"):
    if workflow is not None:
        validate_id(workflow)
    validate_id(task)
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM", "dumb") == "dumb":
        raise RuntimeContractError("ui 需要交互终端和有效 TERM；脚本请使用 ask/status 等 CLI 命令")
    try:
        locale.setlocale(locale.LC_ALL, "")
        if locale.nl_langinfo(locale.CODESET).replace("-", "").lower() != "utf8":
            raise RuntimeContractError("ui 需要 UTF-8 locale，例如 LC_ALL=C.UTF-8")
        TerminalUI(CliClient(config_path), workflow, task).run()
    except (EOFError, KeyboardInterrupt):
        pass
    except locale.Error:
        raise RuntimeContractError("无法初始化 UTF-8 locale，请检查终端环境") from None
    return 0
