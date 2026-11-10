"""Controlled slow filesystem validation through the real Unix control server."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.ipc import request
from oicoach_runtime.processes import InstanceLock
from oicoach_runtime.store import Store
from oicoach_runtime.errors import RuntimeContractError, ConflictError
from oicoach_runtime.workflow_setup import create


class ReleaseConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-release-concurrency-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        create(self.config, "example", self.root, "example")
        self.store = Store(self.config)
        self.addCleanup(self.store.close)
        f = Frontend(self.store)
        lease = f.begin("example", "training", self.root)
        self.owner = {k: lease[k] for k in ("conversation_id", "generation", "owner_token")}
        directory = Path(lease["session_dir"])
        directory.mkdir(parents=True, mode=0o700)
        self.session = directory / "session.jsonl"
        self.session.write_text(json.dumps({"type": "session", "id": "slow", "cwd": str(self.root), "version": 3}) + "\n")
        record = f.get(lease["conversation_id"])
        record.update(session_file=str(self.session), session_id="slow", status="idle")
        with self.store.transaction() as db: f.save(db, record)
        self.marker, self.release = self.root / "scanning", self.root / "continue"
        script = '''import asyncio,sys,time
from pathlib import Path
from oicoach_runtime.config import load_config
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.scheduler import Runtime
original=Frontend.validate_session
def slow(self,record,**kwargs):
    result=original(self,record,**kwargs)
    def commit(statement):
        if statement != 'COMMIT':return
        from oicoach_runtime.processes import InstanceLock
        from oicoach_runtime.errors import ConflictError
        try:
            lock=InstanceLock(Path(record['session_dir'])/'owner.lock')
        except ConflictError: Path(sys.argv[2]+'.commit').write_text('held')
        else:
            lock.close();Path(sys.argv[2]+'.commit').write_text('lost')
    self.store.connection.set_trace_callback(commit)
    Path(sys.argv[2]).write_text(str(self.store.connection.in_transaction))
    end=time.monotonic()+8
    while not Path(sys.argv[3]).exists() and time.monotonic()<end:time.sleep(.01)
    return result
Frontend.validate_session=slow
asyncio.run(Runtime(load_config(Path(sys.argv[1])),Path(sys.argv[1])).serve())
'''
        self.log = (self.root / "daemon.log").open("w")
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([sys.executable, "-c", script, str(self.path), str(self.marker), str(self.release)], stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try: request(self.config.server.socket_path, "status"); break
            except RuntimeContractError: time.sleep(.01)
        # Startup reconciles the synthetic lease; finish explicitly accepts it.

    def stop(self):
        self.release.touch()
        self.process.terminate()
        self.process.wait(timeout=10)

    def slow_scan(self, operation):
        with concurrent.futures.ThreadPoolExecutor() as executor:
            args = {**self.owner, "exit_code": 0} if operation == "frontend.finish" else {"conversation_id": self.owner["conversation_id"]}
            pending = executor.submit(request, self.config.server.socket_path, operation, args)
            deadline = time.monotonic() + 4
            while not self.marker.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(self.marker.exists())
            try:
                self.assertEqual(self.marker.read_text(), "False", "scan is inside a SQLite write transaction")
                with self.assertRaises(ConflictError): InstanceLock(self.session.parent / "owner.lock")
                started = time.monotonic()
                note = request(self.config.server.socket_path, "memory.put", {"workflow_id": "example", "task_id": "another",
                    "key": "unblocked", "title": "Fixture", "content": "IPC remains responsive"})
                self.assertEqual(note["version"], 1)
                self.assertEqual(request(self.config.server.socket_path, "memory.show", {"workflow_id": "example", "task_id": "another", "key": "unblocked"})["version"], 1)
                self.assertLess(time.monotonic() - started, 1)
            finally: self.release.touch()
            self.assertEqual(pending.result(timeout=5)["status"], "released")
            self.assertEqual(Path(str(self.marker) + ".commit").read_text(), "held", "physical lock was released before COMMIT")

    def test_slow_scan_holds_lock_without_blocking_other_tasks(self):
        self.slow_scan("frontend.finish")

    def test_slow_recover_holds_lock_through_commit_without_blocking_other_tasks(self):
        self.slow_scan("frontend.recover")

    def test_generation_and_file_identity_are_rechecked_after_scan(self):
        for change in ("generation", "file"):
            with self.subTest(change=change), concurrent.futures.ThreadPoolExecutor() as executor:
                self.marker.unlink(missing_ok=True); self.release.unlink(missing_ok=True)
                pending = executor.submit(request, self.config.server.socket_path, "frontend.finish", {**self.owner, "exit_code": 0})
                deadline = time.monotonic() + 4
                while not self.marker.exists() and time.monotonic() < deadline: time.sleep(.01)
                self.assertTrue(self.marker.exists())
                if change == "generation":
                    record = Frontend(self.store).get(self.owner["conversation_id"])
                    record.update(generation=record["generation"] + 1, token_hash=hashlib.sha256(b"different").hexdigest(), status="starting")
                    with self.store.transaction() as db: Frontend(self.store).save(db, record)
                else:
                    with self.session.open("a") as stream: stream.write('{"type":"custom","id":"late"}\n')
                self.release.touch()
                if change == "generation":
                    with self.assertRaises(RuntimeContractError): pending.result(timeout=5)
                    self.assertEqual(Frontend(self.store).get(record["conversation_id"])["status"], "starting")
                    # Reset only the isolated fixture to test the file race.
                    record.update(generation=self.owner["generation"], token_hash=hashlib.sha256(self.owner["owner_token"].encode()).hexdigest(), status="uncertain")
                    with self.store.transaction() as db: Frontend(self.store).save(db, record)
                else:
                    outcome = pending.result(timeout=5)
                    self.assertEqual(outcome["status"], "uncertain")
                    self.assertEqual(outcome["release_diagnostic"], "native_session_changed")
                    self.assertNotEqual(Frontend(self.store).get(self.owner["conversation_id"])["status"], "released")
