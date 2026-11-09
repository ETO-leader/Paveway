"""Real first-paint Paveway welcome cells; isolated loopback inference only."""

import importlib
import json
from pathlib import Path
import re
import threading
import time

from pyte.screens import wcwidth

from oicoach_runtime import __version__
from oicoach_runtime.workflow_setup import create
from test_native_omp import NativeFixture
from test_native_statusline import COLOR_ENV, Terminal


OMP_VERSION = importlib.import_module("oicoach_runtime.bundled.omp.install").VERSION
HEADING = "OI COACHING RUNTIME"
TIP = "Tip: Find the insight. Build the solution."
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
BLOCK = re.compile(r"[\u2580-\u259f]")


class EntranceTerminal(Terminal):
    """Observe completed PTY paints from process start, before normal UI readiness."""
    def __init__(self, case, columns=120):
        self.palettes = []
        super().__init__(case, columns)

    def pump(self, seconds=.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            super().pump(min(.02, max(0, deadline - time.monotonic())))
            if self.raw.rfind(b"\x1b[?7l") > self.raw.rfind(b"\x1b[?7h"):
                continue
            heading = next((y for y, row in enumerate(self.screen.display) if HEADING in row), None)
            if heading is None:
                continue
            palette = tuple(sorted({self.screen.buffer[y][x].fg
                for y in range(heading) for x in range(self.screen.columns)
                if BLOCK.search(self.screen.buffer[y][x].data)}))
            if palette and (not self.palettes or self.palettes[-1] != palette):
                self.palettes.append(palette)


class NativeBannerTests(NativeFixture):
    def configure(self, mode="truecolor", *, motion=False, reduced_motion=False):
        self.color_mode = mode
        for key in (*COLOR_ENV, "REDUCED_MOTION"):
            self.env.pop(key, None)
        self.env["TERM"] = "xterm-256color"
        if mode == "truecolor":
            self.env["COLORTERM"] = "truecolor"
        elif mode == "none":
            self.env["NO_COLOR"] = "1"
        elif mode == "force-color-zero":
            self.env.update({"COLORTERM": "truecolor", "FORCE_COLOR": "0"})
        elif mode == "dumb":
            self.env.update({"COLORTERM": "truecolor", "TERM": "dumb"})
        elif mode != "256color":
            raise ValueError(mode)
        if reduced_motion:
            self.env["REDUCED_MOTION"] = "1"
        self.env["PAVEWAY_STATUSLINE_CAPS"] = "plain"
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        settings = json.loads(path.read_text())
        settings.update({"tui": {"motion": motion}, "symbolPreset": "unicode"})
        path.write_text(json.dumps(settings))

    @staticmethod
    def status_row(terminal):
        # The narrow welcome also spells PAVEWAY: the real bottom bar is the
        # last row carrying model/context information, never the first wordmark.
        return next((y for y in range(terminal.screen.lines - 1, -1, -1)
                     if "probe" in terminal.screen.display[y]
                     or re.search(r"\d+%", terminal.screen.display[y])), None)

    def ready(self, terminal):
        terminal.until(lambda: HEADING in "\n".join(terminal.screen.display)
                       and "Tip:" in "\n".join(terminal.screen.display)
                       and self.status_row(terminal) is not None)
        terminal.until(lambda: terminal.raw.rfind(b"\x1b[?7l")
                       <= terminal.raw.rfind(b"\x1b[?7h"), timeout=2)

    def banner_rows(self, terminal):
        rows = terminal.screen.display
        heading = next(y for y, row in enumerate(rows) if HEADING in row)
        tip = next(y for y, row in enumerate(rows) if "Tip:" in row)
        starts = [y for y in range(heading) if BLOCK.search(rows[y]) or "PAVEWAY" in rows[y]]
        self.assertTrue(starts, "Missing Paveway wordmark:\n" + "\n".join(rows))
        return range(min(starts), tip + 1)

    def banner_snapshot(self, terminal):
        return tuple(tuple(terminal.screen.buffer[y][x] for x in range(terminal.screen.columns))
                     for y in self.banner_rows(terminal))

    def assert_first_paint_replaced(self, terminal):
        # Inspect all bytes, not merely the final cells: the old banner must
        # never flash before an extension swaps in the Paveway welcome.
        output = ANSI.sub("", terminal.raw.decode(errors="replace"))
        self.assertNotIn("████████████", output)
        self.assertNotIn("▄▀▀▄ █▀▄▀▄ █▀▀▄", output)
        self.assertNotIn("▀▄▄▀ █ █ █ █▄▄▀", output)

    def assert_welcome(self, terminal, *, workflow="native-test"):
        text = "\n".join(terminal.screen.display)
        self.assertEqual(text.count(HEADING), 1, text)
        self.assertEqual(text.count("Tip:"), 1, text)
        if terminal.screen.columns >= 80:
            self.assertIn(TIP, text)
            banner = "\n".join(terminal.screen.display[y] for y in self.banner_rows(terminal))
            self.assertRegex(banner, rf"(?i)Paveway\s*[:·]?\s*v?{re.escape(__version__)}\b")
            self.assertRegex(banner, rf"OMP\s*[:·]?\s*v?{re.escape(OMP_VERSION)}\b")
            if workflow == "native-test":
                self.assertIn(workflow, banner)
        self.assert_first_paint_replaced(terminal)
        self.assert_cells_fit(terminal)

    def assert_cells_fit(self, terminal):
        columns = terminal.screen.columns
        for y, text in enumerate(terminal.screen.display):
            self.assertLessEqual(sum(max(0, wcwidth(char)) for char in text), columns, text)
            self.assertNotIn("\ufffd", text)
            row = terminal.screen.buffer[y]
            self.assertFalse(any(x >= columns and cell.data.strip() for x, cell in row.items()), text)
            for x in range(columns):
                cell = row[x]
                if cell.data == "":
                    self.assertGreater(x, 0, text)
                    self.assertTrue(row[x - 1].data and wcwidth(row[x - 1].data[0]) == 2, text)
                elif wcwidth(cell.data[0]) == 2:
                    self.assertLess(x + 1, columns, text)
                    self.assertEqual(row[x + 1].data, "", text)
        self.assertLessEqual(terminal.screen.cursor.x, columns)

    def assert_banner_color(self, terminal, mode):
        cells = [terminal.screen.buffer[y][x] for y in self.banner_rows(terminal)
                 for x in range(terminal.screen.columns)]
        ink = [cell for cell in cells if BLOCK.search(cell.data)]
        if mode != "dumb":
            self.assertTrue(ink)
        else:
            self.assertIn("PAVEWAY", "\n".join(terminal.screen.display[y] for y in self.banner_rows(terminal)))
        self.assertTrue(all(cell.bg == "default" for cell in cells),
                        "Welcome must keep the terminal's default background")
        if mode in ("none", "force-color-zero", "dumb"):
            self.assertTrue(all(cell.fg == "default" for cell in cells))
            self.assertTrue(all(not cell.bold and not cell.italics and not cell.underscore
                                for cell in cells), "Disabled color must not retain ANSI styling")
        else:
            self.assertGreater(len({cell.fg for cell in ink}), 3, "Missing horizontal logo gradient")
            raw = bytes(terminal.raw)
            if mode == "truecolor":
                self.assertRegex(raw, rb"\x1b\[(?:[0-9]+;)*38;2;\d+;\d+;\d+m")
            else:
                self.assertRegex(raw, rb"\x1b\[(?:[0-9]+;)*38;5;\d+m")
                self.assertNotRegex(raw, rb"\x1b\[(?:[0-9]+;)*38;2;")

    def test_first_paint_versions_idle_lifecycle_and_normal_turn(self):
        self.configure(motion=True)
        release = threading.Event()
        self.addCleanup(release.set)

        def response(_body):
            yield {"role": "assistant", "content": "BANNER_REPLY_HEAD\n\n"}, None
            release.wait(12)
            yield {"content": "BANNER_REPLY_DONE"}, None
            yield {}, "stop"

        self.server.response_factory = response
        terminal = Terminal(self, 120)
        self.ready(terminal)
        terminal.pump(1.0)  # The welcome's 700 ms entrance is finite.
        self.assert_welcome(terminal)
        self.assert_banner_color(terminal, "truecolor")
        terminal.capture("banner-settled-wide")
        settled = self.banner_snapshot(terminal)
        raw_end = len(terminal.raw)
        terminal.pump(1.0)
        self.assertEqual(self.banner_snapshot(terminal), settled)
        idle_output = ANSI.sub("", terminal.raw[raw_end:].decode(errors="replace"))
        self.assertNotIn(HEADING, idle_output)
        self.assertNotIn("Tip:", idle_output)
        self.assertIsNone(BLOCK.search(idle_output), "Settled welcome keeps repainting on idle")
        terminal.send("/switch paveway-m0/probe\r")
        terminal.until(lambda: any("Session-only model:" in row for row in terminal.screen.display))
        self.assertRegex("\n".join(terminal.screen.display), r"Session.only model:.*probe")
        self.assert_welcome(terminal)
        terminal.capture("banner-model-notice")


        terminal.send("BANNER_NORMAL_TURN\r")
        terminal.until(lambda: "BANNER_REPLY_HEAD" in "\n".join(terminal.screen.display))
        terminal.send("next draft 界")
        terminal.until(lambda: "next draft 界" in "\n".join(terminal.screen.display))
        release.set()
        terminal.until(lambda: "BANNER_REPLY_DONE" in "\n".join(terminal.screen.display))
        self.assertEqual(len(self.prompt_requests("BANNER_NORMAL_TURN")), 1)
        self.assertIn("next draft 界", terminal.screen.display[terminal.screen.cursor.y])
        status = self.status_row(terminal)
        self.assertIsNotNone(status)
        self.assertIn("probe", terminal.screen.display[status])
        self.assert_cells_fit(terminal)
        terminal.capture("banner-normal-turn")
        terminal.send(b"\x03")
        terminal.pump(.2)

    def test_resumed_conversation_settles_to_bright_banner_and_keeps_history(self):
        self.configure(motion=True)
        self.chat("BANNER_RESUME_HISTORY")
        for motion, reduced in ((False, False), (True, True), (True, False)):
            with self.subTest(motion=motion, reduced_motion=reduced):
                self.configure(motion=motion, reduced_motion=reduced)
                terminal = EntranceTerminal(self, 120)
                try:
                    self.ready(terminal)
                    terminal.pump(1.0)
                    self.assert_welcome(terminal)
                    self.assert_banner_color(terminal, "truecolor")
                    rail = next(row for row in terminal.screen.display if "●" in row and "─" in row)
                    self.assertGreater(rail.index("●") - rail.index("─"), rail.count("─") * .85,
                                       "Resuming must not leave the pathline at the entrance")
                    final_palette = terminal.palettes[-1]
                    if motion and not reduced:
                        self.assertTrue(any(palette != final_palette for palette in terminal.palettes),
                                        "Resuming must visibly animate, not substitute a static frame")
                    else:
                        self.assertTrue(all(palette == final_palette for palette in terminal.palettes),
                                        "Disabled motion must remain at the bright resting frame")
                    self.assertIn("BANNER_RESUME_HISTORY", "\n".join(terminal.screen.display))
                    self.assertEqual(len(self.prompt_requests("BANNER_RESUME_HISTORY")), 1)
                    terminal.capture(f"banner-resumed-{motion}-{reduced}")
                    if motion and not reduced:
                        terminal.send("/new\r")
                        terminal.until(lambda: any("New session started" in row for row in terminal.screen.display))
                        terminal.pump(1.0)
                        self.assert_welcome(terminal)
                        self.assert_banner_color(terminal, "truecolor")
                        self.assertEqual(terminal.palettes[-1], final_palette,
                                         "/new must retain the bright settled banner")
                        terminal.capture("banner-native-new-settled")
                finally:
                    terminal.close()

    def test_long_workflow_resize_preserves_banner_status_and_draft(self):
        self.configure()
        self.workflow = "native-test-with-an-exceptionally-long-workflow-name-for-banner-resize"
        create(self.config, self.workflow, self.root / "workspace", "example")
        terminal = Terminal(self, 120)
        self.ready(terminal)
        self.assert_welcome(terminal, workflow=self.workflow)
        banner = "\n".join(terminal.screen.display[y] for y in self.banner_rows(terminal))
        self.assertIn("native-test-with", banner)
        terminal.send("draft 界")
        terminal.until(lambda: "draft 界" in "\n".join(terminal.screen.display))
        for columns in (40, 80, 24):
            with self.subTest(columns=columns):
                terminal.resize(columns)
                self.ready(terminal)
                self.assert_welcome(terminal, workflow=self.workflow)
                self.assertIn("draft 界", terminal.screen.display[terminal.screen.cursor.y])
                self.assertIsNotNone(self.status_row(terminal))
                if columns < 60:
                    banner = "\n".join(terminal.screen.display[y] for y in self.banner_rows(terminal))
                    self.assertIn("PAVEWAY", banner)
                    self.assertNotIn(self.workflow, banner)
                terminal.capture(f"banner-settled-narrow-{columns}")
        self.assertEqual(self.server.requests, [])
        terminal.send(b"\x03")
        terminal.pump(.2)

    def test_static_motion_and_terminal_color_capabilities(self):
        cases = (("256color", False, False), ("none", False, False),
                 ("force-color-zero", False, False), ("dumb", False, False),
                 ("truecolor", True, True))
        for mode, motion, reduced_motion in cases:
            with self.subTest(mode=mode, motion=motion, reduced_motion=reduced_motion):
                self.configure(mode, motion=motion, reduced_motion=reduced_motion)
                terminal = Terminal(self, 120)
                try:
                    self.ready(terminal)
                    static = self.banner_snapshot(terminal)
                    # Sample across the whole entrance, not just once it ends:
                    # both motion=false and REDUCED_MOTION=1 start at rest.
                    for _ in range(8):
                        terminal.pump(.12)
                        self.assertEqual(self.banner_snapshot(terminal), static)
                    self.assert_welcome(terminal)
                    self.assert_banner_color(terminal, mode)
                    terminal.capture(f"banner-static-{mode}")
                finally:
                    terminal.close()
        self.assertEqual(self.server.requests, [])
