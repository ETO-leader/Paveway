"""Real OMP Browser Relay acceptance with an already-running isolated Chrome.

Set PAVEWAY_TEST_NATIVE=1, select official OMP on PATH, and point
PAVEWAY_TEST_RELAY_ROOT at the private temporary Chrome fixture (its native
host publishes under ROOT/home/.omp/browser-relay/endpoints). Never starts
Chrome, reads a real OMP profile, or calls a paid provider.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from oicoach_runtime.config import config_text
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from test_native_omp import NativeFixture


def isolated_relay(root):
    root = Path(root).resolve(strict=True)
    if root == Path(tempfile.gettempdir()) or not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("Relay root must be a private temporary directory")
    home = root / "home"
    port_matches = []
    for file in (home / ".omp/browser-relay/endpoints").glob("*"):
        match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", file.name)
        if not match or int(match[2]) > 65535:
            continue
        process = Path("/proc") / match[1]
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
            env = (process / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if (argv[1:3] == [b"browser-relay", b"native-host"]
                and os.fsencode(f"HOME={home}") in env
                and any(value.startswith(os.fsencode(f"XDG_RUNTIME_DIR={root}/")) for value in env)
                and not any(value.startswith((b"DISPLAY=", b"WAYLAND_DISPLAY=")) for value in env)):
            port_matches.append(match[2])
    if len(port_matches) != 1:
        raise ValueError("Expected exactly one live relay in the private fixture")
    return f"http://127.0.0.1:{port_matches[0]}"


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_RELAY_ROOT"), "set PAVEWAY_TEST_RELAY_ROOT for real relay acceptance")
class BrowserRelayOmpTests(NativeFixture):
    def setUp(self):
        relay = isolated_relay(os.environ["PAVEWAY_TEST_RELAY_ROOT"])
        super().setUp()
        self.stop_daemon()
        self.config = replace(self.config,
            agent=replace(self.config.agent, tools_enabled=True),
            queue=replace(self.config.queue, default_max_attempts=1),
            server=replace(self.config.server, max_concurrent_runs=4, poll_interval_seconds=0.1))
        self.path.write_text(config_text(self.config))
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        path.write_text(json.dumps({**json.loads(path.read_text()), "browser": {"relay": True, "relayUrl": relay}}))
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            env=self.env, stdout=self.log, stderr=self.log)
        self.until(lambda: self.api("status")["daemon"] == "running", "daemon startup")

    def api(self, command, **params):
        return request(self.config.server.socket_path, command, params)

    def until(self, predicate, stage, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if result := predicate():
                    return result
            except RuntimeContractError as error:
                if stage != "daemon startup":
                    raise error
            time.sleep(.05)
        self.fail(f"Timed out in relay fixture: {stage}")

    def test_workers_and_foreground_use_distinct_tabs_during_overlap_and_targeted_restart(self):
        from verify_omp_m0 import Provider
        from test_native_statusline import Terminal

        class FixtureCancelled(Exception):
            pass

        class Pages(Provider):
            def do_POST(self):
                try:
                    super().do_POST()
                except FixtureCancelled:
                    # Do not send a model continuation to the already stopped Worker.
                    return

            def do_GET(self):
                match = re.fullmatch(r"/relay/(PW_RELAY_[0-9a-f]{12}_[0-9]+)", self.path)
                if not match:
                    self.send_error(404)
                    return
                title = f"TITLE_{match[1]}"
                data = f"<!doctype html><title>{title}</title><h1>{title}</h1>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        # Chrome can preconnect to the fixture before sending GET. Keep one page
        # handler across rounds so an idle connection does not capture stale pages.
        self.server.RequestHandlerClass = Pages

        def run_case(width, mixed, restart):
            with self.subTest(overlap=width, foreground=mixed, targeted_restart=restart):
                prefix = "PW_RELAY_" + uuid.uuid4().hex[:12]
                labels = [f"{prefix}_{i}" for i in range(width)]
                title = {label: f"TITLE_{label}" for label in labels}
                origin = f"http://127.0.0.1:{self.server.server_port}"
                urls = {label: f"{origin}/relay/{label}" for label in labels}
                departed = labels[0]
                evidence = {}
                # Hold provider continuations until every browser tab is open, then until every
                # client has observed the simultaneous targets. No timing sleeps establish overlap.
                release_verify = threading.Event()
                release_close = {label: threading.Event() for label in labels}
                foreground = labels[-1] if mixed else None

                def tool(label, stage, code):
                    return [({"role": "assistant", "tool_calls": [{"index": 0, "id": f"{label}/{stage}",
                        "type": "function", "function": {"name": "eval", "arguments": json.dumps({"language": "js", "code": code, "timeout": 25})}}]}, "tool_calls")]

                def response(body):
                    messages = body["messages"]
                    # OMP can append its own tool reference after the actual Eval result.
                    last = next((message for message in reversed(messages)
                        if message.get("role") == "tool"
                        and message.get("tool_call_id", "").partition("/")[0] in urls), messages[-1])
                    text = json.dumps(last)
                    label = next((key for key in labels if key in text), None)
                    if label is not None and last["role"] == "user":
                        return tool(label, "open", f'''const tab = await browser.open({{name:"relay-fixture", url:{json.dumps(urls[label])},
 app:"relay", allowed_domains:["127.0.0.1"], wait_until:"domcontentloaded", timeout:15}});
if (await tab.title() !== {json.dumps(title[label])}) throw new Error("wrong page title");
console.log("OPEN_{label}");''')
                    if label is not None and last["role"] == "tool":
                        call = last.get("tool_call_id", "")
                        stage = call.rsplit("/", 1)[-1]
                        evidence[label, stage] = last["content"]
                        if stage == "open" and release_verify.wait(45):
                            return tool(label, "verify", f'''const all = await browser.targets({{app:"relay"}});
const urls = {json.dumps(list(urls.values()))};
const ids = all.filter(t=>urls.includes(t.url)).map(t=>t.target);
if (ids.length !== urls.length || new Set(ids).size !== urls.length) throw new Error("distinct live targets missing");
const own = all.find(t=>t.url==={json.dumps(urls[label])});
if (!own || own.title !== {json.dumps(title[label])}) throw new Error("wrong live target");
console.log("VERIFY_{label}");''')
                        if stage == "verify" and release_close[label].wait(45):
                            if restart and label == departed:
                                raise FixtureCancelled

                            absent = "" if label == departed else f'''
const remaining = await browser.targets({{app:"relay"}});
if (remaining.some(t=>t.url==={json.dumps(urls[departed])})) throw new Error("departed target still listed");'''
                            return tool(label, "close", f'''const tab = browser.tab("relay-fixture");
if (await tab.title() !== {json.dumps(title[label])}) throw new Error("wrong surviving target");{absent}
await tab.close();
if ((await browser.targets({{app:"relay"}})).some(t=>t.url==={json.dumps(urls[label])})) throw new Error("owned target remained");
console.log("CLOSE_{label}");''')
                    return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]

                def require(stage, expected):
                    try:
                        self.until(lambda: all((label, stage) in evidence for label in expected), stage)
                    except AssertionError:
                        recent = [[(message.get("role"), message.get("tool_call_id")) for message in body["messages"][-5:]]
                                  for body in self.server.requests[-8:]]
                        self.fail(f"{stage} missing; fixture tool stages={list(evidence)}; "
                            f"Worker states={[(row['task_id'], row['status'], row['last_error_code']) for row in self.api('events.list')]}; "
                            f"recent provider input roles and IDs={recent}")
                    for label in expected:
                        result = evidence[label, stage]
                        self.assertIn(f"{stage.upper()}_{label}", result.splitlines(), result)
                        if stage == "open":
                            self.assertIn('on relay http://127.0.0.1:', result)

                self.server.response_factory = response
                events = {}
                task_ids = {label: f"relay-{prefix[-12:]}-{i}" for i, label in enumerate(labels)}

                def run_foreground():
                    terminal = Terminal(self)
                    terminal.send(f"\x1b[200~{foreground}\x1b[201~")
                    terminal.until(lambda: foreground in terminal.editor_line)
                    terminal.send("\r")
                    terminal.until(lambda: "M0_REPLY" in terminal.text and
                        self.api("frontend.list")[0]["status"] == "idle", timeout=65)

                with ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        native = executor.submit(run_foreground) if foreground else None
                        for label in labels:
                            if label != foreground:
                                events[label] = self.api("schedule", workflow_id="native-test", task_id=task_ids[label],
                                    reason=label, dedupe_key=label, at="now")["event_id"]
                        require("open", labels)
                        release_verify.set()
                        require("verify", labels)
                        if restart:
                            job = self.api("agent.restart", workflow_id="native-test", task_id=task_ids[departed])
                            final = self.until(lambda: (state if (state := self.api("agent.show", restart_id=job["restart_id"]))["status"]
                                not in ("requested", "running") else None), "scoped Worker restart")
                            self.assertEqual(final["status"], "succeeded", final)
                            cancelled = self.api("events.show", event_id=events[departed])
                            self.assertEqual((cancelled["status"], cancelled["last_error_code"], cancelled["attempt"]),
                                ("uncertain", "agent_restart_requested", 1))
                        else:
                            release_close[departed].set()
                            require("close", [departed])
                            survivor = lambda label: self.api("events.show", event_id=events[label])
                            ended = self.until(lambda: (row if (row := survivor(departed))["status"]
                                in ("succeeded", "failed", "blocked", "uncertain", "dead_letter") else None),
                                "first Worker exited before releasing surviving tabs")
                            self.assertEqual(ended["status"], "succeeded", ended)
                        for label in labels[1:]:
                            release_close[label].set()
                        require("close", labels[1:])
                        for label, event_id in events.items():
                            if restart and label == departed:
                                continue
                            final = self.until(lambda: (state if (state := self.api("events.show", event_id=event_id))["status"]
                                in ("succeeded", "failed", "blocked", "uncertain", "dead_letter") else None), "Worker settlement")
                            self.assertEqual(final["status"], "succeeded", final)
                        if native is not None:
                            native.result(timeout=20)
                    finally:
                        release_verify.set()
                        for gate in release_close.values():
                            gate.set()

        for width, mixed, restart in ((1, False, False), (2, False, False), (4, False, False), (4, True, True)):
            run_case(width, mixed, restart)
