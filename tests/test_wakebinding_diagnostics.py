"""Persistent, privacy-safe WakeBinding failure diagnostics."""

import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError
from oicoach_runtime.inbox import Inbox
from oicoach_runtime.scheduler import Runtime
from oicoach_runtime.store import Store
from oicoach_runtime.wakebinding import (ERROR_MESSAGES, TodoFacts, TodoSourceInvalid,
                                         TodoUnavailable, WakeBindings)
from oicoach_runtime.workflow_setup import create

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def facts(version=1, *, todo_id="todo-1", due=None, decision="open", description="fixture task"):
    due = due or (NOW + timedelta(hours=1))
    return TodoFacts(todo_id=todo_id, version=version, updated_at=due, decision=decision,
                     scheduled_kind="at", scheduled_value=int(due.timestamp() * 1000),
                     scheduled_at=due, due_at=due, credit_from=None, manual_done=False,
                     automatic_achieved=False, achieved_at=None, description=description,
                     description_truncated=False, related_label=None)


class Source:
    """A controllable stand-in for the stdio Tijian client."""

    def __init__(self, behaviour, calls):
        self.behaviour = behaviour
        self.calls = calls

    def get(self, todo_id):
        self.calls.append(todo_id)
        return self.behaviour(todo_id)

    def close(self):
        pass


class WakeBindingDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-binding-diag-")
        self.root = Path(self.temporary.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        workspace = self.root / "workspace"
        workspace.mkdir(mode=0o700)
        create(self.config, "example", workspace)
        fake = self.root / "tijian-mcp"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        omp = workspace / ".omp"
        omp.mkdir(mode=0o700)
        (omp / "mcp.json").write_text(json.dumps({"mcpServers": {"tijian": {
            "command": str(fake), "args": ["--write-policy=none"]}}}))
        (omp / "mcp.json").chmod(0o600)
        self.now = NOW
        self.store = Store(self.config, clock=lambda: self.now)
        self.calls = []

    async def asyncTearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def service(self, behaviour):
        return WakeBindings(self.store, source_factory=lambda *_: Source(behaviour, self.calls))

    def create_binding(self, name="default", *, todo_id="todo-1", resume_policy="manual", due=None):
        service = self.service(lambda _todo: facts())
        source, source_id, label = service._source("example")
        source.close()
        due = due or (NOW + timedelta(hours=1))
        binding_id = (name.encode().hex() * 64)[:64]
        binding, created = self.store.create_wakebinding(
            binding_id=binding_id, workflow_id="example", task_id="training",
            owner_conversation_id=None, todo_source_id=source_id, todo_source_label=label,
            todo_id=todo_id, name=name, mode="follow", time_source="due", offset_seconds=0,
            reminder_at_utc=None, eligibility="open", resume_policy=resume_policy,
            todo_version=1, todo_fingerprint=facts().fingerprint, todo_title="fixture task",
            due_at_utc=due.isoformat())
        self.assertTrue(created)
        self.created_stamp = self.store.wakebinding(binding_id)["last_reconciled_at"]
        # The periodic reconciler only revisits active bindings after its
        # observation window; advance the fixture clock past it.
        self.now += timedelta(seconds=61)
        return binding_id

    async def test_timeouts_are_persisted_queryable_and_back_off(self):
        binding_id = self.create_binding()
        created_stamp = self.created_stamp
        secret = "MCP exploded with PRIVATE_MCP_PAYLOAD"

        def timeout(_todo_id):
            raise TimeoutError(secret)

        service = self.service(timeout)
        await service.reconcile_all(limit=10, reason="periodic")
        binding = self.store.wakebinding(binding_id)
        self.assertEqual(binding["status"], "pending")
        self.assertEqual(binding["last_error_code"], "todo_unavailable")
        self.assertEqual(binding["last_error_message"], ERROR_MESSAGES["todo_unavailable"])
        self.assertEqual(binding["failure_count"], 1)
        # The failure did not advance the last successful reconcile stamp.
        self.assertEqual(binding["last_reconciled_at"], created_stamp)
        self.assertIsNotNone(binding["last_attempt_at"])
        self.assertIsNotNone(binding["last_failure_at"])
        self.assertGreater(binding["next_attempt_at"], binding["last_attempt_at"])
        diagnostics = service.diagnostics(binding)
        self.assertTrue(diagnostics["backing_off"])
        self.assertTrue(diagnostics["recoverable"])
        self.assertFalse(diagnostics["needs_user_action"])
        self.assertFalse(diagnostics["recovered"])
        self.assertEqual(diagnostics["failure_code"], "todo_unavailable")
        # The fixed sentence is stored; the exception text never is.
        self.assertNotIn("PRIVATE_MCP_PAYLOAD", json.dumps(binding))
        self.assertNotIn("PRIVATE_MCP_PAYLOAD", json.dumps(diagnostics))
        # A second sweep inside the backoff window does not call Tijian again.
        await service.reconcile_all(limit=10, reason="periodic")
        self.assertEqual(len(self.calls), 1)
        # The status APIs expose the same durable evidence.
        runtime = Runtime(self.config)
        runtime.store = self.store
        status = await runtime.dispatch_async(
            "wakebinding.status", {"workflow_id": "example", "task_id": "training"})
        row = next(item for item in status["bindings"] if item["binding_id"] == binding_id)
        self.assertEqual(row["diagnostics"]["failure_code"], "todo_unavailable")
        self.assertTrue(row["diagnostics"]["backing_off"])
        self.assertEqual(self.store.status()["wakebinding_diagnostics"]["failure_codes"],
                         {"todo_unavailable": 1})
        # The Runtime restart keeps the evidence: only the binding row matters.
        reopened = Store(self.config, clock=lambda: self.now)
        try:
            self.assertEqual(WakeBindings(reopened).diagnostics(
                reopened.wakebinding(binding_id))["failure_code"], "todo_unavailable")
        finally:
            reopened.close()

    async def test_backoff_grows_and_recovery_marks_the_failure_resolved(self):
        binding_id = self.create_binding()
        service = self.service(lambda _todo: (_ for _ in ()).throw(TimeoutError()))
        await service.reconcile_all(limit=10)
        first = self.store.wakebinding(binding_id)
        self.now += timedelta(seconds=16)
        await service.reconcile_all(limit=10)
        second = self.store.wakebinding(binding_id)
        self.assertEqual(second["failure_count"], 2)
        first_delay = (datetime.fromisoformat(first["next_attempt_at"].replace("Z", "+00:00"))
                       - datetime.fromisoformat(first["last_attempt_at"].replace("Z", "+00:00")))
        second_delay = (datetime.fromisoformat(second["next_attempt_at"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(second["last_attempt_at"].replace("Z", "+00:00")))
        self.assertGreater(second_delay, first_delay)
        self.now += timedelta(seconds=130)
        recovered_service = self.service(lambda todo_id: facts(todo_id=todo_id))
        await recovered_service.reconcile_all(limit=10)
        binding = self.store.wakebinding(binding_id)
        self.assertEqual(binding["status"], "active")
        self.assertIsNone(binding["last_error_code"])
        self.assertIsNone(binding["last_error_message"])
        self.assertIsNone(binding["next_attempt_at"])
        self.assertEqual(binding["failure_count"], 0)
        self.assertIsNotNone(binding["last_reconciled_at"])
        diagnostics = recovered_service.diagnostics(binding)
        self.assertTrue(diagnostics["recovered"])
        self.assertFalse(diagnostics["backing_off"])
        self.assertEqual(self.store.status()["wakebinding_diagnostics"]["recovered"], 1)

    async def test_one_failing_binding_does_not_block_another(self):
        failing = self.create_binding("failing", todo_id="todo-failing")
        healthy = self.create_binding("healthy", todo_id="todo-ok")

        def behaviour(todo_id):
            if todo_id == "todo-failing":
                raise TodoUnavailable()
            return facts(todo_id=todo_id)

        service = self.service(behaviour)
        results = await service.reconcile_all(limit=10)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.store.wakebinding(failing)["status"], "pending")
        self.assertEqual(self.store.wakebinding(failing)["last_error_code"], "todo_unavailable")
        self.assertEqual(self.store.wakebinding(healthy)["status"], "active")
        self.assertIsNone(self.store.wakebinding(healthy)["last_error_code"])
        # Reminder projection still delivers the healthy binding only.
        self.now += timedelta(hours=2)
        delivered = await Inbox(self.store).project_bound_due(service)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        self.assertEqual(self.store.event(self.store.wakebinding(failing)["event_id"])["status"],
                         "queued")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    async def test_internal_and_malformed_failures_keep_distinct_codes(self):
        binding_id = self.create_binding()
        secret = "INTERNAL_PRIVATE_DETAIL"
        service = self.service(lambda _todo: (_ for _ in ()).throw(RuntimeError(secret)))
        await service.reconcile_all(limit=10)
        binding = self.store.wakebinding(binding_id)
        self.assertEqual(binding["last_error_code"], "binding_internal_error")
        self.assertEqual(binding["last_error_message"], ERROR_MESSAGES["binding_internal_error"])
        self.assertNotIn(secret, json.dumps(binding))
        self.assertNotIn(secret, json.dumps(service.diagnostics(binding)))
        self.assertTrue(service.diagnostics(binding)["recoverable"])
        # Malformed protocol data is a business conflict that needs the user.
        malformed = self.service(lambda _todo: (_ for _ in ()).throw(TodoSourceInvalid()))
        self.now += timedelta(minutes=5)
        await malformed.reconcile_all(limit=10)
        current = self.store.wakebinding(binding_id)
        self.assertEqual(current["status"], "conflict")
        self.assertEqual(current["last_error_code"], "todo_source_invalid")
        self.assertTrue(malformed.diagnostics(current)["needs_user_action"])

    async def test_missing_source_is_not_disguised_as_todo_unavailable(self):
        binding_id = self.create_binding()
        from oicoach_runtime.wakebinding import WakeBindingError

        def missing(_todo_id):
            raise WakeBindingError("todo_source_missing")

        service = self.service(missing)
        await service.reconcile_all(limit=10)
        binding = self.store.wakebinding(binding_id)
        self.assertEqual(binding["last_error_code"], "todo_source_missing")
        self.assertEqual(binding["status"], "pending")
        self.assertTrue(service.diagnostics(binding)["needs_user_action"])

    async def test_disabled_binding_is_never_reactivated_or_overwritten(self):
        binding_id = self.create_binding()
        binding = self.store.wakebinding(binding_id)
        self.store.disable_wakebinding(binding_id, binding["version"])
        service = self.service(lambda _todo: (_ for _ in ()).throw(TimeoutError()))
        self.assertFalse(service.defer(binding_id, "todo_unavailable"))
        await service.reconcile_all(limit=10)
        await service.reconcile_binding(binding_id, reason="periodic")
        current = self.store.wakebinding(binding_id)
        self.assertEqual(current["status"], "disabled")
        self.assertEqual(current["last_error_code"], "cancelled_by_user")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.event(current["event_id"])["status"], "cancelled")

    async def test_stale_failure_never_overwrites_a_competing_success(self):
        binding_id = self.create_binding()
        service = self.service(lambda _todo: (_ for _ in ()).throw(TimeoutError()))
        await service.reconcile_all(limit=10)
        stale = self.store.wakebinding(binding_id)
        self.assertEqual(stale["status"], "pending")
        real = self.store.apply_wakebinding
        committed = {}

        def racing(identity, expected_version, **fields):
            if not committed:
                committed["binding"] = real(identity, expected_version, fields={
                    "status": "active", "last_error_code": None, "last_error_message": None})
                raise ConflictError("concurrent binding update")
            return real(identity, expected_version, **fields)

        self.store.apply_wakebinding = racing
        try:
            self.assertFalse(service.defer(binding_id, "binding_verify_conflict"))
        finally:
            del self.store.apply_wakebinding
        current = self.store.wakebinding(binding_id)
        self.assertEqual(current["status"], "active")
        self.assertIsNone(current["last_error_code"])

    async def test_failure_diagnostics_never_include_private_todo_content(self):
        long_description = "PRIVATE_TODO_BODY" + "x" * 3000
        binding_id = self.create_binding()

        def failing(_todo_id):
            raise TodoUnavailable(long_description)

        service = self.service(failing)
        await service.reconcile_all(limit=10)
        binding = self.store.wakebinding(binding_id)
        self.assertEqual(binding["last_error_message"], ERROR_MESSAGES["todo_unavailable"])
        self.assertNotIn("PRIVATE_TODO_BODY", json.dumps(service.diagnostics(binding)))
        self.assertNotIn("PRIVATE_TODO_BODY", json.dumps(self.store.status()["wakebinding_diagnostics"]))

    async def test_explicit_sync_still_reports_the_failure_to_the_caller(self):
        binding_id = self.create_binding()
        service = self.service(lambda _todo: (_ for _ in ()).throw(TodoUnavailable()))
        result = await service.sync(workflow_id="example", task_id="training",
                                    binding_id=binding_id)
        shown = result["bindings"][0]
        self.assertEqual(shown["status"], "pending")
        self.assertEqual(shown["last_error_code"], "todo_unavailable")
        self.assertEqual(shown["diagnostics"]["failure_code"], "todo_unavailable")
        self.assertEqual(self.store.wakebinding(binding_id)["last_error_code"], "todo_unavailable")


if __name__ == "__main__":
    unittest.main()
