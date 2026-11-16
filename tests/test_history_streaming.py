"""Streaming local history: no archive budget, bounded memory, off-loop scans."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

from oicoach_runtime import git_sync
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import NativeSessionError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.scheduler import Runtime
from oicoach_runtime.store import Store
from oicoach_runtime.workflow_setup import create

HEADER = {"type": "session", "id": "sid", "cwd": "", "version": 3}


def entry(identity, role, text, parent=None, timestamp="2026-11-09T02:02:00Z"):
    return {"type": "message", "id": identity, "parentId": parent, "timestamp": timestamp,
            "message": {"role": role, "content": text}}


class StreamingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-history-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        create(self.config, "example", self.root)
        self.store = Store(self.config)
        self.addCleanup(self.store.close)
        self.frontend = Frontend(self.store)
        lease = self.frontend.begin("example", "training", self.root)
        self.conversation_id = lease["conversation_id"]
        self.directory = Path(lease["session_dir"])
        self.directory.mkdir(parents=True, mode=0o700)
        self.sources = []

    def add_source(self, entries, session_id="sid", filename="session.jsonl"):
        path = self.directory / filename
        with path.open("w") as stream:
            stream.write(json.dumps({**HEADER, "id": session_id, "cwd": str(self.root)}) + "\n")
            for item in entries:
                stream.write(json.dumps(item) + "\n")
        self.sources.append({"session_file": str(path), "session_id": session_id})
        record = self.frontend.get(self.conversation_id)
        record.update(session_file=str(path), session_id=session_id,
                      session_files=list(self.sources), status="idle",
                      session_required=True)
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        return path

    def messages(self, **arguments):
        return self.frontend.messages(self.conversation_id, **arguments)

    def test_session_above_the_git_export_limit_is_searchable_with_bounded_memory(self):
        filler = "filler-" + "x" * 65000
        with (self.directory / "session.jsonl").open("w") as stream:
            stream.write(json.dumps({**HEADER, "cwd": str(self.root)}) + "\n")
            stream.write(json.dumps(entry("first", "user", "FRONT_MARKER opening question")) + "\n")
            for index in range(340):
                role = "assistant" if index % 2 else "user"
                stream.write(json.dumps(entry(f"m{index}", role, filler, parent="first")) + "\n")
            stream.write(json.dumps(entry("last", "assistant", "TAIL_MARKER final answer")) + "\n")
        path = self.directory / "session.jsonl"
        self.assertGreater(path.stat().st_size, 20 * 1024 * 1024)
        self.sources.append({"session_file": str(path), "session_id": "sid"})
        record = self.frontend.get(self.conversation_id)
        record.update(session_file=str(path), session_id="sid", session_files=list(self.sources),
                      status="idle", session_required=True)
        with self.store.transaction() as db:
            self.frontend.save(db, record)

        tracemalloc.start()
        try:
            tail = self.messages(query="TAIL_MARKER")
            front = self.messages(query="FRONT_MARKER")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual([message["id"] for message in tail["messages"]], ["last"])
        self.assertEqual([message["id"] for message in front["messages"]], ["first"])
        self.assertFalse(tail["has_more"])
        self.assertLess(peak, 4 * 1024 * 1024, "history search retained the whole Session")

        # The Git archive policy is unchanged: the same file is skipped.
        with self.assertRaises(NativeSessionError) as failure:
            git_sync.export_session(self.config, {**record, **self.sources[0]}, "device",
                                    root=self.directory)
        self.assertEqual(failure.exception.code, "native_session_export_limit")

    def test_pagination_is_complete_and_has_no_duplicates_across_sessions(self):
        first = [entry(f"a{index}", "user", f"needle one {index}") for index in range(25)]
        second = [entry(f"b{index}", "assistant", f"needle two {index}") for index in range(20)]
        self.add_source(first, "one", "one.jsonl")
        self.add_source(second, "two", "two.jsonl")
        seen = []
        for offset in (0, 20, 40):
            page = self.messages(query="needle", offset=offset)
            seen.extend((message["session_id"], message["id"]) for message in page["messages"])
            self.assertEqual(page["has_more"], offset < 40)
            self.assertLessEqual(len(page["messages"]), 20)
        expected = [("one", f"a{index}") for index in range(25)] + \
                   [("two", f"b{index}") for index in range(20)]
        self.assertEqual(seen, expected)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(self.messages(query="needle", offset=45)["messages"], [])
        self.assertFalse(self.messages(query="needle", offset=45)["has_more"])

    def test_mixed_sessions_preserve_branch_identity_and_skip_non_conversation_text(self):
        self.add_source([
            entry("one", "user", "traceable NEEDLE", timestamp="2026-11-09T02:01:00Z"),
            {"type": "message", "id": "two", "parentId": "one", "timestamp": "2026-11-09T10:02:00+08:00",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "NEEDLE_HIDDEN"},
                 {"type": "toolCall", "name": "bash", "arguments": {"command": "NEEDLE_TOOL_ARG"}},
                 {"type": "text", "text": "visible NEEDLE answer"}]}},
            {"type": "message", "id": "three", "parentId": "two",
             "message": {"role": "toolResult", "content": "NEEDLE_TOOL_RESULT"}},
        ], "alpha", "alpha.jsonl")
        oversized = [entry(f"f{index}", "user", "filler " + "y" * 65000) for index in range(40)]
        self.add_source(oversized, "beta", "beta.jsonl")
        page = self.messages(query="NEEDLE")
        self.assertEqual([message["id"] for message in page["messages"]], ["one", "two"])
        self.assertEqual(page["messages"][0]["session_id"], "alpha")
        self.assertEqual(page["messages"][1]["parent_id"], "one")
        self.assertEqual(page["messages"][1]["timestamp"], "2026-11-09T02:02:00+00:00")
        self.assertNotIn("NEEDLE_HIDDEN", json.dumps(page))
        self.assertNotIn("NEEDLE_TOOL_ARG", json.dumps(page))
        self.assertNotIn("NEEDLE_TOOL_RESULT", json.dumps(page))
        self.assertEqual(page["archive_mode"], "all_branches")
        self.assertEqual(page["source"], "omp_native_session")

    def test_long_messages_are_excerpted_around_the_match(self):
        self.add_source([entry("long", "assistant", "x" * 4000 + "MIDDLE_NEEDLE" + "y" * 4000)])
        message = self.messages(query="MIDDLE_NEEDLE")["messages"][0]
        self.assertIn("MIDDLE_NEEDLE", message["text"])
        self.assertLessEqual(len(message["text"].encode()), 2048)
        self.assertTrue(message["truncated"])

    def assert_rejected(self, code, path=None, session_id="sid", prepare=None):
        path = path or (self.directory / "session.jsonl")
        record = self.frontend.get(self.conversation_id)
        record.update(session_file=str(path), session_id=session_id,
                      session_files=[{"session_file": str(path), "session_id": session_id}],
                      status="idle", session_required=True)
        if prepare:
            prepare(record)
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        with self.assertRaises(NativeSessionError) as failure:
            self.messages()
        self.assertEqual(failure.exception.code, code)

    def test_identity_path_and_entry_violations_are_rejected(self):
        cases = []
        path = self.add_source([entry("one", "user", "text")], "sid")
        cases.append(("identity", "native_session_identity_mismatch", None, "other", None))
        cases.append(("corrupt", "native_session_invalid", None, "sid",
                      lambda _record, path=path: path.write_text(
                          json.dumps({**HEADER, "id": "sid", "cwd": str(self.root)}) + "\nnot-json\n")))
        cases.append(("truncated", "native_session_incomplete", None, "sid",
                      lambda _record, path=path: path.write_text(
                          json.dumps({**HEADER, "id": "sid", "cwd": str(self.root)}) + "\n{\"type\":\"message\"")))
        cases.append(("oversized", "native_session_entry_limit", None, "sid",
                      lambda _record, path=path: path.write_text(
                          json.dumps({**HEADER, "id": "sid", "cwd": str(self.root)}) + "\n" +
                          json.dumps(entry("big", "user", "z" * (1024 * 1024 + 8))) + "\n")))
        outside = self.root / "outside.jsonl"
        outside.write_text(json.dumps({**HEADER, "id": "sid", "cwd": str(self.root)}) + "\n" +
                           json.dumps(entry("one", "user", "text")) + "\n")
        cases.append(("escape", "native_session_unsafe", outside, "sid", None))
        for name, code, custom_path, session_id, prepare in cases:
            with self.subTest(name):
                if custom_path is None and prepare is None:
                    path.write_text(json.dumps({**HEADER, "id": "sid", "cwd": str(self.root)}) + "\n" +
                                    json.dumps(entry("one", "user", "text")) + "\n")
                self.assert_rejected(code, custom_path, session_id, prepare)
        link = self.directory / "link.jsonl"
        link.symlink_to(path)
        self.assert_rejected("native_session_unsafe", link, "sid")

    def test_missing_source_is_skipped_without_hiding_other_results(self):
        self.add_source([entry("one", "user", "needle here")], "one", "one.jsonl")
        missing = self.directory / "missing.jsonl"
        self.sources.append({"session_file": str(missing), "session_id": "gone"})
        record = self.frontend.get(self.conversation_id)
        record.update(session_files=list(self.sources))
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        page = self.messages(query="needle")
        self.assertEqual([message["id"] for message in page["messages"]], ["one"])


class OffLoopHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pw-history-loop-")
        self.root = Path(self.temporary.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        create(self.config, "example", self.root)
        self.store = Store(self.config)
        self.runtime = Runtime(self.config, path)
        self.runtime.store = self.store
        self.frontend = Frontend(self.store)
        lease = self.frontend.begin("example", "training", self.root)
        self.owner = {key: lease[key] for key in ("conversation_id", "generation", "owner_token")}
        self.directory = Path(lease["session_dir"])
        self.directory.mkdir(parents=True, mode=0o700)
        self.session = self.directory / "session.jsonl"
        self.session.write_text(json.dumps({**HEADER, "id": "sid", "cwd": str(self.root)}) + "\n" +
                                json.dumps(entry("one", "user", "LOOP_NEEDLE")) + "\n")
        record = self.frontend.get(lease["conversation_id"])
        record.update(session_file=str(self.session), session_id="sid", status="idle",
                      session_files=[{"session_file": str(self.session), "session_id": "sid"}])
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        self.child = None

    async def asyncTearDown(self):
        if self.child is not None:
            self.child.terminate()
            self.child.wait()
        self.store.close()
        self.temporary.cleanup()

    async def test_messages_scan_runs_on_a_worker_thread_with_its_own_connection(self):
        entered, release = threading.Event(), threading.Event()
        captured = {}
        loop_thread = threading.get_ident()
        original = Frontend.messages

        def blocking(self, *args, **kwargs):
            captured["thread"] = threading.get_ident()
            captured["store"] = self.store
            captured["connection"] = self.store.connection
            captured["ok"] = self.store.metadata("api_version")
            entered.set()
            release.wait(10)
            return original(self, *args, **kwargs)

        with patch.object(Frontend, "messages", blocking):
            pending = asyncio.create_task(self.runtime.dispatch_async(
                "frontend.messages", {"conversation_id": self.owner["conversation_id"],
                                      "query": "LOOP_NEEDLE", "offset": 0}))
            started = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, entered.wait, 5), 10)
            self.assertTrue(started)
            # The event loop keeps serving light IPC while the scan is blocked.
            status = await asyncio.wait_for(self.runtime.dispatch_async("status", {}), 5)
            self.assertEqual(status["daemon"], "running")
            self.assertFalse(pending.done())
            release.set()
            result = await asyncio.wait_for(pending, 10)
        self.assertNotEqual(captured["thread"], loop_thread)
        self.assertIsNot(captured["store"].connection, self.store.connection)
        self.assertEqual(captured["ok"], 1)
        self.assertEqual([message["id"] for message in result["messages"]], ["one"])

    async def test_native_history_search_scans_off_loop_and_preserves_the_contract(self):
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        record = self.frontend.get(self.owner["conversation_id"])
        record["status"] = "starting"
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        self.frontend.started(**self.owner, guardian_pid=os.getpid())
        self.frontend.attest(**self.owner, omp_pid=self.child.pid, session_id="sid",
                             session_file=str(self.session))
        captured = {}
        original = Frontend.messages

        def recording(self, *args, **kwargs):
            captured["thread"] = threading.get_ident()
            return original(self, *args, **kwargs)

        with patch.object(Frontend, "messages", recording):
            result = await self.runtime.dispatch_async("native_history.search", {
                **self.owner, "parameters": {"source": "native", "query": "LOOP_NEEDLE"}})
        self.assertNotEqual(captured["thread"], threading.get_ident())
        self.assertEqual([message["id"] for message in result["messages"]], ["one"])


if __name__ == "__main__":
    unittest.main()
