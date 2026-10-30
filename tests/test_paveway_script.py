"""Targeted tests for paveway.sh startup and workflow creation behaviors."""

import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.workflow_setup import create

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_DIR / "paveway.sh"


class PavewayScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-script-test-")
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(self.config_path)
        fake_omp = Path(__file__).resolve().parent / "fake_omp.py"
        fake_omp.chmod(0o755)
        self.config = replace(config, agent=replace(config.agent, executable=str(fake_omp)))
        self.config_path.write_text(config_text(self.config))

    def tearDown(self):
        pid_file = self.root / "state/daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                for _ in range(40):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.05)
                    except OSError:
                        break
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            except Exception:
                pass
        self.temp.cleanup()

    def _run_script(self, args=(), input_lines=(), pty_eof=False, timeout=15):
        master, slave = pty.openpty()
        env = dict(os.environ, PAVEWAY_PROBE_STARTUP_ONLY="1", PAVEWAY_PYTHON=sys.executable)
        cmd = [str(SCRIPT_PATH), "--config", str(self.config_path), *args]
        proc = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True
        )
        os.close(slave)

        output = b""
        deadline = time.monotonic() + timeout

        try:
            for line in input_lines:
                # Wait until prompt appears
                while time.monotonic() < deadline and b"> " not in output:
                    r, _, _ = select.select([master], [], [], 0.1)
                    if r:
                        try:
                            chunk = os.read(master, 1024)
                            if not chunk:
                                break
                            output += chunk
                        except OSError:
                            break
                os.write(master, (line + "\r").encode("utf-8"))
                time.sleep(0.1)

            if pty_eof:
                while time.monotonic() < deadline and b"> " not in output:
                    r, _, _ = select.select([master], [], [], 0.1)
                    if r:
                        try:
                            chunk = os.read(master, 1024)
                            if not chunk:
                                break
                            output += chunk
                        except OSError:
                            break
                # Send Ctrl+D (EOF)
                os.write(master, b"\x04")

            # Collect remaining output until exit
            while proc.poll() is None and time.monotonic() < deadline:
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(master, 1024)
                        if not chunk:
                            break
                        output += chunk
                    except OSError:
                        break

            proc.wait(timeout=5)
            # Read any leftover bytes
            try:
                while True:
                    chunk = os.read(master, 1024)
                    if not chunk:
                        break
                    output += chunk
            except OSError:
                pass
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        return proc.returncode, output.decode("utf-8", errors="replace")

    def test_first_launch_no_workflow_interactive_creates_id(self):
        """1. 首次启动无 Workflow，输入 oi-coach，实际创建该 ID。"""
        code, out = self._run_script(input_lines=["oi-coach"])
        self.assertEqual(code, 0, out)
        self.assertIn("当前尚未创建任何 Workflow。", out)
        self.assertIn("请输入第一个 Workflow 的名称（ID）：", out)
        created = [p.name for p in (self.root / "workflows").glob("*")]
        self.assertEqual(created, ["oi-coach"])
        manifest = (self.root / "workflows/oi-coach/workflow.toml").read_text()
        self.assertIn('id = "oi-coach"', manifest)

    def test_first_launch_with_workflow_flag_creates_without_prompt(self):
        """2. 首次启动无 Workflow，传入 --workflow custom，无需交互且创建 custom。"""
        code, out = self._run_script(args=["--workflow", "custom"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("当前尚未创建任何 Workflow。", out)
        created = [p.name for p in (self.root / "workflows").glob("*")]
        self.assertEqual(created, ["custom"])
        manifest = (self.root / "workflows/custom/workflow.toml").read_text()
        self.assertIn('id = "custom"', manifest)

    def test_empty_input_reprompts_and_does_not_create_empty(self):
        """3. 空输入不能创建 Workflow。"""
        code, out = self._run_script(input_lines=["", "valid-after-empty"])
        self.assertEqual(code, 0, out)
        self.assertIn("Workflow 名称不能为空，请重新输入。", out)
        created = [p.name for p in (self.root / "workflows").glob("*")]
        self.assertEqual(created, ["valid-after-empty"])

    def test_invalid_name_cannot_bypass_validation(self):
        """4. 非法名称不能绕过现有 ID 校验。"""
        code, out = self._run_script(input_lines=["invalid name with spaces", "valid-name"])
        self.assertEqual(code, 0, out)
        self.assertIn("名称无效：", out)
        created = [p.name for p in (self.root / "workflows").glob("*")]
        self.assertEqual(created, ["valid-name"])

    def test_eof_does_not_create_workflow(self):
        """5. EOF 不创建 Workflow。"""
        code, out = self._run_script(pty_eof=True)
        self.assertNotEqual(code, 0)
        self.assertIn("已取消创建 Workflow。", out)
        created = list((self.root / "workflows").glob("*"))
        self.assertEqual(created, [])

    def test_existing_workflows_launch_without_creating_extra(self):
        """6. 已存在 Workflow 时，再次启动不创建任何额外 Workflow。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        create(self.config, "existing-wf", workspace)
        before = sorted(p.name for p in (self.root / "workflows").glob("*"))
        self.assertEqual(before, ["existing-wf"])

        code, out = self._run_script()
        self.assertEqual(code, 0, out)
        self.assertNotIn("当前尚未创建任何 Workflow。", out)
        after = sorted(p.name for p in (self.root / "workflows").glob("*"))
        self.assertEqual(after, ["existing-wf"])

    def test_existing_gpt_workflow_remains_unchanged(self):
        """7. 已有 gpt Workflow 保持原样。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        create(self.config, "gpt", workspace)
        manifest_before = (self.root / "workflows/gpt/workflow.toml").read_text()

        code, out = self._run_script()
        self.assertEqual(code, 0, out)
        self.assertNotIn("当前尚未创建任何 Workflow。", out)
        manifest_after = (self.root / "workflows/gpt/workflow.toml").read_text()
        self.assertEqual(manifest_before, manifest_after)
        workflows = [p.name for p in (self.root / "workflows").glob("*")]
        self.assertEqual(workflows, ["gpt"])

    def test_workflow_flag_selects_existing_workflow(self):
        """8. --workflow 指定已存在的 Workflow 能正常选择。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        create(self.config, "wf1", workspace)
        create(self.config, "wf2", workspace)

        code, out = self._run_script(args=["--workflow", "wf2"])
        self.assertEqual(code, 0, out)
        workflows = sorted(p.name for p in (self.root / "workflows").glob("*"))
        self.assertEqual(workflows, ["wf1", "wf2"])

    def test_workflow_flag_with_nonexistent_workflow_fails_explicitly(self):
        """9. --workflow 指定不存在的 Workflow 时不静默创建或回退。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        create(self.config, "wf1", workspace)

        code, out = self._run_script(args=["--workflow", "nonexistent"])
        self.assertNotEqual(code, 0)
        self.assertIn("指定的 Workflow 不存在：nonexistent", out)
        workflows = [p.name for p in (self.root / "workflows").glob("*")]
        self.assertEqual(workflows, ["wf1"])

    def test_initial_workflow_flag_with_invalid_name_fails_without_fallback(self):
        """初始状态下 --workflow 传入非法名称明确报错，不静默回退。"""
        code, out = self._run_script(args=["--workflow", "bad name"])
        self.assertNotEqual(code, 0)
        self.assertIn("启动失败：", out)
        workflows = list((self.root / "workflows").glob("*"))
        self.assertEqual(workflows, [])


if __name__ == "__main__":
    unittest.main()
