import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.inbox import Inbox
from oicoach_runtime.notifications import Watcher, configure
from oicoach_runtime.store import Store
from oicoach_runtime.wakebinding import WakeBindingError, WakeBindings
from oicoach_runtime.workflow_setup import create


FAKE_MCP = r'''#!/usr/bin/env python3
import hashlib, json, os, sys

def descriptor(value):
    if isinstance(value, str):
        text, encoding = value, "text"
    else:
        text, encoding = json.dumps(value, ensure_ascii=False, separators=(",", ":")), "json"
    encoded = text.encode()
    result = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(), "encoding": encoding}
    if len(encoded) <= 128:
        result["value"] = value
    return result

def response(identity, result):
    print(json.dumps({"jsonrpc": "2.0", "id": identity, "result": result}), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    identity = request.get("id")
    if identity is None:
        continue
    method = request.get("method")
    if method == "initialize":
        response(identity, {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-tijian", "version": "1"}})
        continue
    if method != "tools/call":
        response(identity, {"tools": []})
        continue
    args = request["params"]["arguments"]
    state = json.load(open(os.environ["FAKE_TODO_STATE"]))
    if state.get("unavailable"):
        response(identity, {"isError": True, "content": [], "structuredContent": {
            "ok": False, "error": {"code": "REMOTE_UNAVAILABLE", "message": "fixture unavailable"}}})
        continue
    todo = state["todo"]
    if request["params"]["name"] != "tijian_todo_get":
        response(identity, {"isError": True, "content": [], "structuredContent": {
            "ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "unknown tool"}}})
        continue
    if args.get("view") == "metadata":
        data = {"todoId": todo["id"], "version": todo["version"], "createdAt": todo["createdAt"],
                "updatedAt": todo["updatedAt"],
                "fields": {key: descriptor(value) for key, value in todo["fields"].items()},
                "relatedLabel": descriptor(todo.get("relatedLabel")),
                "relatedMissing": False, "evaluation": todo["evaluation"],
                "evidenceTotal": 0, "evidenceTruncated": False}
    elif args.get("view") == "field":
        value = todo["fields"].get(args["field"])
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        maximum = args.get("max_bytes", 32768)
        content = text[:maximum]
        data = {"todoId": todo["id"], "eventId": None, "version": todo["version"],
                "field": args["field"], "encoding": "text" if isinstance(value, str) else "json",
                "sha256": "", "offsetBytes": 0, "nextOffsetBytes": len(content),
                "totalBytes": len(text), "content": content, "nextCursor": None,
                "hasMore": len(content) < len(text)}
    else:
        data = {"detail": {"todo": todo}, "evidenceTotal": 0, "evidenceTruncated": False}
    response(identity, {"isError": False, "content": [], "structuredContent": {
        "ok": True, "data": data, "warnings": [], "revision": None}})
'''


class WakeBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-binding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        create(self.config, "example", self.workspace)
        self.fake = self.root / "fake_tijian_mcp.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_MCP)
        self.fake.chmod(0o755)
        omp = self.workspace / ".omp"
        omp.mkdir(mode=0o700)
        (omp / "mcp.json").write_text(json.dumps({"mcpServers": {"tijian": {
            "command": str(self.fake), "args": ["--write-policy=none"]}}}))
        (omp / "mcp.json").chmod(0o600)
        self.state_path = self.root / "todo-state.json"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.environment = patch.dict(os.environ, {"FAKE_TODO_STATE": str(self.state_path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.write_todo()
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.service = WakeBindings(self.store)

    def write_todo(self, **changes):
        due = self.now + timedelta(hours=1)
        state = {"todo": {
            "id": "todo-1", "version": changes.pop("version", 1),
            "createdAt": int(self.now.timestamp() * 1000),
            "updatedAt": int(changes.pop("updated_at", due).timestamp() * 1000),
            "fields": {
                "related": None, "operation": None,
                "description": changes.pop("description", "Finish the fixture task"),
                "scheduledFor": changes.pop("scheduledFor", {"kind": "at", "value": int(due.timestamp() * 1000)}),
                "dueAt": changes.pop("dueAt", int(due.timestamp() * 1000)),
                "creditFrom": None, "decision": changes.pop("decision", "open"),
                "estimateMinutes": None, "noteMd": "", "sortKey": 0,
            },
            "evaluation": {"automaticAchieved": changes.pop("automaticAchieved", False),
                           "achievedAt": None, "onTime": None, "progress": None,
                           "manualDone": changes.pop("manualDone", False), "manualDoneAt": None},
            "relatedLabel": None,
        }}
        if "due" in changes:
            due = changes.pop("due")
            state["todo"]["fields"]["dueAt"] = int(due.timestamp() * 1000)
            state["todo"]["fields"]["scheduledFor"] = {"kind": "at", "value": int(due.timestamp() * 1000)}
        state.update(changes)
        self.state_path.write_text(json.dumps(state))
        return due

    async def create(self, **changes):
        parameters = {"workflow_id": "example", "task_id": "training", "todo_id": "todo-1",
                      "mode": "follow", "time_source": "due", "eligibility": "open_and_incomplete",
                      "authorized_by_user": True}
        parameters.update(changes)
        return await self.service.create(**parameters)

    async def test_follow_creation_is_idempotent_and_schedules_offset(self):
        due = self.now + timedelta(hours=2)
        self.write_todo(due=due)
        result = await self.create(offset_seconds=-300)
        self.assertTrue(result["created"])
        binding = self.store.wakebinding(result["binding"]["binding_id"])
        event = self.store.event(binding["event_id"])
        self.assertEqual(event["not_before_utc"], (due - timedelta(minutes=5)).isoformat(timespec="microseconds").replace("+00:00", "Z"))
        self.assertEqual(event["payload"]["wake_binding_id"], binding["binding_id"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        repeated = await self.create(offset_seconds=-300)
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["binding"]["binding_id"], binding["binding_id"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        with self.assertRaises(ConflictError):
            await self.create(offset_seconds=-600)

    async def test_rename_is_rejected_and_original_name_stays_idempotent(self):
        first = await self.create(name="morning")
        binding_id = first["binding"]["binding_id"]
        with self.assertRaises(RuntimeContractError):
            await self.service.update(binding_id, first["binding"]["version"],
                                      workflow_id="example", task_id="training", name="evening")
        current = self.store.wakebinding(binding_id)
        self.assertEqual(current["name"], "morning")
        self.assertEqual(current["event_id"], first["binding"]["event_id"])
        repeated = await self.create(name="morning")
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["binding"]["binding_id"], binding_id)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM wake_bindings").fetchone()[0], 1)

    async def test_distinct_names_create_distinct_bindings_without_primary_key_errors(self):
        first = await self.create(name="morning")
        second = await self.create(name="evening")
        self.assertTrue(second["created"])
        self.assertNotEqual(first["binding"]["binding_id"], second["binding"]["binding_id"])
        self.assertNotEqual(first["binding"]["event_id"], second["binding"]["event_id"])
        listed = await self.service.list("example", "training")
        self.assertEqual({row["name"] for row in listed}, {"morning", "evening"})
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM events").fetchone()[0], 2)

    async def test_todo_time_change_reschedules_the_same_event(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        event_id = binding["event_id"]
        later = self.now + timedelta(hours=4)
        self.write_todo(due=later, version=2)
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        current = self.store.wakebinding(binding["binding_id"])
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["event_id"], event_id)
        self.assertEqual(self.store.event(event_id)["not_before_utc"],
                         later.isoformat(timespec="microseconds").replace("+00:00", "Z"))
        self.assertEqual(synced["bindings"][0]["todo_version"], 2)

    async def test_completed_todo_cancels_event_and_gate_blocks(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        event = self.store.event(binding["event_id"])
        configure(self.config, True)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        self.write_todo(decision="done", version=2)
        self.now += timedelta(hours=2)
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "ineligible")
        self.assertEqual(self.store.event(event["event_id"])["status"], "cancelled")
        self.assertFalse(await self.service.gate(self.store.event(event["event_id"])))
        await Watcher(self.store, sender).tick()
        sender.assert_not_awaited()
        self.assertIsNone(self.store.claim_due("worker"))

    async def test_unavailable_source_defers_without_delivery_and_recovers(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        event = self.store.event(binding["event_id"])
        self.write_todo(unavailable=True)
        self.now += timedelta(hours=2)
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "pending")
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")
        self.assertFalse(await self.service.gate(self.store.event(event["event_id"])))
        self.assertIsNone(self.store.claim_due("worker"))
        configure(self.config, True)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        await Watcher(self.store, sender).tick()
        sender.assert_not_awaited()
        self.write_todo()
        recovered = await self.service.sync(workflow_id="example", task_id="training",
                                            binding_id=binding["binding_id"])
        self.assertEqual(recovered["bindings"][0]["status"], "active")
        self.now += timedelta(hours=2, seconds=16)
        delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.store.event(event["event_id"])["status"], "succeeded")
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        self.assertIsNone(self.store.claim_due("worker"))
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    async def test_source_change_conflicts_instead_of_retargeting_todo(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        with patch.dict(os.environ, {"TIJIAN_RUNTIME_MODE": "harness"}):
            synced = await self.service.sync(workflow_id="example", task_id="training",
                                             binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "conflict")
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["last_error_code"],
                         "todo_source_mismatch")
        self.assertEqual(self.store.event(binding["event_id"])["status"], "cancelled")

    async def test_user_cancel_is_never_recreated(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        cancelled = await self.service.cancel(binding["binding_id"], binding["version"],
                                              workflow_id="example", task_id="training")
        self.assertEqual(cancelled["status"], "disabled")
        self.assertEqual(self.store.event(binding["event_id"])["status"], "cancelled")
        repeated = await self.service.cancel(binding["binding_id"], binding["version"],
                                             workflow_id="example", task_id="training")
        self.assertEqual(repeated["status"], "disabled")
        self.write_todo(due=self.now + timedelta(hours=5), version=3)
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "disabled")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    async def test_cancelling_the_reminder_event_disables_the_binding(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        event = self.store.event(binding["event_id"])
        self.store.modify(event["event_id"], event["version"], cancel=True)
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "disabled")
        await self.service.sync(workflow_id="example", task_id="training")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    async def test_notification_gate_reschedules_before_desktop_delivery(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        configure(self.config, True)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        self.write_todo(due=self.now + timedelta(hours=3), version=2)
        self.now += timedelta(hours=1, seconds=1)
        await Watcher(self.store, sender).tick()
        sender.assert_not_awaited()
        self.assertEqual(self.store.event(binding["event_id"])["status"], "queued")
        self.assertGreater(self.store.event(binding["event_id"])["not_before_utc"], self.store.timestamp())
        self.assertEqual(Inbox(self.store).list("example")["messages"], [])

    async def test_desktop_delivery_is_once_after_gate(self):
        first = await self.create()
        configure(self.config, True)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        self.now += timedelta(hours=1)
        watcher = Watcher(self.store, sender)
        await watcher.tick()
        await watcher.tick()
        self.assertEqual(sender.await_count, 1)
        messages = Inbox(self.store).list("example")["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIn("Finish the fixture task", messages[0]["text"])

    async def test_independent_binding_does_not_follow_todo_time(self):
        reminder = self.now + timedelta(minutes=30)
        first = await self.create(mode="independent", offset_seconds=0,
                                  reminder_at=reminder.isoformat())
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.write_todo(due=self.now + timedelta(hours=6), version=4)
        await self.service.sync(workflow_id="example", task_id="training",
                                binding_id=binding["binding_id"])
        self.assertEqual(self.store.event(binding["event_id"])["not_before_utc"],
                         reminder.isoformat(timespec="microseconds").replace("+00:00", "Z"))

    async def test_missing_time_and_long_description_are_bounded(self):
        long_text = "x" * 2000
        self.write_todo(description=long_text, scheduledFor={"kind": "day", "value": "2026-11-13"}, dueAt=None)
        with self.assertRaises(WakeBindingError) as failure:
            await self.create(time_source="scheduled")
        self.assertEqual(failure.exception.code, "todo_time_missing")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.write_todo(description=long_text, due=self.now + timedelta(hours=1))
        result = await self.create()
        self.assertTrue(result["todo"]["description_truncated"])
        self.assertLessEqual(len(self.store.wakebinding(result["binding"]["binding_id"])["todo_title"]), 512)

    async def test_missing_time_after_creation_defers_then_repairs(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.write_todo(scheduledFor={"kind": "day", "value": "2026-11-13"}, dueAt=None, version=2)
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "pending")
        self.assertEqual(synced["bindings"][0]["last_error_code"], "todo_time_missing")
        self.assertEqual(self.store.event(binding["event_id"])["status"], "cancelled")
        self.write_todo(due=self.now + timedelta(hours=2), version=3)
        self.now += timedelta(seconds=20)
        await self.service.reconcile_all(limit=10, reason="periodic")
        repaired = self.store.wakebinding(binding["binding_id"])
        self.assertEqual(repaired["status"], "active")
        self.assertNotEqual(repaired["event_id"], binding["event_id"])

    def native_owner(self, workflow_id):
        frontend = Frontend(self.store)
        lease = frontend.begin(workflow_id, "training", self.workspace)
        directory = Path(lease["session_dir"])
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        owner = {key: lease[key] for key in ("conversation_id", "generation", "owner_token")}
        frontend.started(**owner, guardian_pid=os.getpid())
        frontend.attest(**owner, omp_pid=child.pid, session_id="native",
                        session_file=str(directory / "native.jsonl"))
        return owner

    async def test_native_owner_cannot_operate_another_workflow_binding(self):
        from oicoach_runtime.scheduler import Runtime
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        create(self.config, "other", self.workspace)
        runtime = Runtime(self.config)
        runtime.store = self.store
        owner = self.native_owner("other")
        with self.assertRaises(ConflictError):
            await runtime.dispatch_async("wakebinding.show", {
                **owner, "parameters": {"binding_id": binding["binding_id"]}})
        own = self.native_owner("example")
        shown = await runtime.dispatch_async("wakebinding.show", {
            **own, "parameters": {"binding_id": binding["binding_id"]}})
        self.assertEqual(shown["binding"]["binding_id"], binding["binding_id"])

    async def test_missing_bound_event_is_repaired_without_duplicates(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.store.connection.execute("PRAGMA foreign_keys=OFF")
        self.store.connection.execute("DELETE FROM events WHERE event_id=?", (binding["event_id"],))
        self.store.connection.execute("PRAGMA foreign_keys=ON")
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        repaired = self.store.wakebinding(binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "active")
        self.assertNotEqual(repaired["event_id"], binding["event_id"])
        self.assertEqual(repaired["schedule_generation"], 2)
        again = await self.service.sync(workflow_id="example", task_id="training",
                                        binding_id=binding["binding_id"])
        self.assertEqual(again["bindings"][0]["event_id"], repaired["event_id"])

    async def test_scope_and_authorization_are_enforced(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        with self.assertRaises(ConflictError):
            await self.service.show(binding["binding_id"], "another", "training")
        with self.assertRaises(ConflictError):
            await self.service.cancel(binding["binding_id"], binding["version"],
                                      workflow_id="another", task_id="training")
        with self.assertRaises(RuntimeContractError):
            await self.service.create(workflow_id="example", task_id="training", todo_id="todo-1",
                                      authorized_by_user=False)
        self.assertEqual(await self.service.list("another", "training"), [])

    async def test_reopen_requires_policy_or_explicit_sync(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.write_todo(decision="done", version=2)
        await self.service.sync(workflow_id="example", task_id="training",
                                binding_id=binding["binding_id"])
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "ineligible")
        self.write_todo(decision="open", version=3)
        await self.service.reconcile_binding(binding["binding_id"], reason="periodic", reactivate=False)
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "ineligible")
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "active")
        self.assertNotEqual(synced["bindings"][0]["event_id"], binding["event_id"])

    async def test_when_eligible_policy_auto_reopens_after_backoff(self):
        first = await self.create(resume_policy="when_eligible")
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.write_todo(decision="done", version=2)
        await self.service.sync(workflow_id="example", task_id="training",
                                binding_id=binding["binding_id"])
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "ineligible")
        self.write_todo(decision="open", version=3)
        self.now += timedelta(seconds=20)
        await self.service.reconcile_all(limit=10, reason="periodic")
        reopened = self.store.wakebinding(binding["binding_id"])
        self.assertEqual(reopened["status"], "active")
        self.assertNotEqual(reopened["event_id"], binding["event_id"])

    async def test_ineligible_todo_is_never_claimed_or_projected(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.now += timedelta(hours=1)
        self.assertIsNone(self.store.claim_due("worker"))
        self.write_todo(decision="done", version=2)
        delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(delivered, [])
        self.assertEqual(Inbox(self.store).list("example")["messages"], [])
        self.assertEqual(self.store.event(binding["event_id"])["status"], "cancelled")
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "ineligible")
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM runs WHERE status IN ('leased','running')").fetchone()[0], 0)
        self.assertIsNone(self.store.claim_due("worker"))

    async def test_projection_conflict_is_isolated_and_backed_off(self):
        await self.create()
        await self.create(name="second")
        self.now += timedelta(hours=1)
        calls = {"n": 0}
        original = self.service.gate

        async def flaky(event):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConflictError("binding changed concurrently")
            return await original(event)

        with patch.object(self.service, "gate", new=flaky):
            delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        deferred = [row for row in self.store.wakebindings("example") if row["status"] == "pending"]
        self.assertEqual(len(deferred), 1)
        self.assertIsNone(self.store.claim_due("worker"))
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        # The conflicted binding is in backoff; an immediate retry is bounded.
        self.assertEqual(await Inbox(self.store).project_bound_due(self.service), [])

    async def test_projection_task_failure_does_not_escape_the_scheduler(self):
        from oicoach_runtime.scheduler import Runtime
        runtime = Runtime(self.config)
        runtime.store = self.store
        with patch("oicoach_runtime.inbox.Inbox.project_bound_due",
                   new=AsyncMock(side_effect=ConflictError("race"))):
            await runtime._project_bound_reminders(self.service)

    async def test_runtime_cli_route_creates_binding(self):
        from oicoach_runtime.scheduler import Runtime
        runtime = Runtime(self.config)
        runtime.store = self.store
        result = await runtime.dispatch_async("wakebinding.create", {
            "workflow_id": "example", "task_id": "training", "todo_id": "todo-1",
            "mode": "follow", "time_source": "due", "eligibility": "open_and_incomplete"})
        self.assertTrue(result["created"])
        listed = await runtime.dispatch_async("wakebinding.list", {
            "workflow_id": "example", "task_id": "training"})
        self.assertEqual(len(listed["bindings"]), 1)

    async def test_due_guard_reconciles_before_claiming(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.now += timedelta(hours=2)
        self.write_todo(due=self.now + timedelta(hours=3), version=2)
        blocked = await self.service.reconcile_due()
        self.assertEqual(blocked, set())
        self.assertIsNone(self.store.claim_due("worker"))
        self.assertGreater(self.store.event(binding["event_id"])["not_before_utc"], self.store.timestamp())
        self.now += timedelta(hours=4)
        self.write_todo(decision="done", version=3)
        blocked = await self.service.reconcile_due()
        self.assertIn(binding["event_id"], blocked)
        self.assertEqual(self.store.event(binding["event_id"])["status"], "cancelled")
        self.assertIsNone(self.store.claim_due("worker"))

    async def test_reconciler_missing_source_marks_pending_not_empty(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        omp = self.workspace / ".omp" / "mcp.json"
        omp.write_text(json.dumps({"mcpServers": {}}))
        synced = await self.service.sync(workflow_id="example", task_id="training",
                                         binding_id=binding["binding_id"])
        self.assertEqual(synced["bindings"][0]["status"], "pending")
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["last_error_code"],
                         "todo_source_missing")
        self.assertEqual(self.store.event(binding["event_id"])["status"], "queued")
