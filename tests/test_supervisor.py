import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import RestartConfig, load_config
from oicoach_runtime.errors import ConflictError, StorageError
from oicoach_runtime.models import utc_text
from oicoach_runtime.store import Store
from oicoach_runtime.supervisor import RestartPolicy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.policy = RestartPolicy(RestartConfig(max_failures_per_window=3, backoff_initial_seconds=2,
                                                  backoff_max_seconds=8, cooldown_seconds=20, jitter_ratio=0), random_value=lambda: 0)

    def test_exponential_backoff_cap_open_and_failed_half_open_probe(self):
        state = self.policy.empty()
        for delay in (2, 4, 20):
            state = self.policy.failure(state, self.now, "startup_failure")
            self.assertFalse(self.policy.allowed(state, self.now))
            self.assertEqual(state["next_start_utc"], utc_text(self.now + timedelta(seconds=delay)))
            self.now += timedelta(seconds=delay)
        self.assertEqual(self.policy.describe(state, self.now)["phase"], "half_open")
        state = self.policy.failure(state, self.now, "probe_failure")
        self.assertEqual(self.policy.describe(state, self.now)["phase"], "open")
        self.assertEqual(state["next_start_utc"], utc_text(self.now + timedelta(seconds=20)))
        self.assertEqual(self.policy.describe(self.policy.empty(), self.now)["phase"], "closed")

    def test_jitter_is_bounded_and_old_failure_window_expires(self):
        policy = RestartPolicy(RestartConfig(jitter_ratio=0.2), random_value=lambda: 1)
        first = policy.failure(policy.empty(), self.now, "failure")
        self.assertEqual(first["next_start_utc"], utc_text(self.now + timedelta(seconds=2.4)))
        later = policy.failure(first, self.now + timedelta(seconds=301), "failure")
        self.assertEqual(later["consecutive_failures"], 1)
        self.assertEqual(len(later["failures"]), 1)

    def test_clock_rewind_preserves_gate_and_corrupt_state_fails_closed(self):
        state = self.policy.failure(self.policy.empty(), self.now, "failure")
        self.assertFalse(self.policy.allowed(state, self.now - timedelta(days=1)))
        for invalid in ({}, dict(state, version=2), dict(state, open_until_utc=state["next_start_utc"], next_start_utc=None)):
            with self.assertRaises(StorageError):
                self.policy.allowed(invalid, self.now)


class DurableSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        path = root / "config/config.toml"
        initialize(path, str(root / "state"), str(root / "workflows"))
        self.config = load_config(path)
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.policy = RestartPolicy(replace(self.config.restart, jitter_ratio=0), random_value=lambda: 0)
        self.store = Store(self.config, clock=lambda: self.now, policy=self.policy)
        self.addCleanup(self.store.close)

    def event(self, key="event"):
        return self.store.enqueue("echo", "task", "supervisor test", key, at=utc_text(self.now))

    def test_safe_retry_and_dead_letter_preserve_original_due_and_all_runs(self):
        event = self.event()
        for index in range(3):
            claimed = self.store.claim_due("daemon")
            self.assertIsNotNone(claimed)
            self.store.finish(claimed["run_id"], "failed", error="startup_failed", automatic_retry=True)
            self.assertEqual(self.store.event(event["event_id"])["not_before_utc"], event["not_before_utc"])
            self.assertIsNone(self.store.claim_due("daemon"))
            self.now += timedelta(seconds=10)
        self.assertEqual(self.store.event(event["event_id"])["status"], "dead_letter")
        self.assertEqual(len(self.store.runs()), 3)
        self.assertTrue(all(run["dispatch_intent_at"] is None for run in self.store.runs()))

    def test_retry_is_forbidden_after_intent_even_without_ack(self):
        event = self.event()
        claimed = self.store.claim_due("daemon")
        self.store.dispatch(claimed["run_id"])
        with self.assertRaises(ConflictError):
            self.store.finish(claimed["run_id"], "failed", error="lost_ack", automatic_retry=True)
        self.store.finish(claimed["run_id"], "uncertain", error="lost_ack", record_failure=True)
        self.now += timedelta(seconds=200)
        self.assertIsNone(self.store.claim_due("daemon"))
        self.assertEqual(self.store.event(event["event_id"])["attempt"], 1)

    def test_failure_policy_and_run_outcome_roll_back_together(self):
        event = self.event()
        claimed = self.store.claim_due("daemon")
        with patch.object(self.store, "audit", side_effect=RuntimeError("commit boundary")):
            with self.assertRaises(RuntimeError):
                self.store.finish(claimed["run_id"], "failed", error="startup_failed", automatic_retry=True)
        self.assertEqual(self.store.event(event["event_id"])["status"], "leased")
        self.assertEqual(self.store.supervisor_state(), self.policy.empty())

    def test_circuit_persists_across_reopen_and_respects_cancel_during_cooldown(self):
        config = replace(self.config, restart=replace(self.config.restart, max_failures_per_window=1, cooldown_seconds=60))
        other = Store(config, clock=lambda: self.now)
        try:
            first = self.event()
            claimed = other.claim_due("old-daemon")
            other.finish(claimed["run_id"], "failed", error="startup_failed", automatic_retry=True)
        finally:
            other.close()
        reopened = Store(config, clock=lambda: self.now)
        try:
            self.assertEqual(reopened.status()["supervisor"]["phase"], "open")
            self.assertIsNone(reopened.claim_due("new-daemon"))
            edited = reopened.event(first["event_id"])
            reopened.modify(first["event_id"], edited["version"], cancel=True)
            self.now += timedelta(seconds=61)
            self.assertIsNone(reopened.claim_due("new-daemon"))
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
