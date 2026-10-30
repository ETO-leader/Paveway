import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.processes import InstanceLock
from oicoach_runtime.store import Store


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-frontend-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        self.store = Store(self.config)
        self.addCleanup(self.store.close)
        self.frontend = Frontend(self.store)

    def begin(self, conversation_id=None, **extra):
        return self.frontend.begin("example", "training", self.root, conversation_id, **extra)

    @staticmethod
    def owner(lease):
        return {key: lease[key] for key in ("conversation_id", "generation", "owner_token")}

    def test_duplicate_owner_generation_and_scope_are_fenced(self):
        lease = self.begin()
        with self.assertRaises(ConflictError):
            self.begin(lease["conversation_id"])
        with self.assertRaises(ConflictError):
            self.frontend.observe(**{**self.owner(lease), "generation": 0})
        with self.assertRaises(ConflictError):
            self.frontend.observe(**{**self.owner(lease), "owner_token": "0" * 64})
        self.frontend.finish(**self.owner(lease), exit_code=0)
        newer = self.begin(lease["conversation_id"])
        self.assertEqual(newer["generation"], 2)
        self.assertNotEqual(newer["owner_token"], lease["owner_token"])
        with self.assertRaises(ConflictError):
            self.frontend.observe(**self.owner(lease))
        with self.assertRaises(ConflictError):
            self.frontend.begin("other", "training", self.root, lease["conversation_id"])
        self.assertNotIn("token_hash", self.frontend.public(self.frontend.get(newer["conversation_id"])))

    def test_heartbeat_and_loop_end_do_not_release_or_settle_turn(self):
        lease = self.begin()
        owner = self.owner(lease)
        self.frontend.observe(**owner, phase="agent_start")
        observed = self.frontend.observe(**owner, phase="agent_end")
        self.assertEqual(observed["turn_evidence"], "observed_loop_only")
        self.assertEqual(observed["status"], "idle")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        with self.assertRaises(ConflictError):
            self.begin(lease["conversation_id"])
        self.frontend.reconcile()
        self.assertEqual(self.frontend.get(lease["conversation_id"])["status"], "uncertain")
        with self.assertRaises(ConflictError):
            self.frontend.observe(**owner)

    def test_live_process_and_inherited_lock_block_recovery(self):
        lease = self.begin()
        owner = self.owner(lease)
        directory = Path(lease["session_dir"])
        directory.mkdir(mode=0o700, parents=True)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.frontend.started(**owner, guardian_pid=os.getpid())
            path = directory / "session.jsonl"
            path.write_text(json.dumps({"type": "session", "id": "session", "cwd": str(self.root), "version": 3}) + "\n" +
                json.dumps({"type": "message", "id": "entry", "parentId": None,
                            "message": {"role": "user", "content": "PRESERVE_NATIVE_SOURCE"}}) + "\n")
            self.frontend.attest(**owner, omp_pid=child.pid, session_id="session", session_file=str(path))
            self.frontend.reconcile()
            with self.assertRaises(ConflictError):
                self.frontend.recover(lease["conversation_id"])
            # A heartbeat timeout is never substituted for proof of process death.
            self.assertEqual(self.frontend.get(lease["conversation_id"])["status"], "uncertain")
        finally:
            child.terminate()
            child.wait()

    def test_held_lock_blocks_recovery_without_replaying(self):
        lease = self.begin()
        owner = self.owner(lease)
        directory = Path(lease["session_dir"])
        directory.mkdir(mode=0o700, parents=True)
        lock = InstanceLock(directory / "owner.lock")
        self.frontend.finish(**owner, exit_code=1)
        try:
            with self.assertRaises(ConflictError):
                self.frontend.recover(lease["conversation_id"])
        finally:
            lock.close()
        recovered = self.frontend.recover(lease["conversation_id"])
        self.assertEqual(recovered["status"], "released")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
