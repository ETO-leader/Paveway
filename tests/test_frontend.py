import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_launch_lock_conflict_does_not_change_released_owner(self):
        from oicoach_runtime.native import _run_once
        from oicoach_runtime.scheduler import Runtime
        from oicoach_runtime.workflow_setup import create
        create(self.config, "example", self.root, "example")
        runtime = Runtime(self.config)
        runtime.store = self.store
        lease = runtime.dispatch_api("frontend.begin", {"workflow_id": "example", "task_id": "training"})
        self.frontend.finish(**self.owner(lease), exit_code=0)
        original = self.frontend.get(lease["conversation_id"])
        directory = Path(lease["session_dir"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = InstanceLock(directory / "owner.lock")
        try:
            with patch("oicoach_runtime.native.request", side_effect=lambda _socket, command, args=None: runtime.dispatch_api(command, args or {})), \
                    patch("oicoach_runtime.native._run_owned") as launch:
                with self.assertRaises(ConflictError):
                    _run_once(self.config, self.root / "config.toml", "example", "training", lease["conversation_id"], False)
                launch.assert_not_called()
            self.assertEqual(self.frontend.get(lease["conversation_id"]), original)
        finally:
            lock.close()

    def test_valid_session_above_export_limit_can_release_and_recover(self):
        lease = self.begin()
        directory = Path(lease["session_dir"])
        directory.mkdir(parents=True, mode=0o700)
        path = directory / "large.jsonl"
        with path.open("w") as stream:
            stream.write(json.dumps({"type": "session", "id": "large", "cwd": str(self.root), "version": 3}) + "\n")
            for index in range(260):
                stream.write(json.dumps({"type": "message", "id": str(index), "message": {"role": "user", "content": "x" * 65536}}) + "\n")
        record = self.frontend.get(lease["conversation_id"])
        record.update(session_file=str(path), session_id="large", session_files=[{"session_file": str(path), "session_id": "large"}])
        with self.store.transaction() as db:
            self.frontend.save(db, record)
        self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
        self.assertEqual(self.frontend.finish(**self.owner(lease), exit_code=0)["status"], "released")
        resumed = self.begin(lease["conversation_id"])
        self.frontend.finish(**self.owner(resumed), exit_code=1)
        self.assertEqual(self.frontend.recover(lease["conversation_id"])["status"], "released")

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
            self.frontend.finish(**self.owner(lease), exit_code=0)
        self.assertEqual(self.frontend.get(newer["conversation_id"])["status"], "starting")
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

    def test_clean_exit_with_held_lock_is_uncertain_and_has_a_specific_diagnostic(self):
        lease = self.begin()
        directory = Path(lease["session_dir"])
        directory.mkdir(mode=0o700, parents=True)
        lock = InstanceLock(directory / "owner.lock")
        try:
            final = self.frontend.finish(**self.owner(lease), exit_code=0)
            self.assertEqual(final["status"], "uncertain")
            self.assertEqual(final["release_diagnostic"], "native_owner_lock_unavailable")
        finally:
            lock.close()

    def test_unsafe_or_invalid_session_is_not_released_or_recovered(self):
        cases = (("mismatch", "native_session_identity_mismatch"), ("truncated", "native_session_incomplete"),
                 ("missing", "native_session_missing"), ("symlink", "native_session_unsafe"),
                 ("fifo", "native_session_unsafe"))
        for case, code in cases:
            with self.subTest(case=case):
                lease = self.begin(new=True)
                directory = Path(lease["session_dir"])
                directory.mkdir(parents=True, mode=0o700)
                path = directory / "session.jsonl"
                header = json.dumps({"type": "session", "id": "good", "cwd": str(self.root), "version": 3}) + "\n"
                if case == "mismatch":
                    path.write_text(header.replace('"good"', '"wrong"'))
                elif case == "truncated":
                    path.write_text(header + '{"type":"message"')
                elif case == "symlink":
                    path.symlink_to(directory / "absent")
                elif case == "fifo":
                    os.mkfifo(path)
                record = self.frontend.get(lease["conversation_id"])
                record.update(session_file=str(path), session_id="good", session_required=True)
                with self.store.transaction() as db:
                    self.frontend.save(db, record)
                final = self.frontend.finish(**self.owner(lease), exit_code=0)
                self.assertEqual(final["status"], "uncertain")
                self.assertEqual(final["release_diagnostic"], code)
                with self.assertRaises(ConflictError) as error:
                    self.frontend.recover(lease["conversation_id"])
                self.assertEqual(error.exception.code, code)
                self.assertEqual(self.frontend.get(lease["conversation_id"])["session_file"], str(path))
