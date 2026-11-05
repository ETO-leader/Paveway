"""Real native terminal cells; isolated loopback model, no paid inference."""

from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
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
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.workflow_setup import create


COLOR_ENV = ("TERM", "COLORTERM", "WT_SESSION", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
             "TERMINAL", "FORCE_COLOR", "NO_COLOR", "ConEmuANSI", "ANSICON",
             "VSCODE_INJECTION", "VTE_VERSION", "KONSOLE_VERSION", "KITTY_WINDOW_ID",
             "WEZTERM_EXECUTABLE", "ALACRITTY_SOCKET", "PAVEWAY_STATUSLINE_CAPS")
HOSTILE_BACKGROUND = "ff00ff"


def color_environment(environment):
    """Persist capability inputs only, never a real terminal/session identifier."""
    return {key: ("present" if key == "WT_SESSION" and environment.get(key) else environment.get(key))
            for key in COLOR_ENV}


class Terminal:
    def __init__(self, case, columns=120):
        self.case = case
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, columns, 0, 0))
        self.screen = WideScreen(columns, 32)
        self.stream = pyte.ByteStream(self.screen)
        # A missing reset must not accidentally look transparent in the decoder.
        self.stream.feed(b"\x1b[48;2;255;0;255m")
        case.assertEqual(self.screen.cursor.attrs.bg, HOSTILE_BACKGROUND)
        self.raw = bytearray()
        self.process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "native",
            "--config", str(case.path), "--workflow", getattr(case, "workflow", "native-test")],
            env=case.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(self.master, False)
        case.addCleanup(self.close)
        self.until(lambda: self.status() is not None)

    def pump(self, seconds=.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], min(.05, max(0, deadline - time.monotonic())))[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                self.raw.extend(data)
                self.stream.feed(data)
                if b"\x1b[6n" in data:
                    os.write(self.master, b"\x1b[1;1R")

    def until(self, predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.case.assertIsNone(self.process.poll(), "\n".join(self.screen.display))
            self.pump(.08)
        self.capture("failure")
        self.case.fail("Terminal state did not settle:\n" + "\n".join(self.screen.display) +
                       "\n" + self.raw.decode(errors="replace")[-1200:])

    def send(self, value):
        pending = memoryview(value if isinstance(value, bytes) else value.encode())
        deadline = time.monotonic() + 20
        while pending and time.monotonic() < deadline:
            self.pump(.01)
            if select.select([], [self.master], [], .05)[1]:
                pending = pending[os.write(self.master, pending[:4096]):]
        self.case.assertFalse(pending, "Terminal input did not drain")

    def status(self):
        return next((y for y, row in enumerate(self.screen.display) if "PAVEWAY" in row), None)

    def cells(self):
        y = self.status()
        self.case.assertIsNotNone(y)
        return tuple((self.screen.buffer[y][x].data, self.screen.buffer[y][x].fg,
                      self.screen.buffer[y][x].bg) for x in range(self.screen.columns))

    def capture(self, name):
        directory = os.environ.get("PAVEWAY_STATUSLINE_EVIDENCE")
        if not directory:
            return
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        record = request(self.case.config.server.socket_path, "frontend.list")[0]
        host_environment = dict(item.split("=", 1) for item in
                                Path(f"/proc/{record['omp_pid']}/environ").read_text().split("\0") if "=" in item)
        sgr = re.findall(rb"\x1b\[([0-9;]*)m", self.raw)
        data = {"columns": self.screen.columns, "rows": self.screen.lines,
                "status_row": self.status(), "source": "isolated-native-pty",
                "color_mode_case": getattr(self.case, "color_mode", "truecolor"),
                "decoder_initial_background": HOSTILE_BACKGROUND,
                "launch_color_environment": color_environment(self.case.env),
                "observed_host_color_environment": color_environment(host_environment),
                "observed_host_executable": Path(os.readlink(f"/proc/{record['omp_pid']}/exe")).name,
                "observed_sgr": sorted({value.decode("ascii") for value in sgr}),
                "cells": [[self.screen.buffer[y][x]._asdict() for x in range(self.screen.columns)]
                          for y in range(self.screen.lines)]}
        prefix = os.environ.get("PAVEWAY_STATUSLINE_CAPTURE_PREFIX", "bubble")
        (target / (prefix + "-" + name + ".json")).write_text(json.dumps(data, ensure_ascii=False))
        (target / (prefix + "-" + name + ".ansi")).write_bytes(self.raw)

    def resize(self, columns):
        self.screen.resize(lines=32, columns=columns)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 32, columns, 0, 0))
        # The PTY frontend forwards the actual size to the owned OMP terminal.
        self.process.send_signal(signal.SIGWINCH)
        self.pump(.5)

    def close(self):
        if self.process.poll() is None:
            self.send("/exit\r")
            deadline = time.monotonic() + 6
            while self.process.poll() is None and time.monotonic() < deadline:
                self.pump(.1)
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        # Ownership release follows the child exit; do not let the next isolated
        # launch race the old conversation and present a recovery dialog.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if all(row["status"] == "released" for row in request(
                        self.case.config.server.socket_path, "frontend.list")):
                    break
            except Exception:
                break
            time.sleep(.05)
        if self.master is not None:
            os.close(self.master)
            self.master = None


