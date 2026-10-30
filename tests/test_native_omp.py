"""Opt-in real native OMP + loopback fixture; never a paid model request."""

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.workflow_setup import create


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_NATIVE") == "1" and shutil.which("omp"),
                     "set PAVEWAY_TEST_NATIVE=1 for isolated native OMP/PTY fixture tests")
class NativeFixture(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
        from verify_omp_m0 import Probe, Provider
        self.temp = tempfile.TemporaryDirectory(prefix="pw-native-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.env = Probe(self.root, shutil.which("omp"), self.server).env
        project = Path(__file__).resolve().parents[1]
        self.env["PYTHONPATH"] = str(project / "src")
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(self.path)
        self.config = replace(config, agent=replace(config.agent, executable=shutil.which("omp"),
            model="paveway-m0/probe", thinking="off", tools_enabled=False, skills_enabled=False,
            startup_timeout_seconds=15, graceful_stop_seconds=1))
        self.path.write_text(config_text(self.config))
        create(self.config, "native-test", self.root / "workspace", "example")
        self.log = (self.root / "daemon.log").open("w")
        self.addCleanup(self.log.close)
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            env=self.env, stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_daemon)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                request(self.config.server.socket_path, "status")
                break
            except RuntimeContractError:
                time.sleep(0.03)
        else:
            self.fail("isolated daemon did not start")

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def stop_daemon(self):
        if self.daemon.poll() is None:
            self.daemon.terminate()
            try:
                self.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait()

    def chat(self, body, background=False, switch=False, wakeup=False, workflow="native-test", new=False, evidence=lambda: True, wait_seconds=25):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "native", "--config", str(self.path),
            "--workflow", workflow, *(["--new"] if new else [])], env=self.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(master, False)
        output = bytearray()

        def send(data):
            pending = bytearray(data)
            deadline = time.monotonic() + 25
            while pending and time.monotonic() < deadline:
                readable, writable, _ = select.select([master], [master], [], 0.1)
                if readable:
                    chunk = os.read(master, 65536)
                    output.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        pending[:0] = b"\x1b[1;1R"
                if writable:
                    count = os.write(master, pending[:4096])
                    del pending[:count]
            self.assertFalse(pending, "fixture input was not consumed")

        def until(text, requests=0, predicate=lambda: True):
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if text.encode() in output and len(self.server.requests) >= requests and predicate():
                    return
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    output.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        os.write(master, b"\x1b[1;1R")
            tools = [message.get("content") for body in self.server.requests for message in body["messages"] if message.get("role") == "tool"]
            self.fail("native UI did not reach expected fixture state: " + output.decode(errors="replace")[-1800:] +
                "\nFixture tool evidence: " + json.dumps(tools, ensure_ascii=False)[-2000:])

        try:
            until("OMP native")
            if background:
                with self.assertRaises(RuntimeContractError):
                    request(self.config.server.socket_path, "frontend.begin", {"workflow_id": "native-test", "task_id": "training"})
                event = request(self.config.server.socket_path, "schedule", {"workflow_id": "native-test", "task_id": "training",
                    "reason": "independent background", "dedupe_key": "native-background", "at": "now"})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    state = request(self.config.server.socket_path, "events.show", {"event_id": event["event_id"]})
                    if state["status"] == "succeeded": break
                    time.sleep(0.05)
                self.assertEqual(state["status"], "succeeded")
                self.assertTrue(list((self.config.state_dir / "sessions").glob("*/*.jsonl")))
            baseline = len(self.server.requests)
            conversation = request(self.config.server.socket_path, "frontend.list")[0]["conversation_id"]
            send(b"\x1b[200~" + body.encode() + b"\x1b[201~\r")
            until("M0_REPLY", requests=baseline + 1, predicate=lambda:
                request(self.config.server.socket_path, "frontend.show", {"conversation_id": conversation})["status"] == "idle" and evidence())
            if wakeup:
                send(b"/wakeup in 1s TEST_NATIVE_TIMER\r")
                until("native_wakeup")
            if switch:
                before = request(self.config.server.socket_path, "frontend.list")[0]
                send(b"/new\r")
                until("OMP native", predicate=lambda: request(self.config.server.socket_path, "frontend.list")[0]["generation"] > before["generation"])
                baseline = len(self.server.requests)
                send(b"AFTER_NATIVE_NEW\r")
                until("M0_REPLY", requests=baseline + 1)
            send(b"/exit\r")
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try: output.extend(os.read(master, 65536))
                    except OSError: break
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, output.decode(errors="replace")[-2000:])
            archives = list((self.config.state_dir / "native").rglob("*.jsonl"))
            self.assertTrue(any(body in path.read_text() for path in archives))
            self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")
            self.assertNotIn(b"paveway_context_ready", output)
            records = request(self.config.server.socket_path, "frontend.list")
            self.assertEqual(records[0]["status"], "released")
            self.assertNotIn("token_hash", records[0])
            return records[0]
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            os.close(master)



