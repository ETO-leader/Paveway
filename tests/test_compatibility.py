"""Runtime compatibility tests use real startup/IPC and the shared fake OMP executable."""

import asyncio
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import __version__
from oicoach_runtime.build_id import compute_build_id, runtime_build_id
from oicoach_runtime.compatibility import (
    EXPECTED_NATIVE_PROTOCOL_VERSION,
    check_runtime_compatibility,
    probe_daemon,
    restart_guidance,
)
from oicoach_runtime.config import AgentConfig, Config, ServerConfig, config_text, ensure_private_directory, defaults, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.scheduler import Runtime


PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_DIR / "paveway.sh"


class BuildIdComputationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-build-id-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_compute_build_id_deterministic(self):
        """Build ID computation produces the same SHA-256 for identical file tree."""
        (self.root / "sub").mkdir(parents=True)
        (self.root / "a.py").write_text("print('hello')", encoding="utf-8")
        (self.root / "sub/b.mjs").write_text("export default 1;", encoding="utf-8")

        digest1 = compute_build_id(self.root)
        digest2 = compute_build_id(self.root)
        self.assertEqual(digest1, digest2)
        self.assertEqual(len(digest1), 64)

    def test_build_id_changes_when_code_modified(self):
        """Build ID changes when a source file is modified."""
        target = self.root / "module.py"
        target.write_text("v = 1\n", encoding="utf-8")
        digest1 = compute_build_id(self.root)

        target.write_text("v = 2\n", encoding="utf-8")
        digest2 = compute_build_id(self.root)
        self.assertNotEqual(digest1, digest2)

    def test_build_id_changes_when_file_added(self):
        """Build ID changes when a new source or asset file is added."""
        (self.root / "module.py").write_text("v = 1\n", encoding="utf-8")
        digest1 = compute_build_id(self.root)

        (self.root / "extra.md").write_text("# Doc\n", encoding="utf-8")
        digest2 = compute_build_id(self.root)
        self.assertNotEqual(digest1, digest2)

    def test_build_id_tracks_windows_notification_script(self):
        target = self.root / "desktop-notification.ps1"
        target.write_text("Write-Output 'first'", encoding="utf-8")
        before = compute_build_id(self.root)
        target.write_text("Write-Output 'updated'", encoding="utf-8")
        self.assertNotEqual(before, compute_build_id(self.root))

    def test_build_id_ignores_pyc_cache_and_runtime_data(self):
        """Build ID ignores .pyc files, __pycache__, .git, and .runtime directories."""
        (self.root / "module.py").write_text("v = 1\n", encoding="utf-8")
        digest1 = compute_build_id(self.root)

        # Add ignored items
        (self.root / "module.pyc").write_bytes(b"bytecode")
        pycache = self.root / "__pycache__"
        pycache.mkdir()
        (pycache / "module.cpython-311.pyc").write_bytes(b"cached bytecode")
        git_dir = self.root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        runtime_dir = self.root / ".runtime"
        runtime_dir.mkdir()
        (runtime_dir / "data.db").write_bytes(b"sqlite data")

        digest2 = compute_build_id(self.root)
        self.assertEqual(digest1, digest2)

    def test_wheel_and_editable_install_layout(self):
        """Simulate wheel (pure package directory) and editable install layout."""
        # Simulated wheel package directory
        wheel_dir = self.root / "oicoach_runtime"
        wheel_dir.mkdir()
        (wheel_dir / "__init__.py").write_text("__version__ = '0.21.2'\n", encoding="utf-8")
        (wheel_dir / "core.py").write_text("pass\n", encoding="utf-8")
        bundled = wheel_dir / "bundled"
        bundled.mkdir()
        (bundled / "core.md").write_text("Rules\n", encoding="utf-8")

        digest_wheel = compute_build_id(wheel_dir)
        self.assertEqual(len(digest_wheel), 64)

        # In editable install, the package root is also a package directory with the same files
        editable_dir = self.root / "editable" / "src" / "oicoach_runtime"
        editable_dir.mkdir(parents=True)
        (editable_dir / "__init__.py").write_text("__version__ = '0.21.2'\n", encoding="utf-8")
        (editable_dir / "core.py").write_text("pass\n", encoding="utf-8")
        bundled_ed = editable_dir / "bundled"
        bundled_ed.mkdir()
        (bundled_ed / "core.md").write_text("Rules\n", encoding="utf-8")

        digest_editable = compute_build_id(editable_dir)
        self.assertEqual(digest_wheel, digest_editable)


class CompatibilityLogicTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-compat-")
        self.config_path = Path(self.temp_dir.name) / "config.toml"
        self.config_path.write_text("[server]\nsocket_path = 'fake.sock'\n", encoding="utf-8")
        self.current_build_id = runtime_build_id()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_case_b_compatible_daemon_accepted(self):
        """Case B: daemon online, version, build_id, and native_protocol match -> accepted."""
        status = {
            "daemon": "running",
            "runtime_version": __version__,
            "runtime_build_id": self.current_build_id,
            "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
        }
        # Should not raise
        check_runtime_compatibility(status, self.config_path)

    def test_case_c_version_mismatch_rejected(self):
        """Case C: daemon online, but version is different -> rejected."""
        status = {
            "daemon": "running",
            "runtime_version": "0.20.0",
            "runtime_build_id": self.current_build_id,
            "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
        }
        with self.assertRaises(RuntimeContractError) as ctx:
            check_runtime_compatibility(status, self.config_path)
        msg = str(ctx.exception)
        self.assertIn("运行中的 Paveway daemon 版本与当前客户端不一致", msg)
        self.assertIn(__version__, msg)
        self.assertIn("0.20.0", msg)
        self.assertIn("已有任务不会被中断或修改", msg)

    def test_case_d_same_version_different_build_id_rejected(self):
        """Case D: same version, but build_id is different -> rejected."""
        status = {
            "daemon": "running",
            "runtime_version": __version__,
            "runtime_build_id": "0" * 64,
            "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
        }
        with self.assertRaises(RuntimeContractError) as ctx:
            check_runtime_compatibility(status, self.config_path)
        msg = str(ctx.exception)
        self.assertIn("检测到运行中的 Paveway daemon 与当前代码不一致", msg)
        self.assertIn("当前版本与 daemon 版本相同，但代码构建指纹不同", msg)
        self.assertIn("请确认没有正在执行的任务，然后重启 Paveway Runtime", msg)

    def test_case_e_missing_build_id_rejected(self):
        """Case E: old daemon missing runtime_build_id -> rejected."""
        status = {
            "daemon": "running",
            "runtime_version": __version__,
            "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
        }
        with self.assertRaises(RuntimeContractError) as ctx:
            check_runtime_compatibility(status, self.config_path)
        msg = str(ctx.exception)
        self.assertIn("不支持当前版本的构建指纹检查", msg)
        self.assertIn("请重启 Runtime 后重试", msg)

    def test_case_f_native_protocol_mismatch_rejected(self):
        """Case F: native_protocol_version mismatch -> rejected."""
        status = {
            "daemon": "running",
            "runtime_version": __version__,
            "runtime_build_id": self.current_build_id,
            "native_protocol_version": 999,
        }
        with self.assertRaises(RuntimeContractError) as ctx:
            check_runtime_compatibility(status, self.config_path)
        msg = str(ctx.exception)
        self.assertIn("原生协议版本", msg)
        self.assertIn("已有任务不会被中断或修改", msg)

    def test_restart_guidance_unmanaged(self):
        """Restart guidance for unmanaged standalone daemon provides pgrep / kill instructions."""
        guidance = restart_guidance(self.config_path)
        self.assertIn("独立后台进程", guidance)
        self.assertIn("pgrep -fa \"oicoach_runtime serve\"", guidance)
        self.assertIn("kill <PID>", guidance)
        self.assertNotIn("kill $(cat daemon.pid)", guidance)

    def test_restart_guidance_systemd(self):
        """Restart guidance for systemd managed service provides service restart command."""
        with patch("oicoach_runtime.service.status", return_value={"managed": True}):
            guidance = restart_guidance(self.config_path)
            self.assertIn("systemd 托管", guidance)
            self.assertIn("service restart", guidance)


class DaemonFrozenBuildIdTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-daemon-frozen-")
        self.root = Path(self.temp_dir.name)
        self.config = Config(
            state_dir=self.root / "state",
            workflow_root=self.root / "workflows",
            server=ServerConfig(socket_path=str(self.root / "state" / "runtime.sock")),
        )
        ensure_private_directory(self.config.state_dir)
        ensure_private_directory(self.config.workflow_root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daemon_status_returns_frozen_build_id(self):
        """Daemon freezes runtime_build_id at instantiation and returns it in status."""
        runtime = Runtime(self.config)
        self.assertEqual(runtime.runtime_build_id, runtime_build_id())

        # Mock store.status
        class FakeStore:
            def status(self):
                return {"supervisor": {"phase": "running"}}
        runtime.store = FakeStore()

        status = runtime.dispatch_api("status", {})
        self.assertEqual(status["daemon"], "running")
        self.assertEqual(status["runtime_version"], __version__)
        self.assertEqual(status["native_protocol_version"], 1)
        self.assertEqual(status["runtime_build_id"], runtime.runtime_build_id)

    def test_daemon_build_id_does_not_change_if_source_files_modified_later(self):
        """Simulate daemon started: modifying files does not alter already-instantiated daemon's build_id."""
        pkg_dir = self.root / "simulated_pkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("__version__ = '0.21.2'\n", encoding="utf-8")
        (pkg_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")

        initial_id = compute_build_id(pkg_dir)
        runtime = Runtime(self.config)
        runtime.runtime_build_id = initial_id

        # Now simulate user modifying code on disk
        (pkg_dir / "mod.py").write_text("x = 2\n", encoding="utf-8")
        new_client_id = compute_build_id(pkg_dir)

        self.assertNotEqual(initial_id, new_client_id)

        # But daemon's build_id remains initial_id
        class FakeStore:
            def status(self):
                return {"supervisor": {"phase": "running"}}
        runtime.store = FakeStore()
        status = runtime.dispatch_api("status", {})
        self.assertEqual(status["runtime_build_id"], initial_id)


class PavewayStartupCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-paveway-startup-")
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.toml"
        self.workflow_dir = self.root / "workflows"
        self.state_dir = self.root / "state"
        self.workspace_dir = self.root / "workspace"
        fake_omp = PROJECT_DIR / "tests" / "fake_omp.py"
        fake_omp.chmod(0o755)

        self.config = Config(
            state_dir=self.state_dir,
            workflow_root=self.workflow_dir,
            server=ServerConfig(socket_path=str(self.state_dir / "runtime.sock")),
            agent=AgentConfig(executable=str(fake_omp)),
        )
        ensure_private_directory(self.state_dir)
        ensure_private_directory(self.workflow_dir)
        ensure_private_directory(self.workspace_dir)
        self.config_path.write_text(config_text(self.config), encoding="utf-8")

        # Create one existing workflow so startup does not prompt interactively
        from oicoach_runtime.workflow_setup import create
        create(self.config, "test-wf", self.workspace_dir)

    def tearDown(self):
        try:
            pid_file = self.state_dir / "daemon.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    pid_fd = os.pidfd_open(pid)
                except (ProcessLookupError, ValueError):
                    pass
                else:
                    try:
                        signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
                        if not select.select([pid_fd], [], [], 5)[0]:
                            signal.pidfd_send_signal(pid_fd, signal.SIGKILL)
                            self.assertTrue(select.select([pid_fd], [], [], 5)[0],
                                            "Test daemon did not exit after SIGKILL")
                    finally:
                        os.close(pid_fd)
        finally:
            self.temp_dir.cleanup()

    def _run_paveway(self, extra_env=None):
        import pty
        master, slave = pty.openpty()
        env = {
            **os.environ,
            "PAVEWAY_PROBE_STARTUP_ONLY": "1",
            "PAVEWAY_PYTHON": sys.executable,
            **(extra_env or {}),
        }
        proc = subprocess.Popen(
            [str(SCRIPT_PATH), "--config", str(self.config_path), "--workflow", "test-wf"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        output = b""
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try:
                        chunk = os.read(master, 4096)
                        if not chunk:
                            break
                        output += chunk
                    except OSError:
                        break
                elif proc.poll() is not None:
                    break
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        return proc.returncode, output.decode("utf-8", errors="replace")

    def test_case_a_no_daemon_starts_and_verifies(self):
        """Case A: When no daemon is running, paveway.sh starts daemon and verifies compatibility."""
        code, output = self._run_paveway()
        self.assertEqual(code, 0, f"Expected 0, got {code}. Output:\n{output}")
        self.assertIn("正在启动后台 Runtime…", output)

        status = probe_daemon(self.config.server.socket_path)
        self.assertIsNotNone(status)
        self.assertEqual(status["runtime_version"], __version__)
        self.assertEqual(status["native_protocol_version"], EXPECTED_NATIVE_PROTOCOL_VERSION)
        self.assertEqual(status["runtime_build_id"], runtime_build_id())

    def test_case_b_compatible_daemon_reused(self):
        """Case B: When compatible daemon is running, paveway.sh reuses it without starting another."""
        # First start daemon
        code1, _ = self._run_paveway()
        self.assertEqual(code1, 0)

        pid_file = self.state_dir / "daemon.pid"
        self.assertTrue(pid_file.exists())
        pid1 = pid_file.read_text().strip()

        # Second launch should reuse
        code2, output2 = self._run_paveway()
        self.assertEqual(code2, 0, f"Expected 0, got {code2}. Output:\n{output2}")
        self.assertNotIn("正在启动后台 Runtime…", output2)
        self.assertEqual(pid_file.read_text().strip(), pid1)

    def test_case_d_incompatible_build_id_automatically_restarts_idle_daemon(self):
        """Case D: An idle daemon running stale code is handed off and replaced."""
        # Start a daemon that reports a frozen pre-upgrade build fingerprint but
        # still implements the lifecycle protocol.
        log_path = self.root / "stale-daemon.log"
        with log_path.open("w") as log:
            stale = subprocess.Popen(
                [sys.executable, "-c",
                 "import oicoach_runtime.build_id as b; b._CURRENT_BUILD_ID='0'*64; "
                 "from oicoach_runtime.cli import main; raise SystemExit(main())",
                 "serve", "--config", str(self.config_path)],
                stdout=log, stderr=log, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 20
        old = None
        self.addCleanup(lambda: stale.poll() is None and (stale.kill(), stale.wait()))
        while time.monotonic() < deadline:
            old = probe_daemon(self.config.server.socket_path)
            if old is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(old, "Stale daemon did not become ready")
        self.assertEqual(old["runtime_build_id"], "0" * 64)

        code, output = self._run_paveway()
        self.assertEqual(code, 0, f"Expected 0, got {code}. Output:\n{output}")
        self.assertIn("正在检查活动状态并安全重启", output)
        self.assertIn("Runtime 已使用当前代码重启", output)
        stale.wait(timeout=10)
        self.assertEqual(stale.returncode, 0)
        current = probe_daemon(self.config.server.socket_path)
        self.assertIsNotNone(current)
        self.assertEqual(current["runtime_build_id"], runtime_build_id())
        self.assertNotEqual(current["runtime_instance"], old["runtime_instance"])

    def test_old_daemon_without_handoff_protocol_is_not_force_killed(self):
        """A daemon that cannot prove quiescence falls back to explicit manual guidance."""
        fake_status = {"daemon": "running", "runtime_version": __version__,
                       "runtime_build_id": "mismatched", "native_protocol_version": 1}
        from oicoach_runtime import launcher
        with patch.object(launcher, "probe_daemon", return_value=fake_status), \
                patch.object(launcher.service, "status", return_value={"managed": False}):
            with self.assertRaises(RuntimeContractError) as ctx:
                launcher.ensure_runtime(load_config(self.config_path), self.config_path)
        message = str(ctx.exception)
        self.assertIn("不支持空闲检查与安全交接协议", message)
        self.assertIn("未发送终止信号", message)
        self.assertIn("pgrep -fa \"oicoach_runtime serve\"", message)


if __name__ == "__main__":
    unittest.main()