class NativeStatusLineTests(NativeFixture):
    def configure(self, *, motion=True, long_names=False, context_window=128000, preset="nerd",
                  color_mode="truecolor", cap_style="auto"):
        self.color_mode = color_mode
        self.preset = preset
        self.cap_style = cap_style
        for key in COLOR_ENV:
            self.env.pop(key, None)
        self.env["TERM"] = "xterm-256color"
        if color_mode == "truecolor":
            self.env["COLORTERM"] = "truecolor"
        elif color_mode == "wt-session":
            self.env["WT_SESSION"] = "isolated-statusline-probe"
        elif color_mode == "none":
            self.env["NO_COLOR"] = "1"
        elif color_mode != "256color":
            raise ValueError(f"Unknown color matrix case: {color_mode}")
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        data = json.loads(path.read_text())
        data.update({"tui": {"motion": motion}, "composer": {"shape": "box"}, "symbolPreset": preset})
        path.write_text(json.dumps(data))
        models = Path(self.env["PI_CODING_AGENT_DIR"]) / "models.yml"
        data = json.loads(models.read_text())
        model = data["providers"]["paveway-m0"]["models"][0]
        model["contextWindow"] = context_window
        config = self.config
        if long_names:
            model["name"] = "Paveway reasoning model with an exceptionally long display name"
            model["id"] = "paveway-reasoning-model-with-an-exceptionally-long-identifier"
            config = replace(config, agent=replace(config.agent, model="paveway-m0/" + model["id"]))
        if cap_style != "auto":
            config = replace(config, statusline=replace(config.statusline, cap_style=cap_style))
        if config is not self.config:
            self.config = config
            self.path.write_text(config_text(config))
        self.assertEqual(load_config(self.path).statusline.cap_style, cap_style)
        models.write_text(json.dumps(data))
        if long_names:
            self.workflow = "native-test-with-an-exceptionally-long-workflow-name"
            create(self.config, self.workflow, self.root / "workspace", "example")

    def cap_glyphs(self, preset, cap_style):
        style = cap_style if cap_style != "auto" else ("plain" if preset == "ascii" else "powerline")
        return ("(", ")") if style == "plain" else ("\ue0b6", "\ue0b4")

    def assert_bubbles(self, terminal, preset=None, cap_style=None):
        preset = getattr(self, "preset", "nerd") if preset is None else preset
        cap_style = getattr(self, "cap_style", "auto") if cap_style is None else cap_style
        row = terminal.screen.buffer[terminal.status()]
        text = terminal.screen.display[terminal.status()]
        opening, closing = self.cap_glyphs(preset, cap_style)
        starts = [x for x in range(terminal.screen.columns) if row[x].data == opening]
        ends = [x for x in range(terminal.screen.columns) if row[x].data == closing]
        self.assertTrue(starts, text)
        self.assertEqual(len(starts), len(ends), text)
        for index, (start, end) in enumerate(zip(starts, ends)):
            self.assertGreater(end, start + 3, text)
            self.assertEqual(row[start + 1].data, " ", text)
            self.assertEqual(row[end - 1].data, " ", text)
            self.assertEqual(row[start].fg, row[start + 1].bg, text)
            self.assertEqual(row[end].fg, row[end - 1].bg, text)
            self.assertEqual(row[start].bg, "default", text)
            self.assertEqual(row[end].bg, "default", text)
            if index + 1 < len(starts):
                self.assertGreater(starts[index + 1], end + 1, text)
                gap = row[end + 1]
                self.assertEqual((gap.data, gap.bg), (" ", "default"), text)
        self.assertNotIn("\ufffd", text)
        self.assertNotIn("\ue0b0", text)
        self.assertNotIn("\ue0b2", text)
        self.assertNotIn("◖", text)
        self.assertNotIn("◗", text)

    def test_empty_normal_widths_are_independent_nerd_bubbles(self):
        self.configure()
        terminal = Terminal(self, columns=80)
        terminal.until(lambda: "Wakeup ?" not in terminal.screen.display[terminal.status()]
                       and "Inbox ?" not in terminal.screen.display[terminal.status()])
        for columns in (80, 100, 120, 160):
            if columns != terminal.screen.columns:
                terminal.resize(columns)
            terminal.pump(.3)
            self.assert_bubbles(terminal)
            text = terminal.screen.display[terminal.status()]
            self.assertNotIn("Wakeup", text)
            self.assertNotIn("◷", text)
            self.assertNotIn("✉", text)
            self.assertNotIn("Inbox", text)
            self.assertIn("probe", text)
            self.assertRegex(text, r"\d+% · 128K")
            terminal.capture(f"empty-{columns}")
        self.assertEqual(self.server.requests, [])

    def test_powerline_caps_are_independent_of_symbol_preset(self):
        caps = {}
        for preset in ("nerd", "unicode"):
            with self.subTest(preset=preset):
                self.configure(preset=preset)
                terminal = Terminal(self)
                terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
                self.assert_bubbles(terminal)
                text = terminal.screen.display[terminal.status()]
                self.assertIn("\ue0b6", text)
                self.assertIn("\ue0b4", text)
                row = terminal.screen.buffer[terminal.status()]
                caps[preset] = [(x, row[x].data) for x in range(terminal.screen.columns)
                                if row[x].data in ("\ue0b6", "\ue0b4")]
                self.assertTrue(caps[preset], text)
                terminal.capture(f"{preset}-powerline")
                terminal.close()
        self.assertEqual(caps["nerd"], caps["unicode"])

    def test_plain_cap_style_removes_powerline_glyphs(self):
        self.configure(preset="unicode", cap_style="plain")
        terminal = Terminal(self)
        terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
        self.assert_bubbles(terminal)
        text = terminal.screen.display[terminal.status()]
        self.assertNotIn("\ue0b6", text)
        self.assertNotIn("\ue0b4", text)
        terminal.capture("plain-powerline-off")
        terminal.close()

    def test_ascii_preset_defaults_to_ascii_caps(self):
        self.configure(preset="ascii")
        terminal = Terminal(self)
        terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
        self.assert_bubbles(terminal)
        text = terminal.screen.display[terminal.status()]
        self.assertFalse([character for character in text if ord(character) > 127], text)
        terminal.capture("ascii-plain")
        terminal.close()

    def test_color_modes_keep_powerline_caps(self):
        for color_mode in ("256color", "truecolor"):
            with self.subTest(color_mode=color_mode):
                self.configure(preset="unicode", color_mode=color_mode)
                terminal = Terminal(self)
                terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
                self.assert_bubbles(terminal)
                text = terminal.screen.display[terminal.status()]
                self.assertIn("\ue0b6", text)
                self.assertIn("\ue0b4", text)
                terminal.capture(f"{color_mode}-powerline")
                terminal.close()

    def test_countdown_and_runtime_unavailable_are_real_isolated_states(self):
        self.configure()
        terminal = Terminal(self, columns=160)
        terminal.send("/wakeup in 80s STATUS_COUNTDOWN\r")
        terminal.until(lambda: re.search(r"◷ 1m\d+s", terminal.screen.display[terminal.status()]))
        terminal.capture("wakeup-countdown-160")
        self.assert_bubbles(terminal)
        socket = Path(self.config.server.socket_path)
        self.assertTrue(socket.is_relative_to(self.root), "Never fault a user Runtime")
        hidden = socket.with_name(socket.name + ".statusline-fault")
        socket.rename(hidden)
        try:
            terminal.until(lambda: "Wakeup !" in terminal.screen.display[terminal.status()]
                           and "Inbox ?" in terminal.screen.display[terminal.status()], timeout=15)
            terminal.capture("runtime-unavailable-160")
            self.assert_bubbles(terminal)
            text = terminal.screen.display[terminal.status()]
            self.assertNotIn("Inbox 0", text)
            error_cells = [cell for cell in terminal.cells() if cell[2] == "633c4b"]
            self.assertTrue(error_cells, text)
            self.assertTrue(any(cell[1] == "f5dce0" for cell in error_cells), text)
        finally:
            hidden.rename(socket)
        terminal.until(lambda: "Wakeup !" not in terminal.screen.display[terminal.status()]
                       and "Inbox ?" not in terminal.screen.display[terminal.status()], timeout=15)
        terminal.capture("runtime-recovered-160")

    def test_background_running_wakeup_is_observed_from_runtime(self):
        self.configure()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_response(_body):
            yield {"role": "assistant", "content": "BACKGROUND_STATUS_STREAM"}, None
            release.wait(22)
            yield {"content": "M0_REPLY"}, None
            yield {}, "stop"

        self.server.response_factory = slow_response
        terminal = Terminal(self, columns=160)
        event = request(self.config.server.socket_path, "schedule", {
            "workflow_id": "native-test", "task_id": "training", "reason": "statusline running acceptance",
            "dedupe_key": "statusline-running", "at": "now"})
        terminal.until(lambda: request(self.config.server.socket_path, "events.show", {
            "event_id": event["event_id"]})["status"] == "running")
        terminal.until(lambda: "◷ Running" in terminal.screen.display[terminal.status()], timeout=15)
        terminal.capture("wakeup-running-160")
        self.assert_bubbles(terminal)
        release.set()
        terminal.until(lambda: request(self.config.server.socket_path, "events.show", {
            "event_id": event["event_id"]})["status"] == "succeeded")

    def record(self):
        return request(self.config.server.socket_path, "frontend.list")[0]

    def test_bottom_bar_preserves_editor_draft_resize_and_new_session(self):
        self.configure(long_names=True)
        terminal = Terminal(self)
        terminal.send("UNSENT_DRAFT")
        terminal.until(lambda: "UNSENT_DRAFT" in "\n".join(terminal.screen.display))
        terminal.capture("idle-120")
        row = terminal.status()
        text = "\n".join(terminal.screen.display)
        self.assertEqual(sum("PAVEWAY" in line for line in terminal.screen.display), 1)
        self.assertLess(next(y for y, line in enumerate(terminal.screen.display) if "UNSENT_DRAFT" in line), row)
        self.assertNotIn("OMP native", text)
        self.assertNotIn(str(self.root / "workspace"), text)
        self.assertTrue(any("╯" in line for line in terminal.screen.display[:row]), text)
        self.assertNotIn(b"\x1b[?1049h", terminal.raw)
        for columns in (80, 100, 160, 120):
            terminal.resize(columns)
            terminal.until(lambda: "UNSENT_DRAFT" in "\n".join(terminal.screen.display) and terminal.status() is not None)
            terminal.capture(f"resize-{columns}")
            self.assertEqual(sum("PAVEWAY" in line for line in terminal.screen.display), 1)
            self.assertNotIn("\ufffd", "\n".join(terminal.screen.display))
        terminal.send(b"\x01\x0b")
        before = self.record()
        terminal.send("/new\r")
        terminal.until(lambda: self.record()["generation"] > before["generation"])
        terminal.until(lambda: terminal.status() is not None)
        self.assert_bubbles(terminal)
        self.assertIn("\ue0b6", terminal.screen.display[terminal.status()])
        terminal.capture("session-switch")
        self.assertEqual(self.server.requests, [])
        terminal.send("AFTER_STATUS_SWITCH\r")
        terminal.until(lambda: "M0_REPLY" in "\n".join(terminal.screen.display) and self.record()["status"] == "idle")
        terminal.capture("reply-idle")
        # OMP itself briefly borrows the alternate screen during resize.
        self.assertEqual(terminal.raw.count(b"\x1b[?1049h"), terminal.raw.count(b"\x1b[?1049l"))

    def test_working_frames_cancel_idle_and_inbox_are_real(self):
        self.configure(context_window=16000)
        release = threading.Event()
        self.addCleanup(release.set)
        def slow_response(_body):
            yield {"role": "assistant", "content": "WORKING_STREAM"}, None
            release.wait(18)
            yield {"content": "M0_REPLY"}, None
            yield {}, "stop"
        self.server.response_factory = slow_response
        terminal = Terminal(self)
        terminal.send("wave " * 600 + "\r")
        terminal.until(lambda: self.record()["status"] == "active")
        terminal.pump(.4)
        frames = []
        for index in range(32):
            terminal.pump(.125)
            frames.append(terminal.cells())
            terminal.capture(f"working-{index}")
        self.assertEqual(len({tuple(cell[0] for cell in frame) for frame in frames}), 1)
        self.assertGreater(len(set(frames)), 20, "Working context colors must propagate across consecutive frames")
        occupied = [x for x, cell in enumerate(frames[0]) if cell[2] == "67cbd0"]
        self.assertTrue(occupied, "The real prompt must occupy at least one context cell")
        for x in occupied:
            self.assertEqual(len({frame[x] for frame in frames}), 1, "Occupied context cells must not animate")
        terminal.send(b"\x1b")
        terminal.until(lambda: self.record()["status"] == "idle")
        release.set()
        terminal.pump(.4)
        idle = terminal.cells()
        terminal.pump(.5)
        self.assertEqual(terminal.cells(), idle)
        terminal.capture("cancelled-idle")
        terminal.resize(80)
        terminal.capture("compact-idle-80")
        self.assertEqual(sum("PAVEWAY" in line for line in terminal.screen.display), 1)
        terminal.resize(120)
        terminal.send("/wakeup in 1s STATUS_INBOX_MESSAGE\r")
        terminal.until(lambda: request(self.config.server.socket_path, "inbox.list", {
            "workflow_id": "native-test", "task_id": "training"})["unacknowledged"] > 0)
        def inbox_matches_runtime():
            unread = request(self.config.server.socket_path, "inbox.list", {
                "workflow_id": "native-test", "task_id": "training"})["unacknowledged"]
            return unread > 0 and f"✉ {unread}" in terminal.screen.display[terminal.status()]
        terminal.until(inbox_matches_runtime, timeout=15)
        terminal.pump(.25)
        terminal.capture("inbox-unread")
        self.assertTrue(inbox_matches_runtime())

    def test_motion_disabled_does_not_change_context_colors_while_working(self):
        self.configure(motion=False)
        release = threading.Event()
        self.addCleanup(release.set)
        def slow_response(_body):
            yield {"role": "assistant", "content": "STATIC_STREAM"}, None
            release.wait(12)
            yield {"content": "M0_REPLY"}, None
            yield {}, "stop"
        self.server.response_factory = slow_response
        terminal = Terminal(self)
        terminal.send("MOTION_OFF\r")
        terminal.until(lambda: self.record()["status"] == "active")
        terminal.pump(.4)
        frame = terminal.cells()
        terminal.pump(.55)
        self.assertEqual(terminal.cells(), frame)
        terminal.capture("motion-disabled")
        release.set()
        terminal.until(lambda: self.record()["status"] == "idle")

    def test_low_and_high_real_context_capacity(self):
        self.configure(context_window=4000)
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        settings = json.loads(path.read_text())
        settings["compaction"] = {"enabled": False}
        path.write_text(json.dumps(settings))
        terminal = Terminal(self)
        terminal.capture("low-context")
        prompt = "CAPACITY_HEAD " + "word " * 2200 + " CAPACITY_TAIL"
        terminal.send("\x1b[200~" + prompt + "\x1b[201~\r")
        terminal.until(lambda: "M0_REPLY" in "\n".join(terminal.screen.display) and self.record()["status"] == "idle")
        terminal.pump(.3)
        status = terminal.screen.display[terminal.status()]
        percent = re.search(r"(\d+)% · 4K", status)
        self.assertIsNotNone(percent, status)
        self.assertGreaterEqual(int(percent.group(1)), 80)
        self.assertTrue(any(self.is_prompt_request(body, prompt) for body in self.server.requests))
        terminal.capture("high-context")