class NativeOmpTests(NativeFixture):
    def test_native_chat_preserves_long_input_and_exit_keeps_daemon(self):
        body = "NATIVE_HEAD " + "漢字 filler " * 6000 + " NATIVE_MIDDLE " + "漢字 filler " * 6000 + " NATIVE_TAIL"
        self.chat(body, background=True)
        self.assertTrue(any("NATIVE_MIDDLE" in json.dumps(row) for row in self.server.requests))

    def test_native_resume_keeps_identity_and_readonly_archive(self):
        first = self.chat("FIRST_NATIVE_MESSAGE")
        second = self.chat("SECOND_NATIVE_MESSAGE")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertGreater(second["generation"], first["generation"])
        archive = request(self.config.server.socket_path, "frontend.messages", {"conversation_id": second["conversation_id"]})
        self.assertTrue(any("FIRST_NATIVE_MESSAGE" in row["text"] for row in archive["messages"]))
        self.assertTrue(any("SECOND_NATIVE_MESSAGE" in row["text"] for row in archive["messages"]))
        from oicoach_runtime import git_sync
        self.assertEqual(git_sync.cycle(self.config)["state"], "ok")
        repository = Path(git_sync.settings(self.config)["repository"])
        exports = [json.loads(path.read_text()) for path in repository.glob("devices/*/sessions/*.json")]
        native = [row for row in exports if row.get("source") == "omp_native_session"]
        self.assertEqual(len(native), 1)
        text = json.dumps(native)
        self.assertIn("FIRST_NATIVE_MESSAGE", text)
        self.assertIn("SECOND_NATIVE_MESSAGE", text)
        self.assertNotIn("owner_token", text)
        self.assertNotIn("token_hash", text)
        remote = self.root / "fixture-user-data.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
        git_sync.set_remote(self.config, str(remote))
        self.assertEqual(git_sync.cycle(self.config)["state"], "ok")
        other = self.root / "second-device"
        other.mkdir(mode=0o700)
        path = other / "config.toml"
        initialize(path, str(other / "state"), str(other / "workflows"))
        config = load_config(path)
        create(config, "native-test", other, "example")
        git_sync.configure(config, other / "user-data", str(remote))
        self.assertEqual(git_sync.cycle(config)["state"], "ok")
        page = git_sync.sessions(config, workflow="native-test")
        self.assertEqual(len(page["records"]), 1)
        found = page["records"][0]
        self.assertEqual(found["source"], "omp_native_session")
        saved = git_sync.messages(config, found["device_id"], found["session"])
        self.assertIn("FIRST_NATIVE_MESSAGE", json.dumps(saved))
        self.assertIn("SECOND_NATIVE_MESSAGE", json.dumps(saved))

    def test_native_new_session_keeps_both_sources_and_fences_old_generation(self):
        record = self.chat("BEFORE_NATIVE_NEW", switch=True)
        self.assertEqual(record["session_count"], 2)
        archive = request(self.config.server.socket_path, "frontend.messages", {"conversation_id": record["conversation_id"]})
        self.assertTrue(any("BEFORE_NATIVE_NEW" in row["text"] for row in archive["messages"]))
        self.assertTrue(any("AFTER_NATIVE_NEW" in row["text"] for row in archive["messages"]))

    def test_slash_reminder_runs_after_ui_exit_and_is_persisted_without_seen_claim(self):
        self.chat("REMINDER_FIXTURE_CHAT", wakeup=True)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            inbox = request(self.config.server.socket_path, "inbox.list", {"workflow_id": "native-test"})
            if len(inbox["messages"]) >= 2:
                break
            time.sleep(0.05)
        self.assertEqual({row["kind"] for row in inbox["messages"]}, {"reminder", "result"})
        self.assertTrue(all(row["state"] != "acknowledged" for row in inbox["messages"]))
        reminders = [row for row in inbox["messages"] if row["kind"] == "reminder"]
        self.assertEqual(reminders[0]["text"], "TEST_NATIVE_TIMER")

    def test_real_native_agent_tool_schedules_in_the_same_runtime(self):
        self.config = replace(self.config, agent=replace(self.config.agent, tools_enabled=True))
        self.path.write_text(config_text(self.config))
        arguments = {"when": {"kind": "after_now", "seconds": 3600}, "reason": "NATIVE_TOOL_REMINDER",
            "dedupe_key": "native-fixture-tool", "intent": "reminder", "authorized_by_user": True}
        def response(body):
            if body.get("messages", [{}])[-1].get("role") == "user" and "M3_AGENT_WAKEUP" in json.dumps(body["messages"][-1]):
                return [({"role": "assistant", "tool_calls": [{"index": 0, "id": "call-native-wakeup", "type": "function",
                    "function": {"name": "paveway_wakeup_schedule", "arguments": json.dumps(arguments)}}]}, "tool_calls")]
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
        self.server.response_factory = response
        self.chat("M3_AGENT_WAKEUP explicit request")
        events = request(self.config.server.socket_path, "events.upcoming", {"workflow_id": "native-test", "task_id": "training"})
        self.assertEqual(len(events), 1)
        event = request(self.config.server.socket_path, "events.show", {"event_id": events[0]["event_id"]})
        self.assertEqual(event["payload"]["message"], "NATIVE_TOOL_REMINDER")
        self.assertEqual(event["status"], "queued")
        self.assertTrue(any(any(message.get("role") == "tool" for message in body["messages"]) for body in self.server.requests))

    def test_two_persona_profiles_reach_native_model_without_identity_leakage(self):
        from oicoach_runtime.coaches import create
        a = create(self.config, "alpha", "native-test", "ALPHA_NATIVE_PERSONA")
        b = create(self.config, "beta", "native-test", "BETA_NATIVE_PERSONA")
        def main_request(marker):
            # Native auxiliary title requests use a different system prompt.
            # Identify the main GPT request by our own Core, not provider timing
            # or exact OMP user-message wrapper wording.
            return next(row for row in reversed(self.server.requests)
                        if marker in json.dumps(row["messages"]) and "Identity rule:" in json.dumps(row["messages"]))
        first = self.chat("ALPHA_CHAT", workflow=a["runtime_workflow_id"])
        wire = json.dumps(main_request("ALPHA_CHAT"))
        self.assertIn("ALPHA_NATIVE_PERSONA", wire)
        self.assertNotIn("BETA_NATIVE_PERSONA", wire)
        self.assertNotIn("You are omp's trusted coding assistant", wire)
        second = self.chat("BETA_CHAT", workflow=b["runtime_workflow_id"])
        wire = json.dumps(main_request("BETA_CHAT"))
        self.assertIn("BETA_NATIVE_PERSONA", wire)
        self.assertNotIn("ALPHA_NATIVE_PERSONA", wire)
        self.assertNotEqual(first["session_id"], second["session_id"])
