import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.memory import Memory
from oicoach_runtime.models import utc_text
from oicoach_runtime.scheduler import Runtime
from oicoach_runtime.store import Store
from oicoach_runtime.workflow_setup import create
from oicoach_runtime.workflows import WorkflowProvider


class PromptCacheContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pw-cache-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        self.now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.frontend = Frontend(self.store)
        self.runtime = Runtime(self.config)
        self.runtime.store = self.store

    def test_known_notes_distinguish_deletion_expiry_and_byte_selection_omission(self):
        self.memory.put("example", "deleted", "Title", "old")
        self.memory.delete("example", "deleted", 1)
        self.memory.put("example", "expired", "Title", "temporary", expires_at=utc_text(self.now + timedelta(seconds=1)))
        for index in range(20):
            self.memory.put("example", "large-" + str(index), "Large", "x" * 4096, pinned=True)
        self.memory.put("example", "omitted", "Title", "x" * 4096)
        self.now += timedelta(seconds=2)
        result = self.memory.context("example", "training", known=[
            {"task_id": "", "key": key} for key in ("deleted", "expired", "omitted")])
        self.assertEqual({note["key"]: note["reason"] for note in result["withdrawn"]}, {
            "deleted": "deleted", "expired": "expired", "omitted": "outside_current_context"})
        self.assertEqual(self.memory.show("example", "omitted")["content"], "x" * 4096)
        self.assertTrue(result["truncated"])
        with self.assertRaises(ConflictError):
            self.memory.context("example", "training", known=[{"task_id": "another-task", "key": "private"}])
        with self.assertRaises(RuntimeContractError):
            self.memory.context("example", "training", known=[{"task_id": "", "key": "a"}] * 102)

    def native_archive(self):
        lease = self.frontend.begin("example", "training", self.root)
        owner = {key: lease[key] for key in ("conversation_id", "generation", "owner_token")}
        self.frontend.finish(**owner, exit_code=0)
        record = self.frontend.get(lease["conversation_id"])
        directory = Path(record["session_dir"])
        directory.mkdir(parents=True)
        path = directory / "session.jsonl"
        path.write_text(json.dumps({"type": "session", "version": 3, "id": "synthetic-session", "cwd": str(self.root)}) + "\n")
        record.update(session_file=str(path), session_id="synthetic-session")
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        return record, path

    def test_legacy_native_session_requires_new_conversation_without_modifying_history(self):
        record, path = self.native_archive()
        original = path.read_bytes()
        record.pop("composer_version")
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        with self.assertRaisesRegex(ConflictError, "Legacy sessions and history are preserved"):
            self.frontend.begin("example", "training", self.root, record["conversation_id"])
        newer = self.frontend.begin("example", "training", self.root, new=True)
        self.assertNotEqual(newer["conversation_id"], record["conversation_id"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.frontend.get(record["conversation_id"])["status"], "released")

    def test_role_change_is_fenced_but_prompt_skill_edits_keep_same_physical_session(self):
        record, path = self.native_archive()
        with self.assertRaises(ConflictError):
            self.frontend.begin("example", "training", self.root, record["conversation_id"], role_base="oi-coach")
        settings = {"schema_version": 1, "revision": 1, "system_prompt": {"mode": "append", "text": "new user policy"},
                    "skills": [{"name": "sample", "description": "changed skill", "content": "native skill", "enabled": True}]}
        resumed = self.frontend.begin("example", "training", self.root, record["conversation_id"], settings=settings)
        self.assertEqual(resumed["session_file"], str(path))
        self.assertEqual(resumed["generation"], record["generation"] + 1)

    def test_cache_diagnostics_use_same_durable_native_and_background_parser(self):
        record, path = self.native_archive()
        result = self.runtime.dispatch_api("cache.stats", {"conversation_id": record["conversation_id"]})
        self.assertEqual(result["source"], "omp_session_jsonl")
        self.assertEqual(result["cumulative"]["status"], "unavailable")
        with self.assertRaises(RuntimeContractError):
            self.runtime.dispatch_api("cache.stats", {"conversation_id": record["conversation_id"], "task_id": "training"})
        record["session_file"] = str(self.root / "foreign.jsonl")
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        with self.assertRaises(ConflictError):
            self.runtime.dispatch_api("cache.stats", {"conversation_id": record["conversation_id"]})

    def test_legacy_background_session_blocks_before_spawn_and_retains_archive(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        create(self.config, "example", workspace, "example")
        event = self.store.enqueue("example", "training", "legacy", "legacy", {}, at="now")
        claimed = self.store.claim_due("test-owner")
        workflow = WorkflowProvider(self.config.workflow_root).describe("example")
        binding = self.store.bind(claimed, workflow)
        path = Path(binding["session_dir"]) / "legacy.jsonl"
        original = json.dumps({"type": "session", "version": 3, "id": "legacy", "cwd": str(workspace)}) + "\n"
        path.write_text(original)
        self.store.record_session(claimed["run_id"], {"sessionFile": str(path), "sessionId": "legacy"})
        asyncio.run(self.runtime.dispatch_event(claimed))
        run = self.store.run(claimed["run_id"])
        self.assertEqual(run["status"], "blocked")
        self.assertIsNone(run["dispatch_intent_at"])
        self.assertEqual(run["error_code"], "session_prompt_changed")
        self.assertIn("旧会话与历史", run["error_message"])
        self.assertIsNone(run["agent_pid"])
        self.assertEqual(path.read_text(), original)
        self.assertEqual(self.store.event(event["event_id"])["status"], "blocked")

    def test_missing_memory_extension_admission_blocks_dispatch_even_when_rpc_is_ready(self):
        from dataclasses import replace
        from oicoach_runtime.config import AgentConfig
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "fake-mode").write_text("missing-memory-ready")
        create(self.config, "example", workspace, "example")
        self.runtime.config = replace(self.config, agent=AgentConfig(
            executable=str(Path(__file__).with_name("fake_omp.py").resolve()),
            startup_timeout_seconds=0.25, graceful_stop_seconds=0.1))
        self.store.enqueue("example", "training", "admission", "admission", {}, at="now")
        event = self.store.claim_due("test-owner")
        asyncio.run(self.runtime.dispatch_event(event))
        run = self.store.run(event["run_id"])
        self.assertIsNone(run["dispatch_intent_at"])
        self.assertFalse(run["acknowledged_at"])
        self.assertEqual(run["error_code"], "agent_rpc_ready_missing")

    def test_memory_read_failure_blocks_before_dispatch_with_safe_diagnostics(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        create(self.config, "example", workspace, "coach")
        self.store.enqueue("example", "training", "PRIVATE_REASON", "memory-failure", {}, at="now")
        event = self.store.claim_due("fixture")
        with patch.object(Memory, "context", side_effect=OSError("PRIVATE_DATABASE_DETAIL")):
            asyncio.run(self.runtime.dispatch_event(event))
        run = self.store.run(event["run_id"])
        self.assertEqual(run["error_code"], "memory_recall_failed")
        self.assertEqual(run["status"], "blocked")
        self.assertIsNone(run["dispatch_intent_at"])
        diagnostic = self.store.status()["diagnostics"]
        evidence = diagnostic["binding_groups"][0]["events"][0]
        self.assertEqual(evidence["stage"], "memory")
        self.assertFalse(evidence["dispatch_intent"])
        self.assertFalse(evidence["automatic_replay"])
        self.assertNotIn("PRIVATE_", json.dumps(diagnostic))

    def test_missing_native_snapshot_is_not_reconstructed_or_dispatched(self):
        self.store.enqueue("example", "training", "legacy", "missing-snapshot", {"native_wakeup": True}, at="now")
        event = self.store.claim_due("fixture")
        asyncio.run(self.runtime.dispatch_event(event))
        run = self.store.run(event["run_id"])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["error_code"], "native_wakeup_snapshot_missing")
        self.assertIsNone(run["dispatch_intent_at"])
