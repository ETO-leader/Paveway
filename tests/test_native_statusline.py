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
        self.host_metadata = None
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
        # ASCII splash branding is a separate centered PAVEWAY row, not the footer.
        return next((y for y, row in enumerate(self.screen.display)
                     if "PAVEWAY" in row and row.strip() != "PAVEWAY"), None)

    def cells(self):
        # A PTY read may split OMP's erase/repaint. Sample only after its
        # autowrap-restoring paint end, including on hosts without DEC 2026.
        self.until(lambda: self.raw.rfind(b"\x1b[?7l") <= self.raw.rfind(b"\x1b[?7h"),
                   timeout=1)
        y = self.status()
        if y is None:
            self.capture("missing-status")
        self.case.assertIsNotNone(y, "\n".join(self.screen.display) +
                                  "\n" + self.raw.decode(errors="replace")[-1600:])
        return tuple((self.screen.buffer[y][x].data, self.screen.buffer[y][x].fg,
                      self.screen.buffer[y][x].bg, self.screen.buffer[y][x].underscore,
                      self.screen.buffer[y][x].bold) for x in range(self.screen.columns))

    def capture(self, name):
        directory = os.environ.get("PAVEWAY_STATUSLINE_EVIDENCE")
        if not directory:
            return
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        if self.host_metadata is None:
            record = request(self.case.config.server.socket_path, "frontend.list")[0]
            host_environment = dict(item.split("=", 1) for item in
                                    Path(f"/proc/{record['omp_pid']}/environ").read_text().split("\0") if "=" in item)
            self.host_metadata = {
                "observed_host_color_environment": color_environment(host_environment),
                "observed_host_executable": Path(os.readlink(f"/proc/{record['omp_pid']}/exe")).name,
            }
        sgr = re.findall(rb"\x1b\[([0-9;]*)m", self.raw)
        data = {"columns": self.screen.columns, "rows": self.screen.lines,
                "status_row": self.status(), "source": "isolated-native-pty",
                "color_mode_case": getattr(self.case, "color_mode", "truecolor"),
                "decoder_initial_background": HOSTILE_BACKGROUND,
                "launch_color_environment": color_environment(self.case.env),
                **self.host_metadata,
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
            self.assert_context_rail(terminal.cells(), text, 128000, terminal=terminal)
            if columns == 160:
                self.assertIn("auto85% (spec74%)", text)
            terminal.capture(f"empty-{columns}")
        self.assertEqual(self.server.requests, [])

    def test_powerline_caps_are_independent_of_symbol_preset(self):
        caps = {}
        for preset in ("nerd", "unicode"):
            with self.subTest(preset=preset):
                self.configure(preset=preset, cap_style="powerline")
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

    def test_unicode_auto_restores_powerline_caps_and_chevrons(self):
        for color_mode in ("truecolor", "256color", "none"):
            with self.subTest(color_mode=color_mode):
                self.configure(preset="unicode", color_mode=color_mode)
                terminal = Terminal(self)
                try:
                    terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
                    for columns in (80, 100, 120, 160):
                        if columns != terminal.screen.columns:
                            terminal.resize(columns)
                        self.assert_bubbles(terminal)
                        cells = terminal.cells()
                        text = "".join(cell[0] for cell in cells)
                        self.assertTrue(text.startswith("\ue0b6 PAVEWAY \ue0b4"), text)
                        self.assertEqual(cells[-1][0], "\ue0b4", text)
                        meter, opening, _closing = self.context_meter(cells, text)
                        self.assertIn("\ue0b1", [cell[0] for cell in meter], text)
                        if color_mode != "none":
                            surface = "203f49" if color_mode == "truecolor" else "005f5f"
                            self.assertEqual(cells[opening][1:3], (surface, "default"))
                            self.assertEqual(cells[-1][1:3], (surface, "default"))
                        self.assert_context_rail(cells, text, 128000, terminal=terminal)
                        terminal.capture(f"unicode-auto-{color_mode}-{columns}")
                finally:
                    terminal.close()

    def test_explicit_plain_caps_remove_powerline_glyphs(self):
        self.configure(preset="unicode", cap_style="plain")
        terminal = Terminal(self)
        terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
        self.assert_bubbles(terminal)
        text = terminal.screen.display[terminal.status()]
        self.assertIn("(", text)
        self.assertIn(")", text)
        self.assertNotRegex(text, "[\ue000-\uf8ff]")
        self.assertIn(">", text)
        terminal.capture("unicode-explicit-plain")
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
            terminal.until(lambda: (row := terminal.status()) is not None
                           and "Wakeup !" in terminal.screen.display[row]
                           and "Inbox ?" in terminal.screen.display[row], timeout=15)
            terminal.capture("runtime-unavailable-160")
            self.assert_bubbles(terminal)
            text = terminal.screen.display[terminal.status()]
            self.assertNotIn("Inbox 0", text)
            error_cells = [cell for cell in terminal.cells() if cell[2] == "633c4b"]
            self.assertTrue(error_cells, text)
            self.assertTrue(any(cell[1] == "f5dce0" for cell in error_cells), text)
        finally:
            hidden.rename(socket)
        terminal.until(lambda: (row := terminal.status()) is not None
                       and "Wakeup !" not in terminal.screen.display[row]
                       and "Inbox ?" not in terminal.screen.display[row], timeout=15)
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
        switched = terminal.cells()
        terminal.pump(.4)
        self.assertEqual(terminal.cells(), switched, "New idle session must not animate")
        self.assertEqual(self.server.requests, [])
        terminal.send("AFTER_STATUS_SWITCH\r")
        terminal.until(lambda: "M0_REPLY" in "\n".join(terminal.screen.display) and self.record()["status"] == "idle")
        terminal.capture("reply-idle")
        # OMP itself briefly borrows the alternate screen during resize.
        self.assertEqual(terminal.raw.count(b"\x1b[?1049h"), terminal.raw.count(b"\x1b[?1049l"))

    def test_working_frames_cancel_idle_and_inbox_are_real(self):
        self.configure(context_window=16000, preset="unicode")
        release = threading.Event()
        self.addCleanup(release.set)
        def slow_response(_body):
            yield {"role": "assistant", "content": "WORKING_STREAM"}, None
            release.wait(18)
            yield {"content": "M0_REPLY"}, None
            yield {}, "stop"
        self.server.response_factory = slow_response
        terminal = Terminal(self)
        terminal.send("chevron " * 600 + "\r")
        terminal.until(lambda: self.record()["status"] == "active")
        terminal.pump(.4)
        frames = []
        for index in range(32):
            terminal.pump(.125)
            frames.append(terminal.cells())
            terminal.capture(f"working-{index}")
        self.assertEqual(len({tuple(cell[0] for cell in frame) for frame in frames}), 1)
        self.assertGreaterEqual(len(set(frames)), 12, "Working context must expose most of its cycle")
        occupied = [x for x, cell in enumerate(frames[0]) if cell[2] == "67cbd0"]
        self.assertTrue(occupied, "The real prompt must occupy at least one context cell")
        for x in occupied:
            self.assertEqual(len({frame[x] for frame in frames}), 1, "Occupied context cells must not animate")
        meter, opening, _closing = self.context_meter(frames[0], "".join(cell[0] for cell in frames[0]))
        unused = [opening + 2 + index for index, cell in enumerate(meter) if cell[0] == "\ue0b1"]
        self.assertTrue(unused, "Working context must retain unused chevrons")
        for x in unused:
            self.assertEqual({frame[x][0] for frame in frames}, {"\ue0b1"})
            self.assertEqual({frame[x][2] for frame in frames}, {frames[0][x][2]})
        self.assertTrue(any(len({frame[x][1] for frame in frames}) > 1 for x in unused),
                        "Working chevron foregrounds must animate")
        for frame in frames:
            self.assert_context_rail(frame, "".join(cell[0] for cell in frame), 16000)
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

    @staticmethod
    def context_meter(cells, text):
        label = re.search(r"\d+% · \d+K", text)
        if label is None:
            raise AssertionError(text)
        opening = max(x for x, cell in enumerate(cells)
                      if cell[0] in ("\ue0b6", "(") and x < label.start())
        # Context is the final bubble; its Limit text may itself contain (...).
        cap = "\ue0b4" if cells[opening][0] == "\ue0b6" else ")"
        closing = max(x for x, cell in enumerate(cells)
                      if x > opening and cell[0] == cap)
        return cells[opening + 2:label.start() - 1], opening, closing

    def assert_context_rail(self, cells, text, window, *, auto_compact=True, terminal=None):
        meter, opening, closing = self.context_meter(cells, text)
        # Pinned OMP defaults, not invented 75%/85% fixture boundaries:
        # reserve=max(15% window, 16384), recovering to 15% for tiny windows;
        # speculation lead=clamp(floor(threshold/8), 8192, 32000).
        # The text-only loopback model supports handoff speculation.
        spec_percent, spec_starts = {
            128000: (74.375, {4: 3, 8: 6, 16: 12}),
            16000: (33.8, {4: 2, 8: 3, 16: 6}),
            4000: (0, {4: 0, 8: 0, 16: 0}),
        }[window]
        auto_start = {4: 4, 8: 7, 16: 14}[len(meter)]
        spec_start = spec_starts[len(meter)]
        color_mode = getattr(self, "color_mode", "truecolor")
        colors = {
            "truecolor": ("203f49", "30545b", "574939", "67cbd0", "a6c9c2", "d5ab68"),
            "256color": ("005f5f", "005f87", "5f5f00", "5fd7d7", "afd7d7", "d7af5f"),
        }.get(color_mode)
        context = cells[opening:closing + 1]
        self.assertTrue(all(not cell[3] and not cell[4] for cell in context),
                        "Context must have neither underscore nor bold: " + text)
        if color_mode == "none":
            self.assertTrue(all(cell[1:3] == ("default", "default") for cell in context), text)
            if terminal is not None:
                # OMP still uses terminal-control escapes elsewhere. The complete
                # uncolored Context bubble itself must have no embedded ANSI.
                self.assertIn(text[opening:closing + 1].encode(), terminal.raw)
            return
        normal, spec, auto, cyan, notice, warning = colors
        for index, cell in enumerate(meter):
            if cell[0] == " ":
                self.assertEqual(cell[1:3], (cyan, cyan), text)
            else:
                self.assertEqual(cell[0], "\ue0b1", text)
                expected = auto if auto_compact and index >= auto_start else (
                    spec if auto_compact and index >= spec_start else normal)
                self.assertEqual(cell[2], expected, f"Context rail cell {index}: {text}")
        label = re.search(r"(\d+)% · \d+K", text)
        reported = int(label.group(1))
        # OMP reports an unrounded percentage, whereas its visible label rounds.
        # Permit both adjacent label colors only inside that half-percent margin.
        lower, upper = max(0, reported - .5), min(100, reported + .5)
        allowed = {cyan}
        if auto_compact:
            allowed = set()
            if lower < spec_percent:
                allowed.add(cyan)
            if upper >= spec_percent and lower < 85:
                allowed.add(notice)
            if upper >= 85:
                allowed.add(warning)
        self.assertTrue(all(cell[1] in allowed and cell[2] == normal
                            for cell in cells[label.start():label.end()]), text)
        if "auto" in text[label.end():closing]:
            expected = f"auto85% (spec{round(spec_percent)}%)" if auto_compact else "auto off"
            self.assertIn(expected, text[label.end():closing])

    def capture_chevron_frames(self, release, prefix, columns, window, color_mode, motion, prompt):
        self.configure(context_window=window, color_mode=color_mode, motion=motion, preset="unicode")
        release.clear()
        terminal = Terminal(self, columns=columns)
        try:
            terminal.until(lambda: "Inbox ?" not in terminal.screen.display[terminal.status()])
            if prefix.startswith("chevron-4-"):
                # A distant wakeup keeps a static second bubble and squeezes the
                # Context meter to four cells without changing during capture.
                terminal.send("/wakeup in 23h CHEVRON4_SETUP\r")
                terminal.until(lambda: "\u25f7" in terminal.screen.display[terminal.status()])
                # The hour/minute label settles after the first second, then stays
                # fixed for the capture window.
                terminal.pump(1.3)
            if len(prompt) > 500:
                terminal.send("\x1b[200~" + prompt + "\x1b[201~\r")
            else:
                terminal.send(prompt + "\r")
            terminal.until(lambda: self.record()["status"] == "active")
            terminal.pump(.35)
            first_cells = terminal.cells()
            first_text = "".join(cell[0] for cell in first_cells)
            first_meter = self.context_meter(first_cells, first_text)[0]
            animated = color_mode != "none" and motion and sum(cell[0] == "\ue0b1" for cell in first_meter) > 2
            frames = []
            for index in range(2 if color_mode == "none" else 17):
                if index and animated:
                    previous = frames[-1]
                    deadline = time.monotonic() + 1
                    while terminal.cells() == previous and time.monotonic() < deadline:
                        terminal.pump(.01)
                    self.assertNotEqual(terminal.cells(), previous, "Animation stalled")
                elif index:
                    terminal.pump(.125)
                frames.append(terminal.cells())
                self.assert_context_rail(frames[-1], "".join(cell[0] for cell in frames[-1]),
                                         window, terminal=terminal)
                terminal.capture(f"{prefix}-{index}")
            text = terminal.screen.display[terminal.status()]
            release.set()
            terminal.until(lambda: self.record()["status"] == "idle")
            terminal.pump(.35)
            idle = terminal.cells()
            terminal.pump(.35)
            self.assertEqual(terminal.cells(), idle)
            self.assert_context_rail(idle, "".join(cell[0] for cell in idle), window, terminal=terminal)
            terminal.capture(f"{prefix}-idle")
            return frames, text, idle
        finally:
            release.set()
            if terminal.process.poll() is None:
                terminal.close()

    def test_real_chevron_evidence_matrix(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_response(_body):
            yield {"role": "assistant", "content": "CHEVRON_STREAM"}, None
            release.wait(45)
            yield {"content": "M0_REPLY"}, None
            yield {}, "stop"

        self.server.response_factory = slow_response
        scenarios = [
            ("chevron-8-truecolor", 120, 16000, "truecolor", True, "CHEVRON8 "),
            ("chevron-8-256", 120, 16000, "256color", True, "CHEVRON8 "),
            ("chevron-8-none", 120, 16000, "none", True, "CHEVRON8 "),
            ("chevron-16-truecolor", 160, 16000, "truecolor", True, "CHEVRON16 "),
            ("chevron-16-256", 160, 16000, "256color", True, "CHEVRON16 "),
            ("chevron-16-none", 160, 16000, "none", True, "CHEVRON16 "),
            ("chevron-4-truecolor", 64, 16000, "truecolor", True, "CHEVRON4 "),
            ("chevron-4-256", 64, 16000, "256color", True, "CHEVRON4 "),
            ("chevron-4-none", 64, 16000, "none", True, "CHEVRON4 "),
            ("chevron-mid-truecolor", 160, 4000, "truecolor", True, "word " * 600),
            ("chevron-high-truecolor", 160, 4000, "truecolor", True, "word " * 1400),
            ("chevron-motion-off", 120, 16000, "truecolor", False, "CHEVRON_STATIC "),
        ]
        for prefix, columns, window, color_mode, motion, prompt in scenarios:
            with self.subTest(prefix=prefix):
                frames, text, idle = self.capture_chevron_frames(
                    release, prefix, columns, window, color_mode, motion, prompt)
                self.assertEqual(len({tuple(cell[0] for cell in frame) for frame in frames}), 1, text)
                meter, opening, closing = self.context_meter(frames[0], text)
                expected_size = 4 if prefix.startswith("chevron-4-") else (
                    16 if prefix.startswith(("chevron-16", "chevron-mid", "chevron-high")) else 8)
                self.assertEqual(len(meter), expected_size, text)
                idle_text = "".join(cell[0] for cell in idle)
                idle_meter = self.context_meter(idle, idle_text)[0]
                occupied_in_meter = 0
                for cell in meter:
                    if cell[0] not in (" ", "━"):
                        break
                    occupied_in_meter += 1
                remaining = len(meter) - occupied_in_meter
                # Prompt scaffolding and the reply both consume real tokens.
                # Check each observed state against its own rounded percentage,
                # rather than assuming a fixture string is exactly half-full.
                for observed, label in [(meter, text), (idle_meter, idle_text)]:
                    filled = sum(cell[0] in (" ", "━") for cell in observed)
                    reported = int(re.search(r"(\d+)%", label).group(1))
                    self.assertLessEqual(abs(filled * 100 / len(observed) - reported),
                                         100 / len(observed) + .5, label)
                    self.assertTrue(all(cell[0] == "\ue0b1"
                                        and (color_mode == "none" or cell[1] != cell[2])
                                        for cell in observed[filled:]), label)
                unused = set(range(opening + 2 + occupied_in_meter, opening + 2 + len(meter)))
                self.assertEqual(len(idle_meter), len(meter), text)
                for index, cell in enumerate(meter):
                    if index < occupied_in_meter:
                        self.assertEqual(cell[0], "━" if color_mode == "none" else " ", text)
                        self.assertEqual(cell[1], cell[2], text)
                        if color_mode == "truecolor":
                            self.assertEqual(cell[1:3], ("67cbd0", "67cbd0"), text)
                    else:
                        self.assertEqual(cell[0], "\ue0b1", text)
                for frame in frames:
                    for index, cell in enumerate(frame):
                        self.assertEqual(cell[0], frames[0][index][0],
                                         f"{prefix}: glyph {index} changed")
                        self.assertEqual(cell[2], frames[0][index][2],
                                         f"{prefix}: background {index} changed")
                        if index not in unused:
                            self.assertEqual(cell, frames[0][index],
                                             f"{prefix}: non-unused cell {index} changed")
                if color_mode == "none" or not motion or remaining <= 2:
                    self.assertEqual(len(set(frames)), 1,
                                     "motion=false and very short tails must be static")
                else:
                    self.assertEqual(frames[16], frames[0], "Full cycle must close without a jump")
                    self.assertGreaterEqual(len(set(frames)), 12, f"{prefix} has too few visible frames")
                    foregrounds = {frame[index][1] for frame in frames for index in unused}
                    self.assertGreaterEqual(len(foregrounds), 4 if len(meter) == 4 else 6,
                                            f"{prefix} collapsed to {foregrounds}")

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
        self.assert_context_rail(terminal.cells(), terminal.screen.display[terminal.status()],
                                 4000, auto_compact=False, terminal=terminal)
        prompt = "CAPACITY_HEAD " + "word " * 2200 + " CAPACITY_TAIL"
        terminal.send("\x1b[200~" + prompt + "\x1b[201~\r")
        terminal.until(lambda: "M0_REPLY" in "\n".join(terminal.screen.display) and self.record()["status"] == "idle")
        terminal.pump(.3)
        status = terminal.screen.display[terminal.status()]
        percent = re.search(r"(\d+)% · 4K", status)
        self.assertIsNotNone(percent, status)
        self.assertGreaterEqual(int(percent.group(1)), 80)
        self.assertTrue(any(self.is_prompt_request(body, prompt) for body in self.server.requests))
        self.assert_context_rail(terminal.cells(), status, 4000, auto_compact=False, terminal=terminal)
        terminal.capture("high-context")
