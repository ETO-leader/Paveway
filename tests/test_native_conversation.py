"""Real OMP editing and conversation with isolated loopback inference."""

import threading

from test_native_omp import NativeFixture
from test_native_statusline import Terminal


class NativeConversationTests(NativeFixture):
    def test_multiline_paste_reflows_and_remains_editable_and_recallable(self):
        terminal = Terminal(self, columns=80)
        first_line = "中文 café 🙂 first line extends beyond forty columns"
        draft = first_line + "\n第二行 tail"
        terminal.send("\x1b[200~" + draft + "\x1b[201~")
        terminal.until(lambda: first_line in terminal.text and "第二行 tail" in terminal.editor_line)

        terminal.resize(40)
        terminal.until(lambda: "中文 café" in terminal.text and "forty columns" in terminal.text
                       and first_line not in terminal.text and "第二行 tail" in terminal.editor_line)
        self.assertEqual(self.server.requests, [], "Pasting and resizing must not submit the draft")

        # Move left from the end, replace the penultimate character, and submit.
        terminal.send(b"\x1b[D\x7fX\x1b[C\r")
        submitted = first_line + "\n第二行 taXl"
        terminal.until(lambda: len(self.prompt_requests(submitted)) == 1 and "M0_REPLY" in terminal.text)
        terminal.send(b"\x1b[A")
        terminal.until(lambda: any("第二行 taXl" in row for row in
                                   terminal.screen.display[terminal.screen.cursor.y:terminal.screen.cursor.y + 4]))
        self.assertEqual(len(self.prompt_requests(submitted)), 1)

    def test_streamed_conversation_keeps_next_unsent_draft(self):
        release = threading.Event()

        def response(_body):
            yield {"role": "assistant", "content": "STREAM_HEAD\n\n"}, None
            release.wait(20)
            yield {"content": "## 观察\n\n- 检查边界\n\nSTREAM_TAIL"}, None
            yield {}, "stop"

        self.server.response_factory = response
        terminal = Terminal(self, columns=100)
        try:
            terminal.send("你好，检查我的证明。\r")
            terminal.until(lambda: "STREAM_HEAD" in terminal.text)
            self.assertIn("你好，检查我的证明。", terminal.text)
            terminal.send("next draft 中文")
            terminal.until(lambda: "next draft 中文" in terminal.editor_line)
        finally:
            release.set()

        terminal.until(lambda: "STREAM_TAIL" in terminal.text and "next draft 中文" in terminal.editor_line)
        self.assertIn("检查边界", terminal.text)
        self.assertEqual(len(self.prompt_requests("你好，检查我的证明。")), 1)
        self.assertEqual(self.prompt_requests("next draft 中文"), [])
