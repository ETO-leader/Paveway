"""Deterministic lifecycle race tests with explicit scheduling barriers.

Every interleaving is driven by ``asyncio.Event`` or a patched coroutine, never
by guessing how long an operation takes.
"""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import __version__
from oicoach_runtime.cli import initialize
from oicoach_runtime.compatibility import (EXPECTED_LIFECYCLE_PROTOCOL_VERSION,
                                           EXPECTED_NATIVE_PROTOCOL_VERSION)
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError
from oicoach_runtime.models import execution_scope
from oicoach_runtime.scheduler import Runtime
from oicoach_runtime.store import Store
from oicoach_runtime.workflow_setup import create


class LifecycleStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-lifecycle-state-")
        self.root = Path(self.temporary.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(path)
        self.config = replace(config, server=replace(config.server, poll_interval_seconds=0.01,
                                                     max_concurrent_runs=2))
        self.store = Store(self.config)
        self.runtime = Runtime(self.config, path)
        self.runtime.store = self.store
        self.tasks = []
        self.addCleanup(self.cleanup)

    async def cleanup(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.temporary.cleanup()

    def target(self, reason="test lifecycle"):
        return {"runtime_instance": self.runtime.owner,
                "runtime_build_id": self.runtime.runtime_build_id,
                "runtime_version": __version__,
                "native_protocol_version": EXPECTED_NATIVE_PROTOCOL_VERSION,
                "runtime_pid": self.runtime.runtime_pid,
                "runtime_identity": self.runtime.runtime_identity,
                "lifecycle_protocol_version": EXPECTED_LIFECYCLE_PROTOCOL_VERSION,
                "reason": reason}

    def claim(self, task="alpha", key="one", *, dispatch=False):
        self.store.enqueue("echo", task, "fixture", key, payload={}, at="now")
        event = self.store.claim_due("runtime")
        if dispatch:
            self.store.dispatch(event["run_id"])
        return event

    def register(self, event, *, task=None, state="starting"):
        task = task or event["task_id"]
        entry = {"event_id": event["event_id"], "run_id": event["run_id"],
                 "scope": execution_scope("echo", task), "workflow_id": "echo",
                 "task_id": task, "state": state, "progress": None,
                 "dispatch_intent_at": None}
        self.runtime.active_runs[event["run_id"]] = entry

        async def hold():
            await asyncio.Event().wait()

        entry["task"] = asyncio.create_task(hold())
        self.tasks.append(entry["task"])
        self.runtime._refresh_health()
        return entry

    async def test_resume_waits_for_the_drain_critical_section(self):
        event = self.claim()
        self.register(event)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.runtime._stop_run

        async def controlled(run_id, timeout=10):
            entered.set()
            await release.wait()
            return await original(run_id, timeout)

        self.runtime._stop_run = controlled
        drain = asyncio.create_task(self.runtime.quiesce(self.target()))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(self.runtime.lifecycle_state, "draining")
        resume = asyncio.create_task(
            self.runtime.lifecycle_api("lifecycle.resume", self.target()))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(resume.done(), "resume interleaved with the drain transition")
        self.assertTrue(self.runtime.lifecycle_lock.locked())
        release.set()
        self.assertTrue((await asyncio.wait_for(drain, 5))["accepted"])
        self.assertTrue((await asyncio.wait_for(resume, 5))["resumed"])
        self.assertEqual(self.runtime.lifecycle_state, "running")
        self.assertFalse(self.runtime.draining)
        # The pre-dispatch Run was returned to the queue, not failed or replayed.
        self.assertEqual(self.store.run(event["run_id"])["status"], "blocked")
        self.assertEqual(self.store.run(event["run_id"])["error_code"], "runtime_drain")
        requeued = self.store.event(event["event_id"])
        self.assertEqual(requeued["status"], "queued")
        self.assertEqual(requeued["attempt"], 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM runs").fetchone()[0], 1)

    async def test_installation_handoff_does_not_charge_undispatched_leases(self):
        configured = self.root / "omp"
        configured.write_text("#!/bin/sh\nexit 0\n")
        configured.chmod(0o700)
        config = replace(self.config, agent=replace(self.config.agent, executable=str(configured)))
        self.runtime = Runtime(config)
        self.runtime.store = self.store
        create(config, "echo", self.root, "example")
        claimed = self.claim()
        entered = asyncio.Event()

        async def worker_start(*_):
            entered.set()
            await asyncio.Event().wait()

        with patch("oicoach_runtime.adapters.omp_rpc.OmpRpc.start", new=worker_start):
            self.tasks.append(self.runtime._spawn(claimed)["task"])
            await asyncio.wait_for(entered.wait(), 2)
            not_started = self.claim(task="beta", key="two")
            replacement = self.root / "replacement"
            replacement.write_text("#!/bin/sh\nexit 42\n")
            replacement.chmod(0o700)
            replacement.replace(configured)
            self.assertTrue((await self.runtime.quiesce(self.target()))["accepted"])
        for event in (claimed, not_started):
            run = self.store.run(event["run_id"])
            self.assertIsNone(run["dispatch_intent_at"])
            self.assertEqual(run["error_code"], "omp_installation_unavailable")
            retained = self.store.event(event["event_id"])
            self.assertEqual((retained["status"], retained["attempt"]), ("queued", 0))

    async def test_accepted_shutdown_refuses_resume_and_keeps_one_timer(self):
        first = await self.runtime.lifecycle_api("lifecycle.shutdown", self.target())
        self.assertTrue(first["accepted"])
        self.assertTrue(first["stopping"])
        timer = self.runtime.stop_timer
        self.assertIsNotNone(timer)
        second = await self.runtime.lifecycle_api("lifecycle.shutdown", self.target())
        self.assertTrue(second["accepted"])
        self.assertTrue(second["already_draining"])
        self.assertIs(self.runtime.stop_timer, timer, "a competing stop timer was created")
        with self.assertRaises(ConflictError):
            await self.runtime.lifecycle_api("lifecycle.resume", self.target())
        self.assertTrue(self.runtime.stopping)
        await asyncio.wait_for(self.runtime.stop_event.wait(), 2)

    async def test_concurrent_shutdowns_serialize(self):
        results = await asyncio.gather(
            self.runtime.lifecycle_api("lifecycle.shutdown", self.target()),
            self.runtime.lifecycle_api("lifecycle.shutdown", self.target()))
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual(sum(1 for result in results if result["already_draining"]), 1)
        self.assertIsNotNone(self.runtime.stop_timer)
        self.assertEqual(self.runtime.lifecycle_state, "stopping")

    async def test_dispatched_run_blocks_drain_and_pre_dispatch_run_is_preserved(self):
        dispatched = self.claim(dispatch=True)
        self.register(dispatched, state="running")
        leased = self.claim(task="beta", key="two")
        self.register(leased, task="beta")
        result = await self.runtime.quiesce(self.target())
        self.assertFalse(result["accepted"])
        blockers = [item for item in result["blockers"] if item["kind"] == "background_run"]
        self.assertEqual([item["run_id"] for item in blockers], [dispatched["run_id"]])
        self.assertTrue(blockers[0]["dispatch_intent_at"])
        self.assertEqual(self.store.run(dispatched["run_id"])["status"], "running")
        self.assertEqual(self.store.run(leased["run_id"])["status"], "leased",
                         "a safe pre-dispatch Run was cancelled despite a blocker")
        self.assertEqual(self.runtime.lifecycle_state, "running")
        # Once the dispatched Run settles, a later drain is accepted.
        self.store.finish(dispatched["run_id"], "uncertain", error="fixture_settled")
        # The simulated worker task is still holding; stop it before reaping.
        self.runtime.active_runs[dispatched["run_id"]]["task"].cancel()
        await self.runtime._reap([dispatched["run_id"]])
        accepted = await self.runtime.quiesce(self.target())
        self.assertTrue(accepted["accepted"], accepted)
        self.assertEqual(self.store.run(leased["run_id"])["status"], "blocked")
        self.assertEqual(self.store.event(leased["event_id"])["status"], "queued")

    async def test_shutdown_is_refused_while_a_dispatched_run_exists(self):
        dispatched = self.claim(dispatch=True)
        self.register(dispatched, state="running")
        leased = self.claim(task="beta", key="two")
        self.register(leased, task="beta")
        result = await self.runtime.lifecycle_api("lifecycle.shutdown", self.target())
        self.assertFalse(result["accepted"])
        self.assertIsNone(self.runtime.stop_timer, "a stop timer was armed despite a blocker")
        self.assertFalse(self.runtime.stop_event.is_set())
        self.assertEqual(self.runtime.lifecycle_state, "running")
        self.assertEqual(self.store.run(dispatched["run_id"])["status"], "running")
        self.assertEqual(self.store.run(leased["run_id"])["status"], "leased")
        self.assertEqual(self.store.event(leased["event_id"])["status"], "leased")
        self.assertEqual([item["run_id"] for item in result["blockers"]
                          if item["kind"] == "background_run"], [dispatched["run_id"]])

    async def test_failed_drain_restores_running_and_keeps_scheduling(self):
        event = self.claim()
        self.register(event)

        async def timeout(_run_id, timeout=10):
            return False

        self.runtime._stop_run = timeout
        result = await self.runtime.quiesce(self.target())
        self.assertFalse(result["accepted"])
        self.assertEqual(self.runtime.lifecycle_state, "running")
        self.assertFalse(self.runtime.draining)
        # The simulated timeout left the task running; finish it explicitly.
        self.runtime.active_runs[event["run_id"]]["task"].cancel()
        await self.runtime._reap([event["run_id"]])
        self.store.abort_claim(event["run_id"], "fixture_release", keep_event=False)
        claimed = []

        async def dispatch(claimed_event, entry=None):
            claimed.append(claimed_event["event_id"])

        self.runtime.dispatch_event = dispatch
        fresh = self.store.enqueue("echo", "gamma", "fixture", "fresh", payload={}, at="now")
        scheduler = asyncio.create_task(self.runtime.schedule_loop())
        self.tasks.append(scheduler)
        deadline = asyncio.get_running_loop().time() + 5
        while not claimed and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(claimed, [fresh["event_id"]])
        self.assertNotEqual(self.store.event(fresh["event_id"])["run_id"], event["run_id"])

    async def test_new_claims_are_blocked_while_draining(self):
        self.assertTrue((await self.runtime.quiesce(self.target()))["accepted"])
        event = self.store.enqueue("echo", "alpha", "fixture", "drain", payload={}, at="now")
        observed = []
        original = self.store.claim_due

        def spy(owner, exclude=()):
            observed.append(self.runtime.draining)
            return original(owner, exclude)

        self.store.claim_due = spy
        dispatched = []

        async def dispatch(claimed_event, entry=None):
            dispatched.append(claimed_event["event_id"])

        self.runtime.dispatch_event = dispatch
        scheduler = asyncio.create_task(self.runtime.schedule_loop())
        self.tasks.append(scheduler)
        for _ in range(30):
            self.runtime.changed.set()
            await asyncio.sleep(0.01)
        self.assertEqual(dispatched, [])
        self.assertEqual(self.store.event(event["event_id"])["run_id"], None)
        # No claim was even attempted while the drain was in force.
        self.assertEqual(observed, [])
        self.assertTrue((await self.runtime.lifecycle_api(
            "lifecycle.resume", self.target()))["resumed"])
        deadline = asyncio.get_running_loop().time() + 5
        while not dispatched and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(dispatched, [event["event_id"]])
        self.assertTrue(observed and all(value is False for value in observed))

    async def test_resume_rejects_a_stale_runtime_identity(self):
        self.assertTrue((await self.runtime.quiesce(self.target()))["accepted"])
        stale = self.target()
        stale["runtime_build_id"] = "0" * 64
        with self.assertRaises(ConflictError):
            await self.runtime.lifecycle_api("lifecycle.resume", stale)
        self.assertEqual(self.runtime.lifecycle_state, "draining")

    async def test_quarantined_scope_blocks_drain_and_run_cancellation(self):
        event = self.claim()  # leased without dispatch intent: normally cancellable
        entry = self.register(event)
        self.runtime.unreleased_scopes[entry["scope"]] = "agent_cleanup_unproven"
        active = {item["run_id"]: item for item in self.runtime._active_runs()}
        self.assertTrue(active[event["run_id"]]["cleanup_unproven"])
        self.assertFalse(active[event["run_id"]]["safe_to_abort"])
        result = await self.runtime.quiesce(self.target())
        self.assertFalse(result["accepted"])
        blockers = [item for item in result["blockers"] if item["kind"] == "background_run"]
        self.assertEqual([item["run_id"] for item in blockers], [event["run_id"]])
        self.assertTrue(blockers[0]["cleanup_unproven"])
        # The unproven process is never cancelled, released or reused.
        self.assertFalse(await self.runtime._stop_run(event["run_id"]))
        self.assertFalse(entry["task"].done())
        self.assertEqual(self.store.run(event["run_id"])["status"], "leased")
        self.assertEqual(self.runtime.lifecycle_state, "running")
        # Once the stop is proven, the ordinary drain semantics return.
        self.runtime.unreleased_scopes.clear()
        accepted = await self.runtime.quiesce(self.target())
        self.assertTrue(accepted["accepted"], accepted)
        self.assertEqual(self.store.run(event["run_id"])["status"], "blocked")
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")
        self.assertEqual(self.store.event(event["event_id"])["attempt"], 1)

    async def test_repeated_drain_is_idempotent(self):
        event = self.claim()
        self.register(event)
        results = await asyncio.gather(self.runtime.quiesce(self.target()),
                                       self.runtime.quiesce(self.target()))
        self.assertTrue(all(result["accepted"] for result in results))
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM runs WHERE status='blocked'").fetchone()[0], 1)
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
