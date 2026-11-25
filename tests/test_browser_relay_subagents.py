"""Native OMP Subagents over the real relay, using only synthetic loopback pages.

Opt in with PAVEWAY_TEST_NATIVE=1 and PAVEWAY_TEST_RELAY_ROOT; Chrome must
already be running in the private fixture. See test_browser_relay_omp.py.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import threading
import time
import unittest
import uuid

from oicoach_runtime.config import config_text
from oicoach_runtime.ipc import request
from test_browser_relay_omp import isolated_relay
from test_native_omp import NativeFixture
from test_native_statusline import Terminal


WAIT = 40


def reply(content):
    return [({"role": "assistant", "content": content}, "stop")]


def tool(name, ident, **arguments):
    return [({"role": "assistant", "tool_calls": [{"index": 0, "id": ident, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")]


def evaluate(ident, code):
    return tool("eval", ident, language="js", code=code, timeout=25)


def relay_targets():
    return 'await browser.targets({app:"relay"})'


class CancelledContinuation(Exception):
    """Keep the selected child's model response withheld until OMP aborts it."""


def cancel_from_hub(terminal, first, sibling):
    def roster():
        # Restrict the lookup to the roster pane, excluding the detail pane
        # (which repeats the selected name) and the inline subagent preview.
        rows = [row.split("│")[1] for row in terminal.screen.display if row.startswith("│")]
        return [(index, row) for index, row in enumerate(rows) if first in row or sibling in row]

    terminal.send("\x13")  # Ctrl+S opens the full Agent Hub.
    terminal.until(lambda: "2 running" in terminal.text and len(roster()) == 2
        and sum("❯" in row for _, row in roster()) == 1)
    target = next(index for index, row in roster() if first in row)
    selected = next(index for index, row in roster() if "❯" in row)
    if selected != target:
        terminal.send("j" if selected < target else "k")
        terminal.until(lambda: any("❯" in row and first in row for _, row in roster()))
    terminal.send("x")
    terminal.until(lambda: "1 running" in terminal.text and "1 aborted" in terminal.text
        and any("⏹" in row and first in row for _, row in roster()))
    terminal.send("\x1b")


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_RELAY_ROOT"), "set PAVEWAY_TEST_RELAY_ROOT for real relay acceptance")
class BrowserRelaySubagentTests(NativeFixture):
    def setUp(self):
        relay = isolated_relay(os.environ["PAVEWAY_TEST_RELAY_ROOT"])
        super().setUp()
        from verify_omp_m0 import Provider

        self.config = replace(self.config, agent=replace(
            self.config.agent, tools_enabled=True, startup_timeout_seconds=30))
        self.path.write_text(config_text(self.config))
        path = Path(self.env["PI_CODING_AGENT_DIR"]) / "config.yml"
        path.write_text(json.dumps({**json.loads(path.read_text()),
            "browser": {"relay": True, "relayUrl": relay}, "task": {"batch": True, "maxConcurrency": 4}}))
        agents = self.root / "workspace/.omp/agents"
        agents.mkdir(parents=True)
        (agents / "relay-fixture.md").write_text("---\nname: relay-fixture\ndescription: Local relay fixture\n"
            "model: paveway-m0/probe\nblocking: true\ntools: [eval]\n---\nOnly use the synthetic fixture.\n")
        self.page_root = f"http://127.0.0.1:{self.server.server_port}/subagent/"

        class Pages(Provider):
            def do_POST(self):
                try:
                    super().do_POST()
                except CancelledContinuation:
                    # Do not stream another model message into the aborted child.
                    return

            def do_GET(self):
                match = re.fullmatch(r"/subagent/(PW_[0-9a-f]{12}_[0-3])", self.path)
                if not match:
                    self.send_error(404)
                    return
                title = f"TITLE_{match[1]}"
                data = f"<!doctype html><title>{title}</title><main>{title}</main>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        # One handler across all rounds, including Chrome's preconnected sockets.
        self.server.RequestHandlerClass = Pages

    def open_terminal(self, scenario):
        print(f"RELAY scenario={scenario} stage=admitting", flush=True)
        started = time.monotonic()
        terminal = Terminal(self)
        print(f"RELAY scenario={scenario} stage=admitted elapsed={time.monotonic() - started:.2f}s", flush=True)
        return terminal

    def test_unique_aliases_hold_distinct_tabs_and_cleanup(self):
        terminal = self.open_terminal("unique")
        all_ids = set()
        for round_number, width in enumerate((1, 2, *([4] * 10)), 1):
            ids = self.run_round(terminal, width, scenario=f"unique-{round_number:02}")
            self.assertTrue(all_ids.isdisjoint(ids), f"round {round_number} reused an earlier owned tab")
            all_ids.update(ids)

    def test_same_alias_is_shared_within_one_native_session(self):
        self.run_round(self.open_terminal("collision"), 2, scenario="collision", collision=True)

    def test_agent_hub_cancels_only_selected_subagent_and_reaps_its_tab(self):
        self.run_round(self.open_terminal("cancel"), 2, scenario="cancel", cancel=True)

    def run_round(self, terminal, width, *, scenario, collision=False, cancel=False):
        print(f"RELAY scenario={scenario} stage=starting children={width}", flush=True)
        token = f"PW_{uuid.uuid4().hex[:12]}"
        parent = f"{token}_PARENT"
        settled = f"{token}_DONE"
        labels = [f"{token}_{i}" for i in range(width)]
        child_names = {label: f"child-{i}-{token}" for i, label in enumerate(labels)}
        first = labels[0]
        aliases = {label: first if collision else label for label in labels}
        urls = {label: self.page_root + label for label in labels}
        second_verified = threading.Event()
        evidence = {}
        all_open = threading.Barrier(width)
        all_verified = threading.Barrier(width)
        first_open = threading.Event()
        first_closed = threading.Event()
        # Filter out OMP's extra tool-reference messages and older turns in this session.
        calls = {f"{label}-{stage}": (label, stage) for label in labels for stage in ("open", "verify", "close")}
        task_id, end_id = f"{token}-task", f"{token}-end"

        def open_tab(label):
            if collision and label != first and not first_open.wait(WAIT):
                return reply("FIRST_OPEN_MISSING")
            return evaluate(f"{label}-open", f'''const tab = await browser.open({{
 name:{json.dumps(aliases[label])}, url:{json.dumps(urls[label])}, app:"relay",
 allowed_domains:["127.0.0.1"], wait_until:"domcontentloaded", timeout:15}});
if (await tab.title() !== "TITLE_{label}") throw new Error("wrong child page");
const own = ({managed_tabs()}).filter(t=>t.name==={json.dumps(aliases[label])});
console.log("FIXTURE_JSON " + JSON.stringify(own.map(t=>t.targetId)));''')

        def managed_tabs():
            return f'(await browser.tabs()).filter(t=>t.kind==="relay" && t.url.startsWith({json.dumps(self.page_root)}))'

        def relay_pages():
            return f'({relay_targets()}).filter(t=>t.url.startsWith({json.dumps(self.page_root)})).map(t=>t.url)'

        def verify(label):
            return evaluate(f"{label}-verify", f'''const live = {managed_tabs()};
const title = await browser.tab({json.dumps(aliases[label])}).title();
console.log("FIXTURE_JSON " + JSON.stringify({{
 title,tabs:live.map(t=>[t.url,t.targetId]), pages:{relay_pages()}}}));''')

        def close(label):
            # Hub status can update before the browser teardown finishes. Wait
            # for disappearance through the tool before reading the survivor.
            wait_for_reap = "" if not cancel else f'''
const deadline = Date.now() + 10000;
while (({relay_pages()}).includes({json.dumps(urls[first])}) ||
       ({managed_tabs()}).some(t=>t.name==={json.dumps(aliases[first])})) {{
  if (Date.now() >= deadline) throw new Error("cancelled child's tab survived");
  await new Promise(resolve=>setTimeout(resolve, 100));
}}'''
            return evaluate(f"{label}-close", f'''{wait_for_reap}
const tab = browser.tab({json.dumps(aliases[label])});
const title = await tab.title();
const before = {managed_tabs()};
await tab.close();
const after = {managed_tabs()};
console.log("FIXTURE_JSON " + JSON.stringify({{
 title, before:before.map(t=>t.targetId), after:after.map(t=>t.targetId)}}));''')

        def response(body):
            messages = body["messages"]
            result = next((m for m in reversed(messages) if m.get("role") == "tool" and
                (m.get("tool_call_id") in calls or m.get("tool_call_id") in (task_id, end_id))), None)
            if result is None:
                if messages[-1].get("role") != "user":
                    return reply("M0_REPLY")
                text = json.dumps(messages[-1]["content"])
                if parent in text and "<title-request>" not in text:
                    return tool("task", task_id, context="Local Browser Relay acceptance only.", tasks=[
                        {"name": child_names[label], "agent": "relay-fixture", "task": label, "isolated": False,
                         "solutionSpace": "Only the supplied synthetic target"} for label in labels])
                label = next((label for label in labels if label in text), None)
                return open_tab(label) if label else reply("M0_REPLY")

            ident, content = result["tool_call_id"], result["content"]
            if ident == task_id:
                evidence["parent"] = content
                return evaluate(end_id, f'''console.log("FIXTURE_JSON " + JSON.stringify({{
 managed:{managed_tabs()}, pages:{relay_pages()}}}));''')
            if ident == end_id:
                evidence["end"] = content
                return reply(settled)

            label, stage = calls[ident]
            evidence[label, stage] = content
            try:
                if stage == "open":
                    if label == first:
                        first_open.set()
                    all_open.wait(WAIT)
                    # The collision has already been induced by the second open. Read the
                    # shared alias in order so the evidence does not depend on a busy race.
                    if collision and label == first and not second_verified.wait(WAIT):
                        return reply("SECOND_VERIFY_MISSING")
                    return verify(label)
                if stage == "verify":
                    if collision and label != first:
                        second_verified.set()
                    all_verified.wait(WAIT)
                    if cancel and label == first:
                        first_closed.wait(WAIT)
                        raise CancelledContinuation
                    if collision and label != first:
                        return reply(f"CHILD_DONE_{label}")
                    if label == first or first_closed.wait(WAIT):
                        return close(label)
                if stage == "close":
                    if label == first:
                        first_closed.set()
                    return reply(f"CHILD_DONE_{label}")
            except threading.BrokenBarrierError:
                pass
            return reply("FIXTURE_BARRIER_NOT_REACHED")

        def data(key):
            result = evidence.get(key, "")
            found = [line.removeprefix("FIXTURE_JSON ") for line in result.splitlines()
                if line.startswith("FIXTURE_JSON ")]
            self.assertEqual(len(found), 1, f"{scenario}: missing tool evidence for {key}; seen={list(evidence)}")
            return json.loads(found[0])

        self.server.response_factory = response
        try:
            terminal.send(f"\x1b[200~{parent}\x1b[201~")
            terminal.until(lambda: parent in terminal.editor_line)
            terminal.send("\r")
            if cancel:
                terminal.until(lambda: all((label, "verify") in evidence for label in labels), timeout=WAIT)
                cancel_from_hub(terminal, child_names[first], child_names[labels[1]])
                print("RELAY scenario=cancel stage=selected-aborted sibling=running", flush=True)
                first_closed.set()
            terminal.until(lambda: settled in terminal.text and request(self.config.server.socket_path,
                "frontend.list")[0]["status"] == "idle", timeout=2 * WAIT)
        finally:
            all_open.abort()
            all_verified.abort()
            first_open.set()
            first_closed.set()
            second_verified.set()

        self.assertEqual(data("end"), {"managed": [], "pages": []}, "owned tabs survived the parent turn")
        ids = {}
        for label in labels:
            if not cancel or label != first:
                self.assertIn(f"CHILD_DONE_{label}", evidence.get("parent", ""))
            verb = "Reused" if collision and label != first else "Opened"
            self.assertIn(f'{verb} tab "{aliases[label]}" on relay http://127.0.0.1:', evidence.get((label, "open"), ""))
            found = data((label, "open"))
            self.assertEqual(len(found), 1, found)
            self.assertIsInstance(found[0], str)
            self.assertTrue(found[0])
            ids[label] = found[0]

        if cancel:
            self.assertNotIn(f"CHILD_DONE_{first}", evidence["parent"])
            self.assertRegex(evidence["parent"].lower(), r"\baborted\b|\bkilled\b|\bcancelled\b")
            self.assertNotIn((first, "close"), evidence, "cancelled child closed its own tab")

        if collision:
            self.assertEqual(len(set(ids.values())), 1, ids)
            latest = labels[-1]
            for label in labels:
                self.assertEqual(data((label, "verify")), {
                    "title": f"TITLE_{latest}", "tabs": [[urls[latest], ids[first]]], "pages": [urls[latest]]})
            self.assertEqual(data((first, "close")), {"title": f"TITLE_{latest}", "before": [ids[first]], "after": []})
        else:
            self.assertEqual(len(set(ids.values())), width, ids)
            expected = {urls[label]: ids[label] for label in labels}
            survivors = set(ids.values()) - {ids[first]}
            for label in labels:
                live = data((label, "verify"))
                self.assertEqual(live["title"], f"TITLE_{label}")
                self.assertEqual(len(live["tabs"]), width, live)
                self.assertEqual(dict(live["tabs"]), expected)
                self.assertCountEqual(live["pages"], expected)
                if cancel and label == first:
                    continue
                closed = data((label, "close"))
                self.assertEqual(closed["title"], f"TITLE_{label}")
                self.assertIn(ids[label], closed["before"])
                self.assertNotIn(ids[label], closed["after"])
                if label == first:
                    self.assertEqual(set(closed["before"]), set(ids.values()))
                    self.assertEqual(set(closed["after"]), survivors)
                elif cancel:
                    self.assertEqual(closed["before"], [ids[label]])
                    self.assertEqual(closed["after"], [])
                else:
                    self.assertNotIn(ids[first], closed["before"])
        print(f"RELAY scenario={scenario} stage=clean children={width} "
              f"targets={len(set(ids.values()))} ids={json.dumps(list(ids.values()))} remaining=0", flush=True)
        return set(ids.values())
