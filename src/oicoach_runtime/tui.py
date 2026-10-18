"""Terminal presentation only: every runtime operation goes through the public CLI."""

import curses
import json
import locale
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from . import __version__
from .errors import RuntimeContractError
from .models import validate_id

PAGES = ("概览", "对话", "事件", "记忆", "历史", "Workflow", "服务", "恢复")
TERMINAL = {"succeeded", "failed", "blocked", "uncertain", "dead_letter", "cancelled"}
HELP = """1–8/Tab 切换页；r 刷新；s 切换 workflow/task；j/k 选择；Enter 查看/发送；n 新建。
记忆：w 切换共享/task，e 修改，d 删除。历史：f 搜索，a 原文。
事件：n 安排，d 取消未派发事件。恢复页只查询，不自动重播 uncertain。
>/< 翻查询页；[/] 或 PgUp/PgDn 滚动详情和对话。
对话：m 切换 plan/checkin/review；Enter 输入，Ctrl+D 提交；v 用原标识核对上次提交。
若提交未确认，先用 v 核对；z 可显式放弃本地提交记录，但不会取消已入库事件。
输入框支持中文、多行、方向键、Home/End、Backspace/Delete；Ctrl+U 清空，Esc 取消。
概览：i 初始化配置，c 诊断。服务：o 安装，b 启动，x 停止，l 日志。
回复为最终文本，没有实时 token/tool 流。退出 q/Ctrl+C 只关闭界面，持久化任务继续。
不会自动启用服务或设置 linger。按任意键关闭帮助。"""
HINTS = ("r 状态 · c 诊断 · i 初始化配置", "Enter/n 输入 · m 切换模式 · v 核对提交 · r 读取最近对话",
         "Enter 详情/回复 · n 安排 · d 取消 · >/< 翻页（所有任务）",
         "Enter 查看 · n 新建 · e 修改 · d 删除 · w 切换共享/task · f 搜索 · >/< 翻页",
         "Enter 输入/回复 · a 会话原文 · f 搜索 · >/< 翻页（当前 workflow）",
         "Enter 选用 · n 创建 coach workflow", "o 安装 · b 启动 · x 停止 · l 日志 · r 状态",
         "Enter 查看待核对事件 · >/< 翻页；不自动重播")


class CliFailure(RuntimeContractError):
    def __init__(self, message, code="cli_failed"):
        super().__init__(message)
        self.code = code


class CliClient:
    """Owned CLI process groups only; closing never stops the separate daemon."""
    def __init__(self, config_path):
        self.config_path = Path(config_path).absolute()
        self.lock = threading.Lock()
        self.closed = False
        self.process = None

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None:
                self._stop(self.process)

    def call(self, *arguments, text_files=None):
        # Draft bodies never appear in argv or reusable state files.
        with tempfile.TemporaryDirectory(prefix="paveway-ui-") as directory:
            values = {"--workflow", "--task", "--dedupe-key", "--mode", "--query", "--key", "--title", "--kind",
                      "--expected-version", "--source-event", "--expires-at", "--workspace", "--at", "--reason", "--offset"}
            normalized, iterator = [], iter(map(str, arguments))
            for argument in iterator:
                if argument in values:
                    try:
                        normalized.append(argument + "=" + next(iterator))
                    except StopIteration:
                        raise CliFailure("缺少 CLI 参数值") from None
                else:
                    normalized.append(argument)
            argv = [sys.executable, "-m", "oicoach_runtime", *normalized]
            for index, (option, content) in enumerate((text_files or {}).items()):
                path = Path(directory) / str(index)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                argv.extend([option, str(path)])
            argv.extend(["--config", str(self.config_path), "--json"])
            with self.lock:
                if self.closed:
                    raise CliFailure("界面已经关闭")
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
                self.process = process
            try:
                try:
                    stdout, stderr = process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                    process.communicate()
                    raise CliFailure("CLI 响应超时；写入结果可能已保存，请先刷新核对，勿重复提交", "cli_timeout") from None
            finally:
                with self.lock:
                    if self.process is process:
                        self.process = None
            data = stderr if process.returncode and stderr else stdout
            try:
                if len(data) > 1048576:
                    raise ValueError()
                report = json.loads(data)
                if not isinstance(report, dict) or report.get("api_version") != 1:
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                raise CliFailure("CLI 返回了无法解析的结果；请使用独立命令检查配置/运行状态") from None
            if "error" in report:
                error = report["error"]
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    raise CliFailure(error["message"], str(error.get("code", "cli_failed")))
                raise CliFailure("CLI 返回了无效错误")
            if process.returncode and report.get("ok") is not False:
                raise CliFailure("检查未通过：" + json.dumps(report, ensure_ascii=False))
            return report.get("result", report)


