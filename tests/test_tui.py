import curses
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.models import utc_text
from oicoach_runtime.tui import CliClient, CliFailure, TerminalUI, lines, visible, width, run
from oicoach_runtime.workflow_setup import create


class Screen:
    def __init__(self, keys=()):
        self.keys = iter(keys)
        self.text = []
    def timeout(self, *_): pass
    def keypad(self, *_): pass
    def erase(self): self.text = []
    def refresh(self): pass
    def getmaxyx(self): return 30, 100
    def addstr(self, y, x, value, *_): self.text.append(value)
    def get_wch(self): return next(self.keys)


def collect(ui):
    deadline = time.monotonic() + 5
    while ui.busy and time.monotonic() < deadline:
        ui.collect()
        time.sleep(0.01)
    if ui.busy:
        raise AssertionError("UI worker did not finish")


class RenderingTests(unittest.TestCase):
    def test_non_utf8_locale_is_rejected_before_curses_initialization(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch.dict(os.environ, {"TERM": "xterm"}), patch("locale.setlocale"), \
             patch("locale.nl_langinfo", return_value="ANSI_X3.4-1968"), patch("curses.wrapper") as wrapper:
            with self.assertRaises(RuntimeContractError) as failure:
                run(Path("/tmp/ui-config.toml"))
            self.assertIn("UTF-8", str(failure.exception))
            wrapper.assert_not_called()

    def test_wide_unicode_and_terminal_controls(self):
        safe = visible("中文\x1b[2J\x00\u202e结束\n下一行")
        self.assertNotIn("\x1b", safe)
        self.assertNotIn("\u202e", safe)
        wrapped = lines("中文ABC\ne\u0301汉字", 5)
        self.assertTrue(all(sum(width(c) for c in line) <= 5 for line in wrapped))
        self.assertIn("中文", wrapped[0])

    def test_multiline_unicode_composer_cursor_edit_cancel_and_help(self):
        ui = TerminalUI(Screen(["中", "文", curses.KEY_LEFT, "新", curses.KEY_END, "\n", "好", "\x04"]), None)
        self.assertEqual(ui.form("输入", multiline=True), "中新文\n好")
        ui.screen = Screen(["ignored", "\x1b"])
        self.assertIsNone(ui.form("取消"))
        ui.page = 1
        ui.keys("?")
        ui.draw()
        self.assertTrue(any("不自动重播" in value for value in ui.screen.text))
        ui.keys("q")
        self.assertFalse(ui.help_open)
        self.assertFalse(ui.keys("q"))

    def test_unknown_submit_is_not_repeated_with_a_new_key(self):
        class Client:
            def __init__(self): self.calls = []
            def call(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                if args[0] == "events":
                    return {"event_id": "same-event", "workflow_id": "echo", "task_id": "task", "status": "succeeded", "run_id": "run"}
                if args[0] == "runs":
                    return {"status": "available", "text": "model reply", "truncated": True}
                if len(self.calls) == 1:
                    raise CliFailure("timeout", "cli_timeout")
                return {"event_id": "same-event", "workflow_id": "echo", "task_id": "task", "status": "queued"}
        client = Client()
        ui = TerminalUI(Screen(), client, "echo", "task")
        ui.page = 1
        with patch.object(ui, "form", return_value="中文 $(echo literal) `id`"):
            ui.speak()
        collect(ui)
        self.assertIn("v 用原标识", ui.notice)
        ui.speak()  # Must not open another composer or create a second key.
        self.assertEqual(len(client.calls), 1)
        ui.keys("v")
        collect(ui)
        self.assertEqual(client.calls[0], client.calls[1])
        self.assertNotIn("中文", " ".join(client.calls[0][0]))
        self.assertEqual(ui.pending["event_id"], "same-event")
        ui.poll()
        collect(ui)
        self.assertIsNone(ui.pending)
        self.assertIn("model reply", "\n".join(ui.chat[ui.scope]))
        self.assertIn("截断", "\n".join(ui.chat[ui.scope]))

    def test_archive_paging_keeps_selected_task_and_query(self):
        calls = []
        class Client:
            def call(self, *args):
                calls.append(args)
                return {"messages": [], "archive_mode": "all_branches", "has_more": False}
        ui = TerminalUI(Screen(), Client(), "coach", "today")
        ui.page, ui.query = 4, "线性基"
        ui.rows = [{"task_id": "yesterday", "run_id": "old-run"}]
        ui.keys("a")
        collect(ui)
        ui.keys(">")
        collect(ui)
        self.assertEqual(calls[1][calls[1].index("--task") + 1], "yesterday")
        self.assertEqual(calls[1][calls[1].index("--query") + 1], "线性基")
        self.assertEqual(calls[1][calls[1].index("--offset") + 1], "20")

    def test_memory_edit_preserves_scope_source_attributes_and_cas_version(self):
        calls = []
        class Client:
            def call(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {}
        ui = TerminalUI(Screen(), Client(), "coach", "training")
        ui.shared = False
        note = {"key": "progress", "title": "旧标题", "content": "旧摘要", "kind": "summary",
                "pinned": True, "expires_at": None, "source_event_id": "original-event", "version": 7}
        with patch.object(ui, "form", side_effect=["更正", "口胡未编码", "summary", "yes", ""]), patch.object(ui, "saved"):
            ui.memory_form(note)
            collect(ui)
        args, kwargs = calls[0]
        self.assertEqual(args[args.index("--task") + 1], "training")
        self.assertEqual(args[args.index("--expected-version") + 1], "7")
        self.assertEqual(args[args.index("--source-event") + 1], "original-event")
        self.assertIn("--pinned", args)
        self.assertEqual(kwargs["text_files"]["--content-file"], "口胡未编码")

    def test_page_switch_drops_stale_result_and_refreshes_new_page(self):
        release = threading.Event()
        calls = []
        class Client:
            def call(self, *args):
                calls.append(args)
                if args[0] == "status":
                    release.wait(2)
                    return {"daemon": "old-page"}
                return [{"event_id": "visible", "status": "queued", "workflow_id": "echo", "task_id": "task"}]
        ui = TerminalUI(Screen(), Client(), "echo", "task")
        ui.refresh()
        ui.keys("3")
        release.set()
        collect(ui)
        self.assertEqual(ui.rows[0]["event_id"], "visible")
        self.assertNotIn("old-page", ui.detail)
        self.assertEqual(calls[1][:2], ("events", "list"))


class TerminalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-ui-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(self.path)
        self.config = replace(config, server=replace(config.server, poll_interval_seconds=0.02),
            agent=replace(config.agent, executable=str(Path(__file__).with_name("fake_omp.py").absolute()),
                startup_timeout_seconds=3, ack_timeout_seconds=1, turn_timeout_seconds=10,
                settle_timeout_seconds=2, graceful_stop_seconds=1))
        self.path.write_text(config_text(self.config))
        create(self.config, "echo", self.root, "example")
        self.log = (self.root / "daemon.log").open("w")
        self.addCleanup(self.log.close)
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_daemon)
        self.client = CliClient(self.path)
        self.addCleanup(self.client.close)
        self.until(lambda: self.api("status").get("daemon") == "running")

    def stop_daemon(self):
        if self.daemon.poll() is None:
            self.daemon.terminate()
            try: self.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait(timeout=5)

    def api(self, command, args=None):
        return request(self.config.server.socket_path, command, args)

    def until(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = condition()
                if result: return result
            except RuntimeContractError:
                pass
            time.sleep(0.03)
        self.fail("Condition did not become true")

    def test_public_cli_backend_message_memory_and_duplicate_request(self):
        body = "中文 UI literal $(echo secret) `id` ${event_json}"
        event = self.client.call("ask", "--workflow", "echo", "--task", "ui-task", "--dedupe-key", "ui-idempotent",
                                 text_files={"--message-file": body})
        self.assertEqual(self.client.call("events", "show", event["event_id"])["payload"]["message"], body)
        self.until(lambda: self.api("events.show", {"event_id": event["event_id"]})["status"] == "succeeded")
        again = self.client.call("ask", "--workflow", "echo", "--task", "ui-task", "--dedupe-key", "ui-idempotent",
                                 text_files={"--message-file": body})
        self.assertEqual(again["event_id"], event["event_id"])
        self.assertEqual(len(self.api("runs.list")), 1)
        note = self.client.call("memory", "put", "--workflow", "echo", "--key", "ui-preference", "--title", "提示偏好",
                                text_files={"--content-file": "先提示"})
        self.assertEqual(note["version"], 1)
        self.assertEqual(self.client.call("memory", "put", "--workflow", "echo", "--key", "leading-hyphen",
            "--title", "--literal title", text_files={"--content-file": "literal"})["title"], "--literal title")
        with self.assertRaises(CliFailure) as failure:
            self.client.call("memory", "put", "--workflow", "echo", "--key", "ui-preference", "--title", "不覆盖",
                             text_files={"--content-file": "new"})
        self.assertEqual(failure.exception.code, "conflict")
        self.assertNotIn(body, (self.root / "daemon.log").read_text())

    def test_non_terminal_json_rejection_and_closed_client(self):
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "ui", "--config", str(self.path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("交互终端", result.stderr)
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "ui", "--json", "--config", str(self.path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "invalid_input")
        self.client.close()
        with self.assertRaises(CliFailure):
            self.client.call("status")

    def test_diagnostic_failure_remains_viewable(self):
        # No OMP model is needed; a nonexistent selected executable produces doctor ok=false.
        bad = replace(self.config, agent=replace(self.config.agent, executable="/missing-paveway-omp"))
        self.path.write_text(config_text(bad))
        report = self.client.call("doctor")
        self.assertFalse(report["ok"])
        self.assertTrue(report["checks"])

    def test_real_pty_chinese_composer_quit_keeps_daemon_and_event_alive(self):
        (self.root / "fake-mode").write_text("hold")
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        before = termios.tcgetattr(slave)
        environment = dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8")
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "ui", "--config", str(self.path),
            "--workflow", "echo", "--task", "pty-task"], stdin=slave, stdout=slave, stderr=slave,
            env=environment, start_new_session=True, close_fds=True)
        def stop():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        self.addCleanup(stop)
        output = bytearray()
        def read_until(needle, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if needle.encode() in output:
                    return
                self.assertIsNone(process.poll(), output.decode(errors="replace")[-2000:])
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 65536))
            self.fail("Terminal did not render " + needle + ": " + output.decode(errors="replace")[-1500:])
        read_until("Paveway")
        os.write(master, b"2")
        read_until("已读取当前任务最近 5")
        os.write(master, b"\n")
        read_until("Ctrl+D")
        marker = "中文终端 UI_" + uuid.uuid4().hex
        os.write(master, marker.encode() + b"\x04")
        def submitted():
            for row in self.api("events.list"):
                event = self.api("events.show", {"event_id": row["event_id"]})
                if event["payload"].get("message") == marker:
                    return event
        event = self.until(submitted)
        self.until(lambda: (self.root / "fake-entered").exists())
        # Resize down/up while a request runs, then close only the terminal frontend.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        time.sleep(0.15)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        os.write(master, b"q")
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        after = termios.tcgetattr(slave)
        mask = termios.ECHO | termios.ICANON
        self.assertEqual(before[3] & mask, after[3] & mask)
        self.assertIsNone(self.daemon.poll())
        (self.root / "fake-release").touch()
        self.until(lambda: self.api("events.show", {"event_id": event["event_id"]})["status"] == "succeeded")
        finished = self.api("events.show", {"event_id": event["event_id"]})
        self.assertEqual(finished["attempt"], 1)
        self.assertEqual(self.api("runs.output", {"run_id": finished["run_id"]})["text"], "fake completion")
