import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.inbox import Inbox
from oicoach_runtime.notifications import Watcher, configure
from oicoach_runtime.store import Store
from oicoach_runtime import wakebinding
from oicoach_runtime.wakebinding import (TijianTodoSource, TodoFacts,
                                         WakeBindingError, WakeBindings)
from oicoach_runtime.workflow_setup import create


FAKE_MCP = r'''#!/usr/bin/env python3
import hashlib, json, os, signal, sys, time

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

state = json.load(open(os.environ["FAKE_TODO_STATE"]))
if state.get("ignore_eof"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
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
        configure(self.config, True, now=self.now)
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
        self.assertEqual(Inbox(self.store).list("example")["messages"], [])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        configure(self.config, True, now=self.now)
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
        self.assertEqual(await Inbox(self.store).project_bound_due(self.service), [])
        reopened = Store(self.config, clock=lambda: self.now)
        try:
            self.assertEqual(await Inbox(reopened).project_bound_due(
                WakeBindings(reopened)), [])
            self.assertEqual(len(Inbox(reopened).list("example")["messages"]), 1)
        finally:
            reopened.close()

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

    @staticmethod
    def stop_source(source):
        process = getattr(source, "process", None)
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def facts(self, due):
        return TodoFacts("todo-1", 1, self.now, "open", "at",
                         int(due.timestamp() * 1000), due, due, None,
                         False, False, None, "fixture", False, None)

    async def test_cancelled_fetch_cannot_restart_a_closed_mcp(self):
        due = self.now + timedelta(hours=2)
        entered, release, close_started, finished = (
            threading.Event(), threading.Event(), threading.Event(), threading.Event())
        self.addCleanup(release.set)

        class GatedSource(TijianTodoSource):
            def _start(self):
                entered.set()
                release.wait(5)
                super()._start()

            def get(self, todo_id):
                try:
                    return super().get(todo_id)
                finally:
                    finished.set()

            def close(self):
                close_started.set()
                super().close()

        source = None
        def factory(server, workspace):
            nonlocal source
            source = GatedSource(server, workspace)
            return source

        service = WakeBindings(self.store, source_factory=factory)
        self.addCleanup(lambda: self.stop_source(source))
        task = asyncio.create_task(service.create(
            workflow_id="example", task_id="training", todo_id="todo-1",
            authorized_by_user=True, time_source="due"))
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(entered.is_set(), "MCP read thread never entered _start")
        task.cancel()
        deadline = time.monotonic() + 2
        while not close_started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(close_started.is_set(), "cancel did not request an MCP close")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        deadline = time.monotonic() + 2
        while not finished.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        process = source.process
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.assertTrue(finished.is_set(), "cancelled read thread did not finish")
        self.assertIsNone(process, "closed client restarted an MCP process after close returned")

    async def test_cancel_during_mcp_request_reaps_after_the_lock(self):
        due = self.now + timedelta(hours=2)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class GatedRequestSource(TijianTodoSource):
            def _request(self, method, params):
                if method == "tools/call":
                    entered.set()
                    release.wait(5)
                return super()._request(method, params)

        source = None
        def factory(server, workspace):
            nonlocal source
            source = GatedRequestSource(server, workspace)
            return source

        service = WakeBindings(self.store, source_factory=factory)
        self.addCleanup(lambda: self.stop_source(source))
        task = asyncio.create_task(service.create(
            workflow_id="example", task_id="training", todo_id="todo-1",
            authorized_by_user=True, time_source="due"))
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(entered.is_set(), "MCP tools/call was not reached")
        self.assertIsNotNone(source.process)
        task.cancel()
        await asyncio.sleep(.05)
        self.assertFalse(task.done(), "close returned while the request still held the lock")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assertIsNone(source.process, "MCP process survived cancelled request cleanup")

    async def test_close_is_bounded_when_a_read_never_releases_its_lock(self):
        due = self.now + timedelta(hours=2)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class StuckSource(TijianTodoSource):
            def _start(self):
                entered.set()
                release.wait(5)
                super()._start()

        source = None
        def factory(server, workspace):
            nonlocal source
            source = StuckSource(server, workspace)
            return source

        service = WakeBindings(self.store, source_factory=factory)
        self.addCleanup(lambda: self.stop_source(source))
        task = asyncio.create_task(service.create(
            workflow_id="example", task_id="training", todo_id="todo-1",
            authorized_by_user=True, time_source="due"))
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(entered.is_set())
        started = time.monotonic()
        task.cancel()
        with patch.object(wakebinding, "MCP_CLOSE_LOCK_TIMEOUT", 0.2):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertLess(time.monotonic() - started, 1.5)
        release.set()
        await asyncio.sleep(.05)
        self.assertIsNone(source.process, "read thread restarted MCP after bounded close")

    async def test_mcp_close_does_not_block_the_event_loop(self):
        due = self.now + timedelta(hours=2)
        facts = self.facts(due)
        started, release = threading.Event(), threading.Event()
        class Source:
            def get(self, todo_id):
                return facts
            def close(self):
                started.set()
                release.wait(5)
        service = WakeBindings(self.store, source_factory=lambda *_: Source())
        ticks = 0
        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(.01)
                ticks += 1
        beat = asyncio.create_task(heartbeat())
        try:
            task = asyncio.create_task(service.create(
                workflow_id="example", task_id="training", todo_id="todo-1",
                authorized_by_user=True, time_source="due"))
            deadline = time.monotonic() + 2
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            self.assertTrue(started.is_set(), "fake MCP close was not reached")
            baseline = ticks
            await asyncio.sleep(.1)
            self.assertGreater(ticks, baseline + 3, "event loop stalled during MCP close")
            self.assertFalse(task.done())
            release.set()
            result = await asyncio.wait_for(task, 3)
        finally:
            release.set()
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        self.assertTrue(result["created"])

    async def test_force_killed_mcp_close_does_not_block_the_event_loop(self):
        due = self.now + timedelta(hours=2)
        self.write_todo(due=due, ignore_eof=True)
        ticks = 0
        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(.01)
                ticks += 1
        beat = asyncio.create_task(heartbeat())
        try:
            result = await asyncio.wait_for(self.service.create(
                workflow_id="example", task_id="training", todo_id="todo-1",
                authorized_by_user=True, time_source="due"), 5)
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        self.assertTrue(result["created"])
        self.assertGreater(ticks, 20, "event loop stalled while force-killing MCP")

    async def test_cancelled_mcp_close_still_reaps_the_client(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        event = self.store.event(binding["event_id"])
        facts = self.facts(self.now + timedelta(hours=1))
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        class Source:
            def get(self, todo_id):
                return facts
            def close(self):
                started.set()
                release.wait(5)
                closed.set()
        service = WakeBindings(self.store, source_factory=lambda *_: Source())
        task = asyncio.create_task(service.verify(event))
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(started.is_set(), "fake MCP close was not reached")
        task.cancel()
        await asyncio.sleep(.05)
        self.assertFalse(task.done(), "cancellation escaped before cleanup")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set(), "cancelled task left the MCP client unreaped")

    async def test_worker_never_reschedules_or_sends_an_unprojected_bound_event(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        configure(self.config, True, now=self.now)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        self.write_todo(due=self.now + timedelta(hours=3), version=2)
        self.now += timedelta(hours=1, seconds=1)
        before = self.store.event(binding["event_id"])
        await Watcher(self.store, sender).tick()
        sender.assert_not_awaited()
        after = self.store.event(binding["event_id"])
        self.assertEqual(after["status"], "queued")
        self.assertEqual(after["not_before_utc"], before["not_before_utc"])
        self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "active")
        self.assertEqual(Inbox(self.store).list("example")["messages"], [])

    async def test_worker_environment_cannot_revoke_runtime_authorized_reminder(self):
        with patch.dict(os.environ, {"TIJIAN_PROFILE_ROOT": "/runtime-profile"}):
            first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        configure(self.config, True, now=self.now)
        self.now += timedelta(hours=1)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        worker = Store(self.config, clock=lambda: self.now)
        self.addCleanup(worker.close)
        with patch.dict(os.environ, {"TIJIAN_PROFILE_ROOT": "/worker-only-profile"}):
            os.environ.pop("FAKE_TODO_STATE", None)
            # No Runtime projection: a bare due event is not authorization and
            # the Worker must not gate, cancel or send it.
            await Watcher(worker, sender).tick()
            sender.assert_not_awaited()
            self.assertEqual(self.store.event(binding["event_id"])["status"], "queued")
            self.assertEqual(self.store.wakebinding(binding["binding_id"])["status"], "active")
            self.assertEqual(Inbox(worker).list("example")["messages"], [])
        # Runtime, with its own environment, commits the occurrence.
        with patch.dict(os.environ, {"TIJIAN_PROFILE_ROOT": "/runtime-profile"}):
            delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.store.event(binding["event_id"])["status"], "succeeded")
        with patch.dict(os.environ, {"TIJIAN_PROFILE_ROOT": "/worker-only-profile"}):
            # The differently-configured Worker consumes the authorized
            # occurrence without touching binding identity or state.
            await Watcher(worker, sender).tick()
        sender.assert_awaited_once()
        current = self.store.wakebinding(binding["binding_id"])
        self.assertEqual(current["status"], "active")
        self.assertIsNone(current["last_error_code"])
        self.assertEqual(self.store.event(binding["event_id"])["status"], "succeeded")
        self.assertEqual(len(Inbox(worker).list("example")["messages"]), 1)

    async def test_desktop_delivery_is_once_after_runtime_authorization(self):
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        configure(self.config, True, now=self.now)
        sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        self.now += timedelta(hours=1)
        delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        watcher = Watcher(self.store, sender)
        await watcher.tick()
        await watcher.tick()
        self.assertEqual(sender.await_count, 1)
        messages = Inbox(self.store).list("example")["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIn("Finish the fixture task", messages[0]["text"])
        self.assertEqual(self.store.event(binding["event_id"])["status"], "succeeded")
        reopened = Store(self.config, clock=lambda: self.now)
        try:
            await Watcher(reopened, sender).tick()
        finally:
            reopened.close()
        self.assertEqual(sender.await_count, 1)

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

    async def test_gate_cas_conflict_defers_only_that_binding_and_backs_off(self):
        first = await self.create()
        first_id = first["binding"]["binding_id"]
        # A pending binding takes the status-change CAS path on recovery.
        self.assertTrue(self.service.defer(first_id, "todo_unavailable"))
        self.now += timedelta(seconds=1)
        await self.create(name="second")
        self.now += timedelta(hours=1)
        real = self.store.apply_wakebinding
        failures = {"first": 0}

        def flaky(identity, expected_version, **fields):
            # Two conflicting writes exhaust the reconciler's own CAS retry,
            # so the gate must surface binding_conflict to the projection.
            if identity == first_id and failures["first"] < 2:
                failures["first"] += 1
                raise ConflictError("concurrent binding update")
            return real(identity, expected_version, **fields)

        with patch.object(self.store, "apply_wakebinding", side_effect=flaky):
            delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        deferred = self.store.wakebinding(first_id)
        self.assertEqual(deferred["status"], "pending")
        self.assertEqual(deferred["last_error_code"], "binding_verify_conflict")
        self.assertIsNotNone(deferred["last_attempt_at"])
        self.assertIsNone(self.store.claim_due("worker"))

        # The binding backoff, not an exception escaping the gate, suppresses
        # an immediate retry. Other bindings are unaffected.
        with patch.object(self.service, "gate", new=AsyncMock(side_effect=AssertionError("gate must not run"))) as gate:
            self.assertEqual(await Inbox(self.store).project_bound_due(self.service), [])
            gate.assert_not_awaited()

        self.now += timedelta(seconds=16)
        delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 2)
        recovered = self.store.wakebinding(first_id)
        self.assertEqual(recovered["status"], "active")
        self.assertIsNone(recovered["last_error_code"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    async def test_projection_does_not_wait_for_one_binding_close(self):
        first = await self.create(name="one")
        second = await self.create(name="two")
        self.now += timedelta(hours=1)
        blocked, release = asyncio.Event(), asyncio.Event()
        async def gate(event):
            if event["event_id"] == first["binding"]["event_id"]:
                blocked.set()
                await release.wait()
            return True
        with patch.object(self.service, "delivery_allowed", side_effect=gate):
            task = asyncio.create_task(Inbox(self.store).project_bound_due(self.service))
            await asyncio.wait_for(blocked.wait(), 2)
            deadline = time.monotonic() + 2
            while len(Inbox(self.store).list("example")["messages"]) < 1 and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1,
                             "another binding waited for a slow MCP close")
            release.set()
            delivered = await asyncio.wait_for(task, 3)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 2)
        self.assertEqual({self.store.event(second["binding"]["event_id"])["status"],
                          self.store.event(first["binding"]["event_id"])["status"]}, {"succeeded"})

    async def test_defer_survives_its_own_cas_race(self):
        first = await self.create()
        binding_id = first["binding"]["binding_id"]
        self.assertTrue(self.service.defer(binding_id, "todo_unavailable"))
        self.now += timedelta(hours=1)
        real = self.store.apply_wakebinding
        failures = {"n": 0}

        def racing(identity, expected_version, **fields):
            # Four failures: two in the gate's reconcile, two in defer's first
            # attempt. defer must re-read and retry rather than give up.
            if failures["n"] < 4:
                failures["n"] += 1
                raise ConflictError("concurrent binding update")
            return real(identity, expected_version, **fields)

        with patch.object(self.store, "apply_wakebinding", side_effect=racing):
            delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(delivered, [])
        self.assertEqual(Inbox(self.store).list("example")["messages"], [])
        deferred = self.store.wakebinding(binding_id)
        self.assertEqual(deferred["status"], "pending")
        self.assertEqual(deferred["last_error_code"], "binding_verify_conflict")
        self.assertIsNone(self.store.claim_due("worker"))
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.now += timedelta(seconds=16)
        delivered = await Inbox(self.store).project_bound_due(self.service)
        self.assertEqual(len(delivered), 1)
        recovered = self.store.wakebinding(binding_id)
        self.assertEqual(recovered["status"], "active")
        self.assertIsNone(recovered["last_error_code"])

    async def test_bound_reminder_delivers_while_agent_is_busy(self):
        from dataclasses import replace
        from oicoach_runtime.scheduler import Runtime
        fake_omp = Path(__file__).with_name("fake_omp.py")
        config = replace(self.config,
                         server=replace(self.config.server, poll_interval_seconds=0.02),
                         agent=replace(self.config.agent, executable=str(fake_omp)))
        runtime = Runtime(config)
        runtime.store = Store(config, clock=lambda: self.now)
        self.addCleanup(runtime.store.close)
        loop = asyncio.create_task(runtime.schedule_loop())
        release = self.workspace / "fake-release"
        self.addCleanup(loop.cancel)
        self.addCleanup(runtime.stop_event.set)
        self.addCleanup(lambda: release.touch(exist_ok=True))

        self.write_todo(due=self.now + timedelta(minutes=30))
        first = await self.create()
        binding = self.store.wakebinding(first["binding"]["binding_id"])
        self.store.enqueue("example", "training", "busy agent", "busy-agent",
                           payload={"fake_mode": "hold"}, at="now")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = self.store.connection.execute(
                "SELECT status FROM runs ORDER BY started_at").fetchall()
            if rows and rows[-1]["status"] == "running":
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("fake agent did not start")

        self.now += timedelta(minutes=31)
        deadline = time.monotonic() + 10
        while len(Inbox(self.store).list("example")["messages"]) < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        messages = Inbox(self.store).list("example")["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.store.event(binding["event_id"])["status"], "succeeded")
        self.assertEqual(self.store.connection.execute(
            "SELECT status FROM runs ORDER BY started_at").fetchall()[-1]["status"], "running")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

        release.touch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.store.connection.execute(
                    "SELECT status FROM runs ORDER BY started_at").fetchall()[-1]["status"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertIsNone(self.store.claim_due("worker"))

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
