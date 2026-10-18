"""Real model acceptance: write a scoped note, restart, recall it in a fresh task."""

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


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for real memory acceptance")
class RealMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_writes_memory_and_fresh_task_recalls_after_restart(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-memory-") as directory:
            root = Path(directory)
            path = root / "config.toml"
            initialize(path, str(root / "state"), str(root / "workflows"))
            config = load_config(path)
            config = replace(config, server=replace(config.server, poll_interval_seconds=0.05),
                agent=replace(base.agent, tools_enabled=True, extensions_enabled=False, skills_enabled=False,
                    rules_enabled=False, startup_timeout_seconds=30, turn_timeout_seconds=120,
                    settle_timeout_seconds=15, graceful_stop_seconds=1))
            path.write_text(config_text(config))
            workspace = root / "workspace"
            workspace.mkdir(mode=0o700)
            create(config, "coach", workspace)
            marker = "MEMORY_" + uuid.uuid4().hex
            daemon = None
            async def api(command, raw=None):
                return await asyncio.to_thread(request, config.server.socket_path, command, raw)
            async def start(log):
                process = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "serve",
                    "--config", str(path), stdout=log, stderr=log)
                try:
                    async with asyncio.timeout(12):
                        while True:
                            self.assertIsNone(process.returncode)
                            try:
                                if (await api("status"))["daemon"] == "running":
                                    return process
                            except RuntimeContractError:
                                pass
                            await asyncio.sleep(0.05)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    raise
            async def stop(process):
                if process and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
            async def ask(task, message):
                process = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "ask",
                    "--config", str(path), "--workflow", "coach", "--task", task, "--message", message,
                    "--wait", "--timeout", "170", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 180)
                self.assertEqual(process.returncode, 0, stderr.decode())
                result = json.loads(stdout)["result"]
                self.assertEqual(result["event"]["status"], "succeeded")
                return result
            with (root / "daemon.log").open("w") as log:
                try:
                    daemon = await start(log)
                    written = await ask("first-day", "请记住我的长期训练偏好，调用 Runtime memory CLI 真正保存："
                        "workflow=coach，共享 scope（不要 --task），key=training-preference，title=训练偏好，kind=preference，pinned=true。"
                        "内容是：先给提示，不要直接给完整代码；识别码 " + marker + "。使用当前事件作为 source-event。"
                        "不要联网，不修改其他文件、不使用其他记忆后端、不排程。保存成功后简短说明。")
                    note = await api("memory.show", {"workflow_id": "coach", "key": "training-preference"})
                    self.assertIn(marker, note["content"])
                    self.assertEqual(note["source_event_id"], written["event"]["event_id"])
                    await stop(daemon)
                    # Disable tools in the second process: only the injected scoped note can supply the marker.
                    config = replace(config, agent=replace(config.agent, tools_enabled=False))
                    path.write_text(config_text(config))
                    daemon = await start(log)
                    recalled = await ask("second-day", "这是一个新的训练任务。请从本轮已加载的长期笔记说明我的训练偏好，"
                        "并原样写出其中的识别码。不要猜测、联网或调用工具，100字内。")
                    self.assertIn(marker, recalled["output"]["text"])
                    self.assertIn("提示", recalled["output"]["text"])
                    hits = await api("history.search", {"workflow_id": "coach", "query": marker})
                    self.assertEqual(len(hits), 2)
                    transcript = await api("history.messages", {"workflow_id": "coach", "task_id": "first-day", "query": marker})
                    self.assertTrue(transcript["messages"])
                    self.assertEqual(transcript["archive_mode"], "all_branches")
                    self.assertNotIn(marker, (root / "daemon.log").read_text())
                    sessions = list((config.state_dir / "sessions").iterdir())
                    self.assertEqual(len(sessions), 2)
                finally:
                    await stop(daemon)
