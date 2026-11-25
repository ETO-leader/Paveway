"""Public Paveway Runtime status on a real OMP terminal."""

import json
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

import pyte
from terminal_screen import WideScreen
from test_native_omp import NativeFixture
from oicoach_runtime.ipc import request


class Terminal:
    def __init__(self, case, columns=120, rows=32):
        self.case = case
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.screen = WideScreen(columns, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.raw = bytearray()
        self.process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "native",
            "--config", str(case.path), "--workflow", "native-test"],
            env=case.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(self.master, False)
        case.addCleanup(self.close)
        self.until(lambda: bool(self.status))

    @property
    def text(self):
        return "\n".join(self.screen.display)

    @property
    def editor_line(self):
        return self.screen.display[self.screen.cursor.y]

    @property
    def status(self):
        return next((row.strip() for row in self.screen.display if "PAVEWAY native-test/" in row), "")

    def pump(self, seconds=.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], min(.05, max(0, deadline - time.monotonic())))[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                query_start = max(0, len(self.raw) - 3)
                self.raw.extend(data)
                self.stream.feed(data)
                if self.raw.find(b"\x1b[6n", query_start) != -1:
                    os.write(self.master, b"\x1b[1;1R")

    def until(self, predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.process.poll() is not None:
                break
            self.pump(.08)
        self.case.fail(f"Terminal state did not settle (exit={self.process.poll()}):\n"
                       f"{self.text}\n{self.raw.decode(errors='replace')[-1600:]}")

    def send(self, value):
        pending = memoryview(value if isinstance(value, bytes) else value.encode())
        deadline = time.monotonic() + 20
        while pending and time.monotonic() < deadline:
            self.pump(.01)
            if select.select([], [self.master], [], .05)[1]:
                pending = pending[os.write(self.master, pending[:4096]):]
        self.case.assertFalse(pending, "Terminal input did not drain")

    def resize(self, columns, rows=32):
        self.screen.resize(lines=rows, columns=columns)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.process.send_signal(signal.SIGWINCH)

    def close(self):
        try:
            if self.process.poll() is None and self.status:
                self.send(b"\x03")
                self.send("/exit\r")
                deadline = time.monotonic() + 6
                while self.process.poll() is None and time.monotonic() < deadline:
                    self.pump(.1)
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            os.close(self.master)


class NativeStatusLineTests(NativeFixture):
    scope = {"workflow_id": "native-test", "task_id": "training"}

    def runtime(self, command, **arguments):
        return request(self.config.server.socket_path, command, arguments)

    @staticmethod
    def idle_status(terminal):
        return bool(terminal.status) and not re.search(r"\b(?:Wake|Inbox|Busy)\b", terminal.status)

    def test_idle_shows_runtime_identity_and_native_model_without_empty_indicators(self):
        terminal = Terminal(self, columns=80)
        terminal.until(lambda: self.idle_status(terminal) and "probe" in terminal.text)
        self.assertEqual(self.server.requests, [])

    def test_light_narrow_terminal_reserves_live_status_and_keeps_draft_on_resize(self):
        from pathlib import Path
        config = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        settings = json.loads(config.read_text())
        settings["theme"] = {"dark": "light", "light": "light"}
        config.write_text(json.dumps(settings))
        terminal = Terminal(self, columns=42)
        terminal.send("/wakeup in 80s NARROW_WAKEUP\r")
        terminal.until(lambda: "Wake " in terminal.status)
        terminal.send("\x1b[200~first line\n第二行 draft\x1b[201~")
        terminal.until(lambda: "第二行 draft" in terminal.editor_line and "Wake " in terminal.status)
        terminal.resize(92)
        terminal.until(lambda: "PAVEWAY native-test/training" in terminal.status
                       and "Wake " in terminal.status and "第二行 draft" in terminal.editor_line)
        self.assertFalse(self.server.requests)

    def test_wakeup_countdown_until_cancelled(self):
        terminal = Terminal(self)
        terminal.send("/wakeup in 80s STATUS_COUNTDOWN\r")
        terminal.until(lambda: re.search(r"\bWake \d+m\d+s\b", terminal.status))
        event, = self.runtime("events.upcoming", **self.scope)
        self.assertEqual(event["status"], "queued")

        terminal.send(f"/wakeup cancel {event['event_id']} {event['version']}\r")
        terminal.until(lambda: self.idle_status(terminal))
        self.assertEqual(self.runtime("events.show", event_id=event["event_id"])["status"], "cancelled")
        self.assertEqual(self.server.requests, [])

    def test_background_wakeup_shows_running_then_unread_inbox(self):
        release = threading.Event()

        def response(_body):
            yield {"role": "assistant", "content": "BACKGROUND_STATUS_STREAM"}, None
            release.wait(30)
            yield {"content": "BACKGROUND_STATUS_DONE"}, None
            yield {}, "stop"

        self.server.response_factory = response
        terminal = Terminal(self)
        try:
            terminal.send("/wakeup in 1s STATUS_INBOX_MESSAGE\r")
            terminal.until(lambda: "wake running" in terminal.status.lower())
            self.assertNotIn("Busy", terminal.status, "Background wakeups do not occupy the foreground agent")
        finally:
            release.set()

        terminal.until(lambda: "Inbox 1" in terminal.status and "Wake" not in terminal.status, timeout=30)
        inbox = self.runtime("inbox.list", **self.scope)
        self.assertEqual(inbox["unacknowledged"], 1)
        message, = inbox["messages"]
        self.assertEqual(message["kind"], "result")
        self.assertIn("BACKGROUND_STATUS_DONE", message["text"])
        self.runtime("inbox.acknowledge", **self.scope, delivery_id=message["delivery_id"],
                     expected_version=message["version"])
        terminal.until(lambda: self.idle_status(terminal))

    def test_foreground_working_status_clears_after_reply(self):
        release = threading.Event()

        def response(_body):
            yield {"role": "assistant", "content": "WORKING_STREAM\n\n"}, None
            release.wait(20)
            yield {"content": "WORKING_DONE"}, None
            yield {}, "stop"

        self.server.response_factory = response
        terminal = Terminal(self)
        terminal.until(lambda: self.idle_status(terminal))
        try:
            terminal.send("STATUS_FOREGROUND\r")
            terminal.until(lambda: "WORKING_STREAM" in terminal.text and "Busy" in terminal.status)
        finally:
            release.set()

        terminal.until(lambda: "WORKING_DONE" in terminal.text and self.idle_status(terminal))
        self.assertEqual(len(self.prompt_requests("STATUS_FOREGROUND")), 1)