def visible(value):
    """Prevent model/tool/user text from injecting terminal escape or bidi controls."""
    return "".join(c if c == "\n" or not unicodedata.category(c).startswith("C") else " " for c in str(value)).replace("\t", "    ")


def width(character):
    return 0 if unicodedata.combining(character) else (2 if unicodedata.east_asian_width(character) in ("W", "F") else 1)


def lines(value, columns):
    columns = max(2, columns)
    result, current, size = [], "", 0
    for c in visible(value):
        if c == "\n":
            result.append(current)
            current, size = "", 0
        else:
            w = width(c)
            if size + w > columns:
                result.append(current)
                current, size = "", 0
            current += c
            size += w
    result.append(current)
    return result


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


class TerminalUI:
    def __init__(self, screen, client, workflow=None, task="training"):
        self.screen, self.client = screen, client
        self.workflow, self.task = workflow, task
        self.page = 0 if workflow else 5
        self.mode = "plan"
        self.shared = True
        self.query = ""
        self.offset = 0
        self.rows = []
        self.selected = 0
        self.detail = "按 r 刷新。服务离线时可到“服务”页启动已安装的服务。"
        self.scroll = 0
        self.notice = ""
        self.busy = ""
        self.results = queue.Queue()
        self.worker = None
        self.pending = None
        self.last_submission = None
        self.help_open = False
        self.refresh_pending = False
        self.archive = None
        self.last_poll = 0
        self.chat = {}
        self.screen.timeout(100)
        self.screen.keypad(True)

    @property
    def scope(self):
        return self.workflow, self.task

    def put(self, y, value, attr=0):
        height, columns = self.screen.getmaxyx()
        if 0 <= y < height:
            try:
                self.screen.addstr(y, 0, lines(value, max(2, columns - 1))[0], attr)
            except curses.error:
                pass  # Resize and bottom-right writes are normal curses races.

    def form(self, label, initial="", multiline=False, maximum=32768):
        value, cursor = initial, len(initial)
        while True:
            height, columns = self.screen.getmaxyx()
            self.screen.erase()
            self.put(0, label, curses.A_BOLD)
            self.put(1, "Ctrl+D 确认 · Esc 取消" + (" · Enter 换行" if multiline else " · Enter 确认"))
            displayed = lines(value[:cursor] + "▏" + value[cursor:], max(2, columns - 2))
            for y, text in enumerate(displayed[-max(1, height - 4):], 3):
                self.put(y, text)
            self.screen.refresh()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if key in ("\x1b", "\x03"):
                return None
            if key == "\x04" or (not multiline and key in ("\n", "\r", curses.KEY_ENTER)):
                return value
            if key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(value)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if cursor:
                    value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
            elif key == curses.KEY_DC:
                value = value[:cursor] + value[cursor + 1:]
            elif key == "\x15":
                value, cursor = "", 0
            elif isinstance(key, str) and (key.isprintable() or key in ("\n", "\r")):
                key = "\n" if key == "\r" else key
                candidate = value[:cursor] + key + value[cursor:]
                if len(candidate.encode("utf-8")) <= maximum:
                    value, cursor = candidate, cursor + len(key)

    def submit(self, label, operation, done):
        if self.busy:
            self.notice = "正在执行 CLI 查询，请稍候"
            return False
        self.busy = label
        def worker():
            try:
                value, error = operation(), None
            except (RuntimeContractError, OSError) as failure:
                value, error = None, f"{getattr(failure, 'code', 'io_error')}: {failure}"
            except Exception:
                value, error = None, "界面操作失败，请用独立 CLI 检查；没有自动重试写入"
            self.results.put((done, value, error))
        self.worker = threading.Thread(target=worker, name="paveway-ui-cli", daemon=True)
        self.worker.start()
        return True

    def collect(self):
        try:
            done, value, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = ""
        if error:
            self.notice = error
            if self.last_submission and not self.last_submission.get("confirmed"):
                self.notice += " · v 用原标识核对提交：" + self.last_submission["key"]
        else:
            try:
                done(value)
            except RuntimeContractError as failure:
                self.notice = str(failure)
        if self.refresh_pending and not self.busy:
            self.refresh_pending = False
            try:
                self.refresh()
            except RuntimeContractError as failure:
                self.notice = str(failure)

    def scope_args(self, task=True):
        if not self.workflow:
            raise RuntimeContractError("请先到 Workflow 页选择或创建 workflow")
        return ["--workflow", self.workflow] + (["--task", self.task] if task else [])

    def set_data(self, data, page, scope):
        if self.page != page or self.scope != scope:
            return
        self.rows = data if isinstance(data, list) else []
        self.selected = min(self.selected, max(0, len(self.rows) - 1))
        self.detail = "" if self.rows else pretty(data)
        self.scroll = 0

    def refresh(self):
        page, scope = self.page, self.scope
        if page == 4:
            self.archive = None
        if page == 1:
            args = ["history", "search", *self.scope_args(), "--offset", "0"]
            def read_chat():
                recent = self.client.call(*args)[:5]
                return [self.client.call("history", "show", row["run_id"]) for row in reversed(recent)]
            def restored(records):
                if self.scope != scope:
                    return
                messages = []
                for record in records:
                    event = record["event"]
                    message = event["payload"].get("message", event["reason"])
                    output = record["output"]
                    messages.extend([f"你：{message}", f"教练 [{record['run']['status']}]：\n{output.get('text') or '尚无已保存回复'}"])
                    if output.get("truncated"):
                        messages.append("[回复已截断，可在历史页查询会话原文]")
                if not self.pending or (self.pending["workflow_id"], self.pending["task_id"]) != scope:
                    self.chat[scope] = messages
                self.notice = "已读取当前任务最近 5 个 run；更早记录请到历史页"
            self.submit("读取对话", read_chat, restored)
            return
        if page == 0:
            args = ["status"]
        elif page == 2:
            args = ["events", "list", "--offset", str(self.offset)]
        elif page == 3:
            args = ["memory", "list", *self.scope_args(not self.shared), "--query", self.query, "--offset", str(self.offset)]
        elif page == 4:
            args = ["history", "search", *self.scope_args(False), "--query", self.query, "--offset", str(self.offset)]
        elif page == 5:
            args = ["workflow", "list"]
        elif page == 6:
            args = ["service", "status"]
        else:
            args = ["recover", "list", "--offset", str(self.offset)]
        def loaded(result):
            if page == 5:
                result = result["workflows"]
            self.set_data(result, page, scope)
        self.submit("刷新 " + PAGES[page], lambda: self.client.call(*args), loaded)

    def speak(self):
        args = self.scope_args()
        if self.last_submission and not self.last_submission.get("confirmed"):
            self.notice = "上次提交尚未确认：v 用相同标识核对，或 z 显式放弃本地记录；不会自动重复提交"
            return
        if self.pending:
            self.notice = "本界面已有待完成请求，请先等待或到事件页查询"
            return
        message = self.form("发送给教练 · " + self.mode, multiline=True)
        if message is None or not message.strip():
            return
        dedupe = "ui-" + uuid.uuid4().hex
        scope = self.scope
        self.chat.setdefault(scope, []).append("你：" + message)
        self.last_submission = {"args": args, "mode": self.mode, "message": message, "key": dedupe, "confirmed": False}
        self.resubmit()

    def resubmit(self):
        if not self.last_submission:
            self.notice = "本次界面尚无提交记录；旧事件请到事件页查询"
            return
        submission = dict(self.last_submission)
        def queued(event):
            self.pending = event
            self.last_submission["confirmed"] = True
            self.notice = "已持久提交，event=" + event["event_id"]
            self.last_poll = 0
        def send():
            return self.client.call("ask", *submission["args"], "--mode", submission["mode"], "--dedupe-key", submission["key"],
                                    text_files={"--message-file": submission["message"]})
        # Keep the key available even when a CLI timeout leaves the result unknown.
        self.notice = "提交标识：" + submission["key"]
        self.submit("提交请求 " + submission["key"], send, queued)

    def poll(self):
        if not self.pending or self.busy or time.monotonic() - self.last_poll < 2:
            return
        self.last_poll = time.monotonic()
        event_id = self.pending["event_id"]
        def check():
            event = self.client.call("events", "show", event_id)
            output = self.client.call("runs", "output", event["run_id"]) if event["status"] in TERMINAL and event["run_id"] else None
            return event, output
        def checked(result):
            event, output = result
            self.pending = event
            if event["status"] in TERMINAL:
                scope = event["workflow_id"], event["task_id"]
                text = output.get("text") if output else None
                self.chat.setdefault(scope, []).append(f"教练 [{event['status']}]：\n" + (text or "没有已保存的最终回复；请到事件页查看详情"))
                if output and output.get("truncated"):
                    self.chat[scope].append("[回复已截断，可到历史页查询原文]")
                self.pending = None
                self.notice = "请求完成：" + event["status"] + " · " + event_id
                self.scroll = 0
        self.submit("检查请求进度", check, checked)

    def enter(self):
        if self.page == 1:
            self.speak()
            return
        if not self.rows:
            return
        row, page, scope = self.rows[self.selected], self.page, self.scope
        if page == 5:
            self.workflow = row["id"]
            self.page, self.rows, self.detail, self.offset = 1, [], "", 0
            self.notice = "已选择 " + self.workflow
            self.refresh()
            return
        if page in (2, 7):
            args = ["events", "show", row["event_id"]]
            def event_detail():
                event = self.client.call(*args)
                return {"event": event, "output": self.client.call("runs", "output", event["run_id"]) if event["run_id"] else None}
            operation = event_detail
        elif page == 3:
            operation = lambda: self.client.call("memory", "show", *self.scope_args(not self.shared), "--key", row["key"])
        elif page == 4:
            self.archive = None
            operation = lambda: self.client.call("history", "show", row["run_id"])
        else:
            return
        def loaded(value):
            if self.page == page and self.scope == scope:
                self.detail, self.scroll = pretty(value), 0
        self.submit("读取详情", operation, loaded)

    def memory_form(self, note=None):
        args = self.scope_args(not self.shared)
        key = note["key"] if note else self.form("笔记 key（ASCII ID）", maximum=128)
        if not key:
            return
        validate_id(key)
        title = self.form("笔记标题", note["title"] if note else "", maximum=256)
        if title is None:
            return
        content = self.form("笔记正文", note["content"] if note else "", multiline=True, maximum=4096)
        if content is None:
            return
        kind = self.form("类型：fact / preference / summary / hypothesis", note["kind"] if note else "fact", maximum=32)
        if kind is None:
            return
        pinned = self.form("固定优先加载？yes / no", "yes" if note and note["pinned"] else "no", maximum=8)
        if pinned is None:
            return
        if pinned not in ("yes", "no"):
            raise RuntimeContractError("请输入 yes 或 no")
        expiry = self.form("有效期 RFC3339（留空为长期）", note.get("expires_at") or "" if note else "", maximum=64)
        if expiry is None:
            return
        extra = (["--pinned"] if pinned == "yes" else []) + (["--expires-at", expiry] if expiry else [])
        if note and note.get("source_event_id"):
            extra += ["--source-event", note["source_event_id"]]
        self.submit("保存笔记", lambda: self.client.call("memory", "put", *args, "--key", key,
            "--title", title, "--kind", kind, "--expected-version", str(note["version"] if note else 0),
            *extra, text_files={"--content-file": content}), self.saved)

    def saved(self, value):
        self.notice = "操作完成"
        self.refresh()

    def read_archive(self, task, offset=0):
        scope = self.scope
        snapshot = {"task": task, "query": self.query, "offset": offset}
        self.archive = snapshot
        def loaded(value):
            if self.page == 4 and self.scope == scope and self.archive == snapshot:
                self.detail, self.scroll = pretty(value), 0
                self.notice = f"会话原文 {task} · offset={offset} · >/< 翻原文页，r 返回搜索列表"
        self.submit("读取会话原文", lambda: self.client.call("history", "messages", "--workflow", scope[0],
            "--task", task, "--query", snapshot["query"], "--offset", str(offset)), loaded)

    def create(self):
        if self.page == 1:
            self.speak()
        elif self.page == 3:
            self.memory_form()
        elif self.page == 5:
            identifier = self.form("新 workflow ID", "coach", maximum=128)
            if not identifier:
                return
            validate_id(identifier)
            workspace = self.form("已存在的 workspace 绝对路径", maximum=4096)
            if not workspace:
                return
            self.submit("创建 workflow", lambda: self.client.call("workflow", "create", identifier, "--workspace", workspace), self.saved)
        elif self.page == 2:
            args = self.scope_args()
            at = self.form("执行时间：带时区 RFC3339，或 now", "now", maximum=64)
            if not at:
                return
            message = self.form("唤醒请求内容", multiline=True)
            if not message or not message.strip():
                return
            self.submit("安排事件", lambda: self.client.call("schedule", *args, "--at", at, "--reason", "UI scheduled request",
                "--dedupe-key", "ui-" + uuid.uuid4().hex,
                text_files={"--payload-file": json.dumps({"message": message, "mode": self.mode}, ensure_ascii=False)}), self.saved)

    def delete(self):
        if not self.rows or self.page not in (2, 3):
            return
        row = self.rows[self.selected]
        identifier = row["key"] if self.page == 3 else row["event_id"]
        if self.form("确认删除笔记/取消未派发事件 " + identifier + "？输入 yes", maximum=8) != "yes":
            return
        args = ["memory", "delete", *self.scope_args(not self.shared), "--key", identifier] if self.page == 3 else ["events", "cancel", identifier]
        self.submit("删除/取消", lambda: self.client.call(*args, "--expected-version", str(row["version"])), self.saved)

    def keys(self, key):
        if self.help_open:
            if key != curses.KEY_RESIZE:
                self.help_open = False
            return True
        if key in ("q", "\x03"):
            return False
        if key in tuple(str(i) for i in range(1, 9)) or key in ("\t", curses.KEY_BTAB):
            self.page = int(key) - 1 if isinstance(key, str) and key.isdigit() else (self.page + (-1 if key == curses.KEY_BTAB else 1)) % len(PAGES)
            self.rows, self.detail, self.scroll, self.offset, self.selected = [], "", 0, 0, 0
            self.archive = None
            if not self.busy:
                self.refresh()
            else:
                self.refresh_pending = True
            return True
        if key in (curses.KEY_DOWN, "j"):
            self.selected = min(max(0, len(self.rows) - 1), self.selected + 1)
        elif key in (curses.KEY_UP, "k"):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_NPAGE, "]"):
            self.scroll = max(0, self.scroll - 5) if self.page == 1 else self.scroll + 5
        elif key in (curses.KEY_PPAGE, "["):
            self.scroll = self.scroll + 5 if self.page == 1 else max(0, self.scroll - 5)
        elif key == "?":
            self.help_open = True
        elif self.busy:
            self.notice = "CLI 操作进行中，可切页或退出，请稍后再操作"
        elif key == "r":
            self.refresh()
        elif key == "s":
            workflow = self.form("workflow ID", self.workflow or "", maximum=128)
            if workflow is None:
                return True
            validate_id(workflow)
            task = self.form("task ID（新 ID 新会话，相同 ID 接续）", self.task, maximum=128)
            if task is None:
                return True
            validate_id(task)
            self.workflow, self.task = workflow, task
            self.rows, self.detail, self.offset = [], "", 0
            self.refresh()
        elif key == "m" and self.page == 1:
            modes = ("plan", "checkin", "review")
            self.mode = modes[(modes.index(self.mode) + 1) % 3]
        elif key == "v" and self.page == 1:
            if self.pending:
                self.notice = "已确认的请求仍在等待，请到事件页查看；无需重新提交"
            else:
                self.resubmit()
        elif key == "z" and self.page == 1 and self.last_submission and not self.last_submission.get("confirmed"):
            if self.form("原事件可能已入库；放弃本地记录不会取消事件。确认请输入 yes", maximum=8) == "yes":
                self.last_submission = None
                self.notice = "仅清除了本地记录；请到事件页核对可能已入库的任务"
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.enter()
        elif key == "n":
            self.create()
        elif key == "d":
            self.delete()
        elif key == "w" and self.page == 3:
            self.shared, self.offset = not self.shared, 0
            self.refresh()
        elif key == "e" and self.page == 3 and self.rows:
            row = self.rows[self.selected]
            scope = self.scope
            self.submit("读取笔记版本", lambda: self.client.call("memory", "show", *self.scope_args(not self.shared),
                "--key", row["key"]), lambda note: self.memory_form(note) if self.page == 3 and self.scope == scope else None)
        elif key == "f" and self.page in (3, 4):
            query = self.form("字面搜索（留空显示全部）", self.query, maximum=256)
            if query is not None:
                self.query, self.offset = query, 0
                self.refresh()
        elif key in (">", "<") and self.page in (2, 3, 4, 7):
            if self.page == 4 and self.archive:
                offset = max(0, self.archive["offset"] + (20 if key == ">" else -20))
                self.read_archive(self.archive["task"], offset)
                return True
            self.offset = max(0, self.offset + (1 if key == ">" else -1) * (20 if self.page in (3, 4) else 100))
            self.selected = 0
            self.refresh()
        elif key == "a" and self.page == 4 and self.rows:
            row = self.rows[self.selected]
            self.read_archive(row["task_id"])
        elif self.page == 0 and key in ("i", "c"):
            self.submit("初始化" if key == "i" else "诊断", lambda: self.client.call("init" if key == "i" else "doctor"),
                        lambda value: setattr(self, "detail", pretty(value)))
        elif self.page == 6 and key in ("o", "b", "x", "l"):
            action = {"o": "install", "b": "start", "x": "stop", "l": "logs"}[key]
            if key == "x" and self.form("停止服务会中断活动回合；确认请输入 yes", maximum=8) != "yes":
                return True
            self.submit("服务 " + action, lambda: self.client.call("service", action),
                        lambda value: setattr(self, "detail", pretty(value)))
        return True

    def draw(self):
        self.screen.erase()
        height, columns = self.screen.getmaxyx()
        self.put(0, f"Paveway {__version__}  {self.workflow or '(未选 workflow)'} / {self.task}  {self.mode}", curses.A_BOLD)
        self.put(1, " ".join(f"{i + 1}:{name}" + ("*" if i == self.page else "") for i, name in enumerate(PAGES)))
        self.put(2, (self.busy or self.notice or "r 刷新 · ? 帮助") + (" · 等待 " + self.pending["status"] if self.pending else ""))
        self.put(3, HINTS[self.page])
        if height < 12 or columns < 60:
            self.put(4, "终端过小，请调整到至少 60 列 × 12 行；q 退出")
            self.screen.refresh()
            return
        self.put(height - 1, "Enter 查看/发送 · n 新建 · r 刷新 · s 切任务 · ? 帮助 · q 退出")
        body = []
        if self.help_open:
            body = lines(HELP, columns - 2)
        elif self.page == 1:
            body = lines("\n\n".join(self.chat.get(self.scope, [])) or "按 Enter 输入请求，Ctrl+D 提交。m 切换安排/反馈/复盘。", columns - 2)
        elif self.rows:
            body.append(("共享笔记" if self.shared else "当前 task 笔记") + " · w 切换" if self.page == 3 else "选择后 Enter 查看详情")
            start = max(0, self.selected - 5)
            for index, row in enumerate(self.rows[start:start + 8], start):
                if self.page in (2, 7):
                    label = f"{row['event_id'][:8]} | {row['status']} | {row.get('workflow_id', '')}/{row.get('task_id', '')}"
                elif self.page == 3:
                    label = f"{row['key']} | v{row['version']} | {'* ' if row['pinned'] else ''}{row['title']}"
                elif self.page == 4:
                    label = f"{row['status']} | {row['task_id']} | {row['started_at']} | {row['snippet']}"
                else:
                    label = f"{row['id']} | v{row['version']} | {row['workspace']}"
                body.append(("> " if index == self.selected else "  ") + lines(label, columns - 4)[0])
            body.extend(["", "详情（[/] 滚动，>/< 翻查询页）：", *lines(self.detail, columns - 2)[self.scroll:]])
        else:
            body = lines(self.detail or "暂无记录。n 新建，r 刷新，? 查看操作说明。", columns - 2)
        if self.page == 1 and not self.help_open:
            end = max(0, len(body) - self.scroll)
            body = body[max(0, end - (height - 5)):end]
        elif not self.rows and not self.help_open:
            self.scroll = min(self.scroll, max(0, len(body) - 1))
            body = body[self.scroll:]
        for y, text in enumerate(body[:height - 5], 4):
            self.put(y, text)
        self.screen.refresh()

    def run(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            self.refresh()
            while True:
                self.collect()
                self.poll()
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                try:
                    if not self.keys(key):
                        break
                except RuntimeContractError as error:
                    self.notice = str(error)
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
            raise RuntimeContractError("ui 需要 UTF-8 locale，例如 LC_ALL=C.UTF-8；请先调整终端环境")
        curses.wrapper(lambda screen: TerminalUI(screen, CliClient(config_path), workflow, task).run())
    except (curses.error, locale.Error):
        raise RuntimeContractError("无法初始化终端；请检查 TERM、UTF-8 locale 和终端大小") from None
    except KeyboardInterrupt:
        pass
    return 0
