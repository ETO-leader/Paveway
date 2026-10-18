"""Actual model acceptance for the bundled coach, using only supplied training data."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.workflow_setup import create


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for a real coach model turn")
class CoachTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_coach_ask_and_saved_reply_without_tijian(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-coach-") as directory:
            root = Path(directory)
            path = root / "config.toml"
            initialize(path, str(root / "state"), str(root / "workflows"))
            config = load_config(path)
            config = replace(config, server=replace(config.server, poll_interval_seconds=0.05),
                agent=replace(base.agent, tools_enabled=False, extensions_enabled=False, skills_enabled=False,
                    rules_enabled=False, startup_timeout_seconds=30, turn_timeout_seconds=90,
                    settle_timeout_seconds=15, graceful_stop_seconds=1))
            path.write_text(config_text(config))
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            create(config, "coach", workspace)
            marker = "COACH_READY_" + uuid.uuid4().hex
            message = root / "request.txt"
            message.write_text(
                "请按用户数据给一份简短 OI 训练安排，300 字以内，不联网、不修改题库、不安排自动提醒。\n"
                "日期：2026-10-18；时区 Asia/Singapore；可用 16:00–18:00，其中 17:00–17:15 休息。\n"
                "TODO：P1001，已会思路但尚未编码；P1002，尚未读题。先给思考与编码时间，再说明检查点。\n"
                "题目只用这两个标识，不猜测题面或难度。最后一行写 " + marker + "。\n")
            async def api(command, arguments=None):
                return await asyncio.to_thread(request, config.server.socket_path, command, arguments)
            with (root / "daemon.log").open("w") as log:
                daemon = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "serve",
                    "--config", str(path), stdout=log, stderr=log)
                try:
                    async with asyncio.timeout(10):
                        while True:
                            self.assertIsNone(daemon.returncode)
                            try:
                                if (await api("status"))["daemon"] == "running":
                                    break
                            except RuntimeContractError:
                                pass
                            await asyncio.sleep(0.05)
                    arguments = [sys.executable, "-m", "oicoach_runtime", "ask", "--config", str(path),
                        "--workflow", "coach", "--task", "training", "--mode", "plan", "--message-file", str(message),
                        "--dedupe-key", "coach-request", "--json"]
                    cli = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(cli.communicate(), 10)
                    self.assertEqual(cli.returncode, 0, stderr.decode())
                    event = json.loads(stdout)["result"]
                    async with asyncio.timeout(150):
                        while True:
                            current = await api("events.show", {"event_id": event["event_id"]})
                            if current["status"] in ("succeeded", "failed", "blocked", "uncertain", "dead_letter"):
                                break
                            await asyncio.sleep(0.1)
                    self.assertEqual(current["status"], "succeeded", current)
                    output = await api("runs.output", {"run_id": current["run_id"]})
                    self.assertEqual(output["status"], "available")
                    self.assertIn(marker, output["text"])
                    self.assertIn("P1001", output["text"])
                    self.assertIn("P1002", output["text"])
                    self.assertNotIn(marker, (root / "daemon.log").read_text())
                    repeated = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, _ = await asyncio.wait_for(repeated.communicate(), 10)
                    self.assertEqual(repeated.returncode, 0)
                    self.assertEqual(json.loads(stdout)["result"]["event_id"], event["event_id"])
                    self.assertEqual(len(await api("runs.list")), 1)
                    first_run = current["run_id"]
                    for mode, text in (
                        ("checkin", "P1001 已独立通过，P1002 看了提示后口胡但没有编码，还剩 20 分钟。请基于原安排调整，不联网、不修改题库、不安排提醒，200 字内。"),
                        ("review", "请结合本任务前面两轮记录复盘，区分 P1001 独立完成和 P1002 借助提示口胡，给下一次训练建议。不联网、不修改题库、不安排提醒，200 字内。")):
                        token = mode.upper() + "_" + uuid.uuid4().hex
                        followup = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "ask",
                            "--config", str(path), "--workflow", "coach", "--task", "training", "--mode", mode,
                            "--message", text + " 最后一行写 " + token,
                            "--dedupe-key", "coach-" + mode, "--wait", "--timeout", "140", "--json",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        stdout, stderr = await asyncio.wait_for(followup.communicate(), 150)
                        self.assertEqual(followup.returncode, 0, stderr.decode())
                        delivered = json.loads(stdout)["result"]
                        self.assertEqual(delivered["event"]["status"], "succeeded")
                        self.assertEqual(delivered["output"]["status"], "available")
                        self.assertIn(token, delivered["output"]["text"])
                        self.assertIn("P1002", delivered["output"]["text"])
                        self.assertNotIn(token, (root / "daemon.log").read_text())
                    self.assertEqual(len(await api("runs.list")), 3)
                    self.assertIn(marker, (await api("runs.output", {"run_id": first_run}))["text"])
                finally:
                    if daemon.returncode is None:
                        daemon.terminate()
                    try:
                        await asyncio.wait_for(daemon.wait(), 10)
                    except TimeoutError:
                        daemon.kill()
                        await daemon.wait()
