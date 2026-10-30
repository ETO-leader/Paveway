"""Targeted tests for native tijian-mcp configuration and stdio MCP protocol boundary."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from oicoach_runtime.cli import initialize, main
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, WorkflowError
from oicoach_runtime.workflow_setup import connect_tijian, create


FAKE_MCP_SERVER_CODE = """#!/usr/bin/env python3
import sys, json

read_only = "--read-only" in sys.argv

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-tijian-mcp", "version": "1.0.0"}
            }
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        tools = [
            {"name": "tijian_problem_get", "description": "Read problem", "inputSchema": {"type": "object"}}
        ]
        if not read_only:
            tools.append(
                {"name": "tijian_problem_ensure", "description": "Ensure problem", "inputSchema": {"type": "object"}}
            )
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools}
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        name = msg.get("params", {}).get("name")
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"ok": True, "tool": name})}]
            }
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
"""


class NativeMcpConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-native-mcp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.config_path)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        create(self.config, "coach", self.workspace)
        self.fake_mcp = self.root / "fake_tijian_mcp.py"
        self.fake_mcp.write_text(FAKE_MCP_SERVER_CODE)
        self.fake_mcp.chmod(0o755)

    def test_default_read_only_configuration_includes_flag(self):
        report = connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(report["availability"], "configured_not_verified")
        self.assertTrue(report["read_only"])
        mcp_path = self.workspace / ".mcp.json"
        self.assertTrue(mcp_path.is_file())
        self.assertEqual(mcp_path.stat().st_mode & 0o777, 0o600)
        data = json.loads(mcp_path.read_text())
        server = data["mcpServers"]["tijian"]
        self.assertEqual(server["command"], str(self.fake_mcp.resolve()))
        self.assertEqual(server["args"], ["--read-only"])

    def test_explicit_read_write_configuration_omits_read_only_flag(self):
        report = connect_tijian(self.config, "coach", self.fake_mcp, read_only=False)
        self.assertFalse(report["read_only"])
        data = json.loads((self.workspace / ".mcp.json").read_text())
        server = data["mcpServers"]["tijian"]
        self.assertEqual(server["command"], str(self.fake_mcp.resolve()))
        self.assertEqual(server["args"], [])

    def test_merges_with_existing_servers_and_disabled_servers(self):
        mcp_path = self.workspace / ".mcp.json"
        initial = {
            "mcpServers": {
                "git": {"command": "/usr/bin/git", "args": ["status"]}
            },
            "disabledServers": ["unused"]
        }
        mcp_path.write_text(json.dumps(initial))
        connect_tijian(self.config, "coach", self.fake_mcp)
        data = json.loads(mcp_path.read_text())
        self.assertEqual(data["mcpServers"]["git"], initial["mcpServers"]["git"])
        self.assertEqual(data["disabledServers"], ["unused"])
        self.assertIn("tijian", data["mcpServers"])

    def test_idempotent_when_called_repeatedly_with_same_config(self):
        connect_tijian(self.config, "coach", self.fake_mcp)
        first_content = (self.workspace / ".mcp.json").read_bytes()
        connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual((self.workspace / ".mcp.json").read_bytes(), first_content)

    def test_raises_conflict_when_existing_tijian_differs_and_preserves_file(self):
        mcp_path = self.workspace / ".mcp.json"
        existing = {"mcpServers": {"tijian": {"command": "/different/tijian-mcp", "args": []}}}
        mcp_path.write_text(json.dumps(existing))
        original_bytes = mcp_path.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(mcp_path.read_bytes(), original_bytes)

    def test_rejects_missing_or_non_executable_binary(self):
        missing = self.root / "nonexistent"
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", missing)
        non_exec = self.root / "non_exec"
        non_exec.touch()
        non_exec.chmod(0o644)
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", non_exec)

    def test_rejects_symlink_mcp_config_without_modifying(self):
        mcp_path = self.workspace / ".mcp.json"
        mcp_path.symlink_to(self.config_path)
        before = self.config_path.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_rejects_malformed_existing_json_without_modifying(self):
        mcp_path = self.workspace / ".mcp.json"
        mcp_path.write_text("{not: valid json}")
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(mcp_path.read_text(), "{not: valid json}")

    def test_cli_connect_tijian_and_mutual_exclusion(self):
        # Default read-only via CLI
        code = main([
            "workflow", "connect-tijian", "coach",
            "--executable", str(self.fake_mcp),
            "--config", str(self.config_path),
            "--json"
        ])
        self.assertEqual(code, 0)
        data = json.loads((self.workspace / ".mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["tijian"]["args"], ["--read-only"])

        # Mutually exclusive flags rejected
        with self.assertRaises(SystemExit):
            main([
                "workflow", "connect-tijian", "coach",
                "--executable", str(self.fake_mcp),
                "--read-only", "--read-write",
                "--config", str(self.config_path)
            ])


class StdioMcpProtocolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-proto-mcp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.config_path)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        create(self.config, "coach", self.workspace)
        self.fake_mcp = self.root / "fake_tijian_mcp.py"
        self.fake_mcp.write_text(FAKE_MCP_SERVER_CODE)
        self.fake_mcp.chmod(0o755)

    def _interact(self, command, args):
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True
        )
        try:
            # 1. initialize
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
            proc.stdin.flush()
            init_res = json.loads(proc.stdout.readline())
            self.assertEqual(init_res["id"], 1)
            self.assertIn("capabilities", init_res["result"])

            # 2. initialized notification
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()

            # 3. tools/list
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
            proc.stdin.flush()
            tools_res = json.loads(proc.stdout.readline())
            self.assertEqual(tools_res["id"], 2)
            return [t["name"] for t in tools_res["result"]["tools"]]
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.terminate()
            proc.wait(timeout=2)

    def test_configured_read_only_mcp_server_exposes_only_read_tools(self):
        connect_tijian(self.config, "coach", self.fake_mcp, read_only=True)
        mcp_config = json.loads((self.workspace / ".mcp.json").read_text())
        server = mcp_config["mcpServers"]["tijian"]
        tools = self._interact(server["command"], server["args"])
        self.assertIn("tijian_problem_get", tools)
        self.assertNotIn("tijian_problem_ensure", tools)

    def test_configured_read_write_mcp_server_exposes_write_tools(self):
        connect_tijian(self.config, "coach", self.fake_mcp, read_only=False)
        mcp_config = json.loads((self.workspace / ".mcp.json").read_text())
        server = mcp_config["mcpServers"]["tijian"]
        tools = self._interact(server["command"], server["args"])
        self.assertIn("tijian_problem_get", tools)
        self.assertIn("tijian_problem_ensure", tools)

    def test_paveway_has_no_python_bridge_or_schema_assembly(self):
        # Verify that Python bridge modules cannot be imported
        for mod in (
            "oicoach_runtime.integrations.tijian_catalog",
            "oicoach_runtime.integrations.tijian_mcp",
            "oicoach_runtime.integrations.tijian",
        ):
            with self.subTest(module=mod), self.assertRaises(ModuleNotFoundError):
                __import__(mod)


if __name__ == "__main__":
    unittest.main()
