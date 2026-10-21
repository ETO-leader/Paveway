"""Opt-in real-model acceptance of the UI -> CLI -> daemon -> saved reply path."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.tui import CliClient, TerminalUI
from oicoach_runtime.workflow_setup import create
from test_tui import collect


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for a real UI model reply")
class RealUITests(unittest.TestCase):
    def test_ui_cli_submission_polling_and_real_reply(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-ui-model-") as directory:
            root = Path(directory)
            path = root / "config.toml"
            initialize(path, str(root / "state"), str(root / "workflows"))
            config = load_config(path)
            config = replace(config, server=replace(config.server, poll_interval_seconds=0.05),
                agent=replace(base.agent, tools_enabled=False, extensions_enabled=False, skills_enabled=False,
                    rules_enabled=False, startup_timeout_seconds=30, turn_timeout_seconds=90,
                    settle_timeout_seconds=15, graceful_stop_seconds=1))
            path.write_text(config_text(config))
            create(config, "coach", root)
            marker = "UI_REPLY_" + uuid.uuid4().hex
            with (root / "daemon.log").open("w") as log:
                daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(path)], stdout=log, stderr=log)
                client = CliClient(path)
                ui = TerminalUI(client, "coach", "ui-real", input=DummyInput(), output=DummyOutput())
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        self.assertIsNone(daemon.poll())
                        try:
                            request(config.server.socket_path, "status")
                            break
                        except RuntimeContractError:
                            time.sleep(0.05)
                    ui.editor.text = "请只回复‘终端对话可用’以及识别码 " + marker + "，不调用工具，不安排提醒。"
                    ui.accept()
                    collect(ui)
                    self.assertIsNotNone(ui.pending, ui.notice)
                    event_id = ui.pending["event_id"]
                    deadline = time.monotonic() + 130
                    while ui.pending and time.monotonic() < deadline:
                        ui.poll()
                        ui.collect()
                        time.sleep(0.05)
                    self.assertIsNone(ui.pending, ui.notice)
                    text = "\n".join(ui.chat[ui.scope])
                    self.assertIn("GPT · succeeded", text)
                    self.assertIn("终端对话可用", text)
                    event = client.call("events", "show", event_id)
                    output = client.call("runs", "output", event["run_id"])
                    self.assertEqual(event["attempt"], 1)
                    self.assertIn(marker, output["text"])
                    self.assertNotIn(marker, (root / "daemon.log").read_text())
                    ui.draw()
                    self.assertIn(marker, ui.transcript.text)
                    client.close()
                    self.assertIsNone(daemon.poll())
                finally:
                    client.close()
                    if ui.worker:
                        ui.worker.join(timeout=3)
                    if daemon.poll() is None:
                        daemon.terminate()
                    try:
                        daemon.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=5)
