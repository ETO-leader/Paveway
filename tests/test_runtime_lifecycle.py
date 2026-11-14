"""Lifecycle regressions: safe handoff, activity blocking, ownership, concurrency."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import service
from oicoach_runtime.cli import initialize
from oicoach_runtime.compatibility import probe_daemon
from oicoach_runtime.config import AgentConfig, Config, ServerConfig, config_text, ensure_private_directory, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.launcher import (
    _lifecycle_target,
    ensure_runtime,
    ownership_path,
    read_ownership,
    shutdown_owned_runtime,
)
from oicoach_runtime.models import utc_text
from oicoach_runtime.workflow_setup import create

PROJECT_DIR = Path(__file__).resolve().parent.parent


class RuntimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-lifecycle-")
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
            server=ServerConfig(socket_path=str(self.state_dir / "runtime.sock"), poll_interval_seconds=0.05),
            agent=AgentConfig(executable=str(fake_omp)),
        )
        ensure_private_directory(self.state_dir)
        ensure_private_directory(self.workflow_dir)
        ensure_private_directory(self.workspace_dir)
        self.config_path.write_text(config_text(self.config), encoding="utf-8")
        create(self.config, "test-wf", self.workspace_dir)
        self.stale = None
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.stale is not None and self.stale.poll() is None:
            self.stale.kill()
            self.stale.wait(timeout=5)
        status = None
        try:
            status = probe_daemon(self.config.server.socket_path)
        except RuntimeContractError:
            pass
        if status is not None:
            try:
                request(self.config.server.socket_path, "lifecycle.shutdown",
                        {**_lifecycle_target(status), "lifecycle_protocol_version": 1, "reason": "test cleanup"},
                        timeout=10)
            except RuntimeContractError:
                pass
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if probe_daemon(self.config.server.socket_path) is None:
                        break
                except RuntimeContractError:
                    break
                time.sleep(0.05)
        self.temp_dir.cleanup()

    def start_stale_daemon(self):
        log_path = self.root / "stale.log"
        log = log_path.open("w")
        self.addCleanup(log.close)
        self.stale = subprocess.Popen(
            [sys.executable, "-c",
             "import oicoach_runtime.build_id as b; b._CURRENT_BUILD_ID='0'*64; "
             "from oicoach_runtime.cli import main; raise SystemExit(main())",
             "serve", "--config", str(self.config_path)],
            stdout=log, stderr=log, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = probe_daemon(self.config.server.socket_path)
            if status is not None:
                self.assertEqual(status["runtime_build_id"], "0" * 64)
                return status
            time.sleep(0.05)
        self.fail("Stale daemon did not become ready")

    def event(self, event_id):
        return request(self.config.server.socket_path, "events.show", {"event_id": event_id})

    def wait_event(self, event_id, statuses=("succeeded",), timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.event(event_id)
            if event["status"] in statuses:
                return event
            time.sleep(0.02)
        self.fail(f"Event did not reach {statuses}: {self.event(event_id)}")

    def test_compatible_runtime_is_reused_without_new_ownership(self):
        first = ensure_runtime(self.config, self.config_path)
        self.assertTrue(first.created)
        self.assertTrue(first.launch_token)
        second = ensure_runtime(self.config, self.config_path)
        self.assertTrue(second.reused)
        self.assertFalse(second.created)
        self.assertIsNone(second.launch_token)
        self.assertEqual(second.status["runtime_instance"], first.status["runtime_instance"])

    def test_restart_preserves_future_reminder_without_duplicate_execution(self):
        old = self.start_stale_daemon()
        due = utc_text(datetime.now(timezone.utc) + timedelta(days=1))
        event = request(self.config.server.socket_path, "schedule", {
            "workflow_id": "test-wf", "task_id": "training", "reason": "future reminder",
            "dedupe_key": "future-reminder", "at": due, "payload": {}})
        handle = ensure_runtime(self.config, self.config_path)
        self.assertNotEqual(handle.status["runtime_instance"], old["runtime_instance"])
        retained = self.event(event["event_id"])
        self.assertEqual(retained["status"], "queued")
        self.assertEqual(retained["not_before_utc"], event["not_before_utc"])
        self.assertEqual(retained["version"], event["version"])
        self.assertEqual(request(self.config.server.socket_path, "runs.list"), [])

        request(self.config.server.socket_path, "events.reschedule", {
            "event_id": event["event_id"], "expected_version": retained["version"], "at": "now"})
        completed = self.wait_event(event["event_id"])
        self.assertEqual(completed["attempt"], 1)
        self.assertEqual(len(request(self.config.server.socket_path, "runs.list")), 1)

    def test_active_background_run_blocks_destructive_restart(self):
        old = self.start_stale_daemon()
        event = request(self.config.server.socket_path, "schedule", {
            "workflow_id": "test-wf", "task_id": "training", "reason": "hold",
            "dedupe_key": "hold-run", "at": "now", "payload": {"fake_mode": "hold"}})
        self.wait_event(event["event_id"], ("running",))
        with self.assertRaises(RuntimeContractError) as ctx:
            ensure_runtime(self.config, self.config_path)
        message = str(ctx.exception)
        self.assertIn("仍有活动工作", message)
        self.assertIn("后台运行", message)
        current = probe_daemon(self.config.server.socket_path)
        self.assertEqual(current["runtime_instance"], old["runtime_instance"])
        self.assertEqual(self.event(event["event_id"])["status"], "running")
        (self.workspace_dir / "fake-release").touch()
        self.wait_event(event["event_id"], ("succeeded", "failed"))

    def test_live_frontend_session_blocks_restart(self):
        old = self.start_stale_daemon()
        lease = request(self.config.server.socket_path, "frontend.begin", {
            "workflow_id": "test-wf", "task_id": "training", "new": True})
        request(self.config.server.socket_path, "frontend.started", {
            "conversation_id": lease["conversation_id"], "generation": lease["generation"],
            "owner_token": lease["owner_token"], "guardian_pid": os.getpid()})
        with self.assertRaises(RuntimeContractError) as ctx:
            ensure_runtime(self.config, self.config_path)
        message = str(ctx.exception)
        self.assertIn("前台会话", message)
        self.assertIn(lease["conversation_id"], message)
        self.assertEqual(probe_daemon(self.config.server.socket_path)["runtime_instance"], old["runtime_instance"])

    def test_concurrent_launchers_start_exactly_one_daemon(self):
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from oicoach_runtime.config import load_config\n"
            "from oicoach_runtime.launcher import ensure_runtime\n"
            "path = Path(sys.argv[1])\n"
            "handle = ensure_runtime(load_config(path), path)\n"
            "print(json.dumps({'created': handle.created, 'instance': handle.status['runtime_instance']}))\n")
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.config_path)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout.strip().splitlines()[-1]))
        self.assertEqual(sum(1 for result in results if result["created"]), 1, results)
        self.assertEqual(len({result["instance"] for result in results}), 1, results)
        self.assertIsNotNone(probe_daemon(self.config.server.socket_path))

    def test_shutdown_on_exit_requires_this_launch_token(self):
        handle = ensure_runtime(self.config, self.config_path)
        record = read_ownership(self.config)
        self.assertEqual(record["token"], handle.launch_token)
        with patch.dict(os.environ, {"PAVEWAY_SHUTDOWN_ON_EXIT": "1"}, clear=False):
            os.environ.pop("PAVEWAY_LAUNCH_TOKEN", None)
            shutdown_owned_runtime(self.config, self.config_path)
            self.assertIsNotNone(probe_daemon(self.config.server.socket_path))
        with patch.dict(os.environ, {"PAVEWAY_SHUTDOWN_ON_EXIT": "1",
                                     "PAVEWAY_LAUNCH_TOKEN": handle.launch_token}, clear=False):
            shutdown_owned_runtime(self.config, self.config_path)
        self.assertIsNone(probe_daemon(self.config.server.socket_path))
        self.assertFalse(ownership_path(self.config).exists())

    def test_systemd_managed_runtime_is_never_launcher_owned(self):
        handle = ensure_runtime(self.config, self.config_path)
        with patch.object(service, "status", return_value={"managed": True}):
            reused = ensure_runtime(self.config, self.config_path)
            self.assertTrue(reused.managed)
            self.assertFalse(reused.created)
            self.assertIsNone(reused.launch_token)
            with patch.dict(os.environ, {"PAVEWAY_SHUTDOWN_ON_EXIT": "1",
                                          "PAVEWAY_LAUNCH_TOKEN": handle.launch_token}, clear=False):
                shutdown_owned_runtime(self.config, self.config_path)
        self.assertIsNotNone(probe_daemon(self.config.server.socket_path))

    def test_launcher_resumes_a_compatible_drained_runtime(self):
        handle = ensure_runtime(self.config, self.config_path)
        drained = request(self.config.server.socket_path, "lifecycle.drain", {
            **_lifecycle_target(handle.status), "lifecycle_protocol_version": 1,
            "reason": "test drain"}, timeout=10)
        self.assertTrue(drained["accepted"])
        reused = ensure_runtime(self.config, self.config_path)
        self.assertTrue(reused.reused)
        self.assertEqual(reused.status["runtime_instance"], handle.status["runtime_instance"])
        self.assertFalse(reused.status["draining"])

    def test_explicit_restart_replaces_a_compatible_idle_runtime(self):
        first = ensure_runtime(self.config, self.config_path)
        second = ensure_runtime(self.config, self.config_path, restart=True)
        self.assertFalse(second.reused)
        self.assertTrue(second.created)
        self.assertNotEqual(second.status["runtime_instance"], first.status["runtime_instance"])
        self.assertNotEqual(second.launch_token, first.launch_token)

    def test_drain_requeues_an_undispatched_claim_without_losing_schedule(self):
        handle = ensure_runtime(self.config, self.config_path)
        (self.workspace_dir / "fake-mode").write_text("hang-before-ready")
        event = request(self.config.server.socket_path, "schedule", {
            "workflow_id": "test-wf", "task_id": "training", "reason": "pre-dispatch drain",
            "dedupe_key": "pre-dispatch", "at": "now", "payload": {}})
        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            runs = request(self.config.server.socket_path, "runs.list")
            if runs and runs[0]["status"] == "leased":
                run = runs[0]
                break
            time.sleep(0.02)
        self.assertIsNotNone(run, "Event was not leased before dispatch")
        self.assertIsNone(run["dispatch_intent_at"])
        result = request(self.config.server.socket_path, "lifecycle.shutdown", {
            **_lifecycle_target(handle.status), "lifecycle_protocol_version": 1,
            "reason": "pre-dispatch drain"}, timeout=30)
        self.assertTrue(result["accepted"], result)
        retained = self.event(event["event_id"])
        self.assertEqual(retained["status"], "queued")
        self.assertEqual(retained["attempt"], 1)
        stored = request(self.config.server.socket_path, "runs.show", {"run_id": run["run_id"]})
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(stored["error_code"], "runtime_drain")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and probe_daemon(self.config.server.socket_path) is not None:
            time.sleep(0.05)
        self.assertIsNone(probe_daemon(self.config.server.socket_path))
        (self.workspace_dir / "fake-mode").unlink()

    def test_cli_exit_hook_shuts_down_only_on_success_and_owned_token(self):
        from oicoach_runtime.cli import finish_owned_runtime
        handle = ensure_runtime(self.config, self.config_path)
        with patch.dict(os.environ, {"PAVEWAY_SHUTDOWN_ON_EXIT": "1"}, clear=False):
            os.environ.pop("PAVEWAY_LAUNCH_TOKEN", None)
            finish_owned_runtime(self.config, self.config_path, 1)
            self.assertIsNotNone(probe_daemon(self.config.server.socket_path))
            finish_owned_runtime(self.config, self.config_path, 0)
            self.assertIsNotNone(probe_daemon(self.config.server.socket_path))
            os.environ["PAVEWAY_LAUNCH_TOKEN"] = handle.launch_token
            finish_owned_runtime(self.config, self.config_path, 0)
        self.assertIsNone(probe_daemon(self.config.server.socket_path))

    def test_stale_lifecycle_target_cannot_stop_a_new_instance(self):
        handle = ensure_runtime(self.config, self.config_path)
        stale = {**_lifecycle_target(handle.status), "lifecycle_protocol_version": 1,
                 "reason": "stale request"}
        stale["runtime_instance"] = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(RuntimeContractError) as ctx:
            request(self.config.server.socket_path, "lifecycle.shutdown", stale)
        self.assertIn("Runtime 实例已变化", str(ctx.exception))
        self.assertIsNotNone(probe_daemon(self.config.server.socket_path))


if __name__ == "__main__":
    unittest.main()
