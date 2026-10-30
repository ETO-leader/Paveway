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
class NativeOmpTests(unittest.TestCase):
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

    def test_native_chat_preserves_long_input_and_exit_keeps_daemon(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "native", "--config", str(self.path),
            "--workflow", "native-test"], env=self.env, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()

        def until(text):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if text.encode() in output:
                    return
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    output.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        os.write(master, b"\x1b[1;1R")
            self.fail("native UI did not reach expected fixture state: " + output.decode(errors="replace")[-3000:])

        try:
            until("OMP native")
            body = "NATIVE_HEAD " + "漢字 filler " * 6000 + " NATIVE_MIDDLE " + "漢字 filler " * 6000 + " NATIVE_TAIL"
            os.write(master, b"\x1b[200~" + body.encode() + b"\x1b[201~\r")
            until("M0_REPLY")
            os.write(master, b"/exit\r")
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try: output.extend(os.read(master, 65536))
                    except OSError: break
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, output.decode(errors="replace")[-2000:])
            self.assertTrue(any("NATIVE_MIDDLE" in json.dumps(row) for row in self.server.requests))
            archives = list((self.config.state_dir / "native").rglob("*.jsonl"))
            self.assertTrue(any(body in path.read_text() for path in archives))
            self.assertEqual(request(self.config.server.socket_path, "status")["daemon"], "running")
            self.assertNotIn(b"paveway_context_ready", output)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            os.close(master)
