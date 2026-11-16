"""Run cleanup isolation: one cleanup failure never leaks into other Runs.

The subprocess daemon patches OmpRpc so a single task's process cleanup fails,
then proves through the real control socket that other Runs, the Scheduler and
the durable execution scope stay safe.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import AgentConfig, Config, ServerConfig, config_text, load_config
from oicoach_runtime.ipc import request
from oicoach_runtime.launcher import _lifecycle_target
from oicoach_runtime.models import utc_text

FAKE_OMP = Path(__file__).with_name("fake_omp.py").resolve()

FAULT_DAEMON = '''
import asyncio, os, sys
from pathlib import Path
from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import AgentError
from oicoach_runtime.scheduler import Runtime

target, mode = os.environ["FAULT_TASK"], os.environ["FAULT_MODE"]
original_start, original_stop = OmpRpc.start, OmpRpc.stop

async def start(self, binding, run_id):
    self._fault = binding.get("task_id") == target
    return await original_start(self, binding, run_id)

async def stop(self, abort=False):
    fault = getattr(self, "_fault", False)
    if fault and mode == "alive":
        raise AgentError("injected cleanup failure", code="agent_stop_timeout")
    if fault and mode == "dead":
        await original_stop(self, abort)
        raise AgentError("injected cleanup failure", code="agent_stop_timeout")
    if fault and mode == "settled":
        self._stop_calls = getattr(self, "_stop_calls", 0) + 1
        if self._stop_calls >= 2:
            await original_stop(self, abort)
            raise AgentError("injected cleanup failure", code="agent_stop_timeout")
    return await original_stop(self, abort)

OmpRpc.start, OmpRpc.stop = start, stop
path = Path(sys.argv[1])
asyncio.run(Runtime(load_config(path), path).serve())
'''


class CleanupFaultRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-cleanup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config/config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.process = None
        self.log = (self.root / "daemon.log").open("w+")
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop)
        self.configure()

    def configure(self, concurrency=2, turn_timeout=3):
        config = load_config(self.config_path)
        config = replace(config, server=replace(config.server, poll_interval_seconds=0.02,
                                                max_concurrent_runs=concurrency),
                         agent=replace(config.agent, executable=str(FAKE_OMP),
                                       startup_timeout_seconds=5, ack_timeout_seconds=5,
                                       turn_timeout_seconds=turn_timeout,
                                       settle_timeout_seconds=5, graceful_stop_seconds=1),
                         restart=replace(config.restart, backoff_initial_seconds=0.05,
                                         backoff_max_seconds=0.1, cooldown_seconds=0.3,
                                         jitter_ratio=0))
        self.config_path.write_text(config_text(config))
        self.config = load_config(self.config_path)
        self.socket_path = self.config.server.socket_path
        directory = self.config.workflow_root / "echo"
        if not directory.exists():
            directory.mkdir()
            (directory / "workflow.toml").write_text(
                'schema_version = 1\nid = "echo"\nversion = "1"\n'
                f'workspace = {json.dumps(str(self.root))}\nsession_policy = "per_task"\n'
                'start_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
            (directory / "event.md").write_text("${event_json}\n")

    def start(self, fault_task=None, fault_mode=None):
        if fault_task:
            argv = [sys.executable, "-c", FAULT_DAEMON, str(self.config_path)]
            environment = {**os.environ, "FAULT_TASK": fault_task, "FAULT_MODE": fault_mode}
        else:
            argv = [sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.config_path)]
            environment = None
        self.process = subprocess.Popen(argv, stdout=self.log, stderr=self.log, env=environment)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Daemon exited during startup: " + (self.root / "daemon.log").read_text())
            try:
                if request(self.socket_path, "status")["daemon"] == "running":
                    return
            except Exception:
                time.sleep(0.01)
        self.fail("Daemon did not become ready")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def schedule(self, key, task, mode="hold-tagged"):
        return request(self.socket_path, "schedule", {
            "workflow_id": "echo", "task_id": task, "reason": "cleanup",
            "dedupe_key": key, "payload": {"fake_mode": mode},
            "at": utc_text(datetime.now(timezone.utc))})

    def event(self, event_id):
        return request(self.socket_path, "events.show", {"event_id": event_id})

    def run_of(self, event_id):
        return request(self.socket_path, "runs.show", {"run_id": self.event(event_id)["run_id"]})

    def wait(self, predicate, message, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = predicate()
            except Exception:
                value = None
            if value:
                return value
            if self.process.poll() is not None:
                self.fail("Daemon exited during " + message + "\n" +
                          (self.root / "daemon.log").read_text())
            time.sleep(0.01)
        self.fail(message + "\n" + (self.root / "daemon.log").read_text())

    def wait_status(self, event_id, statuses=("succeeded",), timeout=20):
        return self.wait(lambda: (event := self.event(event_id))["status"] in statuses and event,
                         f"Event {event_id} did not reach {statuses}: {self.event(event_id)}",
                         timeout)

    def entered(self):
        return sorted(int(path.name.rsplit("-", 1)[1])
                      for path in self.root.glob("fake-entered-*") if "-" in path.name)

    def release(self, pid):
        (self.root / ("fake-release-" + str(pid))).write_text("go")

    def test_cleanup_failure_quarantines_scope_without_stopping_the_scheduler(self):
        self.start(fault_task="alpha", fault_mode="alive")
        alpha = self.schedule("alpha", "alpha")
        alpha_pid = self.wait(lambda: self.entered() and self.entered()[0],
                              "Alpha never entered its turn")
        beta = self.schedule("beta", "beta")
        beta_pid = self.wait(lambda: len(self.entered()) == 2 and
                             [pid for pid in self.entered() if pid != alpha_pid][0],
                             "Beta never ran while Alpha held its Session")
        self.assertTrue(Path(f"/proc/{alpha_pid}").exists())
        self.assertTrue(Path(f"/proc/{beta_pid}").exists())
        # Restarting Alpha cancels its Run; the injected cleanup failure leaves
        # the owned process alive, so the execution scope must stay owned.
        job = request(self.socket_path, "agent.restart", {
            "workflow_id": "echo", "task_id": "alpha", "reason": "cleanup fault"})
        failed = self.wait(lambda: (state := request(
            self.socket_path, "agent.show", {"restart_id": job["restart_id"]}))["status"] == "failed" and state,
            "Restart job did not fail closed")
        self.assertEqual(failed["error_code"], "agent_cleanup_unproven")
        self.assertEqual(self.event(alpha["event_id"])["status"], "running")
        stored = self.run_of(alpha["event_id"])
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["error_code"], "agent_cleanup_unproven")
        # Beta was neither cancelled nor disturbed by Alpha's cleanup failure.
        self.assertEqual(self.event(beta["event_id"])["status"], "running")
        self.assertTrue(Path(f"/proc/{beta_pid}").exists())
        # The Scheduler survived the failure and still answers control requests.
        self.assertEqual(request(self.socket_path, "status")["daemon"], "running")
        inspect = request(self.socket_path, "lifecycle.inspect", {})
        quarantined = [item for item in inspect["active_runs"] if item["run_id"] == stored["run_id"]]
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(quarantined[0]["cleanup_unproven"])
        self.assertFalse(quarantined[0]["safe_to_abort"])
        # Shutdown must refuse rather than kill or reuse the unproven process.
        status = request(self.socket_path, "status")
        refused = request(self.socket_path, "lifecycle.shutdown", {
            **_lifecycle_target(status), "lifecycle_protocol_version": 1,
            "reason": "quarantined scope"}, timeout=10)
        self.assertFalse(refused["accepted"])
        self.assertTrue(any(item.get("cleanup_unproven") for item in refused["blockers"]))
        self.assertFalse(refused["stopping"])
        # The blocked scope is not re-claimed, while another scope still runs.
        blocked = self.schedule("alpha2", "alpha", mode="normal")
        other = self.schedule("gamma", "gamma", mode="normal")
        self.release(beta_pid)
        self.wait_status(beta["event_id"])
        self.wait_status(other["event_id"])
        self.assertEqual(self.event(blocked["event_id"])["status"], "queued")
        self.assertEqual(self.event(blocked["event_id"])["attempt"], 0)
        # A restart recovers the durable Run as uncertain and never replays it.
        self.stop()
        self.log.truncate(0)
        self.start()
        self.wait(lambda: self.event(alpha["event_id"])["status"] == "uncertain",
                  "Recovered Run was not marked uncertain")
        recovered = self.event(alpha["event_id"])
        self.assertEqual(recovered["attempt"], 1)
        self.assertEqual(self.run_of(alpha["event_id"])["status"], "uncertain")
        # The recovered Session stays fail-closed: the queued event is blocked,
        # never replayed, and no request with unknown side effects is repeated.
        blocked_event = self.wait(
            lambda: (event := self.event(blocked["event_id"]))["status"] == "blocked" and event,
            "Queued event was not failed closed after recovery")
        self.assertEqual(self.run_of(blocked["event_id"])["error_code"], "session_uncertain")
        self.assertNotEqual(blocked_event["attempt"], 2)
        self.assertEqual(self.event(alpha["event_id"])["status"], "uncertain")
        self.assertEqual(self.event(alpha["event_id"])["attempt"], 1)

    def test_cleanup_error_after_a_settled_run_keeps_the_result_and_scope(self):
        self.start(fault_task="alpha", fault_mode="settled")
        first = self.schedule("alpha", "alpha", mode="normal")
        self.wait_status(first["event_id"])
        self.assertEqual(self.run_of(first["event_id"])["agent_exit_code"], 0)
        second = self.schedule("alpha2", "alpha", mode="normal")
        self.wait_status(second["event_id"])
        self.assertEqual(self.run_of(second["event_id"])["agent_exit_code"], 0)
        self.assertEqual(request(self.socket_path, "status")["daemon"], "running")
        statuses = sorted(row["status"] for row in request(self.socket_path, "runs.list"))
        self.assertEqual(statuses, ["succeeded", "succeeded"])
        self.assertIn("cleanup_failed", (self.root / "daemon.log").read_text())

    def test_cleanup_error_after_a_real_stop_never_quarantines_the_scope(self):
        self.configure(turn_timeout=2)
        self.start(fault_task="alpha", fault_mode="dead")
        first = self.schedule("alpha", "alpha")
        self.wait_status(first["event_id"], ("uncertain",))
        self.assertEqual(self.run_of(first["event_id"])["status"], "uncertain")
        # The scope is released (no quarantine); the uncertain Session keeps its
        # pre-existing fail-closed rule instead of being silently replayed.
        inspect = request(self.socket_path, "lifecycle.inspect", {})
        self.assertEqual(inspect["active_runs"], [])
        second = self.schedule("alpha2", "alpha", mode="normal")
        self.wait(lambda: (event := self.event(second["event_id"]))["status"] == "blocked" and event,
                  "The uncertain Session did not fail closed")
        self.assertEqual(self.run_of(second["event_id"])["error_code"], "session_uncertain")
        other = self.schedule("gamma", "gamma", mode="normal")
        self.wait_status(other["event_id"])
        self.assertEqual(request(self.socket_path, "status")["daemon"], "running")


class CleanupOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-cleanup-owner-")
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(state_dir=Path(self.temporary.name) / "state",
                             workflow_root=Path(self.temporary.name) / "workflows",
                             server=ServerConfig(socket_path=str(Path(self.temporary.name) / "state/runtime.sock")),
                             agent=AgentConfig(executable=str(FAKE_OMP), graceful_stop_seconds=1))

    async def test_one_cleanup_task_survives_a_cancelled_caller(self):
        adapter = OmpRpc(self.config)
        adapter.process = object()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def fake_stop(abort):
            calls.append(abort)
            entered.set()
            await release.wait()

        adapter._stop = fake_stop
        first = asyncio.create_task(adapter.stop(abort=True))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(adapter.stop(abort=False))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        await asyncio.wait_for(second, 5)
        self.assertEqual(calls, [True], "a second cleanup task was started")
        self.assertTrue(adapter.cleanup_task.done())
        self.assertFalse(adapter.cleanup_task.cancelled())
        # Later callers reuse the completed cleanup instead of starting a new one.
        await asyncio.wait_for(adapter.stop(abort=False), 5)
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
