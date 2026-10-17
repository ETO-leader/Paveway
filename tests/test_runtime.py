import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.models import utc_text


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config/config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(self.config_path)
        config = replace(config, server=replace(config.server, poll_interval_seconds=0.02),
                         agent=replace(config.agent, executable=str(Path(__file__).with_name("fake_omp.py").resolve()),
                                       startup_timeout_seconds=2, ack_timeout_seconds=0.5,
                                       turn_timeout_seconds=1, settle_timeout_seconds=1, graceful_stop_seconds=0.05))
        self.config_path.write_text(config_text(config))
        self.config = load_config(self.config_path)
        self.socket_path = self.config.server.socket_path
        directory = self.config.workflow_root / "echo"
        directory.mkdir()
        (directory / "workflow.toml").write_text('schema_version = 1\nid = "echo"\nversion = "1"\n'
                                               f'workspace = {json.dumps(str(self.root))}\nsession_policy = "per_task"\n'
                                               'start_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
        (directory / "event.md").write_text("${event_json}\n")
        self.process = None
        self.log = (self.root / "daemon.log").open("w+")
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        self.process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.config_path)],
                                        stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 6
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
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def schedule(self, key="one", task="task", mode="normal", at=None):
        return request(self.socket_path, "schedule", {
            "workflow_id": "echo", "task_id": task, "reason": "integration", "dedupe_key": key,
            "payload": {"fake_mode": mode}, "at": at or utc_text(datetime.now(timezone.utc))})

    def event(self, event_id):
        return request(self.socket_path, "events.show", {"event_id": event_id})

    def wait_status(self, event_id, statuses=("succeeded",), timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.event(event_id)
            if event["status"] in statuses:
                return event
            time.sleep(0.01)
        self.fail(f"Event did not reach {statuses}: {self.event(event_id)}; log=" + (self.root / "daemon.log").read_text())

    def test_cli_exits_and_daemon_completes_matching_result_and_settlement(self):
        due = utc_text(datetime.now(timezone.utc) + timedelta(seconds=0.3))
        cli = subprocess.run([sys.executable, "-m", "oicoach_runtime", "schedule", "--config", str(self.config_path),
                              "--workflow", "echo", "--task", "cli", "--at", due, "--reason", "CLI ended",
                              "--dedupe-key", "cli-one", "--json"], capture_output=True, text=True, timeout=5)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        event_id = json.loads(cli.stdout)["result"]["event_id"]
        self.assertIsNone(self.process.poll())
        event = self.wait_status(event_id)
        run = request(self.socket_path, "runs.show", {"run_id": event["run_id"]})
        self.assertIsNotNone(run["acknowledged_at"])
        self.assertEqual(run["prompt_result_status"], "completed")
        self.assertIsNotNone(run["settled_at"])
        self.assertEqual(Path(self.socket_path).stat().st_mode & 0o777, 0o600)

    def test_restart_preserves_future_event_and_single_instance(self):
        event = self.schedule(at=utc_text(datetime.now(timezone.utc) + timedelta(seconds=0.6)))
        other = subprocess.run([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.config_path), "--json"],
                               capture_output=True, text=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertEqual(json.loads(other.stderr)["error"]["code"], "conflict")
        self.stop()
        self.assertEqual(self.process.returncode, 0)
        self.start()
        self.wait_status(event["event_id"])
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)

    def test_startup_failure_is_blocked_and_does_not_restart_in_a_loop(self):
        (self.root / "fake-mode").write_text("crash-before-ready")
        event = self.schedule()
        self.wait_status(event["event_id"], ("blocked",))
        run = request(self.socket_path, "runs.list")[0]
        self.assertIsNone(run["dispatch_intent_at"])
        self.assertEqual(self.event(event["event_id"])["attempt"], 1)
        self.assertIsNone(self.process.poll())
        (self.root / "fake-mode").unlink()
        request(self.socket_path, "recover.retry", {"event_id": event["event_id"]})
        self.wait_status(event["event_id"])
        self.assertEqual(len(request(self.socket_path, "runs.list")), 2)

    def test_shutdown_of_active_turn_marks_uncertain_and_releases_socket(self):
        event = self.schedule(mode="hold")
        self.wait_status(event["event_id"], ("running",))
        self.stop()
        self.assertEqual(self.process.returncode, 0)
        self.assertFalse(Path(self.socket_path).exists())
        self.start()
        self.assertEqual(self.event(event["event_id"])["status"], "uncertain")

    def test_stale_regular_control_path_is_preserved(self):
        self.stop()
        control_path = Path(self.socket_path)
        control_path.write_text("preserve")
        process = subprocess.run([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.config_path), "--json"],
                                 capture_output=True, text=True, timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(control_path.read_text(), "preserve")

    def test_repeated_rpc_schedule_is_idempotent_but_changed_payload_conflicts(self):
        at = utc_text(datetime.now(timezone.utc) + timedelta(days=1))
        original = self.schedule(at=at)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.schedule(at=at), range(12)))
        self.assertEqual({event["event_id"] for event in results}, {original["event_id"]})
        with self.assertRaises(RuntimeContractError):
            self.schedule(at=at, mode="changed")

    def test_same_task_resumes_and_two_tasks_remain_isolated(self):
        first = self.wait_status(self.schedule("one", "a")["event_id"])
        self.wait_status(self.schedule("two", "a")["event_id"])
        self.wait_status(self.schedule("three", "b")["event_id"])
        run = request(self.socket_path, "runs.show", {"run_id": first["run_id"]})
        sessions = list((self.config.state_dir / "sessions").glob("*/session.json"))
        self.assertEqual(sorted(len(json.loads(p.read_text())["prompts"]) for p in sessions), [1, 2])
        self.assertIsNotNone(run["binding_id"])
        self.assertEqual(len({json.loads(p.read_text())["session_id"] for p in sessions}), 2)

    def test_twenty_due_events_run_serially(self):
        due = utc_text(datetime.now(timezone.utc) + timedelta(seconds=0.1))
        events = [self.schedule(str(i), task=f"task-{i}", at=due) for i in range(20)]
        for event in events:
            self.wait_status(event["event_id"], timeout=12)
        runs = request(self.socket_path, "runs.list")
        self.assertEqual(len(runs), 20)
        ordered = sorted(runs, key=lambda run: run["started_at"])
        for before, after in zip(ordered, ordered[1:]):
            self.assertLessEqual(before["completed_at"], after["started_at"])

    def test_crash_boundaries_and_explicit_provider_failure(self):
        for index, (mode, status) in enumerate((("crash-before-ack", "uncertain"), ("crash-after-ack", "uncertain"),
                                                ("crash-before-settled", "uncertain"), ("ack-only", "uncertain"),
                                                ("malformed", "uncertain"), ("needs-user", "uncertain"),
                                                ("provider-error", "failed"))):
            with self.subTest(mode=mode):
                event = self.schedule(str(index), f"boundary-{index}", mode)
                finished = self.wait_status(event["event_id"], (status,))
                self.assertEqual(finished["attempt"], 1)
                runs = request(self.socket_path, "runs.list", {"task": f"boundary-{index}"})
                self.assertEqual(len(runs), 1)
                if status == "uncertain":
                    self.assertIsNone(runs[0]["settled_at"])
        self.assertEqual(len(request(self.socket_path, "recover.list")), 7)

    def test_delayed_settle_local_completion_and_stderr_flood(self):
        for index, mode in enumerate(("delayed-settle", "local-only", "stderr-flood")):
            with self.subTest(mode=mode):
                self.wait_status(self.schedule(str(index), f"mode-{index}", mode)["event_id"])
        self.assertNotIn("private diagnostic", (self.root / "daemon.log").read_text())

    def test_omp_crash_does_not_leave_a_tool_process_alive(self):
        event = self.schedule(mode="crash-with-tool")
        self.wait_status(event["event_id"], ("uncertain",))
        pid = int((self.root / "fake-tool-pid").read_text())
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            self.assertEqual(stat_path.read_text().rsplit(")", 1)[1].split()[0], "Z")

    def test_daemon_sigkill_kills_owned_agent_and_does_not_replay(self):
        event = self.schedule(mode="hold")
        deadline = time.monotonic() + 5
        while not (self.root / "fake-entered").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        agent_pid = int((self.root / "fake-entered").read_text())
        self.process.kill()
        self.process.wait(timeout=5)
        deadline = time.monotonic() + 3
        # Linux may leave a reparented zombie briefly; it cannot run or retain a lock.
        while Path(f"/proc/{agent_pid}/stat").exists():
            status = Path(f"/proc/{agent_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if status == "Z":
                break
            self.assertLess(time.monotonic(), deadline, "Owned fake OMP survived daemon SIGKILL")
            time.sleep(0.01)
        self.start()
        finished = self.wait_status(event["event_id"], ("uncertain",))
        self.assertEqual(finished["attempt"], 1)
        with self.assertRaises(RuntimeContractError):
            request(self.socket_path, "recover.retry", {"event_id": event["event_id"]})
        (self.root / "fake-release").touch()
        request(self.socket_path, "recover.retry", {"event_id": event["event_id"], "confirmed": True})
        self.wait_status(event["event_id"])
        self.assertEqual(len(request(self.socket_path, "runs.list")), 2)

    def test_after_turn_anchor_checked_and_armed_after_settled(self):
        event = self.schedule(mode="hold")
        self.wait_status(event["event_id"], ("running",))
        event = self.event(event["event_id"])
        child = request(self.socket_path, "schedule", {"workflow_id": "echo", "task_id": "task", "reason": "after",
                                                       "dedupe_key": "after", "after_turn": 0.1, "run_id": event["run_id"]})
        self.assertEqual(child["status"], "waiting_anchor")
        with self.assertRaises(RuntimeContractError):
            request(self.socket_path, "schedule", {"workflow_id": "echo", "task_id": "wrong", "reason": "after",
                                                   "dedupe_key": "wrong", "after_turn": 0.1, "run_id": event["run_id"]})
        (self.root / "fake-release").touch()
        parent = self.wait_status(event["event_id"])
        child = self.wait_status(child["event_id"])
        self.assertGreater(child["not_before_utc"], parent["completed_at"])

    def test_agent_cli_uses_injected_config_run_and_executable(self):
        parent = self.schedule(mode="schedule-child")
        self.wait_status(parent["event_id"])
        events = request(self.socket_path, "events.list")
        child = next(event for event in events if event["event_id"] != parent["event_id"])
        self.wait_status(child["event_id"])
        self.assertEqual(len(request(self.socket_path, "runs.list")), 2)

    def test_manage_future_events_concurrently_while_agent_is_running(self):
        active = self.schedule(mode="hold")
        self.wait_status(active["event_id"], ("running",))
        due = utc_text(datetime.now(timezone.utc) + timedelta(days=1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            events = list(executor.map(lambda i: self.schedule(f"future-{i}", at=due), range(12)))
        event = events[0]
        moved = request(self.socket_path, "events.reschedule", {"event_id": event["event_id"], "expected_version": event["version"], "at": due})
        with self.assertRaises(RuntimeContractError):
            request(self.socket_path, "events.cancel", {"event_id": event["event_id"], "expected_version": event["version"]})
        request(self.socket_path, "events.cancel", {"event_id": event["event_id"], "expected_version": moved["version"]})
        (self.root / "fake-release").touch()
        self.wait_status(active["event_id"])
        counts = request(self.socket_path, "status")["counts"]
        self.assertEqual(counts["queued"], 11)
        self.assertEqual(counts["cancelled"], 1)

    def test_ipc_rejects_unknown_versions_fields_and_oversized_frames(self):
        frames = [b'{"api_version":2,"id":"test","command":"status","arguments":{}}\n',
                  b'{"api_version":1,"id":"test","command":"status","arguments":{},"extra":1}\n',
                  b"x" * 131073 + b"\n", b"not-json\n"]
        for frame in frames:
            with self.subTest(size=len(frame)), socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(self.socket_path)
                client.sendall(frame)
                data = client.recv(8192)
                self.assertIn("error", json.loads(data))
        self.assertEqual(request(self.socket_path, "status")["daemon"], "running")


if __name__ == "__main__":
    unittest.main()
