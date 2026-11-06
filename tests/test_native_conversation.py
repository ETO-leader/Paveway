"""Real Paveway TUI: native composer, chat, menus and loopback streaming only."""
import fcntl
import json
from pathlib import Path
import signal
import struct
import termios
import threading
import unicodedata

from test_native_omp import NativeFixture
from test_native_statusline import COLOR_ENV, Terminal


class NativeConversationTests(NativeFixture):
    def configure(self, color_mode="truecolor", preset="unicode"):
        self.color_mode = color_mode
        for key in COLOR_ENV:
            self.env.pop(key, None)
        self.env["TERM"] = "xterm-256color"
        if color_mode == "truecolor":
            self.env["COLORTERM"] = "truecolor"
        elif color_mode == "none":
            self.env["NO_COLOR"] = "1"
        self.env["PAVEWAY_STATUSLINE_CAPS"] = "plain"
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        settings = json.loads(path.read_text())
        settings.update({"tui": {"motion": False}, "symbolPreset": preset})
        path.write_text(json.dumps(settings))

    def test_multiline_edit_menu_restore_resize_and_history(self):
        self.configure()
        terminal = Terminal(self, 80)
        # Slash completion and the native command selector must still own focus.
        terminal.send("/paveway settings\r")
        terminal.until(lambda: any("Paveway / Settings" in row for row in terminal.screen.display))
        terminal.capture("composer-settings")
        terminal.send(b"\x1b")
        terminal.pump(.3)
        draft = "中文 e\u0301 🙂 first\n第二行 tail"
        terminal.send(b"\x1b[200~" + draft.encode() + b"\x1b[201~")
        terminal.until(lambda: any("第二行 tail" in row for row in terminal.screen.display))
        terminal.capture("composer-multiline")
        terminal.resize(40)
        terminal.until(lambda: any("第二行 tail" in row for row in terminal.screen.display))
        caret = terminal.screen.cursor
        self.assertIn("第二行 tail", terminal.screen.display[caret.y])
        tail_start = next(x for x in range(terminal.screen.columns - 3)
                          if "".join(terminal.screen.buffer[caret.y][x + offset].data
                                     for offset in range(4)) == "tail")
        self.assertEqual(caret.x, tail_start + 4, "Native caret must include composer chrome and wide cells")
        # Left/backspace must operate at the native caret, not at an added border.
        terminal.send(b"\x1b[D\x7fX\x1b[C\r")
        submitted = "中文 e\u0301 🙂 first\n第二行 taXl"
        terminal.until(lambda: bool(self.prompt_requests(unicodedata.normalize("NFC", submitted))))
        terminal.until(lambda: any("M0_REPLY" in row for row in terminal.screen.display))
        terminal.capture("chat-narrow")
        terminal.send(b"\x1b[A")
        terminal.until(lambda: any("第二行 taXl" in row for row in terminal.screen.display))
        terminal.capture("composer-history")
        # Clear the recalled multiline entry before Terminal.close submits /exit.
        terminal.send(b"\x03")
        terminal.pump(.2)

    def test_streaming_chat_preserves_draft_and_role_boundaries(self):
        self.configure()
        release = threading.Event()
        self.addCleanup(release.set)

        def response(_body):
            yield {"role": "assistant", "content": "STREAM_HEAD\n\n"}, None
            release.wait(12)
            yield {"content": "## 观察\n\n中英文 mixed，先取 $x=1$。\n\n- 保留列表\n- 检查边界\n\n```python\nprint('中文')\n```\n\n> 引用\n\nSTREAM_TAIL"}, None
            yield {}, "stop"

        self.server.response_factory = response
        terminal = Terminal(self, 100)
        terminal.send("你好\r")
        terminal.until(lambda: any("STREAM_HEAD" in row for row in terminal.screen.display))
        terminal.send("next draft 中文")
        terminal.until(lambda: any("next draft 中文" in row for row in terminal.screen.display))
        terminal.capture("chat-streaming")
        release.set()
        terminal.until(lambda: any("STREAM_TAIL" in row for row in terminal.screen.display))
        self.assertTrue(any("next draft 中文" in row for row in terminal.screen.display))
        self.assertEqual(len(self.prompt_requests("你好")), 1)
        terminal.capture("chat-markdown")
        terminal.send(b"\x03")
        terminal.pump(.2)
        # A command notification is not a second assistant answer.
        terminal.send("/coach\r")
        terminal.until(lambda: any("Context: OMP native" in row for row in terminal.screen.display))
        terminal.capture("chat-system-event")

    def test_plain_and_limited_color_small_terminal(self):
        for mode, preset in (("256color", "unicode"), ("none", "ascii")):
            with self.subTest(mode=mode):
                self.configure(mode, preset)
                terminal = Terminal(self, 40)
                previous = len(self.prompt_requests("短消息"))
                terminal.send("短消息\r")
                terminal.until(lambda: len(self.prompt_requests("短消息")) == previous + 1 and
                               any("M0_REPLY" in row for row in terminal.screen.display))
                terminal.send("draft 界")
                terminal.until(lambda: any("draft 界" in row for row in terminal.screen.display))
                terminal.capture("chat-" + mode)
                terminal.screen.resize(lines=12, columns=40)
                fcntl.ioctl(terminal.master, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
                terminal.process.send_signal(signal.SIGWINCH)
                terminal.pump(.5)
                self.assertTrue(any("draft 界" in row for row in terminal.screen.display))
                self.assertIsNotNone(terminal.status())
                terminal.capture("chat-short-" + mode)
                terminal.send(b"\x03")
                terminal.pump(.2)
                terminal.close()
