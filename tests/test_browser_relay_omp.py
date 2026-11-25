"""Real OMP Browser Relay acceptance with an already-running isolated Chrome.

Set PAVEWAY_TEST_NATIVE=1, select official OMP on PATH, and point
PAVEWAY_TEST_RELAY_ROOT at the private temporary Chrome fixture (its native
host publishes under ROOT/home/.omp/browser-relay/endpoints). Never starts
Chrome, reads a real OMP profile, or calls a paid provider.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import takewhile
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
from urllib.request import ProxyHandler, build_opener

from oicoach_runtime.config import config_text
from oicoach_runtime.errors import RuntimeContractError
from relay_lifecycle_trace import emit
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
        self.relay = relay
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        path.write_text(json.dumps({**json.loads(path.read_text()), "browser": {"relay": True, "relayUrl": relay}}))
        self.trace = Path(os.environ["PAVEWAY_TEST_RELAY_TRACE"]) / f"{self.root.name}.jsonl" if (
            "PAVEWAY_TEST_RELAY_TRACE" in os.environ) else self.root / "lifecycle.jsonl"
        trace_script = Path(__file__).with_name("relay_lifecycle_trace.py").resolve()
        self.daemon = subprocess.Popen([sys.executable, str(trace_script), str(self.path), str(self.trace)],
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
        recent = self.trace.read_text().splitlines()[-15:] if self.trace.exists() else []
        if stage == "daemon startup":
            recent += (self.root / "daemon.log").read_text().splitlines()[-8:]
        self.fail(f"Timed out in relay fixture: {stage}; trace={self.trace}\n" + "\n".join(recent))

    @contextmanager
    def round_trace(self, **scenario):
        emit(self.trace, "round_started", **scenario)
        yield
        emit(self.trace, "round_passed", **scenario)

    def test_four_workers_reuse_slots_over_ten_continuous_rounds(self):
        seen = set()
        for attempt in range(1, 11):
            # Stop at the first failure; subsequent batches would inherit
            # workers still shutting down, not prove an independent failure.
            targets = self.run_worker_case(4, False, False, "close", attempt=attempt)
            self.assertTrue(seen.isdisjoint(targets), "a batch reused an earlier target")
            seen.update(targets)

    def test_two_workers_keep_distinct_targets_through_normal_exit(self):
        self.run_worker_case(2, False, False, "close")

    def test_foreground_and_three_workers_keep_targets_through_scoped_restart(self):
        self.run_worker_case(4, True, True, "close")

    def test_plain_text_worker_exits(self):
        self.run_worker_case(1, False, False, "text")

    def test_nonbrowser_eval_worker_exits(self):
        self.run_worker_case(1, False, False, "eval")

    def test_relay_list_without_opening_a_target_exits(self):
        self.run_worker_case(1, False, False, "list")

    def test_owned_target_closes_on_normal_worker_exit(self):
        self.run_worker_case(1, False, False, "open")

    def test_explicit_target_close_then_worker_exit(self):
        self.run_worker_case(1, False, False, "close")

    def test_worker_withheld_final_sse_keeps_slot_until_normal_exit(self):
        self.run_worker_case(4, False, False, "close", hold_final=True)

    def test_independent_runtimes_share_relay_without_cross_cleanup(self):
        other = BrowserRelayOmpTests()
        self.addCleanup(other.doCleanups)
        other.setUp()
        self.run_worker_case(2, False, True, "close", other=other)

    def run_worker_case(self, width, mixed, restart, kind, *, attempt=1, other=None, hold_final=False):
        from verify_omp_m0 import Provider
        from test_native_statusline import Terminal

        class FixtureCancelled(Exception):
            pass

        class Pages(Provider):
            def do_POST(self):
                thread = threading.current_thread()
                thread.relay_continuation = None
                try:
                    super().do_POST()
                except FixtureCancelled:
                    phase = "provider_cancelled"
                except ConnectionError:
                    if thread.relay_continuation is None:
                        raise
                    phase = "provider_disconnected"
                else:
                    phase = "provider_sse_sent"
                if thread.relay_continuation is not None:
                    emit(self.server.lifecycle_trace, phase, **thread.relay_continuation)

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

        servers = [self.server, other.server] if other else [self.server]
        for server in servers:
            server.RequestHandlerClass = Pages
            server.lifecycle_trace = self.trace

        with self.round_trace(width=width, mixed=mixed, restart=restart, kind=kind, attempt=attempt, runtimes=len(servers)):
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
                release_final = threading.Event()
                foreground = labels[-1] if mixed else None
                owners = {label: other if other and label != departed else self for label in labels}

                def run_trace(label):
                    owner = owners[label]
                    event = owner.api("events.show", event_id=events[label])
                    rows = [json.loads(line) for line in owner.trace.read_text().splitlines()]
                    run = next((row["run"] for row in rows if row["stage"] == "guardian_started"
                                and row["task"] == task_ids[label]), None)
                    return event, [row for row in rows if run and row.get("run") == run]

                def worker_finished(label, *, cancelled=False):
                    def released():
                        event, rows = run_trace(label)
                        if any(row["stage"] == "slot_released" for row in rows):
                            return event, rows
                    event, rows = self.until(released, f"{label} process and slot released")
                    expected = ("uncertain", "agent_restart_requested") if cancelled else ("succeeded", None)
                    self.assertEqual((event["status"], event["last_error_code"], event["attempt"]), (*expected, 1), event)
                    phases = {row["stage"]: row for row in rows}
                    ordered = ["stdin_closed", "cleanup_begin", "cleanup_complete", "stop_return",
                               "run_persisted", "slot_released"]
                    if not cancelled:
                        ordered.insert(0, "prompt_result")
                    self.assertTrue(all(name in phases for name in ordered), rows)
                    self.assertIsNotNone(phases["cleanup_begin"]["guardian_returncode"], rows)
                    if not cancelled:
                        self.assertEqual(phases["cleanup_begin"]["guardian_returncode"], 0)
                        self.assertEqual(phases["stop_return"]["code"], 0)
                    times = [phases[name]["ns"] for name in ordered]
                    self.assertEqual(times, sorted(times), rows)

                def tool(label, stage, code):
                    return [({"role": "assistant", "tool_calls": [{"index": 0, "id": f"{label}/{stage}",
                        "type": "function", "function": {"name": "eval", "arguments": json.dumps({"language": "js", "code": code, "timeout": 25})}}]}, "tool_calls")]

                def response(body):
                    messages = body["messages"]
                    last = messages[-1]
                    # Eval can append a reference after the actual result. Do not
                    # replay an old tool result across a newer assistant turn.
                    if last["role"] == "user" and not any(key in json.dumps(last) for key in labels):
                        last = next((message for message in takewhile(
                            lambda message: message["role"] != "assistant", reversed(messages))
                            if message["role"] == "tool"
                            and message.get("tool_call_id", "").partition("/")[0] in urls), last)
                    text = json.dumps(last)
                    label = next((key for key in labels if key in text), None)
                    if label is not None:
                        facts = {"tab": label, "continuation": last.get("tool_call_id", "initial").rsplit("/", 1)[-1]}
                        threading.current_thread().relay_continuation = facts
                        emit(self.trace, "provider_enter", **facts)
                    if label is not None and last["role"] == "user":
                        if kind == "text":
                            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
                        if kind == "eval":
                            return tool(label, "control", f'console.log("CONTROL_{label}")')
                        if kind == "list":
                            return tool(label, "control", f'''await browser.targets({{app:"relay"}});
console.log("CONTROL_{label}");''')
                        return tool(label, "open", f'''const tab = await browser.open({{name:{json.dumps(label)}, url:{json.dumps(urls[label])},
 app:"relay", allowed_domains:["127.0.0.1"], wait_until:"domcontentloaded", timeout:15}});
if (await tab.title() !== {json.dumps(title[label])}) throw new Error("wrong page title");
console.log("OPEN_{label}");''')
                    if label is not None and last["role"] == "tool":
                        call = last.get("tool_call_id", "")
                        stage = call.rsplit("/", 1)[-1]
                        evidence[label, stage] = last["content"]
                        emit(self.trace, "tool_result", tab=label, tool_stage=stage)
                        if hold_final and stage == "close" and label == departed:
                            if not release_final.wait(45):
                                raise TimeoutError("fixture never released final SSE barrier")
                        if stage == "open" and kind != "open":
                            if not release_verify.wait(45):
                                raise TimeoutError("fixture never released open barrier")
                            return tool(label, "verify", f'''const all = await browser.targets({{app:"relay"}});
const urls = {json.dumps(list(urls.values()))};
if (all.filter(t=>urls.includes(t.url)).length !== urls.length) throw new Error("live targets missing");
const own = (await browser.tabs()).find(t=>t.name==={json.dumps(label)} && t.url==={json.dumps(urls[label])});
if (!own?.targetId) throw new Error("wrong live target");
console.log("VERIFY_{label}:" + own.targetId);''')
                        if stage == "verify":
                            if not release_close[label].wait(45):
                                raise TimeoutError("fixture never released close barrier")
                            if restart and label == departed:
                                raise FixtureCancelled

                            absent = "" if label == departed else f'''
const remaining = await browser.targets({{app:"relay"}});
if (remaining.some(t=>t.url==={json.dumps(urls[departed])})) throw new Error("departed target still listed");'''
                            return tool(label, "close", f'''const tab = browser.tab({json.dumps(label)});
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
                    target_ids = set()
                    for label in expected:
                        result = evidence[label, stage]
                        if stage == "verify":
                            match = re.search(rf"(?m)^VERIFY_{label}:(\S+)$", result)
                            self.assertIsNotNone(match, result)
                            target = match[1]
                            self.assertNotIn(target, target_ids)
                            target_ids.add(target)
                            emit(self.trace, "target_verified", tab=label, target=target)
                            continue
                        self.assertIn(f"{stage.upper()}_{label}", result.splitlines(), result)
                        if stage == "open":
                            self.assertIn('on relay http://127.0.0.1:', result)
                    return target_ids

                for server in servers:
                    server.response_factory = response
                events = {}
                task_ids = {label: f"relay-{prefix[-12:]}-{i}" for i, label in enumerate(labels)}

                def run_foreground(terminal):
                    terminal.send(f"\x1b[200~{foreground}\x1b[201~")
                    terminal.until(lambda: foreground in terminal.editor_line)
                    terminal.send("\r")
                    terminal.until(lambda: "M0_REPLY" in terminal.text and
                        self.api("frontend.list")[0]["status"] == "idle", timeout=65)

                with ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        # Complete actual Composer/Runtime admission before the
                        # deliberate overlap. A cold-start race isn't a Relay run.
                        native = executor.submit(run_foreground, Terminal(self)) if foreground else None
                        for label in labels:
                            if label != foreground:
                                events[label] = owners[label].api("schedule", workflow_id="native-test", task_id=task_ids[label],
                                    reason=label, dedupe_key=label, at="now")["event_id"]
                        if kind != "close":
                            if kind != "text":
                                require("open" if kind == "open" else "control", labels)
                            for label in events:
                                worker_finished(label)
                            if kind == "open":
                                transport = build_opener(ProxyHandler({}))
                                def target_removed():
                                    with transport.open(self.relay + "/json/list", timeout=2) as stream:
                                        return not any(tab["url"] in urls.values() for tab in json.load(stream))
                                self.until(target_removed, "owned target removed after Worker exit")
                            return
                        require("open", labels)
                        release_verify.set()
                        targets = require("verify", labels)
                        if restart:
                            job = self.api("agent.restart", workflow_id="native-test", task_id=task_ids[departed])
                            final = self.until(lambda: (state if (state := self.api("agent.show", restart_id=job["restart_id"]))["status"]
                                not in ("requested", "running") else None), "scoped Worker restart")
                            self.assertEqual(final["status"], "succeeded", final)
                            worker_finished(departed, cancelled=True)
                        else:
                            release_close[departed].set()
                            require("close", [departed])
                            if hold_final:
                                event, phases = run_trace(departed)
                                self.assertEqual(event["status"], "running")
                                observed = {row["stage"] for row in phases}
                                self.assertFalse({"prompt_result", "stdin_closed"} & observed)
                                canary = "slot-" + prefix[-12:]
                                event_id = self.api("schedule", workflow_id="native-test", task_id=canary,
                                    reason=canary, dedupe_key=canary, at="now")["event_id"]
                                self.assertEqual(self.api("events.show", event_id=event_id)["status"], "queued")
                                owners[canary], task_ids[canary], events[canary] = self, canary, event_id
                                release_final.set()
                            worker_finished(departed)
                            if hold_final:
                                worker_finished(canary)
                                del events[canary]
                        for label in labels[1:]:
                            release_close[label].set()
                        require("close", labels[1:])
                        for label in events:
                            if label != departed:
                                worker_finished(label)
                        if native is not None:
                            native.result(timeout=20)
                        return targets
                    finally:
                        release_verify.set()
                        release_final.set()
                        for gate in release_close.values():
                            gate.set()
