import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from test_native_omp import NativeFixture
from oicoach_runtime import coaches, customization
from oicoach_runtime.ipc import request


class NativeManagementTests(NativeFixture):
    def open_ui(self, launcher=False):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        argv = [sys.executable, "-m", "oicoach_runtime", "native"]
        if launcher:
            argv = [str(Path(__file__).resolve().parents[1] / "paveway.sh")]
        process = subprocess.Popen([*argv, "--config", str(self.path), "--workflow", "native-test"],
            env={**self.env, "PAVEWAY_PYTHON": sys.executable}, cwd=self.root, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(master, False)
        raw = bytearray()
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            os.close(master)
        self.addCleanup(cleanup)
        def pump(timeout=0.1):
            if select.select([master], [], [], timeout)[0]:
                try: chunk = os.read(master, 65536)
                except OSError: return
                raw.extend(chunk)
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
        def until(text, predicate=lambda: True):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if text.encode() in raw and predicate(): return
                pump()
            self.fail("Native management did not reach state: " + raw.decode(errors="replace")[-3000:])
        def send(text):
            os.write(master, text if isinstance(text, bytes) else text.encode())
        def exit_ui():
            send("/exit\r")
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                pump()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, raw.decode(errors="replace")[-2000:])
        until("OMP native")
        return process, raw, send, until, pump, exit_ui

    def test_one_key_native_launcher_from_another_directory(self):
        process, raw, send, until, pump, exit_ui = self.open_ui(launcher=True)
        exit_ui()
        self.assertEqual(self.server.requests, [])
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")

    def test_prompt_dialog_saves_privately_without_a_model_turn(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway prompt\r")
        until("append")
        send(b"\x1b[B\r")
        until("Custom user system prompt")
        send("PRIVATE_PROMPT_UI_MARKER")
        send(b"\x11")  # OMP hook editor's native submit binding.
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 1)
        self.assertEqual(customization.show(self.config, "native-test")["settings"]["system_prompt"]["text"], "PRIVATE_PROMPT_UI_MARKER")
        exit_ui()
        self.assertEqual(self.server.requests, [])
        for path in (self.config.state_dir / "native").rglob("*.jsonl"):
            self.assertNotIn("PRIVATE_PROMPT_UI_MARKER", path.read_text())

    def test_vjudge_secret_dialog_masks_input_and_cancel_does_not_save(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway vjudge\r")
        until("VJudge login")
        send(b"\x1b[B\x1b[B\r")
        until("VJudge username")
        send("fixture-user\r")
        until("VJudge password")
        send("NOT_REAL_SECRET_8274")
        for _ in range(5): pump(0.1)
        self.assertNotIn(b"NOT_REAL_SECRET_8274", raw)
        send(b"\x1b")
        for _ in range(5): pump(0.1)
        exit_ui()
        self.assertEqual(self.server.requests, [])
        for path in self.root.rglob("*"):
            if path.is_file() and path.stat().st_size < 32 * 1024 * 1024:
                self.assertNotIn(b"NOT_REAL_SECRET_8274", path.read_bytes(), str(path))

    def test_skill_dialog_preserves_category_and_body_without_chat_submission(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway skills\r")
        until("Create Skill")
        send("\r")
        until("Skill name")
        send("private-ui-skill\r")
        until("Skill category")
        send(b"\x1b[B\r")
        until("When should GPT read this Skill?")
        send(b"Native skill description\x11")
        until("Skill body")
        send(b"PRIVATE_SKILL_UI_BODY\x11")
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 1)
        skill = customization.show(self.config, "native-test")["settings"]["skills"][0]
        self.assertEqual(skill["category"], "user-information")
        self.assertEqual(skill["content"], "PRIVATE_SKILL_UI_BODY")
        exit_ui()
        self.assertEqual(self.server.requests, [])
        for path in (self.config.state_dir / "native").rglob("*.jsonl"):
            self.assertNotIn("PRIVATE_SKILL_UI_BODY", path.read_text())

    def test_profile_switch_releases_old_process_before_new_scope(self):
        profile = coaches.create(self.config, "beta", "native-test", "BETA_MANAGEMENT_PERSONA")
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway profile\r")
        until("Switch profile")
        send("\r")
        until("Select GPT profile")
        index = [row["coach_id"] for row in coaches.profiles(self.config)].index("beta")
        send(b"\x1b[B" * index + b"\r")
        until("beta/training", lambda: any(row["workflow_id"] == profile["runtime_workflow_id"] and row["status"] == "idle"
            for row in request(self.config.server.socket_path, "frontend.list")))
        rows = request(self.config.server.socket_path, "frontend.list")
        self.assertEqual(next(row for row in rows if row["workflow_id"] == "native-test")["status"], "released")
        self.assertIsNone(process.poll())
        exit_ui()

    def test_legacy_management_roundtrip_preserves_native_session_and_daemon(self):
        process, raw, send, until, pump, exit_ui = self.open_ui()
        before = request(self.config.server.socket_path, "frontend.list")[0]
        send("/paveway legacy\r")
        until("Enter 发送")
        record = request(self.config.server.socket_path, "frontend.show", {"conversation_id": before["conversation_id"]})
        self.assertEqual(record["status"], "released")
        send("/quit\r")
        until("OMP native", lambda: request(self.config.server.socket_path, "frontend.show",
            {"conversation_id": before["conversation_id"]})["generation"] > before["generation"] and
            request(self.config.server.socket_path, "frontend.show", {"conversation_id": before["conversation_id"]})["status"] == "idle")
        exit_ui()
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")
