import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from dataclasses import replace

from test_native_omp import NativeFixture
from oicoach_runtime.config import config_text
from oicoach_runtime.ipc import request
from oicoach_runtime.errors import RuntimeContractError


class NativeCapabilityTests(NativeFixture):
    def enable_tools(self):
        self.config = replace(self.config, agent=replace(self.config.agent, tools_enabled=True))
        self.path.write_text(config_text(self.config))

    def tool_response(self, marker, name, arguments, child_marker=None):
        def response(body):
            latest = json.dumps(body.get("messages", [{}])[-1])
            if child_marker and child_marker in latest:
                return [({"role": "assistant", "content": "SUBAGENT_NATIVE_OK"}, "stop")]
            if self.is_prompt_request(body, marker):
                return [({"role": "assistant", "tool_calls": [{"index": 0, "id": "native-capability-call", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")]
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
        self.server.response_factory = response

    @unittest.skipUnless(Path("/opt/google/chrome/chrome").is_file(), "isolated browser test requires this local Chrome binary; no auto-download")
    def test_native_browser_runs_chrome_against_loopback_page(self):
        self.enable_tools()
        url = f"http://127.0.0.1:{self.server.server_port}/paveway-browser-fixture"
        code = f'''const tab = await browser.open({{url:{json.dumps(url)},timeout:30,wait_until:"domcontentloaded",allowed_domains:["127.0.0.1"],app:{{path:"/opt/google/chrome/chrome",args:["--headless=new","--no-sandbox","--disable-dev-shm-usage","--disable-extensions","--disable-background-networking"]}}}});
console.log(await tab.title());
await browser.close({{all:true,kill:true}});'''
        self.tool_response("NATIVE_BROWSER_PROBE", "eval", {"language": "js", "code": code, "timeout": 40})
        self.chat("NATIVE_BROWSER_PROBE", evidence=lambda: any(message.get("role") == "tool" and "PAVEWAY_BROWSER_OK" in json.dumps(message.get("content"))
            for body in self.server.requests for message in body["messages"]), wait_seconds=50)
        self.assertTrue(any(message.get("role") == "tool" and "PAVEWAY_BROWSER_OK" in json.dumps(message.get("content"))
            for body in self.server.requests for message in body["messages"]),
            [message.get("content") for body in self.server.requests for message in body["messages"] if message.get("role") == "tool"])

    def test_native_subagent_uses_its_own_session_and_returns_result(self):
        self.enable_tools()
        directory = self.root / "workspace/.omp/agents"
        directory.mkdir(parents=True)
        (directory / "native-fixture.md").write_text("---\nname: native-fixture\ndescription: Local fixture only\n"
            "model: paveway-m0/probe\nblocking: true\ntools: []\n---\nReply briefly to the fixture. Do not use external tools.\n")
        self.tool_response("NATIVE_SUBAGENT_PROBE", "task", {"agent": "native-fixture",
            "task": "SUBAGENT_ASSIGNMENT_MARKER: complete the isolated fixture", "solutionSpace": "No external tools or mutations", "isolated": False},
            child_marker="SUBAGENT_ASSIGNMENT_MARKER")
        self.chat("NATIVE_SUBAGENT_PROBE", evidence=lambda: any(message.get("role") == "tool" and "SUBAGENT_NATIVE_OK" in json.dumps(message.get("content"))
            for body in self.server.requests for message in body["messages"]))
        self.assertTrue(any(message.get("role") == "tool" and "SUBAGENT_NATIVE_OK" in json.dumps(message.get("content"))
            for body in self.server.requests for message in body["messages"]),
            [message.get("content") for body in self.server.requests for message in body["messages"] if message.get("role") == "tool"])
        self.assertGreater(len(list((self.config.state_dir / "native").rglob("*.jsonl"))), 1)

    def test_native_memory_tool_persists_scoped_fact_and_reports_version(self):
        self.enable_tools()
        manifest = self.config.workflow_root / "native-test/workflow.toml"
        manifest.write_text(manifest.read_text().replace("memory_enabled = false", "memory_enabled = true"))
        self.tool_response("NATIVE_MEMORY_PROBE", "paveway_memory_put", {"key": "fixture-fact",
            "title": "Fixture fact", "content": "MEMORY_NATIVE_OK", "expected_version": 0, "kind": "fact"})
        self.chat("NATIVE_MEMORY_PROBE", evidence=lambda: any(message.get("role") == "tool" and 'MEMORY_NATIVE_OK' in json.dumps(message.get("content"))
            for body in self.server.requests for message in body["messages"]))
        saved = request(self.config.server.socket_path, "memory.show", {"workflow_id": "native-test", "key": "fixture-fact"})
        self.assertEqual(saved["content"], "MEMORY_NATIVE_OK")
        self.assertEqual(saved["version"], 1)
        self.stop_daemon()
        self.daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(self.path)],
            env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                request(self.config.server.socket_path, "status")
                break
            except RuntimeContractError:
                time.sleep(.03)
        else:
            self.fail("Isolated daemon did not restart")
        count = len(self.server.requests)
        self.chat("MEMORY_FRESH_SESSION_PROBE", new=True)
        main = next(body for body in self.server.requests[count:] if
            self.is_prompt_request(body, "MEMORY_FRESH_SESSION_PROBE"))
        system = json.dumps([m for m in main["messages"] if m["role"] == "system"])
        self.assertIn("Runtime memory snapshot", system)
        self.assertIn("MEMORY_NATIVE_OK", system)
        self.assertNotIn("NATIVE_MEMORY_PROBE", json.dumps(main["messages"]))

    def test_native_memory_tool_crud_loop_enforces_versions_and_tombstone(self):
        self.enable_tools()
        manifest = self.config.workflow_root / "native-test/workflow.toml"
        manifest.write_text(manifest.read_text().replace("memory_enabled = false", "memory_enabled = true"))
        steps = [("put", {"key": "cycle", "title": "Fixture", "content": "MEMORY_CYCLE_V1", "expected_version": 0}),
                 ("show", {"key": "cycle"}),
                 ("put", {"key": "cycle", "title": "Fixture", "content": "MEMORY_CYCLE_V2", "expected_version": 1}),
                 ("put", {"key": "cycle", "title": "Stale", "content": "MUST_NOT_OVERWRITE", "expected_version": 1}),
                 ("delete", {"key": "cycle", "expected_version": 2}),
                 ("list", {"query": "Fixture"})]
        def response(body):
            if "MEMORY_CYCLE_PROBE" in json.dumps(body["messages"]) and any(m.get("role") == "system" and "# Paveway Core" in str(m.get("content")) for m in body["messages"]):
                step = sum(m.get("role") == "tool" for m in body["messages"])
                if step < len(steps):
                    operation, parameters = steps[step]
                    return [({"role": "assistant", "tool_calls": [{"index": 0, "id": "memory-cycle-" + str(step), "type": "function",
                        "function": {"name": "paveway_memory_" + operation, "arguments": json.dumps(parameters)}}]}, "tool_calls")]
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
        self.server.response_factory = response
        self.chat("MEMORY_CYCLE_PROBE", evidence=lambda: any(sum(m.get("role") == "tool" for m in b["messages"]) >= len(steps) for b in self.server.requests))
        note = request(self.config.server.socket_path, "memory.show", {"workflow_id": "native-test", "key": "cycle"})
        self.assertEqual(note["version"], 3)
        self.assertTrue(note["deleted"])
        self.assertEqual(note["content"], "")
        self.assertEqual(request(self.config.server.socket_path, "memory.recall", {"workflow_id": "native-test", "task_id": "training"})["notes"], [])
        tools = [m for b in self.server.requests for m in b["messages"] if m.get("role") == "tool"]
        self.assertTrue(any("Memory version changed" in json.dumps(m) for m in tools))

    def test_native_mcp_discovers_and_invokes_stdio_server(self):
        self.enable_tools()
        from test_native_mcp import FAKE_MCP_SERVER_CODE
        server = self.root / "fixture-mcp.py"
        server.write_text(FAKE_MCP_SERVER_CODE)
        server.chmod(0o700)
        directory = self.root / "workspace/.omp"
        directory.mkdir()
        (directory / "mcp.json").write_text(json.dumps({"mcpServers": {
            "fixture": {"command": str(server), "args": ["--read-only"]}}}))
        def response(body):
            if self.is_prompt_request(body, "NATIVE_MCP_PROBE"):
                names = [tool["function"]["name"] for tool in body.get("tools", [])]
                name = next((name for name in names if name.endswith("tijian_problem_get")), None)
                arguments = "{}"
                if name is None:
                    # Current OMP mounts MCP tools as xd:// devices dispatched
                    # through native write, rather than provider functions.
                    import re
                    devices = re.findall(r'xd://([\w_-]*tijian_problem_get)', json.dumps(body["messages"]))
                    self.assertTrue(devices, json.dumps(body["messages"])[-3000:])
                    name, arguments = "write", json.dumps({"path": "xd://" + devices[0], "content": "{}"})
                return [({"role": "assistant", "tool_calls": [{"index": 0, "id": "native-mcp-call", "type": "function",
                    "function": {"name": name, "arguments": arguments}}]}, "tool_calls")]
            return [({"role": "assistant", "content": "M0_REPLY"}, "stop")]
        self.server.response_factory = response
        self.chat("NATIVE_MCP_PROBE", evidence=lambda: any(message.get("role") == "tool" and '"ok"' in json.dumps(message.get("content")).replace('\\"', '"')
            and "tijian_problem_get" in json.dumps(message.get("content"))
            for body in self.server.requests for message in body["messages"]))
