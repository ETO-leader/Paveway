import json
from pathlib import Path
import unittest
from dataclasses import replace

from test_native_omp import NativeFixture
from oicoach_runtime.config import config_text
from oicoach_runtime.ipc import request


class NativeCapabilityTests(NativeFixture):
    def enable_tools(self):
        self.config = replace(self.config, agent=replace(self.config.agent, tools_enabled=True))
        self.path.write_text(config_text(self.config))

    def tool_response(self, marker, name, arguments, child_marker=None):
        def response(body):
            latest = json.dumps(body.get("messages", [{}])[-1])
            if child_marker and child_marker in latest:
                return [({"role": "assistant", "content": "SUBAGENT_NATIVE_OK"}, "stop")]
            if body["messages"][-1].get("role") == "user" and marker in latest and "Identity rule:" in json.dumps(body["messages"]):
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
            latest = body["messages"][-1]
            if latest.get("role") == "user" and "NATIVE_MCP_PROBE" in json.dumps(latest) and "Identity rule:" in json.dumps(body["messages"]):
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
