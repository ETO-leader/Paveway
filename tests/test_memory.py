import concurrent.futures
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.db import initialize_database, inspect_database
from oicoach_runtime.errors import ConflictError, RuntimeContractError, StorageError
from oicoach_runtime.memory import Memory, History, CONTEXT_TOKENS
from oicoach_runtime.models import utc_text
from oicoach_runtime.context_budget import count_tokens
from oicoach_runtime.store import Store, canonical_json
from oicoach_runtime.workflows import Workflow


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)

    def put(self, key="preference", **kwargs):
        return self.memory.put("coach", key, "用户偏好", "每次先给提示，不直接给代码", **kwargs)

    def event(self, task="training", key="one"):
        return self.store.enqueue("coach", task, "请求", key, {"message": "线性基 口胡未编码 %_"}, at="now")

    def test_cas_concurrency_delete_tombstone_and_restart(self):
        first = self.put(pinned=True)
        def update():
            store = Store(self.config, clock=lambda: self.now)
            try:
                return Memory(store).put("coach", "preference", "更正", "独立完成后复盘", expected_version=1)
            except ConflictError:
                return "conflict"
            finally:
                store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: update(), range(2)))
        self.assertEqual(sum(r == "conflict" for r in results), 1)
        self.assertEqual(first["version"], 1)
        with self.assertRaises(ConflictError):
            self.memory.delete("coach", "preference", 1)
        deleted = self.memory.delete("coach", "preference", 2)
        self.assertEqual(deleted["version"], 3)
        self.assertEqual(deleted["content"], "")
        self.assertEqual(self.memory.list("coach"), [])
        with self.assertRaises(ConflictError):
            self.put()
        restored = self.put(expected_version=3)
        self.assertEqual(restored["version"], 4)
        reopened = Store(self.config)
        try:
            self.assertEqual(Memory(reopened).show("coach", "preference")["content"], restored["content"])
        finally:
            reopened.close()

    def test_scope_provenance_expiry_unicode_search_and_context_budget(self):
        event = self.event()
        self.put("shared", pinned=True, source_event_id=event["event_id"])
        self.put("task", task_id="training", kind="hypothesis", source_event_id=event["event_id"])
        self.put("other", task_id="other")
        Memory(self.store).put("another", "private", "其他用户", "不能串入", pinned=True)
        context = self.memory.context("coach", "training")
        self.assertEqual({n["key"] for n in context["notes"]}, {"shared", "task"})
        self.assertEqual(context["notes"][1]["kind"], "hypothesis")
        with self.assertRaises(ConflictError):
            self.put("bad", task_id="other", source_event_id=event["event_id"])
        self.assertEqual(len(self.memory.list("coach", query="提示")), 1)
        self.assertEqual(self.memory.list("coach", query="%"), [])
        self.put("temporary", expires_at=utc_text(self.now + timedelta(seconds=1)))
        self.now += timedelta(seconds=2)
        self.assertNotIn("temporary", [n["key"] for n in self.memory.context("coach", "training")["notes"]])
        for index in range(15):
            self.memory.put("coach", f"large-{index}", "数据", "汉" * 1300, task_id="training")
        bounded = self.memory.context("coach", "training")
        self.assertLessEqual(count_tokens(canonical_json(bounded)), CONTEXT_TOKENS)
        self.assertTrue(bounded["truncated"])
        self.assertIn("shared", [n["key"] for n in bounded["notes"]])
        self.assertEqual(bounded["token_limit"], 10000)
        self.assertEqual(bounded["tokenizer"], "o200k_base")
        # A token limit permits substantially more than the previous 8 KiB,
        # while omitted notes remain queryable in the durable store.
        self.assertGreater(len(canonical_json(bounded).encode()), 8192)
        self.assertIsNotNone(self.memory.show("coach", "large-14", task_id="training"))

    def test_validation_and_metadata_only_audit(self):
        for changes in ({"content": "x" * 4097}, {"content": "汉" * 1366}, {"content": "\ud800"},
                        {"title": " "}, {"kind": "guessed"}, {"expected_version": True},
                        {"pinned": 1}, {"task_id": "../other"}, {"expires_at": "yesterday"}):
            values = dict(workflow_id="coach", key="safe", title="safe", content="SECRET_MEMORY_MARKER")
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(RuntimeContractError):
                self.memory.put(**values)
        self.put()
        audit = [dict(row) for row in self.store.connection.execute("SELECT * FROM audit_log")]
        self.assertNotIn("每次先给提示", json.dumps(audit, ensure_ascii=False))
        self.assertEqual(self.config.database_path.stat().st_mode & 0o777, 0o600)

    def test_history_existing_runs_literal_search_and_failure_is_not_success(self):
        event = self.event()
        claimed = self.store.claim_due("test")
        run_id = claimed["run_id"]
        self.store.dispatch(run_id)
        self.store.finish(run_id, "uncertain", error="simulated_crash")
        history = History(self.store)
        hits = history.search("coach", query="口胡")
        self.assertEqual(hits[0]["run_id"], run_id)
        self.assertEqual(hits[0]["status"], "uncertain")
        self.assertEqual(history.search("coach", task_id="other", query="口胡"), [])
        self.assertEqual(history.search("another", query="口胡"), [])
        self.assertEqual(len(history.search("coach", query="%_")), 1)
        self.assertEqual(history.search("coach", query="' OR 1=1 --"), [])
        self.assertEqual(history.show(run_id)["event"]["event_id"], event["event_id"])
        self.assertEqual(history.show(run_id)["output"]["status"], "unavailable")

    def archive(self):
        event = self.event()
        claimed = self.store.claim_due("test")
        workflow = Workflow("coach", "1", self.root, "per_task", {})
        binding = self.store.bind(claimed, workflow)
        directory = Path(binding["session_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "session.jsonl"
        entries = [{"type": "title", "title": "history"},
            {"type": "session", "id": "session-id", "version": 3, "cwd": str(self.root)},
            {"type": "message", "id": "m1", "parentId": None,
             "message": {"role": "user", "content": "昨天的推理：线性基 尚未编码"}},
            {"type": "message", "id": "m2", "parentId": "m1", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "HIDDEN_THOUGHT"}, {"type": "text", "text": "提示：消元"}]}},
            {"type": "message", "id": "m3", "parentId": "m1", "message": {"role": "toolResult", "content": [
                {"type": "text", "text": "题库结果 尚未通过"}]}},
        ]
        path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries))
        self.store.record_session(claimed["run_id"], {"sessionFile": str(path), "sessionId": "session-id"})
        self.store.finish(claimed["run_id"], "blocked", error="test_fixture")
        return path, binding

    def test_native_archive_read_only_all_branches_and_pagination(self):
        path, binding = self.archive()
        before = path.read_bytes()
        history = History(self.store)
        result = history.messages("coach", "training")
        self.assertEqual(result["archive_mode"], "all_branches")
        self.assertEqual(len(result["messages"]), 3)
        self.assertEqual(result["messages"][1]["parent_id"], result["messages"][2]["parent_id"])
        self.assertNotIn("HIDDEN_THOUGHT", json.dumps(result))
        self.assertEqual(len(history.messages("coach", "training", query="线性基")["messages"]), 1)
        self.assertEqual(history.messages("coach", "training", offset=2)["messages"][0]["entry_id"], "m3")
        self.assertEqual(before, path.read_bytes())
        with path.open("a") as stream:
            for index in range(22):
                stream.write(json.dumps({"type": "message", "id": str(index), "message": {"role": "user", "content": "long " + "x" * 3000}}) + "\n")
        page = history.messages("coach", "training", query="long")
        self.assertEqual(len(page["messages"]), 20)
        self.assertTrue(page["has_more"])
        self.assertTrue(page["messages"][0]["truncated"])
        self.assertFalse(history.messages("coach", "training", query="long", offset=20)["has_more"])

    def test_archive_refuses_foreign_symlink_corrupt_and_oversized_files(self):
        path, binding = self.archive()
        history = History(self.store)
        original = path.read_bytes()
        path.write_bytes(original.replace(b'session-id', b'foreign-id'))
        with self.assertRaises(ConflictError):
            history.messages("coach", "training")
        path.write_text("not-json\n")
        with self.assertRaises(RuntimeContractError):
            history.messages("coach", "training")
        path.unlink()
        path.symlink_to(self.config.database_path)
        with self.assertRaises(ConflictError):
            history.messages("coach", "training")
        path.unlink()
        with path.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.assertRaises(RuntimeContractError):
            history.messages("coach", "training")


