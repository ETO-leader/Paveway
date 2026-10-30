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
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.models import utc_text


class RuntimeTests(unittest.TestCase):
    def test_report_survives_a_later_assistant_postscript_without_hidden_content(self):
        done = self.wait_status(self.schedule(mode="report-then-postscript")["event_id"])
        output = request(self.socket_path, "runs.output", {"run_id": done["run_id"]})
        self.assertEqual(output["text"], "FULL_REPORT_BODY\n\nSHORT_POSTSCRIPT")
        self.assertFalse(output["truncated"])
        self.assertNotIn("PRIVATE_", json.dumps(output))
        large = request(self.socket_path, "schedule", {"workflow_id": "echo", "task_id": "task", "reason": "stream output limit",
            "dedupe_key": "stream-bounded", "at": "now", "payload": {"fake_mode": "report-then-postscript", "fake_reply": "字" * 10000}})
        large = self.wait_status(large["event_id"])
        bounded = request(self.socket_path, "runs.output", {"run_id": large["run_id"]})
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(len(bounded["text"].encode("utf-8")), 16384)
        self.assertNotIn("PRIVATE_", bounded["text"])
        next_turn = self.wait_status(self.schedule(key="following-turn")["event_id"])
        self.assertEqual(request(self.socket_path, "runs.output", {"run_id": next_turn["run_id"]})["text"], "fake completion")

    def test_static_workflow_rules_are_separate_from_user_envelope(self):
        directory = self.config.workflow_root / "echo"
        manifest = directory / "workflow.toml"
        manifest.write_text(manifest.read_text() + "instructions_in_system = true\n")
        (directory / "event.md").write_text("TRUSTED_WORKFLOW_RULE\n${event_json}\n")
        done = self.wait_status(self.schedule()["event_id"])
        session = next((self.config.state_dir / "sessions").glob("*/session.json"))
        message = json.loads(session.read_text())["prompts"][0]
        self.assertEqual(message["event_id"], done["event_id"])
        system = (session.parent / "customizations" / done["run_id"] / "system.md").read_text()
        self.assertIn("TRUSTED_WORKFLOW_RULE", system)
        self.assertNotIn(done["event_id"], system)

    def test_builtin_scope_changes_apply_next_dispatch_without_replay_or_rebinding(self):
        from oicoach_runtime.builtin_skills import registry
        from oicoach_runtime.customization import update
        from oicoach_runtime.workflow_setup import create
        manifest = self.config.workflow_root / "echo/workflow.toml"
        original = manifest.read_text()
        manifest.write_text(original + 'builtin_skills = ["paveway-tijian"]\n')
        update(self.config, "echo", 0, "skill-put", name="user-review", description="review", content="USER_REVIEW_BODY")
        first = self.wait_status(self.schedule("builtin-on")["event_id"])
        session = next((self.config.state_dir / "sessions").glob("*/session.json"))
        old_skill = session.parent / "customizations" / first["run_id"] / "skills/paveway-tijian/SKILL.md"
        expected = registry()["paveway-tijian"].markdown
        self.assertEqual(old_skill.read_text(), expected)
        create(self.config, "isolated", self.root, "example")
        isolated = request(self.socket_path, "schedule", {"workflow_id": "isolated", "task_id": "task",
            "dedupe_key": "isolated", "reason": "scope check", "at": "now", "payload": {}})
        other = self.wait_status(isolated["event_id"])
        self.assertFalse(any((self.config.state_dir / "sessions").glob(
            "*/customizations/" + other["run_id"] + "/skills/paveway-tijian")))
        manifest.write_text(original + "builtin_skills = []\n")
        second = self.wait_status(self.schedule("builtin-off")["event_id"])
        next_snapshot = session.parent / "customizations" / second["run_id"]
        self.assertFalse((next_snapshot / "skills/paveway-tijian").exists())
        self.assertTrue((next_snapshot / "skills/user-review/SKILL.md").is_file())
        self.assertEqual(old_skill.read_text(), expected)
        # Same bound file contains exactly the two requested echo turns.
        self.assertEqual(len(json.loads(session.read_text())["prompts"]), 2)
        self.assertEqual(self.event(first["event_id"])["status"], "succeeded")

    def test_running_event_reports_safe_live_tool_progress(self):
        event = self.schedule(mode="hold")
        deadline = time.monotonic() + 5
        while True:
            current = self.event(event["event_id"])
            if current.get("progress", {}).get("tool_calls") == 1:
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(current["progress"]["phase"], "tool")
        self.assertEqual(current["progress"]["tool"], "bash")
        self.assertNotIn("PRIVATE_PROGRESS", json.dumps(current["progress"]))
        (self.root / "fake-release").touch()
        finished = self.wait_status(event["event_id"])
        self.assertNotIn("progress", finished)

    def test_uncertain_session_explains_new_task_without_replaying_old_request(self):
        original = self.schedule(mode="crash-after-ack")
        self.wait_status(original["event_id"], ("uncertain",))
        old_file = next((self.config.state_dir / "sessions").glob("*/session.json"))
        before = old_file.read_bytes()
        blocked = self.schedule(key="blocked-followup")
        result = self.wait_status(blocked["event_id"], ("blocked",))
        self.assertEqual(result["last_error_code"], "session_uncertain")
        self.assertIn("/new", result["last_error_message"])
        self.assertIn("/recover", result["last_error_message"])
        run = request(self.socket_path, "runs.show", {"run_id": result["run_id"]})
        self.assertIsNone(run["dispatch_intent_at"])
        self.assertEqual(run["error_message"], result["last_error_message"])
        self.assertNotIn(str(self.root), run["error_message"])
        fresh = self.schedule(key="fresh-followup", task="fresh-task")
        self.wait_status(fresh["event_id"])
        self.assertEqual(old_file.read_bytes(), before)
        self.assertEqual(self.event(original["event_id"])["status"], "uncertain")
        self.assertEqual(len((self.root / "fake-starts.jsonl").read_text().splitlines()), 2)

    def test_native_context_no_longer_requires_fixed_budget_extension(self):
        (self.root / "fake-mode").write_text("missing-context-guard")
        event = self.schedule()
        self.wait_status(event["event_id"])
        runs = request(self.socket_path, "runs.list")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "succeeded")
        self.assertIsNotNone(runs[0]["dispatch_intent_at"])

    def test_upcoming_cli_reports_due_time_and_filters_scope(self):
        due = utc_text(datetime.now(timezone.utc) + timedelta(minutes=10))
        event = self.schedule("upcoming-one", "timer-task", at=due)
        self.schedule("upcoming-other", "other-task", at=due)
        report = subprocess.run([sys.executable, "-m", "oicoach_runtime", "events", "upcoming", "--workflow", "echo",
            "--task", "timer-task", "--config", str(self.config_path), "--json"], capture_output=True, text=True)
        self.assertEqual(report.returncode, 0, report.stderr)
        rows = json.loads(report.stdout)["result"]
        self.assertEqual([r["event_id"] for r in rows], [event["event_id"]])
        self.assertEqual(rows[0]["not_before_utc"], due)
        self.assertNotIn("payload", rows[0])

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
                                       turn_timeout_seconds=1, settle_timeout_seconds=1, graceful_stop_seconds=1),
                         restart=replace(config.restart, backoff_initial_seconds=0.05, backoff_max_seconds=0.1,
                                         cooldown_seconds=0.3, jitter_ratio=0))
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

    def wait_status(self, event_id, statuses=("succeeded",), timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.event(event_id)
            if event["status"] in statuses:
                return event
            time.sleep(0.01)
        self.fail(f"Event did not reach {statuses}: {self.event(event_id)}; log=" + (self.root / "daemon.log").read_text())

    def wait_restart(self, job_id, statuses=("succeeded",), timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = request(self.socket_path, "agent.show", {"restart_id": job_id})
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail("Restart did not finish: " + json.dumps(job))

    def test_idle_restart_cli_probes_same_session_without_replaying_prompt(self):
        self.wait_status(self.schedule()["event_id"])
        session_file = next((self.config.state_dir / "sessions").glob("*/session.json"))
        before = session_file.read_text()
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "agent", "restart", "--task", "task", "--workflow", "echo",
                                 "--config", str(self.config_path), "--json"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout)["result"]
        completed = self.wait_restart(job["restart_id"])
        self.assertFalse(completed["prompt_replayed"])
        self.assertEqual(session_file.read_text(), before)
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)
        starts = [json.loads(line) for line in (self.root / "fake-starts.jsonl").read_text().splitlines()]
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0]["pid"], starts[1]["pid"])
        self.assertTrue(starts[1]["resumed"])

    def test_builtin_conflict_cannot_be_bypassed_by_explicit_restart(self):
        from oicoach_runtime.customization import update
        self.wait_status(self.schedule()["event_id"])
        session = next((self.config.state_dir / "sessions").glob("*/session.json"))
        before = session.read_bytes()
        manifest = self.config.workflow_root / "echo/workflow.toml"
        manifest.write_text(manifest.read_text() + 'builtin_skills = ["paveway-tijian"]\n')
        update(self.config, "echo", 0, "skill-put", name="paveway-tijian", description="Legacy user record", content="USER_BODY")
        update(self.config, "echo", 1, "skill-disable", name="paveway-tijian")
        settings = manifest.parent / "customization.json"
        original_settings = settings.read_bytes()
        job = request(self.socket_path, "agent.restart", {"task_id": "task", "workflow_id": "echo"})
        result = self.wait_restart(job["restart_id"], statuses=("succeeded", "failed"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["prompt_replayed"])
        self.assertEqual(session.read_bytes(), before)
        self.assertEqual(settings.read_bytes(), original_settings)
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)

    def test_ask_is_idempotent_and_final_reply_is_readable_through_cli(self):
        command = [sys.executable, "-m", "oicoach_runtime", "ask", "--workflow", "echo", "--task", "ask",
                   "--message", "请安排今天的训练", "--dedupe-key", "request-1", "--config", str(self.config_path), "--json"]
        first = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(first.returncode, 0, first.stderr)
        event = self.wait_status(json.loads(first.stdout)["result"]["event_id"])
        repeated = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["result"]["event_id"], event["event_id"])
        output = subprocess.run([sys.executable, "-m", "oicoach_runtime", "runs", "output", event["run_id"],
                                 "--config", str(self.config_path), "--json"], capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(output.stdout)["result"]["text"], "fake completion")
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)

    def test_output_is_bounded_private_and_local_commands_do_not_reuse_old_text(self):
        reply = "CONFIDENTIAL_" + "字" * 10000
        event = request(self.socket_path, "schedule", {"workflow_id": "echo", "task_id": "task", "reason": "output test",
            "dedupe_key": "bounded", "at": "now", "payload": {"fake_reply": reply}})
        completed = self.wait_status(event["event_id"])
        output = request(self.socket_path, "runs.output", {"run_id": completed["run_id"]})
        self.assertTrue(output["truncated"])
        self.assertLessEqual(len(output["text"].encode()), 16384)
        self.assertNotIn("CONFIDENTIAL_", (self.root / "daemon.log").read_text())
        local = self.wait_status(self.schedule("local", mode="local-only")["event_id"])
        self.assertEqual(request(self.socket_path, "runs.output", {"run_id": local["run_id"]})["status"], "unavailable")

    def test_late_persistence_notice_during_output_query_is_uncertain(self):
        completed = self.wait_status(self.schedule(mode="output-persistence-notice")["event_id"], ("uncertain",))
        self.assertEqual(completed["last_error_code"], "agent_session_persistence_error")
        self.assertEqual(completed["attempt"], 1)
        self.assertEqual(request(self.socket_path, "runs.output", {"run_id": completed["run_id"]})["status"], "unavailable")

    def test_wait_timeout_preserves_queue_and_cancelled_event_returns_failure(self):
        event = self.schedule(at=utc_text(datetime.now(timezone.utc) + timedelta(seconds=30)))
        command = [sys.executable, "-m", "oicoach_runtime", "events", "wait", event["event_id"],
                   "--timeout", "0.1", "--config", str(self.config_path), "--json"]
        waiting = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(waiting.returncode, 1)
        self.assertEqual(json.loads(waiting.stderr)["error"]["code"], "wait_timeout")
        queued = self.event(event["event_id"])
        self.assertEqual(queued["status"], "queued")
        request(self.socket_path, "events.cancel", {"event_id": event["event_id"], "expected_version": queued["version"]})
        cancelled = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(cancelled.returncode, 1)
        self.assertEqual(json.loads(cancelled.stdout)["result"]["status"], "cancelled")

    def test_ask_wait_returns_reply_and_remote_conflicts_keep_their_error_code(self):
        command = [sys.executable, "-m", "oicoach_runtime", "ask", "--workflow", "echo", "--task", "waiting",
                   "--message", "training request", "--dedupe-key", "wait-request", "--wait", "--timeout", "5",
                   "--config", str(self.config_path), "--json"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)["result"]
        self.assertEqual(result["event"]["status"], "succeeded")
        self.assertEqual(result["output"]["text"], "fake completion")
        command[command.index("training request")] = "different request"
        conflict = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(conflict.returncode, 1)
        self.assertEqual(json.loads(conflict.stderr)["error"]["code"], "conflict")

    def test_active_restart_stops_owned_run_and_keeps_uncertain_for_review(self):
        active = self.schedule(mode="hold")
        deadline = time.monotonic() + 5
        while not (self.root / "fake-entered").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        job = request(self.socket_path, "agent.restart", {"task_id": "task"})
        self.wait_restart(job["restart_id"])
        event = self.wait_status(active["event_id"], ("uncertain",))
        self.assertEqual(event["last_error_code"], "agent_restart_requested")
        self.assertEqual(event["attempt"], 1)
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)
        session = json.loads(next((self.config.state_dir / "sessions").glob("*/session.json")).read_text())
        self.assertEqual(len(session["prompts"]), 1)
        self.assertEqual(request(self.socket_path, "status")["supervisor"]["consecutive_failures"], 0)

    def test_restart_waits_for_another_task_and_rejects_duplicate_request(self):
        self.wait_status(self.schedule("idle", task="b")["event_id"])
        active = self.schedule("hold", task="a", mode="hold")
        self.wait_status(active["event_id"], ("running",))
        job = request(self.socket_path, "agent.restart", {"task_id": "b"})
        self.assertEqual(request(self.socket_path, "agent.show", {"restart_id": job["restart_id"]})["status"], "requested")
        with self.assertRaises(RuntimeContractError):
            request(self.socket_path, "agent.restart", {"task_id": "b"})
        self.assertEqual(self.event(active["event_id"])["status"], "running")
        (self.root / "fake-release").touch()
        self.wait_status(active["event_id"])
        self.wait_restart(job["restart_id"])

    def test_failed_restart_does_not_reset_circuit(self):
        self.wait_status(self.schedule()["event_id"])
        (self.root / "fake-mode").write_text("crash-before-ready")
        job = request(self.socket_path, "agent.restart", {"task_id": "task", "reset_circuit": True})
        failed = self.wait_restart(job["restart_id"], ("failed",))
        self.assertEqual(failed["error_code"], "agent_restart_failed")
        self.assertEqual(request(self.socket_path, "status")["supervisor"]["consecutive_failures"], 1)

    def test_successful_explicit_reset_leaves_uncertain_event_unreplayed(self):
        self.stop()
        config = replace(self.config, restart=replace(self.config.restart, max_failures_per_window=1, cooldown_seconds=30))
        self.config_path.write_text(config_text(config))
        self.start()
        event = self.wait_status(self.schedule(mode="crash-after-ack")["event_id"], ("uncertain",))
        self.assertEqual(request(self.socket_path, "status")["supervisor"]["phase"], "open")
        job = request(self.socket_path, "agent.restart", {"task_id": "task", "reset_circuit": True})
        self.wait_restart(job["restart_id"])
        self.assertEqual(request(self.socket_path, "status")["supervisor"]["phase"], "closed")
        self.assertEqual(self.event(event["event_id"])["status"], "uncertain")
        self.assertEqual(self.event(event["event_id"])["attempt"], 1)
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)
        self.wait_status(self.schedule("healthy", task="other")["event_id"])

    def test_daemon_shutdown_interrupts_a_pending_restart_without_replay(self):
        self.wait_status(self.schedule()["event_id"])
        (self.root / "fake-mode").write_text("hang-before-ready")
        job = request(self.socket_path, "agent.restart", {"task_id": "task"})
        self.wait_restart(job["restart_id"], ("running",))
        self.stop()
        self.assertEqual(self.process.returncode, 0)
        (self.root / "fake-mode").unlink()
        self.start()
        self.assertEqual(request(self.socket_path, "agent.show", {"restart_id": job["restart_id"]})["status"], "interrupted")
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)

    def test_circuit_preserves_queue_and_survives_daemon_restart(self):
        self.stop()
        config = replace(self.config, restart=replace(self.config.restart, max_failures_per_window=1, cooldown_seconds=2))
        self.config_path.write_text(config_text(config))
        (self.root / "fake-mode").write_text("crash-before-ready")
        self.start()
        events = [self.schedule(str(i), task=f"circuit-{i}") for i in range(3)]
        deadline = time.monotonic() + 5
        while request(self.socket_path, "status")["supervisor"]["phase"] != "open":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)
        self.assertEqual(request(self.socket_path, "status")["counts"]["queued"], 3)
        next_start = request(self.socket_path, "status")["supervisor"]["next_start_utc"]
        self.stop()
        (self.root / "fake-mode").unlink()
        self.start()
        self.assertEqual(request(self.socket_path, "status")["supervisor"]["next_start_utc"], next_start)
        for event in events:
            self.wait_status(event["event_id"])
        self.assertEqual(request(self.socket_path, "status")["supervisor"]["phase"], "closed")
        self.assertEqual(len(request(self.socket_path, "runs.list")), 4)

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
        # Hold the event in the future until persistence across restart has
        # been checked; CLI startup must not race a subsecond due time.
        due = utc_text(datetime.now(timezone.utc) + timedelta(days=1))
        event = self.schedule(at=due)
        other = subprocess.run([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.config_path), "--json"],
                               capture_output=True, text=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertEqual(json.loads(other.stderr)["error"]["code"], "conflict")
        self.stop()
        self.assertEqual(self.process.returncode, 0)
        self.start()
        retained = self.event(event["event_id"])
        self.assertEqual(retained["status"], "queued")
        self.assertEqual(retained["not_before_utc"], due)
        self.assertEqual(retained["version"], event["version"])
        self.assertEqual(request(self.socket_path, "runs.list"), [])
        request(self.socket_path, "events.reschedule", {
            "event_id": event["event_id"], "expected_version": retained["version"], "at": "now"})
        completed = self.wait_status(event["event_id"])
        self.assertEqual(completed["attempt"], 1)
        self.assertEqual(len(request(self.socket_path, "runs.list")), 1)

    def test_startup_failure_retries_with_a_bound_and_reaches_dead_letter(self):
        (self.root / "fake-mode").write_text("crash-before-ready")
        event = self.schedule()
        self.wait_status(event["event_id"], ("dead_letter",))
        runs = request(self.socket_path, "runs.list")
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(run["dispatch_intent_at"] is None for run in runs))
        self.assertEqual(self.event(event["event_id"])["attempt"], 3)
        self.assertIsNone(self.process.poll())
        (self.root / "fake-mode").unlink()
        self.wait_status(self.schedule("repaired")["event_id"])
        self.assertEqual(len(request(self.socket_path, "runs.list")), 4)

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

    def test_provider_auth_error_is_saved_without_provider_secrets_or_replay(self):
        event = self.schedule("auth-error", "auth-task", "auth-error")
        finished = self.wait_status(event["event_id"], ("failed",))
        self.assertEqual(finished["last_error_code"], "agent_auth_failed")
        self.assertIn("401", finished["last_error_message"])
        run = request(self.socket_path, "runs.show", {"run_id": finished["run_id"]})
        self.assertEqual(run["error_message"], finished["last_error_message"])
        self.assertIsNotNone(run["settled_at"])
        self.assertEqual(finished["attempt"], 1)
        self.assertEqual(request(self.socket_path, "runs.output", {"run_id": run["run_id"]})["status"], "unavailable")
        for secret in ("sk-private-test-value", "private-token"):
            self.assertNotIn(secret, json.dumps(finished) + json.dumps(run) + (self.root / "daemon.log").read_text())

    def test_customization_applies_next_dispatch_without_rebinding_existing_task(self):
        from oicoach_runtime.customization import update, IDENTITY_PROMPT
        from oicoach_runtime.context_budget import CONTEXT_PROMPT
        update(self.config, "echo", 0, "prompt", mode="append", content="first prompt")
        first = self.wait_status(self.schedule("custom-first", "custom-task")["event_id"])
        update(self.config, "echo", 1, "prompt", mode="replace", content="second prompt")
        second = self.wait_status(self.schedule("custom-second", "custom-task")["event_id"])
        first_run = request(self.socket_path, "runs.show", {"run_id": first["run_id"]})
        second_run = request(self.socket_path, "runs.show", {"run_id": second["run_id"]})
        self.assertEqual(first_run["binding_id"], second_run["binding_id"])
        parent = self.config.state_dir / "sessions" / first_run["binding_id"] / "customizations"
        self.assertEqual((parent / first["run_id"] / "system.md").read_text(), "first prompt\n\n" + IDENTITY_PROMPT + "\n\n" + CONTEXT_PROMPT)
        self.assertEqual((parent / second["run_id"] / "system.md").read_text(), "second prompt\n\n" + IDENTITY_PROMPT + "\n\n" + CONTEXT_PROMPT)
        self.assertEqual(second["attempt"], 1)

    def test_model_settings_apply_next_dispatch_and_resume_the_existing_session(self):
        from oicoach_runtime.customization import update
        update(self.config, "echo", 0, "inference", model="fake/coach", thinking="high")
        first = self.wait_status(self.schedule("model-first", "model-task", mode="hold")["event_id"], ("running",))
        entered = self.root / "fake-entered"
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(entered.exists())
        update(self.config, "echo", 1, "inference", model="fake/fast", thinking="off")
        launches = [json.loads(line) for line in (self.root / "fake-starts.jsonl").read_text().splitlines()]
        self.assertEqual((launches[0]["model"], launches[0]["thinking"]), ("fake/coach", "high"))
        (self.root / "fake-release").touch()
        self.wait_status(first["event_id"])
        self.wait_status(self.schedule("model-second", "model-task")["event_id"])
        launches = [json.loads(line) for line in (self.root / "fake-starts.jsonl").read_text().splitlines()]
        self.assertEqual((launches[1]["model"], launches[1]["thinking"]), ("fake/fast", "off"))
        self.assertTrue(launches[1]["resumed"])
        self.assertEqual(launches[0]["session_dir"], launches[1]["session_dir"])

    def test_proxy_updates_apply_to_next_omp_without_daemon_restart(self):
        import hashlib
        from oicoach_runtime import service
        first_proxy, second_proxy = "http://127.0.0.1:18081", "http://127.0.0.1:18082"
        service.update_environment(self.config_path, {"HTTPS_PROXY": first_proxy, "https_proxy": first_proxy})
        self.wait_status(self.schedule("proxy-first", "proxy-task")["event_id"])
        daemon_pid = self.process.pid
        service.update_environment(self.config_path, {"HTTPS_PROXY": second_proxy, "https_proxy": second_proxy})
        self.wait_status(self.schedule("proxy-second", "proxy-task")["event_id"])
        launches = [json.loads(line) for line in (self.root / "fake-starts.jsonl").read_text().splitlines()]
        self.assertEqual(launches[0]["https_proxy_hash"], hashlib.sha256(first_proxy.encode()).hexdigest())
        self.assertEqual(launches[1]["https_proxy_hash"], hashlib.sha256(second_proxy.encode()).hexdigest())
        self.assertTrue(launches[1]["resumed"])
        self.assertEqual(self.process.pid, daemon_pid)

    def test_default_local_git_archives_all_coaches_without_setup(self):
        from oicoach_runtime import git_sync
        from oicoach_runtime.workflow_setup import create
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not git_sync.settings(self.config):
            time.sleep(0.02)
        values = git_sync.settings(self.config)
        self.assertIsNotNone(values)
        self.assertEqual(values["remote"], "")
        create(self.config, "another-coach", self.root, "example")
        first = self.wait_status(self.schedule("default-git", "one")["event_id"])
        second = request(self.socket_path, "schedule", {
            "workflow_id": "another-coach", "task_id": "two", "reason": "integration", "dedupe_key": "other-coach",
            "payload": {}, "at": utc_text(datetime.now(timezone.utc))})
        second = self.wait_status(second["event_id"])
        request(self.socket_path, "sync.request")
        deadline = time.monotonic() + 6
        records = []
        while time.monotonic() < deadline:
            records = git_sync.history(self.config)["records"]
            if {first["run_id"], second["run_id"]} <= {r["run_id"] for r in records}:
                break
            time.sleep(0.05)
        self.assertEqual({r["workflow_id"] for r in records}, {"echo", "another-coach"})
        self.assertEqual({r["device_id"] for r in records}, {values["device_id"]})
        self.assertEqual(git_sync.settings(self.config)["repository"], values["repository"])
        self.assertEqual(subprocess.check_output(["git", "-C", values["repository"], "remote"], text=True), "")

    def test_automatic_two_device_git_sync_without_ui_or_queue_import(self):
        from oicoach_runtime import customization, git_sync
        from oicoach_runtime.workflow_setup import create
        remote = self.root / "user-remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], capture_output=True, check=True)
        other = self.root / "other-device"
        other.mkdir(mode=0o700)
        path = other / "config.toml"
        initialize(path, str(other / "state"), str(other / "workflows"))
        base = load_config(path)
        base = replace(base, agent=self.config.agent, server=replace(base.server, poll_interval_seconds=0.02))
        path.write_text(config_text(base))
        create(base, "echo", other, "example")
        log = (other / "daemon.log").open("w")
        daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(path)], stdout=log, stderr=log)
        try:
            event = self.wait_status(self.schedule("sync-auto", "sync-history")["event_id"])
            customization.update(self.config, "echo", 0, "prompt", mode="append", content="automatic profile transfer")
            def safe_configure(cfg, repo):
                sync_deadline = time.monotonic() + 10
                while True:
                    try:
                        git_sync.configure(cfg, repo, str(remote), interval=10)
                        break
                    except ConflictError:
                        if time.monotonic() > sync_deadline:
                            raise
                        time.sleep(0.05)

            safe_configure(self.config, self.config.state_dir / "user-data-git")
            safe_configure(base, base.state_dir / "user-data-git")
            deadline = time.monotonic() + 30
            transferred = False
            while time.monotonic() < deadline:
                self.assertIsNone(daemon.poll())
                prompt = customization.show(base, "echo")["settings"]["system_prompt"]["text"]
                rows = git_sync.history(base)["records"]
                if prompt == "automatic profile transfer" and any(r["run_id"] == event["run_id"] for r in rows):
                    transferred = True
                    break
                time.sleep(0.05)
            self.assertTrue(transferred, git_sync.status(base))
            self.assertEqual(request(base.server.socket_path, "events.list"), [])
            self.assertEqual(request(base.server.socket_path, "status")["daemon"], "running")
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=5)
            log.close()

    def test_git_sync_failure_does_not_block_foreground_training(self):
        from oicoach_runtime import git_sync
        # Configure the failure before startup, independent of the initial archive lock.
        self.stop()
        git_sync.configure(self.config, self.config.state_dir / "user-data-git", str(self.root / "missing.git"), interval=10)
        self.start()
        event = self.schedule("sync-offline-agent", "still-runs")
        self.assertEqual(self.wait_status(event["event_id"])["status"], "succeeded")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and git_sync.status(self.config)["last"].get("state") != "error":
            time.sleep(0.05)
        self.assertEqual(git_sync.status(self.config)["last"].get("state"), "error")
        health = request(self.socket_path, "status")
        self.assertEqual(health["daemon"], "running")
        self.assertEqual(health["agent_health"], "idle")

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

    def test_guardian_sigkill_reaps_omp_and_adopted_tool_before_next_run(self):
        event = self.schedule(mode="hold-with-tool")
        deadline = time.monotonic() + 5
        while not (self.root / "fake-entered").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        event = self.event(event["event_id"])
        run = request(self.socket_path, "runs.show", {"run_id": event["run_id"]})
        omp_pid = int((self.root / "fake-entered").read_text())
        tool_pid = int((self.root / "fake-tool-pid").read_text())
        os.kill(run["agent_pid"], signal.SIGKILL)
        self.wait_status(event["event_id"], ("uncertain",))
        self.assertFalse(Path(f"/proc/{omp_pid}").exists())
        self.assertFalse(Path(f"/proc/{tool_pid}").exists())
        self.wait_status(self.schedule("next", task="healthy")["event_id"])
        self.assertEqual(len(request(self.socket_path, "runs.list")), 2)

    def test_persistence_failure_cannot_succeed(self):
        for index, mode in enumerate(("persistence-error", "persistence-notice")):
            with self.subTest(mode=mode):
                event = self.schedule(str(index), f"persistence-{index}", mode)
                finished = self.wait_status(event["event_id"], ("uncertain",))
                self.assertEqual(finished["attempt"], 1)
                run = request(self.socket_path, "runs.show", {"run_id": finished["run_id"]})
                self.assertIsNone(run["settled_at"])
                if mode == "persistence-error":
                    self.assertEqual(run["prompt_result_status"], "completed")
                    self.assertEqual(run["agent_exit_code"], 15)
        self.assertNotIn("private failure detail", (self.root / "daemon.log").read_text())

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
        # This test holds a real child while 15 durable IPC mutations run. Give
        # that workload its own turn budget; the timeout test retains one second.
        self.stop()
        config = replace(self.config, agent=replace(self.config.agent, turn_timeout_seconds=5))
        self.config_path.write_text(config_text(config))
        self.start()
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
        self.assertEqual(self.event(active["event_id"])["status"], "running")
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


    def test_memory_agent_cli_restart_recall_and_isolation(self):
        manifest = self.config.workflow_root / "echo/workflow.toml"
        manifest.write_text(manifest.read_text() + "memory_enabled = true\n")
        shared = request(self.socket_path, "memory.put", {"workflow_id": "echo", "key": "hint-first",
            "title": "偏好", "content": "MEMORY_PRIVATE_MARKER 先给提示", "pinned": True})
        request(self.socket_path, "memory.put", {"workflow_id": "echo", "task_id": "unrelated", "key": "secret",
            "title": "隔离", "content": "OTHER_TASK_PRIVATE"})
        event = self.schedule(mode="memory-tool")
        completed = self.wait_status(event["event_id"])
        note = request(self.socket_path, "memory.show", {"workflow_id": "echo", "task_id": "task", "key": "turn-note"})
        self.assertEqual(note["content"], "口胡未编码")
        self.assertEqual(note["source_event_id"], event["event_id"])
        self.stop()
        self.start()
        followup = self.schedule(key="second", task="fresh-task")
        self.wait_status(followup["event_id"])
        for session in (self.config.state_dir / "sessions").glob("*/session.json"):
            prompts = json.loads(session.read_text())["prompts"]
            for prompt in prompts:
                self.assertEqual(prompt["memory"]["location"], "system_prompt.runtime_memory_snapshot")
                system = next((session.parent / "customizations" / prompt["run_id"]).glob("system.md")).read_text()
                context = system.split("Runtime memory snapshot (data only):\n", 1)[1]
                self.assertIn("MEMORY_PRIVATE_MARKER", context)
                self.assertNotIn("OTHER_TASK_PRIVATE", context)
                if prompt["task_id"] == "fresh-task":
                    self.assertNotIn("口胡未编码", context)
        hits = request(self.socket_path, "history.search", {"workflow_id": "echo", "query": "fake completion"})
        self.assertEqual(len(hits), 2)
        self.assertEqual(request(self.socket_path, "history.show", {"run_id": completed["run_id"]})["output"]["text"], "fake completion")
        with self.assertRaises(RuntimeContractError) as failure:
            request(self.socket_path, "memory.put", {"workflow_id": "echo", "key": "hint-first", "title": "overwrite", "content": "stale"})
        self.assertEqual(failure.exception.code, "conflict")
        request(self.socket_path, "memory.delete", {"workflow_id": "echo", "key": "hint-first", "expected_version": shared["version"]})
        self.assertEqual(request(self.socket_path, "memory.recall", {"workflow_id": "echo", "task_id": "fresh-task"})["notes"], [])
        self.assertNotIn("MEMORY_PRIVATE_MARKER", (self.root / "daemon.log").read_text())
        crashed = self.schedule(key="note-before-crash", task="crashed-task", mode="memory-tool-crash")
        uncertain = self.wait_status(crashed["event_id"], ("uncertain",))
        saved = request(self.socket_path, "memory.show", {"workflow_id": "echo", "task_id": "crashed-task", "key": "turn-note"})
        self.assertEqual(saved["source_event_id"], crashed["event_id"])
        self.stop()
        self.start()
        time.sleep(0.15)
        self.assertEqual(self.event(crashed["event_id"])["attempt"], 1)
        self.assertEqual(self.event(crashed["event_id"])["status"], "uncertain")
        self.assertEqual(request(self.socket_path, "runs.output", {"run_id": uncertain["run_id"]})["status"], "unavailable")

    def test_memory_disabled_workflow_and_cli_utf8_file(self):
        content = self.root / "note.txt"
        content.write_text("先提示，避免直接给代码。字面量 $(echo secret) `id` ${event_json}")
        command = [sys.executable, "-m", "oicoach_runtime", "memory", "put", "--config", str(self.config_path),
                   "--workflow", "echo", "--key", "preference", "--title", "提示偏好", "--content-file", str(content), "--json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["content"], content.read_text())
        event = self.schedule()
        self.wait_status(event["event_id"])
        session = next((self.config.state_dir / "sessions").glob("*/session.json"))
        self.assertNotIn("memory", json.loads(session.read_text())["prompts"][0])
        self.assertEqual(len(request(self.socket_path, "memory.list", {"workflow_id": "echo", "query": "提示"})), 1)
        content.write_bytes(b"\xff")
        invalid = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(json.loads(invalid.stderr)["error"]["code"], "invalid_input")
        for command, raw in (("memory.unknown", {}), ("memory.put", {"workflow_id": "echo"}),
                             ("history.search", {"workflow_id": "echo", "unexpected": True})):
            with self.assertRaises(RuntimeContractError):
                request(self.socket_path, command, raw)
        self.assertEqual(request(self.socket_path, "status")["daemon"], "running")

if __name__ == "__main__":
    unittest.main()
