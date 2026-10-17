import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.models import utc_text
from oicoach_runtime.store import Store
from oicoach_runtime.workflows import Workflow


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config_path = self.root / "config/config.toml"
        initialize(config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(config_path)
        self.now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def enqueue(self, **kwargs):
        request = dict(workflow_id="echo", task_id="task", reason="test", dedupe_key="one", at=utc_text(self.now))
        request.update(kwargs)
        return self.store.enqueue(**request)

    def active(self):
        event = self.enqueue()
        claimed = self.store.claim_due("daemon")
        self.store.dispatch(claimed["run_id"])
        return event, claimed["run_id"]

    def test_persistent_dedupe_and_original_fingerprint_after_reschedule(self):
        event = self.enqueue(payload={"b": 2, "a": 1})
        duplicate = self.enqueue(payload={"a": 1, "b": 2})
        self.assertEqual(event["event_id"], duplicate["event_id"])
        edited = self.store.modify(event["event_id"], event["version"], at=utc_text(self.now + timedelta(days=1)))
        self.assertEqual(self.enqueue(payload={"a": 1, "b": 2})["version"], edited["version"])
        with self.assertRaises(ConflictError):
            self.enqueue(reason="changed")
        reopened = Store(self.config)
        try:
            self.assertEqual(reopened.event(event["event_id"])["not_before_utc"], edited["not_before_utc"])
        finally:
            reopened.close()

    def test_transaction_rolls_back_event_if_audit_fails(self):
        with patch.object(self.store, "audit", side_effect=RuntimeError("crash-before-commit")):
            with self.assertRaises(RuntimeError):
                self.enqueue()
        self.assertEqual(self.store.events(), [])

    def test_dispatch_commit_boundary_never_loses_or_replays_intent(self):
        event = self.enqueue()
        claimed = self.store.claim_due("daemon")
        with patch.object(self.store, "audit", side_effect=RuntimeError("before-commit")):
            with self.assertRaises(RuntimeError):
                self.store.dispatch(claimed["run_id"])
        self.assertEqual(self.store.run(claimed["run_id"])["status"], "leased")
        self.assertIsNone(self.store.run(claimed["run_id"])["dispatch_intent_at"])
        self.store.dispatch(claimed["run_id"])
        reopened = Store(self.config)
        try:
            reopened.reconcile()
            self.assertEqual(reopened.event(event["event_id"])["status"], "uncertain")
            self.assertIsNone(reopened.claim_due("new-daemon"))
        finally:
            reopened.close()

    def test_concurrent_same_key_and_cas_conflicts(self):
        def enqueue(_):
            store = Store(self.config, clock=lambda: self.now)
            try:
                return store.enqueue("echo", "task", "test", "concurrent", at=utc_text(self.now))["event_id"]
            finally:
                store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            identifiers = list(executor.map(enqueue, range(24)))
        self.assertEqual(len(set(identifiers)), 1)
        event = self.store.event(identifiers[0])
        self.store.modify(event["event_id"], event["version"], cancel=True)
        with self.assertRaises(ConflictError):
            self.store.modify(event["event_id"], event["version"], cancel=True)

    def test_due_order_single_claim_and_wall_clock_changes(self):
        first = self.enqueue(dedupe_key="first", at=utc_text(self.now + timedelta(seconds=1)))
        second = self.enqueue(dedupe_key="second", at=utc_text(self.now + timedelta(seconds=2)))
        self.now -= timedelta(hours=1)
        self.assertIsNone(self.store.claim_due("daemon"))
        self.now += timedelta(hours=2)
        claimed = self.store.claim_due("daemon")
        self.assertEqual(claimed["event_id"], first["event_id"])
        self.assertIsNone(self.store.claim_due("other"))
        with self.assertRaises(ConflictError):
            self.store.modify(first["event_id"], claimed["version"], cancel=True)
        self.store.dispatch(claimed["run_id"])
        self.store.evidence(claimed["run_id"], acknowledged=True, result="completed")
        self.store.finish(claimed["run_id"], "succeeded", result="completed", settled=True)
        self.assertEqual(self.store.claim_due("daemon")["event_id"], second["event_id"])
        self.assertEqual(self.store.event(first["event_id"])["attempt"], 1)

    def test_after_turn_starts_only_after_success_and_settlement(self):
        _, run_id = self.active()
        child = self.enqueue(dedupe_key="child", at=None, after_turn=10, run_id=run_id)
        self.assertEqual(child["status"], "waiting_anchor")
        self.now += timedelta(hours=1)
        self.store.evidence(run_id, acknowledged=True, result="completed")
        self.assertEqual(self.store.event(child["event_id"])["status"], "waiting_anchor")
        self.store.finish(run_id, "succeeded", result="completed", settled=True)
        anchored = self.store.event(child["event_id"])
        self.assertEqual(anchored["not_before_utc"], utc_text(self.now + timedelta(seconds=10)))
        # Original request remains idempotent after the anchor run finishes.
        self.assertEqual(self.enqueue(dedupe_key="child", at=None, after_turn=10.0, run_id=run_id)["event_id"], child["event_id"])

    def test_after_turn_rejects_wrong_scope_and_blocks_failed_anchor(self):
        _, run_id = self.active()
        with self.assertRaises(ConflictError):
            self.enqueue(task_id="other", dedupe_key="wrong", at=None, after_turn=1, run_id=run_id)
        child = self.enqueue(dedupe_key="child", at=None, after_turn=1, run_id=run_id)
        self.store.finish(run_id, "uncertain", error="crash")
        self.assertEqual(self.store.event(child["event_id"])["status"], "blocked")
        self.assertIsNone(self.store.event(child["event_id"])["not_before_utc"])

    def test_reconcile_does_not_replay_intent_and_retry_preserves_old_run(self):
        event, run_id = self.active()
        self.store.evidence(run_id, acknowledged=True, result="completed")
        self.store.reconcile()
        self.assertEqual(self.store.event(event["event_id"])["status"], "uncertain")
        self.assertIsNone(self.store.claim_due("new-daemon"))
        with self.assertRaises(ConflictError):
            self.store.retry(event["event_id"])
        self.store.retry(event["event_id"], confirmed=True)
        claimed = self.store.claim_due("new-daemon")
        self.assertNotEqual(claimed["run_id"], run_id)
        self.assertEqual(claimed["attempt"], 2)
        self.assertEqual(self.store.run(run_id)["status"], "uncertain")

    def test_reconcile_predispatch_is_blocked_not_replayed(self):
        event = self.enqueue()
        self.store.claim_due("old")
        self.store.reconcile()
        self.assertEqual(self.store.event(event["event_id"])["status"], "blocked")
        self.assertIsNone(self.store.claim_due("new"))

    def test_success_cannot_be_recorded_from_ack_only(self):
        _, run_id = self.active()
        self.store.evidence(run_id, acknowledged=True)
        with self.assertRaises(ConflictError):
            self.store.finish(run_id, "succeeded", result="completed", settled=False)
        with self.assertRaises(ConflictError):
            self.store.finish(run_id, "succeeded", result="completed", settled=True)

    def test_event_pages_are_bounded_and_offsets_are_validated(self):
        for index in range(103):
            self.enqueue(dedupe_key=str(index))
        page = self.store.events()
        rest = self.store.events(offset=100)
        self.assertEqual(len(page), 100)
        self.assertEqual(len(rest), 3)
        self.assertFalse({event["event_id"] for event in page} & {event["event_id"] for event in rest})
        with self.assertRaises(RuntimeContractError):
            self.store.events(offset=-1)

    def test_validation_rejects_oversized_nonfinite_and_wrong_types(self):
        for override in ({"payload": {"large": "x" * 65537}}, {"payload": []}, {"payload": {"bad": float("nan")}},
                         {"reason": ""}, {"task_id": "../unsafe"}, {"at": "2026-10-17T00:00:00"},
                         {"at": None, "after_turn": -1, "run_id": "run"}, {"at": None, "after_turn": True, "run_id": "run"}):
            with self.subTest(override=list(override)), self.assertRaises(RuntimeContractError):
                self.enqueue(**override)

    def test_missing_or_uncertain_session_is_not_silently_recreated(self):
        _, run_id = self.active()
        event = self.store.event(self.store.run(run_id)["event_id"])
        workflow = Workflow("echo", "1", self.root, "per_task", {})
        binding = self.store.bind(event, workflow)
        path = Path(binding["session_dir"]) / "session.json"
        path.write_text("{}")
        self.store.record_session(run_id, {"sessionFile": str(path), "sessionId": "fake"})
        self.store.finish(run_id, "uncertain", error="crash")
        with self.assertRaises(ConflictError):
            self.store.bind(event, workflow)
        self.store.retry(event["event_id"], confirmed=True)
        path.unlink()
        with self.assertRaises(ConflictError):
            self.store.bind(event, workflow)


if __name__ == "__main__":
    unittest.main()
