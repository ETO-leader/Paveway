from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.inbox import Inbox
from oicoach_runtime.store import Store
from oicoach_runtime.wakeup import Wakeup
from oicoach_runtime.workflows import WorkflowProvider


class WakeupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-wakeup-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        path = root / "config.toml"
        initialize(path, str(root / "state"), str(root / "workflows"))
        self.config = load_config(path)
        self.now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        frontend = Frontend(self.store)
        lease = frontend.begin("example", "training", root)
        self.owner = {key: lease[key] for key in ("conversation_id", "generation", "owner_token")}
        directory = Path(lease["session_dir"])
        directory.mkdir(parents=True, mode=0o700)
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.stop_child)
        frontend.started(**self.owner, guardian_pid=os.getpid())
        frontend.attest(**self.owner, omp_pid=self.child.pid, session_id="native", session_file=str(directory / "native.jsonl"))
        self.wakeup = Wakeup(self.store)

    def stop_child(self):
        self.child.terminate()
        self.child.wait()

    def schedule(self, **changes):
        parameters = {"when": {"kind": "after_now", "seconds": 600}, "reason": "explicit reminder",
            "dedupe_key": "same-request", "intent": "reminder", "authorized_by_user": True}
        parameters.update(changes)
        return self.wakeup.call("schedule", self.owner, parameters)

    def test_after_now_uses_runtime_clock_and_semantic_dedupe(self):
        event = self.schedule()
        self.now += timedelta(seconds=30)
        repeated = self.schedule()
        self.assertEqual(event["event_id"], repeated["event_id"])
        self.assertEqual(event["not_before_utc"], repeated["not_before_utc"])
        self.assertIn("12:10:00", event["not_before_utc"])
        with self.assertRaises(ConflictError):
            self.schedule(reason="different request")
        with self.assertRaises(RuntimeContractError):
            self.schedule(when={"kind": "after_turn_settled", "seconds": 600})
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_scope_cas_opt_in_and_old_generation_are_enforced(self):
        with self.assertRaises(RuntimeContractError): self.schedule(authorized_by_user=False)
        event = self.schedule()
        with self.assertRaises(ConflictError):
            self.wakeup.call("cancel", self.owner, {"event_id": event["event_id"], "expected_version": event["version"] + 1})
        other = self.store.enqueue("another", "training", "another scope", "other", at="now")
        with self.assertRaises(ConflictError):
            self.wakeup.call("cancel", self.owner, {"event_id": other["event_id"], "expected_version": other["version"]})
        with self.assertRaises(ConflictError):
            self.wakeup.call("list", {**self.owner, "generation": 0}, {})
        done = self.wakeup.call("cancel", self.owner, {"event_id": event["event_id"], "expected_version": event["version"]})
        self.assertEqual(done["status"], "cancelled")

    def test_reminder_persists_before_model_and_offer_is_not_completion(self):
        event = self.schedule()
        inbox = Inbox(self.store)
        created = inbox.due(event)
        self.assertEqual(inbox.due(event)["delivery_id"], created["delivery_id"])
        self.assertEqual(inbox.list("example")["messages"][0]["state"], "created")
        offered = inbox.offer("example")["messages"][0]
        self.assertEqual(offered["state"], "offered")
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        with self.assertRaises(ConflictError):
            inbox.acknowledge("another", offered["delivery_id"], offered["version"])
        acknowledged = inbox.acknowledge("example", offered["delivery_id"], offered["version"])
        self.assertEqual(acknowledged["state"], "acknowledged")
        with self.assertRaises(ConflictError):
            inbox.acknowledge("example", offered["delivery_id"], offered["version"])
        reopened = Store(self.config)
        try:
            self.assertEqual(Inbox(reopened).list("example")["messages"][0]["state"], "acknowledged")
        finally:
            reopened.close()

    def test_successful_agent_wake_can_remain_quiet_without_losing_explicit_reminders(self):
        from oicoach_runtime.workflow_setup import create
        create(self.config, "example", self.config.workflow_root.parent)
        inbox = Inbox(self.store)
        for intent in ("agent_wake", "reminder"):
            event = self.schedule(intent=intent, dedupe_key=intent, when={"kind": "after_now", "seconds": 0})
            claimed = self.store.claim_due("fixture")
            inbox.due(event)
            self.store.bind(claimed, WorkflowProvider(self.config.workflow_root).describe("example"))
            self.store.dispatch(claimed["run_id"])
            self.store.evidence(claimed["run_id"], acknowledged=True)
            self.store.evidence(claimed["run_id"], result="completed")
            self.store.finish(claimed["run_id"], "succeeded", result="completed", settled=True)
            inbox.collect()
        rows = inbox.list("example")["messages"]
        self.assertEqual({row["event_id"] for row in rows}, {event["event_id"]})
        self.assertEqual({row["kind"] for row in rows}, {"reminder", "result"})
        self.assertTrue(all(row["state"] == "created" for row in rows))
