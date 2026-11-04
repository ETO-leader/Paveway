import fcntl
import json
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
    def open_ui(self, launcher=False, recover=False):
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
            pending = text if isinstance(text, bytes) else text.encode()
            deadline = time.monotonic() + 20
            while pending and time.monotonic() < deadline:
                pump(0)
                if select.select([], [master], [], .1)[1]:
                    try:
                        count = os.write(master, pending[:4096])
                        pending = pending[count:]
                    except BlockingIOError:
                        pass
            self.assertFalse(pending, "native fixture input did not drain")
        def exit_ui():
            send("/exit\r")
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                pump()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, raw.decode(errors="replace")[-2000:])
        if recover:
            until("Session recovery")
            send(b" \t\r")
        until("OMP native")
        return process, raw, send, until, pump, exit_ui

    def test_one_key_native_launcher_from_another_directory(self):
        original = self.path.read_text() + '\n[tijian]\nenabled = true\nexecutable = "/fixture/retired-cli-must-not-run"\n'
        self.path.write_text(original)
        process, raw, send, until, pump, exit_ui = self.open_ui(launcher=True)
        exit_ui()
        self.assertEqual(self.server.requests, [])
        self.assertIn("旧 [tijian] 配置已保留但不再执行".encode(), raw)
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")

    def test_due_inbox_notice_preserves_draft_and_does_not_ack_or_replay(self):
        def response(body):
            background = "UI_NOTICE_FIXTURE" in json.dumps(body["messages"][-1])
            return [({"role": "assistant", "content": "BACKGROUND_UI_REPLY_SENTINEL" if background else "M0_REPLY"}, "stop")]
        self.server.response_factory = response
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/wakeup in 1s UI_NOTICE_FIXTURE\r")
        until("native_wakeup")
        draft = "UNSENT_DRAFT_MUST_SURVIVE_NOTICE"
        send(draft)
        until("新的唤醒消息", lambda: len(request(self.config.server.socket_path, "inbox.list", {"workflow_id": "native-test"})["messages"]) >= 2)
        until("BACKGROUND_UI_REPLY_SENTINEL")
        self.assertIn("GPT · 后台定时任务回复".encode(), raw)
        self.assertFalse(any(draft in json.dumps(body["messages"]) for body in self.server.requests))
        send("\r")
        until("M0_REPLY", lambda: any(draft in json.dumps(body["messages"]) for body in self.server.requests) and
            request(self.config.server.socket_path, "frontend.list")[0]["status"] == "idle")
        self.assertFalse(any("新的唤醒消息" in json.dumps(body["messages"], ensure_ascii=False) for body in self.server.requests))
        main = [body for body in self.server.requests if draft in json.dumps(body["messages"]) and "Identity rule:" in json.dumps(body["messages"])]
        self.assertTrue(main)
        self.assertTrue(all("BACKGROUND_UI_REPLY_SENTINEL" not in json.dumps(body["messages"]) for body in main))
        send("/inbox\r")
        until("Paveway Inbox")
        send("\r")
        until("Acknowledge this message?")
        send(b"\x1b")
        for _ in range(3): pump(.1)
        exit_ui()
        rows = request(self.config.server.socket_path, "inbox.list", {"workflow_id": "native-test"})["messages"]
        self.assertEqual({row["kind"] for row in rows}, {"reminder", "result"})
        self.assertTrue(all(row["state"] != "acknowledged" for row in rows))
        process, raw, send, until, pump, exit_ui = self.open_ui()
        for _ in range(3): pump(.1)
        self.assertNotIn("新的唤醒消息".encode(), raw)
        self.assertNotIn(b"BACKGROUND_UI_REPLY_SENTINEL", raw)
        exit_ui()

    def test_desktop_notification_failure_is_visible_without_a_model_turn(self):
        from oicoach_runtime.notifications import configure
        configure(self.config, True, backend="linux")
        binary = self.root / "bin/notify-send"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 2\n")
        binary.chmod(0o700)
        self.env["PATH"] = str(binary.parent) + os.pathsep + self.env["PATH"]
        process, raw, send, until, pump, exit_ui = self.open_ui()
        send("/paveway notifications\r")
        until("定时任务桌面弹窗")
        send(b"\x1b[B\r")
        until("系统未接受桌面通知")
        self.assertEqual(self.server.requests, [])
        exit_ui()

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
        body = "PRIVATE_SKILL_UI_BODY\n" + "中文 Skill body line\n" * 1800
        self.assertGreater(len(body.encode()), 32768)
        self.assertLess(len(body.encode()), 65536)
        send(b"\x1b[200~" + body.encode() + b"\x1b[201~")
        send(b"\x11")
        until("Saved.", lambda: customization.show(self.config, "native-test")["settings"]["revision"] == 1)
        skill = customization.show(self.config, "native-test")["settings"]["skills"][0]
        self.assertEqual(skill["category"], "user-information")
        self.assertEqual(skill["content"], body)
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
