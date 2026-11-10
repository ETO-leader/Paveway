from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_absolute_time_is_persisted_for_each_intent_with_timezone(self):
        for intent in ("reminder", "agent_wake"):
            event = self.schedule(intent=intent, dedupe_key="absolute-" + intent,
                when={"kind": "at", "at": "2026-11-11T21:00:00+08:00"})
            self.assertEqual(event["payload"]["intent"], intent)
            self.assertEqual(event["not_before_utc"], "2026-11-11T13:00:00.000000Z")
            self.assertEqual(event["schedule_mode"], "at")

    def test_snapshot_failure_rolls_back_event_and_audit_then_retry_is_idempotent(self):
        before = self.store.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        original = self.store._set_metadata
        def fail(db, key, value):
            if key.startswith("native-wakeup-config:"):
                raise OSError("injected snapshot write failure")
            original(db, key, value)
        with patch.object(self.store, "_set_metadata", side_effect=fail):
            with self.assertRaises(OSError):
                self.schedule(intent="agent_wake")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], before)
        event = self.schedule(intent="agent_wake")
        key = "native-wakeup-config:v1:" + event["event_id"]
        snapshot = self.store.metadata(key)
        record = Frontend(self.store).authenticate(**self.owner)
        with self.store.transaction() as db:
            self.store._set_metadata(db, record["config_key"], {"changed": True})
        self.assertEqual(self.schedule(intent="agent_wake")["event_id"], event["event_id"])
        self.assertEqual(self.store.metadata(key), snapshot)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        with self.store.transaction() as db:
            db.execute("DELETE FROM runtime_metadata WHERE key=?", (key,))
        with self.assertRaises(RuntimeContractError) as error:
            self.schedule(intent="agent_wake")
        self.assertEqual(error.exception.code, "native_wakeup_snapshot_missing")
        self.assertIsNone(self.store.metadata(key))

    def test_invalid_launch_snapshot_never_leaves_an_event(self):
        record = Frontend(self.store).authenticate(**self.owner)
        before = self.store.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        for snapshot in (None, {"invalid": True}):
            with self.subTest(snapshot=snapshot):
                with self.store.transaction() as db:
                    self.store._set_metadata(db, record["config_key"], snapshot)
                with self.assertRaises(RuntimeContractError):
                    self.schedule(intent="agent_wake")
                self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                self.assertEqual(self.store.connection.execute("SELECT count(*) FROM audit_log").fetchone()[0], before)

    def test_bound_memory_expiry_source_and_background_logical_scope(self):
        from oicoach_runtime.workflow_setup import create
        from oicoach_runtime.scheduler import Runtime
        from oicoach_runtime.memory import Memory
        from oicoach_runtime.models import utc_text
        create(self.config, "example", self.config.workflow_root.parent)
        runtime = Runtime(self.config)
        runtime.store = self.store
        event = self.schedule()
        values = {"scope": "task", "key": "training.plan-one", "title": "Checkpoint",
                  "content": "question A; user reported progress; hints not authorized", "expected_version": 0,
                  "source_event_id": event["event_id"], "expires_at": utc_text(self.now + timedelta(seconds=5))}
        result = runtime.dispatch_api("native_memory.put", {**self.owner, "parameters": values})
        self.assertEqual(result["task_id"], "training")
        self.assertEqual(result["source_event_id"], event["event_id"])
        recalled = runtime.dispatch_api("memory.recall", {"workflow_id": "example", "task_id": "training"})
        self.assertEqual(recalled["notes"][0]["version"], 1)
        foreign = self.store.enqueue("example", "another-task", "foreign", "foreign", at="now")
        with self.assertRaises(ConflictError):
            runtime.dispatch_api("native_memory.put", {**self.owner, "parameters": {**values,
                "source_event_id": foreign["event_id"], "expected_version": 1}})
        self.now += timedelta(seconds=6)
        expired = Memory(self.store).context("example", "training", known=[{"task_id": "training", "key": values["key"]}])
        self.assertEqual(expired["notes"], [])
        self.assertEqual(expired["withdrawn"][0]["reason"], "expired")

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

    def test_intent_filters_and_mutations_reject_wrong_type_and_stale_revision(self):
        reminder = self.schedule()
        agent = self.schedule(intent="agent_wake", dedupe_key="ai-work")
        for intent, expected in (("reminder", reminder), ("agent_wake", agent)):
            rows = self.wakeup.call("list", self.owner, {"intent": intent})
            self.assertEqual([row["event_id"] for row in rows], [expected["event_id"]])
            self.assertEqual(rows[0]["version"], expected["version"])
            self.assertEqual(rows[0]["intent"], intent)
        with self.assertRaises(ConflictError):
            self.wakeup.call("cancel", self.owner, {"event_id": reminder["event_id"], "expected_version": reminder["version"], "intent": "agent_wake"})
        with self.assertRaises(ConflictError):
            self.wakeup.call("reschedule", self.owner, {"event_id": agent["event_id"], "expected_version": agent["version"], "intent": "reminder", "at": agent["not_before_utc"]})
        changed = self.wakeup.call("reschedule", self.owner, {"event_id": reminder["event_id"], "expected_version": reminder["version"], "intent": "reminder", "at": "2026-11-01T12:00:00Z"})
        with self.assertRaises(ConflictError):
            self.wakeup.call("cancel", self.owner, {"event_id": reminder["event_id"], "expected_version": reminder["version"], "intent": "reminder"})
        self.assertEqual(self.store.event(reminder["event_id"])["version"], changed["version"])

    def test_reminder_delivery_and_completion_roll_back_together(self):
        event = self.schedule(when={"kind": "after_now", "seconds": 0})
        with patch.object(self.store, "complete_reminder", side_effect=OSError("injected commit boundary")):
            with self.assertRaises(OSError): Inbox(self.store).project_due(complete=True)
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")
        self.assertEqual(Inbox(self.store).list("example")["messages"], [])
        Inbox(self.store).project_due(complete=True)
        Inbox(self.store).project_due(complete=True)
        self.assertEqual(self.store.event(event["event_id"])["status"], "succeeded")
        self.assertIsNone(self.store.event(event["event_id"])["run_id"])
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)

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
            if intent == "reminder":
                inbox.project_due(complete=True)
                continue
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
        self.assertEqual({row["kind"] for row in rows}, {"reminder"})
        self.assertTrue(all(row["state"] == "created" for row in rows))
