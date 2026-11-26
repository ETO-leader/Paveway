"""Controlled background-Agent concurrency through the real control server.

Overlap is proven with per-process entry markers written by the fake OMP host
while it blocks, not by measuring total elapsed time.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.models import parse_timestamp, utc_text

from fake_admission import admit_fake

FAKE_OMP = Path(__file__).with_name("fake_omp.py").resolve()


class ConcurrentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-concurrency-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config/config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.process = None
        self.log = (self.root / "daemon.log").open("w+")
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop)
        self.configure(1)

    def configure(self, concurrency, turn_timeout=20):
        config = load_config(self.config_path)
        config = replace(config, server=replace(config.server, poll_interval_seconds=0.02,
                                                max_concurrent_runs=concurrency),
                         agent=replace(config.agent, executable=str(FAKE_OMP),
                                       startup_timeout_seconds=5, ack_timeout_seconds=5,
                                       turn_timeout_seconds=turn_timeout,
                                       settle_timeout_seconds=5, graceful_stop_seconds=2),
                         restart=replace(config.restart, backoff_initial_seconds=0.05,
                                         backoff_max_seconds=0.1, cooldown_seconds=0.3,
                                         jitter_ratio=0))
        self.config_path.write_text(config_text(config))
        self.config = load_config(self.config_path)
        admit_fake(self.config)
        self.socket_path = self.config.server.socket_path
        directory = self.config.workflow_root / "echo"
        if not directory.exists():
            directory.mkdir()
            (directory / "workflow.toml").write_text(
                'schema_version = 1\nid = "echo"\nversion = "1"\n'
                f'workspace = {json.dumps(str(self.root))}\nsession_policy = "per_task"\n'
                'start_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
            (directory / "event.md").write_text("${event_json}\n")

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.config_path)],
            stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Daemon exited during startup: " + (self.root / "daemon.log").read_text())
            try:
                if request(self.socket_path, "status")["daemon"] == "running":
                    return
            except RuntimeContractError:
                pass
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

    def schedule(self, key, task, mode="hold-tagged", at=None):
        return request(self.socket_path, "schedule", {
            "workflow_id": "echo", "task_id": task, "reason": "concurrency",
            "dedupe_key": key, "payload": {"fake_mode": mode},
            "at": at or utc_text(datetime.now(timezone.utc))})

    def event(self, event_id):
        return request(self.socket_path, "events.show", {"event_id": event_id})

    def wait(self, predicate, message, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail(message + "\n" + (self.root / "daemon.log").read_text())

    def wait_status(self, event_id, statuses=("succeeded",), timeout=15):
        return self.wait(lambda: (event := self.event(event_id))["status"] in statuses and event,
                         f"Event {event_id} did not reach {statuses}: {self.event(event_id)}",
                         timeout)

    def entered(self):
        return sorted(int(path.name.rsplit("-", 1)[1])
                      for path in self.root.glob("fake-entered-*") if "-" in path.name)

    def release(self, pid):
        (self.root / ("fake-release-" + str(pid))).write_text("go")

    def test_two_independent_sessions_execute_at_the_same_time(self):
        self.configure(2)
        self.start()
        first = self.schedule("a", "alpha")
        second = self.schedule("b", "beta")
        active = self.wait(lambda: len(self.entered()) == 2 and self.entered(),
                           "Two fake Agents never ran at the same time")
        self.assertEqual(len(set(active)), 2)
        running = [row for row in request(self.socket_path, "runs.list")
                   if row["status"] == "running"]
        self.assertEqual(len(running), 2)
        # Progress is reported per Run, never from another Run.
        for event in (first, second):
            shown = self.event(event["event_id"])
            self.assertEqual(shown["progress"]["run_id"], shown["run_id"])
        self.assertEqual({self.event(first["event_id"])["progress"]["run_id"],
                          self.event(second["event_id"])["progress"]["run_id"]},
                         {self.event(first["event_id"])["run_id"],
                          self.event(second["event_id"])["run_id"]})
        for pid in active:
            self.release(pid)
        self.wait_status(first["event_id"])
        self.wait_status(second["event_id"])

    def test_same_physical_session_stays_strictly_serial(self):
        self.configure(2)
        self.start()
        first = self.schedule("one", "shared")
        second = self.schedule("two", "shared")
        active = self.wait(lambda: len(self.entered()) == 1 and self.entered(),
                           "The first Run did not start")
        holder = active[0]
        # The queued second event must not start while the first holds the session.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.assertEqual(len(self.entered()), 1, "Same-session Runs overlapped")
            time.sleep(0.02)
        self.assertEqual(self.event(second["event_id"])["status"], "queued")
        self.release(holder)
        self.wait_status(first["event_id"])
        follower = self.wait(lambda: [pid for pid in self.entered() if pid != holder] or None,
                             "Second same-session Run did not start")
        self.release(follower[0])
        self.wait_status(second["event_id"])
        first_done = self.event(first["event_id"])
        second_done = self.event(second["event_id"])
        first_run = request(self.socket_path, "runs.show", {"run_id": first_done["run_id"]})
        second_run = request(self.socket_path, "runs.show", {"run_id": second_done["run_id"]})
        self.assertGreaterEqual(parse_timestamp(second_run["started_at"]),
                                parse_timestamp(first_run["completed_at"]))
        self.assertEqual(len(request(self.socket_path, "runs.list")), 2)

    def test_blocked_head_of_line_does_not_idle_another_slot(self):
        self.configure(2)
        self.start()
        holder = self.schedule("hold", "alpha")
        pid = self.wait(lambda: len(self.entered()) == 1 and self.entered()[0],
                        "First Run did not start")
        queued = self.schedule("queued", "alpha")
        other = self.schedule("other", "beta")
        pids = self.wait(lambda: len(self.entered()) == 2 and self.entered(),
                         "The independent task never used the free slot")
        independent = [value for value in pids if value != pid]
        self.assertEqual(len(independent), 1)
        self.assertEqual(self.event(queued["event_id"])["status"], "queued")
        self.assertEqual(self.event(other["event_id"])["status"], "running")
        self.release(pid)
        self.release(independent[0])
        self.wait_status(holder["event_id"])
        self.wait_status(other["event_id"])
        follower = self.wait(lambda: [value for value in self.entered()
                                      if value not in (pid, independent[0])] or None,
                             "Queued same-session Run never started")
        self.release(follower[0])
        self.wait_status(queued["event_id"])

    def test_four_slots_never_exceeded_and_queue_drains(self):
        self.configure(4)
        self.start()
        events = [self.schedule(f"task-{index}", f"slot-{index}") for index in range(5)]
        active = self.wait(lambda: len(self.entered()) == 4 and self.entered(),
                           "Four slots never filled")
        self.assertEqual(len(active), 4)
        original = set(active)
        self.assertEqual(len([row for row in request(self.socket_path, "runs.list")
                              if row["status"] in ("leased", "running")]), 4)
        waiting = [event for event in events
                   if self.event(event["event_id"])["status"] == "queued"]
        self.assertEqual(len(waiting), 1)
        for pid in active:
            self.release(pid)
        fifth = self.wait(lambda: [pid for pid in self.entered() if pid not in original] or None,
                          "The fifth Run never claimed the freed slot")
        for pid in fifth:
            self.release(pid)
        for event in events:
            self.wait_status(event["event_id"])
        self.assertEqual(len([row for row in request(self.socket_path, "runs.list")
                              if row["status"] == "succeeded"]), 5)

    def test_reminder_delivery_while_every_slot_is_busy(self):
        self.configure(2)
        self.start()
        self.schedule("alpha", "alpha")
        self.schedule("beta", "beta")
        self.wait(lambda: len(self.entered()) == 2, "Both slots never filled")
        reminder = request(self.socket_path, "schedule", {
            "workflow_id": "echo", "task_id": "reminder", "reason": "pure reminder",
            "dedupe_key": "busy-reminder", "at": utc_text(datetime.now(timezone.utc)),
            "payload": {"native_wakeup": True, "intent": "reminder",
                        "message": "REMINDER_WHILE_BUSY"}})
        delivered = self.wait(
            lambda: [row for row in request(self.socket_path, "inbox.list",
                                            {"workflow_id": "echo"})["messages"]
                     if row["text"] == "REMINDER_WHILE_BUSY"],
            "Reminder was not delivered while all model slots were busy")
        self.assertEqual(delivered[0]["state"], "created")
        self.assertIsNone(self.event(reminder["event_id"])["run_id"])
        for pid in self.entered():
            self.release(pid)

    def test_restart_of_one_session_does_not_cancel_another_run(self):
        self.configure(2)
        self.start()
        alpha = self.schedule("alpha", "alpha")
        beta = self.schedule("beta", "beta")
        pids = self.wait(lambda: len(self.entered()) == 2 and self.entered(),
                         "Both Runs never ran concurrently")
        alpha_run = request(self.socket_path, "runs.show", {"run_id": self.event(alpha["event_id"])["run_id"]})
        beta_run = request(self.socket_path, "runs.show", {"run_id": self.event(beta["event_id"])["run_id"]})
        job = request(self.socket_path, "agent.restart", {"task_id": "alpha", "workflow_id": "echo",
                                                          "reason": "isolation"})
        self.wait(lambda: (job_state := request(self.socket_path, "agent.show",
                                                {"restart_id": job["restart_id"]}))["status"]
                  in ("succeeded", "failed", "interrupted") and job_state,
                  "Restart job did not finish")
        self.assertEqual(self.event(alpha["event_id"])["status"], "uncertain")
        self.assertEqual(self.event(beta["event_id"])["status"], "running")
        self.assertTrue(Path(f"/proc/{beta_run['agent_pid']}").exists())
        for pid in pids:
            self.release(pid)
        self.wait_status(beta["event_id"])
        self.assertEqual(request(self.socket_path, "runs.show", {"run_id": beta_run["run_id"]})["status"],
                         "succeeded")
        self.assertEqual(request(self.socket_path, "runs.show", {"run_id": alpha_run["run_id"]})["status"],
                         "uncertain")

    def test_daemon_crash_preserves_two_runs_as_uncertain_without_replay(self):
        self.configure(2)
        self.start()
        events = [self.schedule("alpha", "alpha"), self.schedule("beta", "beta")]
        self.wait(lambda: len(self.entered()) == 2, "Both Runs never ran concurrently")
        self.process.kill()
        self.process.wait(timeout=5)
        self.start()
        for event in events:
            finished = self.wait_status(event["event_id"], ("uncertain",), timeout=20)
            self.assertEqual(finished["attempt"], 1)
            with self.assertRaises(RuntimeContractError):
                request(self.socket_path, "recover.retry", {"event_id": event["event_id"]})
        self.assertEqual(len([row for row in request(self.socket_path, "runs.list")
                              if row["status"] == "uncertain"]), 2)


if __name__ == "__main__":
    unittest.main()
