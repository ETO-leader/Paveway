"""Paveway's startup widget in the real OMP terminal."""

from oicoach_runtime import __version__
from oicoach_runtime.omp_installation import omp_version
from test_native_omp import NativeFixture
from test_native_statusline import Terminal


HEADING = "OI TRAINING RUNTIME"


class NativeBannerTests(NativeFixture):
    def test_welcome_identifies_the_runtime_and_leaves_the_conversation(self):
        host_version = omp_version(self.config.agent.executable)
        terminal = Terminal(self)
        terminal.until(lambda: HEADING in terminal.text and "Tip:" in terminal.text)
        version_line = next(row for row in terminal.screen.display if f"Paveway v{__version__}" in row)
        self.assertIn("native-test", version_line)
        self.assertIn(f"OMP {host_version}", version_line)
        self.assertEqual(self.server.requests, [])

        terminal.send("WELCOME_REMOVAL_TURN\r")
        terminal.until(lambda: "M0_REPLY" in terminal.text and HEADING not in terminal.text)
        self.assertNotIn(version_line.strip(), terminal.text)
        self.assertEqual(len(self.prompt_requests("WELCOME_REMOVAL_TURN")), 1)

    def test_short_terminal_shows_compact_branding_and_expands_on_resize(self):
        self.env["NO_COLOR"] = "1"
        terminal = Terminal(self, columns=40, rows=12)
        terminal.until(lambda: any(row.strip() == "PAVEWAY" for row in terminal.screen.display))
        self.assertNotIn(HEADING, terminal.text)

        terminal.resize(80)
        terminal.until(lambda: HEADING in terminal.text and "Tip:" in terminal.text and terminal.status)
        self.assertEqual(self.server.requests, [])
