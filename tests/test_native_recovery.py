"""Owned-process failure tests against real OMP and an isolated local provider."""
import json
import subprocess
import sys
import threading
import time

from test_native_omp import NativeFixture
import test_native_management
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.frontend import alive
from oicoach_runtime.ipc import request


class NativeRecoveryTests(NativeFixture):
    open_ui = test_native_management.NativeManagementTests.open_ui

    def restart_daemon(self):
        self.stop_daemon()
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if request(self.config.server.socket_path, "status")["daemon"] == "running": return
            except RuntimeContractError:
                pass
            time.sleep(.03)
        self.fail("isolated daemon did not restart")

    def test_daemon_restart_never_takes_over_a_living_native_owner(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        before = request(self.config.server.socket_path, "frontend.list")[0]
        self.restart_daemon()
        record = request(self.config.server.socket_path, "frontend.show", {"conversation_id": before["conversation_id"]})
        self.assertEqual(record["status"], "uncertain")
        self.assertIsNone(process.poll())
        with self.assertRaises(RuntimeContractError):
            request(self.config.server.socket_path, "frontend.recover", {"conversation_id": record["conversation_id"]})
        with self.assertRaises(RuntimeContractError):
            request(self.config.server.socket_path, "frontend.begin", {"workflow_id": "native-test", "task_id": "training"})
        self.assertEqual(self.server.requests, [])
        exit_ui()
        self.assertEqual(request(self.config.server.socket_path, "frontend.show", {"conversation_id": record["conversation_id"]})["status"], "released")

    def test_parent_sigkill_mid_turn_requires_explicit_recovery_and_never_replays(self):
        # OMP may buffer a brand-new Session until its first assistant reply.
        # Establish a durable Session before interrupting a subsequent turn.
        established = self.chat("ESTABLISHED_NATIVE_SOURCE")
        entered, release = threading.Event(), threading.Event()
        def response(body):
            if "INTERRUPTED_NATIVE_TURN" in json.dumps(body.get("messages", [])) and "Identity rule:" in json.dumps(body):
                entered.set()
                release.wait(15)
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
        self.server.response_factory = response
        process, raw, send, until, pump, exit_ui = self.open_ui()
        self.addCleanup(release.set)
        send("INTERRUPTED_NATIVE_TURN\r")
        until("INTERRUPTED_NATIVE_TURN", entered.is_set)
        before = request(self.config.server.socket_path, "frontend.list")[0]
        self.assertEqual(before["status"], "active")
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(before.get(kind + "_pid"), before.get(kind + "_identity")) for kind in ("guardian", "omp")):
            pump(.05)
        self.assertFalse(any(alive(before.get(kind + "_pid"), before.get(kind + "_identity")) for kind in ("guardian", "omp")))
        release.set()
        self.restart_daemon()
        scope = {"conversation_id": before["conversation_id"]}
        self.assertEqual(request(self.config.server.socket_path, "frontend.show", scope)["status"], "uncertain")
        self.assertEqual(request(self.config.server.socket_path, "frontend.recover", scope)["status"], "released")
        count = len(self.server.requests)
        resumed, _, _, _, pump_resumed, exit_resumed = self.open_ui()
        for _ in range(5): pump_resumed(.1)
        self.assertEqual(len(self.server.requests), count, "ordinary resume must never restore/replay the interrupted prompt")
        exit_resumed()
        self.assertTrue(any("INTERRUPTED_NATIVE_TURN" in path.read_text() for path in (self.config.state_dir / "native").rglob("*.jsonl")))
        self.assertTrue(any("ESTABLISHED_NATIVE_SOURCE" in path.read_text() for path in (self.config.state_dir / "native").rglob("*.jsonl")))
