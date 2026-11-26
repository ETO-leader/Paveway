"""Opt-in real native OMP + loopback fixture; never a paid model request."""

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import shlex
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
        if cache := os.environ.get("PAVEWAY_TEST_NATIVE_CACHE"):
            # Share only OMP's officially versioned addon extraction directory.
            # Each fixture still owns its HOME, auth, settings and sessions.
            directory = Path(cache).resolve()
            if not directory.is_relative_to(Path(tempfile.gettempdir()).resolve()) or directory == Path(tempfile.gettempdir()).resolve():
                self.fail("PAVEWAY_TEST_NATIVE_CACHE must be inside the private test TMPDIR")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
                self.fail("PAVEWAY_TEST_NATIVE_CACHE must be a private directory")
            self.env["PI_NATIVES_DIR"] = str(directory)
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

    @staticmethod
    def is_prompt_request(body, text):
        """Match a submitted turn, not auxiliary requests carrying its history."""
        messages = [message for message in body.get("messages", []) if not (
            isinstance(message.get("content"), str) and message["content"].startswith("Runtime Memory Context —"))]
        if not messages or messages[-1].get("role") != "user":
            return False
        content = messages[-1].get("content")
        if isinstance(content, str):
            return content == text
        return isinstance(content, list) and any(
            part.get("type") == "text" and part.get("text") == text
            for part in content if isinstance(part, dict))

    def prompt_requests(self, text):
        return [body for body in self.server.requests if self.is_prompt_request(body, text)]

    def chat(self, body, background=False, switch=False, wakeup=False, workflow="native-test", new=False, evidence=lambda: True, wait_seconds=25):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "native", "--config", str(self.path),
            "--workflow", workflow, *(["--new"] if new else [])], env=self.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.set_blocking(master, False)
        output = bytearray()
        import pyte
        from terminal_screen import WideScreen
        screen = WideScreen(120, 40)
        stream = pyte.ByteStream(screen)

        def received(chunk):
            start = max(0, len(output) - 3)
            output.extend(chunk)
            stream.feed(chunk)
            return output.find(b"\x1b[6n", start) != -1

        def send(data):
            pending = bytearray(data)
            deadline = time.monotonic() + 25
            while pending and time.monotonic() < deadline:
                readable, writable, _ = select.select([master], [master], [], 0.1)
                if readable:
                    chunk = os.read(master, 65536)
                    if received(chunk):
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
                    if received(chunk):
                        os.write(master, b"\x1b[1;1R")
            tools = [message.get("content") for body in self.server.requests for message in body["messages"] if message.get("role") == "tool"]
            self.fail("native UI did not reach expected fixture state: " + output.decode(errors="replace")[-1800:] +
                "\nFixture tool evidence: " + json.dumps(tools, ensure_ascii=False)[-2000:])

        try:
            until("", predicate=lambda: any("PAVEWAY " in row for row in screen.display) and any(
                row["workflow_id"] == workflow and row["status"] == "idle" and row["session_id"]
                for row in request(self.config.server.socket_path, "frontend.list")))
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
            send(b"\x1b[200~" + body.encode() + b"\x1b[201~")
            # Wait for the native editor to process the paste before pressing
            # Enter; an attested process can still be repainting its first view.
            tail = body.rstrip().splitlines()[-1][-8:]
            until("", predicate=lambda: any(tail in row or "📄 #1" in row for row in screen.display))
            send(b"\r")
            until("M0_REPLY", requests=baseline + 1, predicate=lambda:
                request(self.config.server.socket_path, "frontend.show", {"conversation_id": conversation})["status"] == "idle" and evidence())
            if wakeup:
                send(b"/reminder in 1s TEST_NATIVE_TIMER\r")
                until("native_wakeup")
            if switch:
                before = request(self.config.server.socket_path, "frontend.list")[0]
                send(b"/new\r")
                until("PAVEWAY", predicate=lambda: request(self.config.server.socket_path, "frontend.list")[0]["generation"] > before["generation"])
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

    def test_frontend_and_worker_keep_same_selected_installation_after_alias_changes(self):
        from unittest.mock import patch
        from oicoach_runtime.launcher import ensure_runtime

        worker = self.root / "worker-omp"
        invocations = self.root / "worker-invocations"
        worker.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            "  printf 'omp/99.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            f'printf "%s\\n" "$@" >> {shlex.quote(str(invocations))}\n'
            f'exec {shlex.quote(self.config.agent.executable)} "$@"\n')
        worker.chmod(0o700)
        selected = self.root / "omp-selected"
        selected.symlink_to(worker)
        saved_config = self.path.read_text()
        with patch.dict(os.environ, self.env, clear=True):
            ensure_runtime(self.config, self.path, omp_path=str(selected))
        self.addCleanup(self.shutdown_detached_runtime)
        invocations.write_text("")  # Only count production-path invocations, not admission.
        # Runtime has selected the wrapper. Changing the original symlink must
        # not silently move its next RPC Worker to the other installation.
        selected.unlink()
        selected.symlink_to(self.config.agent.executable)
        self.env["PAVEWAY_OMP_PATH"] = str(selected)
        self.chat("NATIVE_SHARED_EXECUTABLE", background=True)
        self.assertIn("--mode\nrpc\n", invocations.read_text())
        self.assertIn("--trusted-extension", invocations.read_text())
        status = request(self.config.server.socket_path, "status")["omp"]
        self.assertEqual(status["path"], str(worker))
        self.assertEqual(status["version"], "99.0.0")  # A version label is not a compatibility gate.
        self.assertEqual(status["extension"], "attested")
        self.assertEqual(status["rpc_compatibility"], "verified")
        self.assertEqual(self.path.read_text(), saved_config)

    def shutdown_detached_runtime(self):
        from oicoach_runtime.launcher import _request_handoff, probe_daemon, reap_detached_runtimes
        socket = self.config.server.socket_path
        if (status := probe_daemon(socket)) is not None:
            _request_handoff(status, socket, stop=True, reason="installation fixture cleanup")
        reap_detached_runtimes(5)

    def test_running_runtime_keeps_lkg_until_compatible_replacement_is_admitted(self):
        self.check_replaced_official_omp()

    @unittest.skipUnless(os.environ.get("PAVEWAY_TEST_SYSTEMD") == "1", "requires a real systemd user manager")
    def test_systemd_runtime_keeps_lkg_until_compatible_replacement_is_admitted(self):
        self.check_replaced_official_omp(systemd=True)

    def check_replaced_official_omp(self, *, systemd=False):
        replacement = os.environ.get("PAVEWAY_TEST_REPLACEMENT_OMP")
        if not replacement:
            self.skipTest("set PAVEWAY_TEST_REPLACEMENT_OMP to a second official installation")
        from contextlib import ExitStack
        from unittest.mock import patch
        from oicoach_runtime.launcher import _wait_ready, ensure_runtime
        from oicoach_runtime.omp_installation import omp_version
        from oicoach_runtime.notifications import configure as configure_notifications
        from test_native_statusline import Terminal

        original = self.config.agent.executable
        self.assertNotEqual(omp_version(original), omp_version(replacement))
        self.stop_daemon()
        # Count actual dispatches exactly. OMP can otherwise issue a separate
        # next-message prediction after each foreground turn in either version.
        omp_settings = self.root / "agent" / "config.yml"
        profile = json.loads(omp_settings.read_text())
        profile.setdefault("composer", {})["predictions"] = False
        omp_settings.write_text(json.dumps(profile))
        selected, staged = self.root / "omp", self.root / "omp-new"
        shutil.copy2(original, selected)
        shutil.copy2(replacement, staged)
        agent = replace(self.config.agent, executable=str(selected))
        old_workflows = self.config.workflow_root
        self.path = self.root / "upgrade.toml"
        initialize(self.path, str(self.root / "upgrade-state"), str(old_workflows))
        self.config = replace(load_config(self.path), agent=agent)
        self.path.write_text(config_text(self.config))
        socket = self.config.server.socket_path
        configure_notifications(self.config, False)
        unit = f"paveway-verify-{self.root.name}.service"
        manager_environment = os.environ.copy()
        entered, release = threading.Event(), threading.Event()

        def service(*args, capture_output=False):
            return subprocess.run(["systemctl", "--user", *args, unit], env=manager_environment,
                                  check=True, timeout=20, capture_output=capture_output, text=True)

        def service_status(_path, *, name):
            self.assertEqual(name, unit)
            result = service("show", "--property=MainPID", "--value", capture_output=True)
            return {"managed": True, "MainPID": result.stdout.strip()}

        def service_control(_path, action, *, name):
            self.assertEqual(name, unit)
            service(action)
            return {"proxy": None}

        def response(body):
            yield {"role": "assistant", "content": "M0_REPLY"}, None
            if "active-v1" in json.dumps(body):
                entered.set()
                release.wait(30)
            yield {}, "stop"

        def schedule(key, payload=None):
            return request(socket, "schedule", {"workflow_id": "native-test", "task_id": key,
                "reason": key, "dedupe_key": key, "at": "now", "payload": payload or {}})["event_id"]

        def event(key):
            return request(socket, "events.show", {"event_id": key})

        def completed(key):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                row = event(key)
                if row["status"] == "succeeded":
                    return row
                time.sleep(0.05)
            self.fail(f"{key} did not complete; last status: {row['status']}")

        self.server.response_factory = response
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.env, clear=True))
            if systemd:
                subprocess.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                    "--property=TimeoutStopSec=15s", "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent",
                    *(f"--setenv={key}={value}" for key, value in self.env.items()),
                    sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
                    env=manager_environment, check=True, timeout=20)
                self.addCleanup(service, "stop")
                stack.enter_context(patch("oicoach_runtime.launcher.service.status", side_effect=service_status))
                stack.enter_context(patch("oicoach_runtime.launcher.service.control", side_effect=service_control))
            else:
                ensure_runtime(self.config, self.path, omp_path=str(selected))
                self.addCleanup(self.shutdown_detached_runtime)
            old = _wait_ready(self.config)
            self.assertNotEqual(old["omp"]["path"], str(selected))
            self.assertEqual(old["omp"]["version"], omp_version(original))
            if systemd:
                self.assertEqual(str(old["runtime_pid"]), service_status(self.path, name=unit)["MainPID"])
            saved = self.chat("NATIVE_SESSION_BEFORE_OMP_UPDATE")
            terminal = Terminal(self)
            active = schedule("active-v1")
            try:
                self.assertTrue(entered.wait(20), "old official Worker never reached the loopback provider")
                staged.replace(selected)
                # An upgraded source cannot invalidate the old, running verified copy.
                self.assertEqual(request(socket, "status")["omp"]["installation_status"], "selected")
                reminder = schedule("no-model-reminder", {"native_wakeup": True,
                    "intent": "reminder", "message": "REMINDER_WITH_REPLACED_OMP"})
                self.assertIsNone(completed(reminder)["run_id"])
                messages = request(socket, "inbox.list", {"workflow_id": "native-test", "task_id": "no-model-reminder"})
                self.assertEqual([row["text"] for row in messages["messages"]], ["REMINDER_WITH_REPLACED_OMP"])
                with self.assertRaises(RuntimeContractError):
                    ensure_runtime(self.config, self.path, restart=systemd, omp_path=str(selected), service_name=unit)
                self.assertEqual(request(socket, "status")["runtime_instance"], old["runtime_instance"])
                self.assertEqual(event(active)["status"], "running")
                terminal.send("/exit\r")
                terminal.until(lambda: request(socket, "frontend.list")[0]["status"] == "released")
            finally:
                release.set()
            self.assertEqual(completed(active)["attempt"], 1)
            self.assertEqual(completed(schedule("next-v1"))["attempt"], 1)
            self.assertEqual(request(socket, "status")["omp"]["version"], omp_version(original))
            new = ensure_runtime(self.config, self.path, restart=systemd, omp_path=str(selected), service_name=unit).status
            self.assertNotEqual(new["runtime_instance"], old["runtime_instance"])
            self.assertEqual(new["omp"]["version"], omp_version(replacement))
            self.assertNotEqual(new["omp"]["path"], old["omp"]["path"])
            if systemd:
                self.assertEqual(str(new["runtime_pid"]), service_status(self.path, name=unit)["MainPID"])
            resumed = self.chat("NATIVE_SESSION_AFTER_OMP_UPDATE")
            self.assertEqual((resumed["conversation_id"], resumed["session_id"]), (saved["conversation_id"], saved["session_id"]))
            self.assertIn("NATIVE_SESSION_BEFORE_OMP_UPDATE", json.dumps(self.prompt_requests("NATIVE_SESSION_AFTER_OMP_UPDATE")[-1]))
            selected.unlink()
            self.assertEqual(completed(schedule("source-missing-v2"))["attempt"], 1)
            self.assertEqual(request(socket, "status")["omp"]["path"], new["omp"]["path"])
            for text in ("NATIVE_SESSION_BEFORE_OMP_UPDATE", "NATIVE_SESSION_AFTER_OMP_UPDATE"):
                self.assertEqual(len(self.prompt_requests(text)), 1)
            self.assertEqual(len(self.server.requests), 5)
            self.assertEqual(len(request(socket, "runs.list")), 3)

    def test_rejected_real_omp_candidate_never_drains_the_active_runtime(self):
        from unittest.mock import patch
        from oicoach_runtime.launcher import ensure_runtime
        from oicoach_runtime.diagnostics import NATIVE_FAILURE_FILE
        from oicoach_runtime.omp_installation import OmpCandidateRejected, omp_version

        real_omp = shlex.quote(self.config.agent.executable)
        candidate = self.root / "incompatible-omp"
        attempted = self.root / "attempted-native"
        candidate.write_text("#!/bin/sh\n"
            'for arg; do if [ "$arg" = "--trusted-extension" ]; then\n'
            f'    : > {shlex.quote(str(attempted))}\n'
            f'    exec {real_omp} --trusted-extension /nonexistent/paveway-probe.mjs "$@"\n'
            'fi; done\n'
            f'exec {real_omp} "$@"\n')
        candidate.chmod(0o700)
        self.assertEqual(omp_version(str(candidate)), omp_version(self.config.agent.executable))
        socket = self.config.server.socket_path
        original = request(socket, "status")
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(OmpCandidateRejected):
                ensure_runtime(self.config, self.path, omp_path=str(candidate))
        self.assertTrue(attempted.is_file(), "candidate never reached the real native launch")
        diagnostic = self.config.state_dir / NATIVE_FAILURE_FILE
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        record = json.loads(diagnostic.read_text())
        self.assertEqual(record["omp_version"], original["omp"]["version"])
        self.assertIn(record["stage"], {"host_exit", "channel", "probe_native"})
        after = request(socket, "status")
        self.assertEqual(after["runtime_instance"], original["runtime_instance"])
        self.assertFalse(after["draining"])
        self.assertEqual(after["omp"]["path"], original["omp"]["path"])
        self.chat("STILL_USABLE_AFTER_REJECTED_UPGRADE")

    def test_remapped_resume_shortcut_cannot_open_native_session_picker(self):
        from test_native_statusline import Terminal
        (self.root / "agent" / "keybindings.yml").write_text("app.session.resume: alt+r\n")
        terminal = Terminal(self)
        terminal.send(b"\x1br")
        terminal.until(lambda: "Resume conversation for another Paveway conversation" in terminal.text)
        terminal.send("/paveway help\r")
        terminal.until(lambda: "Use /paveway to select an action" in terminal.text)
        self.assertEqual(self.server.requests, [])

    def test_real_native_refusals_persist_safe_diagnostics_and_never_send_a_turn(self):
        from oicoach_runtime.diagnostics import NATIVE_FAILURE_FILE
        import re
        expected_version = request(self.config.server.socket_path, "status")["omp"]["version"]
        runner = """
from pathlib import Path
from runpy import run_module
import os
import sys
from oicoach_runtime import native
real_terminal = native.run_terminal
mode = sys.argv.pop(1)
def inject(command, environment, **kwargs):
    if mode == 'syntax':
        Path(environment['PAVEWAY_NATIVE_DATA']).write_text('SECRET_MUST_NOT_BE_PERSISTED')
    elif mode == 'nonce':
        environment['PAVEWAY_NATIVE_NONCE'] = '0' * 64
    elif mode == 'cwd':
        command[command.index('--cwd') + 1] = str(Path(environment['HOME']))
    elif mode == 'parent':
        consumer = kwargs['ready']
        def wrong_owner(frame, guardian):
            if frame.get('sessionFile'):
                frame = dict(frame, pid=os.getpid())
            return consumer(frame, guardian)
        kwargs['ready'] = wrong_owner
    return real_terminal(command, environment, **kwargs)
native.run_terminal = inject
run_module('oicoach_runtime', run_name='__main__')
"""
        cases = (
            ("syntax", "launch_data", "SyntaxError", "Exception message redacted"),
            ("nonce", "attestation", "RuntimeContractError", "Native OMP integration identity mismatch"),
            ("cwd", "context", "TypeError", "Session context is missing required UI, branch, prompt or cwd"),
            ("parent", "attestation", "RuntimeContractError", "Native OMP is not the owned guardian child"),
        )
        for mode, stage, exception, message in cases:
            with self.subTest(mode=mode):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                process = subprocess.Popen([sys.executable, "-c", runner, mode, "native", "--config", str(self.path),
                    "--workflow", "native-test", "--new"], env=self.env, stdin=slave, stdout=slave, stderr=slave)
                os.close(slave)
                output = bytearray()
                os.write(master, b"INPUT_MUST_NOT_REACH_THE_PROVIDER\r")
                try:
                    deadline = time.monotonic() + 25
                    while process.poll() is None and time.monotonic() < deadline:
                        if not select.select([master], [], [], .1)[0]:
                            continue
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                    self.assertNotEqual(process.wait(timeout=5), 0, output.decode(errors="replace"))
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                    os.close(master)
                path = self.config.state_dir / NATIVE_FAILURE_FILE
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                text = path.read_text()
                self.assertNotIn("SECRET_MUST_NOT_BE_PERSISTED", text)
                self.assertNotIn(str(self.root), text)
                record = json.loads(text)
                self.assertEqual((record["stage"], record["exception_type"], record["omp_version"]),
                                 (stage, exception, expected_version))
                self.assertEqual(record["message"], message)
                for entry in record["stack"]:
                    self.assertIsNotNone(re.fullmatch(r"(?:omp-host|paveway-[a-z-]+\.mjs):\d+:\d+", entry))
                if mode == "syntax":
                    self.assertTrue(any(frame.startswith("paveway-native.mjs:") for frame in record["stack"]))
        self.assertEqual(self.server.requests, [])
        rows = request(self.config.server.socket_path, "frontend.list")
        self.assertTrue(rows)
        self.assertTrue(all(row["session_id"] is None for row in rows))

    def test_active_rpc_restart_disposes_worker_without_replaying(self):
        entered, release = threading.Event(), threading.Event()

        def response(_body):
            yield {"role": "assistant", "content": "RPCACTIVE"}, None
            entered.set()
            release.wait(30)
            yield {}, "stop"

        self.server.response_factory = response
        socket = self.config.server.socket_path
        event = request(socket, "schedule", {"workflow_id": "native-test", "task_id": "rpc-cancel",
            "reason": "cancel a live real OMP request", "dedupe_key": "live-cancel", "at": "now"})
        try:
            self.assertTrue(entered.wait(20), "Real OMP did not reach the loopback model")
            job = request(socket, "agent.restart", {"workflow_id": "native-test", "task_id": "rpc-cancel"})
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline:
                result = request(socket, "agent.show", {"restart_id": job["restart_id"]})
                if result["status"] not in ("requested", "running"):
                    break
                time.sleep(0.05)
            self.assertEqual(result["status"], "succeeded", result)
            cancelled = request(socket, "events.show", {"event_id": event["event_id"]})
            self.assertEqual((cancelled["status"], cancelled["last_error_code"], cancelled["attempt"]),
                             ("uncertain", "agent_restart_requested", 1))
            self.assertEqual(len(request(socket, "runs.list")), 1)
            self.assertEqual(len(self.server.requests), 1)
        finally:
            release.set()

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
        self.assertIsNotNone(found["created_at"])
        self.assertIsNotNone(found["updated_at"])
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
            if inbox["messages"]:
                break
            time.sleep(0.05)
        self.assertEqual({row["kind"] for row in inbox["messages"]}, {"reminder"})
        self.assertTrue(all(row["state"] != "acknowledged" for row in inbox["messages"]))
        reminders = [row for row in inbox["messages"] if row["kind"] == "reminder"]
        self.assertEqual(reminders[0]["text"], "TEST_NATIVE_TIMER")
        event = request(self.config.server.socket_path, "events.show", {"event_id": reminders[0]["event_id"]})
        self.assertEqual(event["status"], "succeeded")
        self.assertIsNone(event["run_id"])
        self.assertFalse(list((self.config.state_dir / "sessions").glob("*")))

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

    def test_wakeup_survives_daemon_restart_and_ignores_later_broken_customization(self):
        from test_native_management import NativeManagementTests
        process, raw, send, until, pump, exit_ui = NativeManagementTests.open_ui(self)
        send("/wakeup in 5s FROZEN_WAKEUP_AFTER_RESTART\r")
        until("native_wakeup")
        events = request(self.config.server.socket_path, "events.upcoming", {"workflow_id": "native-test", "task_id": "training"})
        self.assertEqual(len(events), 1)
        event_id = events[0]["event_id"]
        exit_ui()
        self.stop_daemon()
        # Emulate a later incomplete settings edit. It must remain invalid for
        # new launches, without replacing this event's trusted launch snapshot.
        path = self.config.workflow_root / "native-test/customization.json"
        path.write_text('{"broken_future_edit":')
        path.chmod(0o600)
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 18
        event = None
        while time.monotonic() < deadline:
            try:
                event = request(self.config.server.socket_path, "events.show", {"event_id": event_id})
                if event["status"] in ("succeeded", "blocked", "failed", "uncertain"): break
            except RuntimeContractError:
                pass
            time.sleep(.05)
        self.assertIsNotNone(event)
        self.assertEqual(event["status"], "succeeded", event.get("last_error_code"))
        inbox = request(self.config.server.socket_path, "inbox.list", {"workflow_id": "native-test"})
        self.assertEqual({row["kind"] for row in inbox["messages"]}, {"result"})
        self.assertTrue(all(row["state"] == "created" for row in inbox["messages"]))
        self.assertTrue(any("FROZEN_WAKEUP_AFTER_RESTART" in json.dumps(body["messages"]) for body in self.server.requests))
        self.assertEqual(path.read_text(), '{"broken_future_edit":')
        from oicoach_runtime.workflows import WorkflowProvider
        with self.assertRaises(RuntimeContractError):
            WorkflowProvider(self.config.workflow_root).describe("native-test")

    def test_two_persona_profiles_reach_native_model_without_identity_leakage(self):
        from oicoach_runtime.coaches import create
        a = create(self.config, "alpha", "native-test", "ALPHA_NATIVE_PERSONA")
        b = create(self.config, "beta", "native-test", "BETA_NATIVE_PERSONA")
        def main_request(marker):
            return self.prompt_requests(marker)[-1]
        first = self.chat("ALPHA_CHAT", workflow=a["runtime_workflow_id"])
        wire = json.dumps(main_request("ALPHA_CHAT"))
        self.assertIn("ALPHA_NATIVE_PERSONA", wire)
        self.assertNotIn("BETA_NATIVE_PERSONA", wire)
        second = self.chat("BETA_CHAT", workflow=b["runtime_workflow_id"])
        wire = json.dumps(main_request("BETA_CHAT"))
        self.assertIn("BETA_NATIVE_PERSONA", wire)
        self.assertNotIn("ALPHA_NATIVE_PERSONA", wire)
        self.assertNotEqual(first["session_id"], second["session_id"])
