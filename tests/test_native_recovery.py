"""Owned-process failure tests against real OMP and an isolated local provider."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from test_native_omp import NativeFixture
import test_native_management
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.frontend import alive
from oicoach_runtime.ipc import request
from oicoach_runtime.processes import InstanceLock


class NativeRecoveryTests(NativeFixture):
    open_ui = test_native_management.NativeManagementTests.open_ui

    def test_startup_recovery_dialog_resumes_without_replaying_a_prompt(self):
        saved = self.chat("PRESERVE_RECOVERY_DIALOG_SOURCE")
        process, raw, send, until, pump, exit_ui = self.open_ui()
        owner = request(self.config.server.socket_path, "frontend.list")[0]
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(owner.get(k + "_pid"), owner.get(k + "_identity")) for k in ("guardian", "omp")):
            time.sleep(.03)
        self.restart_daemon()
        count = len(self.server.requests)
        resumed, _, _, _, _, exit_resumed = self.open_ui(recover=True)
        current = request(self.config.server.socket_path, "frontend.list")[0]
        self.assertEqual(current["conversation_id"], saved["conversation_id"])
        self.assertEqual(current["session_id"], saved["session_id"])
        self.assertEqual(len(self.server.requests), count)
        exit_resumed()
    def persisted_owner(self, conversation_id):
        with closing(sqlite3.connect(f"file:{self.config.database_path}?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT value FROM runtime_metadata WHERE key = ?",
                ("frontend:v1:" + conversation_id,)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def wait_for_ui_exit(self, process, raw, pump):
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            pump()
        process.wait(timeout=5)
        # Drain the final diagnostic bytes even when process.poll() observed exit first.
        pump(0)
        return raw.decode(errors="replace")

    def assert_owner_stopped(self, record):
        for kind in ("guardian", "omp"):
            self.assertFalse(alive(record.get(kind + "_pid"), record.get(kind + "_identity")),
                kind + " survived native exit")
        lock = InstanceLock(Path(record["session_dir"]) / "owner.lock")
        lock.close()

    def assert_persisted_release(self, before):
        record = self.persisted_owner(before["conversation_id"])
        self.assertEqual(record["status"], "released", record)
        self.assertEqual(record.get("exit_code"), 0, record)
        self.assert_owner_stopped(before)
        return record


    def normal_exit_resume_cycle(self, exit_input):
        first = None
        previous = None
        for attempt in range(3):
            if attempt == 2:
                self.restart_daemon()
                self.assert_persisted_release(previous)
            process, raw, send, until, pump, _exit_ui = self.open_ui(launcher=True)
            before = request(self.config.server.socket_path, "frontend.list")[0]
            if first is None:
                send("DURABLE_NORMAL_EXIT_SOURCE\r")
                until("M0_REPLY", lambda: request(self.config.server.socket_path, "frontend.show",
                    {"conversation_id": before["conversation_id"]})["status"] == "idle")
                self.assertEqual(len(self.prompt_requests("DURABLE_NORMAL_EXIT_SOURCE")), 1)
                before = request(self.config.server.socket_path, "frontend.show",
                    {"conversation_id": before["conversation_id"]})
                self.assertTrue(Path(before["session_file"]).is_file())
                first = before
            else:
                self.assertEqual(before["conversation_id"], first["conversation_id"])
                self.assertEqual(before["session_id"], first["session_id"])
                self.assertGreater(before["generation"], previous["generation"])
                self.assertEqual(len(self.prompt_requests("DURABLE_NORMAL_EXIT_SOURCE")), request_count,
                    "ordinary resume must not replay a completed prompt")
                archive = request(self.config.server.socket_path, "frontend.messages",
                    {"conversation_id": before["conversation_id"]})
                self.assertEqual(sum(row["role"] == "user" and "DURABLE_NORMAL_EXIT_SOURCE" in row["text"]
                    for row in archive["messages"]), 1, archive)
            request_count = len(self.prompt_requests("DURABLE_NORMAL_EXIT_SOURCE"))
            send(exit_input)
            output = self.wait_for_ui_exit(process, raw, pump)
            self.assertEqual(process.returncode, 0, output[-3000:])
            previous = self.assert_persisted_release(before)
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")

    def test_ctrl_d_persists_release_across_immediate_resume_and_daemon_restart(self):
        self.normal_exit_resume_cycle(b"\x04")

    def test_slash_exit_persists_release_across_immediate_resume_and_daemon_restart(self):
        self.normal_exit_resume_cycle("/exit\r")

    def test_launcher_new_preserves_history_and_conversation_resumes_selected_session(self):
        process, raw, send, until, pump, _exit_ui = self.open_ui(launcher=True)
        original = request(self.config.server.socket_path, "frontend.list")[0]
        original_scope = {"conversation_id": original["conversation_id"]}
        send("LAUNCHER_ORIGINAL_HISTORY\r")
        until("M0_REPLY", lambda: request(self.config.server.socket_path,
            "frontend.show", original_scope)["status"] == "idle")
        original = request(self.config.server.socket_path, "frontend.show", original_scope)
        send(b"\x04")
        output = self.wait_for_ui_exit(process, raw, pump)
        self.assertEqual(process.returncode, 0, output[-3000:])
        self.assert_persisted_release(original)
        source = Path(original["session_file"])
        original_source = source.read_bytes()

        process, raw, send, until, pump, _exit_ui = self.open_ui(launcher=True, args=("--new",))
        records = request(self.config.server.socket_path, "frontend.list")
        self.assertEqual(len(records), 2, records)
        created = next(row for row in records if row["conversation_id"] != original["conversation_id"])
        created_scope = {"conversation_id": created["conversation_id"]}
        self.assertNotEqual(created["session_id"], original["session_id"])
        send("LAUNCHER_NEW_HISTORY\r")
        until("M0_REPLY", lambda: request(self.config.server.socket_path,
            "frontend.show", created_scope)["status"] == "idle")
        created = request(self.config.server.socket_path, "frontend.show", created_scope)
        send("/exit\r")
        output = self.wait_for_ui_exit(process, raw, pump)
        self.assertEqual(process.returncode, 0, output[-3000:])
        self.assert_persisted_release(created)
        self.assertEqual(source.read_bytes(), original_source,
            "--new must preserve the previous conversation's original source")
        history = request(self.config.server.socket_path, "frontend.messages", original_scope)
        self.assertTrue(any("LAUNCHER_ORIGINAL_HISTORY" in row["text"] for row in history["messages"]), history)
        self.assertFalse(any("LAUNCHER_NEW_HISTORY" in row["text"] for row in history["messages"]), history)

        process, raw, send, until, pump, _exit_ui = self.open_ui(launcher=True,
            args=("--conversation", original["conversation_id"]))
        resumed = request(self.config.server.socket_path, "frontend.show", original_scope)
        self.assertEqual(resumed["status"], "idle", resumed)
        self.assertEqual(resumed["session_id"], original["session_id"])
        self.assertGreater(resumed["generation"], original["generation"])
        self.assertEqual(request(self.config.server.socket_path, "frontend.show", created_scope)["status"], "released")
        response_offset = len(raw)
        send("LAUNCHER_EXPLICIT_RESUME_HISTORY\r")
        until("M0_REPLY", lambda: b"M0_REPLY" in raw[response_offset:] and
            bool(self.prompt_requests("LAUNCHER_EXPLICIT_RESUME_HISTORY")) and
            request(self.config.server.socket_path, "frontend.show", original_scope)["status"] == "idle")
        resumed = request(self.config.server.socket_path, "frontend.show", original_scope)
        send(b"\x04")
        output = self.wait_for_ui_exit(process, raw, pump)
        self.assertEqual(process.returncode, 0, output[-3000:])
        self.assert_persisted_release(resumed)
        history = request(self.config.server.socket_path, "frontend.messages", original_scope)
        for marker in ("LAUNCHER_ORIGINAL_HISTORY", "LAUNCHER_EXPLICIT_RESUME_HISTORY"):
            self.assertTrue(any(marker in row["text"] for row in history["messages"]), history)
        self.assertFalse(any("LAUNCHER_NEW_HISTORY" in row["text"] for row in history["messages"]), history)
        created_history = request(self.config.server.socket_path, "frontend.messages", created_scope)
        self.assertTrue(any("LAUNCHER_NEW_HISTORY" in row["text"] for row in created_history["messages"]), created_history)
        self.assertFalse(any("LAUNCHER_EXPLICIT_RESUME_HISTORY" in row["text"]
            for row in created_history["messages"]), created_history)

    def test_ctrl_d_releases_current_generation_after_native_new(self):
        process, raw, send, until, pump, _exit_ui = self.open_ui()
        before = request(self.config.server.socket_path, "frontend.list")[0]
        send("DURABLE_BEFORE_NATIVE_NEW\r")
        until("M0_REPLY", lambda: request(self.config.server.socket_path, "frontend.show",
            {"conversation_id": before["conversation_id"]})["status"] == "idle")
        before = request(self.config.server.socket_path, "frontend.show",
            {"conversation_id": before["conversation_id"]})
        switch_offset = len(raw)
        send("/new\r")
        until("PAVEWAY", lambda: b"PAVEWAY" in raw[switch_offset:] and
            request(self.config.server.socket_path, "frontend.show",
                {"conversation_id": before["conversation_id"]})["generation"] > before["generation"])
        response_offset = len(raw)
        send("DURABLE_AFTER_NATIVE_NEW\r")
        until("M0_REPLY", lambda: b"M0_REPLY" in raw[response_offset:] and
            any("DURABLE_AFTER_NATIVE_NEW" in json.dumps(body.get("messages", []))
                for body in self.server.requests) and
            request(self.config.server.socket_path, "frontend.show",
                {"conversation_id": before["conversation_id"]})["status"] == "idle")
        current = request(self.config.server.socket_path, "frontend.show",
            {"conversation_id": before["conversation_id"]})
        self.assertNotEqual(current["session_id"], before["session_id"])
        self.assertGreater(current["generation"], before["generation"])
        send(b"\x04")
        output = self.wait_for_ui_exit(process, raw, pump)
        self.assertEqual(process.returncode, 0, output[-3000:])
        released = self.assert_persisted_release(current)
        self.assertEqual(released["generation"], current["generation"])
        history = request(self.config.server.socket_path, "frontend.messages",
            {"conversation_id": before["conversation_id"]})
        for marker in ("DURABLE_BEFORE_NATIVE_NEW", "DURABLE_AFTER_NATIVE_NEW"):
            self.assertEqual(sum(row["role"] == "user" and marker in row["text"]
                for row in history["messages"]), 1, history)
        self.restart_daemon()
        self.assert_persisted_release(current)

    def test_runtime_unavailable_at_ctrl_d_reports_failure_without_fake_release(self):
        self.chat("DURABLE_RUNTIME_OUTAGE_SOURCE")
        process, raw, send, _until, pump, _exit_ui = self.open_ui()
        before = request(self.config.server.socket_path, "frontend.list")[0]
        request_count = len(self.prompt_requests("DURABLE_RUNTIME_OUTAGE_SOURCE"))
        self.stop_daemon()
        diagnostic_offset = len(raw)
        send(b"\x04")
        output = self.wait_for_ui_exit(process, raw, pump)
        self.assertNotEqual(process.returncode, 0, output[-3000:])
        saved = self.persisted_owner(before["conversation_id"])
        self.assertNotEqual(saved["status"], "released", saved)
        self.assertNotEqual(saved.get("exit_code"), 0, saved)
        self.assert_owner_stopped(before)
        self.restart_daemon()
        scope = {"conversation_id": before["conversation_id"]}
        self.assertEqual(request(self.config.server.socket_path, "frontend.show", scope)["status"], "uncertain")
        with self.assertRaises(RuntimeContractError):
            request(self.config.server.socket_path, "frontend.begin", {"workflow_id": "native-test", "task_id": "training"})
        self.assertEqual(len(self.prompt_requests("DURABLE_RUNTIME_OUTAGE_SOURCE")), request_count)
        diagnostic = raw[diagnostic_offset:].decode(errors="replace").lower()
        self.assertIn("native_finish_failed", diagnostic)
        self.assertIn("daemon_unavailable", diagnostic)

    def interrupted_owner_stays_uncertain(self, kind):
        self.chat("DURABLE_ABNORMAL_EXIT_SOURCE")
        process, raw, _send, _until, pump, _exit_ui = self.open_ui()
        before = request(self.config.server.socket_path, "frontend.list")[0]
        request_count = len(self.prompt_requests("DURABLE_ABNORMAL_EXIT_SOURCE"))
        self.assertTrue(alive(before[kind + "_pid"], before[kind + "_identity"]))
        os.kill(before[kind + "_pid"], signal.SIGKILL)
        output = self.wait_for_ui_exit(process, raw, pump)
        self.assertNotEqual(process.returncode, 0, output[-3000:])
        saved = self.persisted_owner(before["conversation_id"])
        self.assertEqual(saved["status"], "uncertain", saved)
        self.assertNotEqual(saved.get("exit_code"), 0, saved)
        self.assert_owner_stopped(before)
        self.restart_daemon()
        self.assertEqual(self.persisted_owner(before["conversation_id"])["status"], "uncertain")
        with self.assertRaises(RuntimeContractError):
            request(self.config.server.socket_path, "frontend.begin", {"workflow_id": "native-test", "task_id": "training"})
        self.assertEqual(len(self.prompt_requests("DURABLE_ABNORMAL_EXIT_SOURCE")), request_count)

    def test_killed_omp_is_uncertain_not_a_normal_release(self):
        self.interrupted_owner_stays_uncertain("omp")

    def test_killed_guardian_is_uncertain_not_a_normal_release(self):
        self.interrupted_owner_stays_uncertain("guardian")


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
            if self.is_prompt_request(body, "INTERRUPTED_NATIVE_TURN"):
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
        count = len(self.prompt_requests("INTERRUPTED_NATIVE_TURN"))
        resumed, _, _, _, pump_resumed, exit_resumed = self.open_ui()
        for _ in range(5): pump_resumed(.1)
        self.assertEqual(len(self.prompt_requests("INTERRUPTED_NATIVE_TURN")), count,
            "ordinary resume must never restore/replay the interrupted prompt")
        exit_resumed()
        self.assertTrue(any("INTERRUPTED_NATIVE_TURN" in path.read_text() for path in (self.config.state_dir / "native").rglob("*.jsonl")))
        self.assertTrue(any("ESTABLISHED_NATIVE_SOURCE" in path.read_text() for path in (self.config.state_dir / "native").rglob("*.jsonl")))