class MemoryMigrationTests(unittest.TestCase):
    def test_v1_migration_preserves_queue_and_rolls_back_v2_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v1.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(files("oicoach_runtime").joinpath("migrations/001_initial.sql").read_text())
                connection.execute("INSERT INTO runtime_metadata VALUES ('keep','\"private-value\"')")
                connection.execute("""INSERT INTO events
                    (event_id,workflow_id,task_id,kind,reason,payload_json,schedule_mode,not_before_utc,
                     status,max_attempts,dedupe_key,request_fingerprint,created_at,updated_at)
                    VALUES ('old-event','coach','old-task','wake','preserve','{"message":"old input"}',
                     'at','2026-10-19T00:00:00.000000Z','queued',3,'old-dedupe','old-fingerprint',
                     '2026-10-18T00:00:00.000000Z','2026-10-18T00:00:00.000000Z')""")
                connection.execute("""INSERT INTO session_bindings
                    (binding_id,workflow_id,task_id,session_dir,workspace,workflow_revision,status,created_at,updated_at)
                    VALUES ('old-binding','coach','old-task','/private/session','/private/workspace','1',
                     'ready','2026-10-18T00:00:00.000000Z','2026-10-18T00:00:00.000000Z')""")
                connection.execute("""INSERT INTO runs
                    (run_id,event_id,binding_id,request_id,status,started_at,error_code)
                    VALUES ('old-run','old-event','old-binding','old-request','failed',
                     '2026-10-18T00:00:00.000000Z','old_failure')""")
                preserved = {table: connection.execute("SELECT * FROM " + table).fetchall()
                             for table in ("events", "runs", "session_bindings", "runtime_metadata", "audit_log")}
                connection.execute("PRAGMA user_version=1")
            path.chmod(0o600)
            class BrokenMigration:
                def joinpath(self, _):
                    return self
                def read_text(self, **_):
                    return "CREATE TABLE partial_v2(x TEXT);\nINVALID SQL;\n"
            with patch("oicoach_runtime.db.files", return_value=BrokenMigration()), self.assertRaises(StorageError):
                initialize_database(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='partial_v2'").fetchone())
            self.assertEqual(initialize_database(path)["schema_version"], 2)
            self.assertEqual(initialize_database(path)["schema_version"], 2)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT value FROM runtime_metadata WHERE key='keep'").fetchone()[0], '"private-value"')
                for table, rows in preserved.items():
                    self.assertEqual(connection.execute("SELECT * FROM " + table).fetchall(), rows)
            self.assertEqual(inspect_database(path)["integrity"], "ok")
