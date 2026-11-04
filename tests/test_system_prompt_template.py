"""Comprehensive acceptance tests for Paveway's OMP native Handlebars system prompt template."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from oicoach_runtime import customization
from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.config import AgentConfig, Config, ServerConfig, config_text, load_config
from oicoach_runtime.doctor import probe_omp
from oicoach_runtime.prompt_composer import (
    compose,
    compose_template,
    escape_for_handlebars,
    load_core,
    load_role_policy,
    manifest,
)
from oicoach_runtime.workflow_setup import connect_tijian, create
from oicoach_runtime.workflows import WorkflowProvider


FAKE_MCP_SERVER_CODE = """#!/usr/bin/env python3
import sys, json

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
            {"name": "tijian_problem_get", "description": "Read problem statement", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}}
        ]
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


class SystemPromptTemplateUnitTests(unittest.TestCase):
    """Unit tests for template composition, escaping, and snapshot argument generation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-tmpl-unit-")
        self.root = Path(self.temp_dir.name)
        self.session_dir = self.root / "session"
        self.session_dir.mkdir(mode=0o700)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_launch_arguments_and_snapshot_files(self):
        """1. Template entrance: snapshot produces --system-prompt-template, no --system-prompt."""
        settings = {
            "schema_version": 1,
            "revision": 1,
            "system_prompt": {"mode": "append", "text": "CUSTOM_APPEND_PROMPT"},
            "skills": [],
            "persona": "MY_GPT_PERSONA",
        }
        argv = customization.snapshot(settings, self.session_dir, "run1", True, base="oi-coach")
        self.assertIn("--system-prompt-template", argv)
        self.assertNotIn("--system-prompt", argv)

        tmpl_idx = argv.index("--system-prompt-template")
        template_file = Path(argv[tmpl_idx + 1])
        self.assertTrue(template_file.is_file())
        self.assertEqual(template_file.name, "system-template.md")

        # system.md is also written for static audit
        system_file = template_file.parent / "system.md"
        self.assertTrue(system_file.is_file())

        system_text = system_file.read_text(encoding="utf-8")
        self.assertIn("# Paveway Core", system_text)
        self.assertIn("OI 训练与教学策略", system_text)
        self.assertIn("CUSTOM_APPEND_PROMPT", system_text)
        self.assertIn("Configured GPT Persona:\nMY_GPT_PERSONA", system_text)
        self.assertNotIn("{{#if skills.length}}", system_text)

        tmpl_text = template_file.read_text(encoding="utf-8")
        self.assertIn("# Paveway Core", tmpl_text)
        self.assertIn("OI 训练与教学策略", tmpl_text)
        self.assertIn("CUSTOM_APPEND_PROMPT", tmpl_text)
        self.assertIn("{{#if skills.length}}", tmpl_text)
        self.assertIn("{{#if xdevTools.length}}", tmpl_text)
        self.assertIn("{{#if toolInfo.length}}", tmpl_text)

        manifest_file = template_file.parent / "prompt-manifest.json"
        self.assertTrue(manifest_file.is_file())
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest_data["template_mode"], "omp-handlebars")
        self.assertIn("template_digest", manifest_data)
        self.assertIn("system_digest", manifest_data)

    def test_escaping_user_text_literal_safety(self):
        """6. Literal escaping: special syntax in user prompt and persona is escaped safely."""
        tricky_user_prompt = (
            "Special syntax tests:\n"
            "{{example}}\n"
            "{{#if xdevTools.length}}\n"
            "{{/if}}\n"
            "${event_json}\n"
            r"\path\to\file with \\ backslashes\n"
            'Quotes: "double" and \'single\''
        )
        settings = {
            "schema_version": 1,
            "revision": 1,
            "system_prompt": {"mode": "append", "text": tricky_user_prompt},
            "skills": [],
            "persona": "Persona with {{mustache}} and ${var}",
        }
        template_text = compose_template(settings, base="oi-coach")
        # Escaped {{ became {{default "{{" ""}}
        self.assertNotIn("{{example}}", template_text)
        self.assertIn('{{default "{{" ""}}example}}', template_text)
        self.assertIn('{{default "{{" ""}}#if xdevTools.length}}', template_text)
        self.assertIn('{{default "{{" ""}}/if}}', template_text)
        self.assertIn('{{default "{{" ""}}mustache}}', template_text)
        # ${...} and backslashes remain intact
        self.assertIn("${event_json}", template_text)
        self.assertIn("${var}", template_text)
        self.assertIn(r"\path\to\file with \\ backslashes\n", template_text)

    def test_doctor_probe_requires_system_prompt_template_flag(self):
        """Doctor check fails if --system-prompt-template flag is missing."""
        executable = self.root / "fake-omp"
        # Omits --system-prompt-template
        executable.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "omp/18.15.0";;\n--help) echo "--cwd --session-dir --resume --mode --no-ui";;\nesac\n')
        executable.chmod(0o700)
        checks = probe_omp(str(executable))
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertIn("--system-prompt-template", checks[0]["detail"])

        # Includes --system-prompt-template
        executable.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "omp/18.15.0";;\n--help) echo "--cwd --session-dir --resume --mode --no-ui --system-prompt-template";;\nesac\n')
        checks = probe_omp(str(executable))
        self.assertEqual(checks[0]["status"], "PASS")

    def test_tool_inventory_template_expression_handles_missing_label(self):
        """Verify tool inventory template uses OMP official conditional label expression."""
        from oicoach_runtime.prompt_composer import DYNAMIC_TEMPLATE_SECTIONS
        self.assertIn(
            "- {{#if label}}{{label}}: `{{name}}`{{else}}`{{name}}`{{/if}}",
            DYNAMIC_TEMPLATE_SECTIONS,
        )
        self.assertNotIn("- {{label}}: `{{name}}`", DYNAMIC_TEMPLATE_SECTIONS)


@unittest.skipUnless(shutil.which("omp"), "requires real omp binary")
class RealOmpTemplateSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Real OMP execution tests without model calls via get_state inspection."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pw-omp-smoke-")
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.session_dir = self.root / "session"
        self.session_dir.mkdir(mode=0o700)
        self.config_path = self.root / "config.toml"
        self.config = Config(
            state_dir=self.root / "state",
            workflow_root=self.root / "workflows",
            server=ServerConfig(socket_path=str(self.root / "test.sock")),
            agent=AgentConfig(executable="omp", tools_enabled=True, skills_enabled=True),
        )
        self.config_path.write_text(config_text(self.config))

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_real_omp_renders_static_layers_and_dynamic_tools(self):
        """2, 3, 6, 7: Real OMP renders 4 layers, dynamic tools, xd:// devices, without unparsed Handlebars."""
        user_text = (
            "Special syntax in prompt:\n"
            "{{example}}\n"
            "{{#if xdevTools.length}}\n"
            "{{/if}}\n"
            "${event_json}\n"
            r"\path\to\code"
        )
        settings = {
            "schema_version": 1,
            "revision": 1,
            "system_prompt": {"mode": "append", "text": user_text},
            "skills": [],
            "persona": "COACH_PERSONA_VERIFIED",
        }
        memory_data = {"notes": [{"key": "status", "content": "SOLVING_P1001"}]}
        argv = customization.snapshot(
            settings, self.session_dir, "smoke1", True,
            memory_context=memory_data, base="oi-coach"
        )
        template_file = argv[argv.index("--system-prompt-template") + 1]

        # Launch real OMP in rpc mode and inspect get_state
        process = await asyncio.create_subprocess_exec(
            "omp", "--cwd", str(self.workspace), "--session-dir", str(self.session_dir),
            "--mode", "rpc", "--no-session", "--system-prompt-template", str(template_file),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            req = json.dumps({"id": "smoke-state", "type": "get_state"}) + "\n"
            process.stdin.write(req.encode())
            await process.stdin.drain()

            system_prompts = []
            while line := await process.stdout.readline():
                msg = json.loads(line.decode())
                if msg.get("id") == "smoke-state":
                    system_prompts = msg.get("data", {}).get("systemPrompt", [])
                    break

            self.assertTrue(len(system_prompts) >= 1)
            full_prompt = "\n\n".join(system_prompts)

            # 2. Paveway instructions integrity
            self.assertIn("# Paveway Core", full_prompt)
            self.assertIn("OI 训练与教学策略", full_prompt)
            self.assertIn("COACH_PERSONA_VERIFIED", full_prompt)
            self.assertIn("SOLVING_P1001", full_prompt)

            # Ensure Core and Role Policy are not duplicated
            self.assertEqual(full_prompt.count("# Paveway Core"), 1)
            self.assertEqual(full_prompt.count("OI 训练与教学策略"), 1)

            # 6. User text literal safety (preserved exactly)
            self.assertIn("{{example}}", full_prompt)
            self.assertIn("{{#if xdevTools.length}}", full_prompt)
            self.assertIn("{{/if}}", full_prompt)
            self.assertIn("${event_json}", full_prompt)
            self.assertIn(r"\path\to\code", full_prompt)
            self.assertNotIn('{{default "{{" ""}}', full_prompt)

            # 3. Dynamic tool inventory rendered
            self.assertIn("# Tool Inventory", full_prompt)
            self.assertIn("- Read: `read`", full_prompt)
            self.assertIn("- Bash: `bash`", full_prompt)
            self.assertIn("- Write: `write`", full_prompt)
            self.assertNotIn("{{toolInventory}}", full_prompt)
            self.assertNotIn("{{#if toolInfo.length}}", full_prompt)

            # xd:// Tool Devices
            self.assertIn("# xd:// Tool Devices", full_prompt)
            self.assertIn("Write JSON args as `content` to `xd://<tool>` via `write`.", full_prompt)
            self.assertIn("## Additional devices (docs on demand)", full_prompt)
            self.assertIn("xd://ast_edit", full_prompt)

            # Internal URLs
            self.assertIn("# Internal URLs", full_prompt)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_tool_inventory_label_rendering_and_descriptors_mode(self):
        """Test tool inventory rendering with/without label and in descriptors mode via real OMP."""
        # 1. Test template evaluating with and without label through real OMP Handlebars compiler
        test_tmpl = self.root / "tool-label-test.md"
        test_tmpl.write_text(
            "{{#with (lookup toolInfo 0)}}\n"
            "With label: - {{#if label}}{{label}}: `{{name}}`{{else}}`{{name}}`{{/if}}\n"
            "{{/with}}\n"
            "{{#with workspaceTree}}\n"
            "Without label: - {{#if label}}{{label}}: `custom_tool`{{else}}`custom_tool`{{/if}}\n"
            "{{/with}}\n"
        )
        process = await asyncio.create_subprocess_exec(
            "omp", "--cwd", str(self.workspace), "--session-dir", str(self.session_dir),
            "--mode", "rpc", "--no-session", "--system-prompt-template", str(test_tmpl),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            req = json.dumps({"id": "label-check", "type": "get_state"}) + "\n"
            process.stdin.write(req.encode())
            await process.stdin.drain()
            system_prompts = []
            while line := await process.stdout.readline():
                msg = json.loads(line.decode())
                if msg.get("id") == "label-check":
                    system_prompts = msg.get("data", {}).get("systemPrompt", [])
                    break
            rendered = "\n".join(system_prompts)
            self.assertIn("With label: - Read: `read`", rendered)
            self.assertIn("Without label: - `custom_tool`", rendered)
            self.assertNotIn("- : `custom_tool`", rendered)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

        # 2. Test when not toolListMode (Gemini model activates inline descriptors mode -> toolInventory)
        settings = {
            "schema_version": 1,
            "revision": 1,
            "system_prompt": {"mode": "append", "text": ""},
            "skills": [],
        }
        argv = customization.snapshot(settings, self.session_dir, "descriptors_run", True, base="oi-coach")
        template_file = argv[argv.index("--system-prompt-template") + 1]
        process_desc = await asyncio.create_subprocess_exec(
            "omp", "--model", "gemini-2.5-flash", "--cwd", str(self.workspace),
            "--session-dir", str(self.session_dir), "--mode", "rpc", "--no-session",
            "--system-prompt-template", str(template_file),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            req = json.dumps({"id": "desc-check", "type": "get_state"}) + "\n"
            process_desc.stdin.write(req.encode())
            await process_desc.stdin.drain()
            system_prompts = []
            while line := await process_desc.stdout.readline():
                msg = json.loads(line.decode())
                if msg.get("id") == "desc-check":
                    system_prompts = msg.get("data", {}).get("systemPrompt", [])
                    break
            full_prompt = "\n\n".join(system_prompts)
            # In descriptors mode, toolInventory expands function schemas
            self.assertIn("namespace functions", full_prompt)
            # Should not render the toolListMode simple list
            self.assertNotIn("- Read: `read`", full_prompt)
        finally:
            if process_desc.returncode is None:
                process_desc.kill()
                await process_desc.wait()

    async def test_real_omp_with_mcp_fixture(self):
        """4. xd:// MCP device verification: connecting an MCP server registers routes and instructions."""
        fake_mcp = self.root / "fake_mcp.py"
        fake_mcp.write_text(FAKE_MCP_SERVER_CODE)
        fake_mcp.chmod(0o755)

        omp_dir = self.workspace / ".omp"
        omp_dir.mkdir(mode=0o700)
        (omp_dir / "mcp.json").write_text(json.dumps({
            "mcpServers": {
                "tijian": {
                    "command": str(fake_mcp),
                    "args": []
                }
            }
        }))

        settings = {
            "schema_version": 1,
            "revision": 1,
            "system_prompt": {"mode": "append", "text": "MCP_SMOKE_TEST"},
            "skills": [],
        }
        argv = customization.snapshot(settings, self.session_dir, "mcp_run", True, base="oi-coach")
        template_file = argv[argv.index("--system-prompt-template") + 1]

        process = await asyncio.create_subprocess_exec(
            "omp", "--cwd", str(self.workspace), "--session-dir", str(self.session_dir),
            "--mode", "rpc", "--no-session", "--system-prompt-template", str(template_file),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            # Wait for MCP initialization and query state
            await asyncio.sleep(1)
            req = json.dumps({"id": "mcp-state", "type": "get_state"}) + "\n"
            process.stdin.write(req.encode())
            await process.stdin.drain()

            system_prompts = []
            while line := await process.stdout.readline():
                msg = json.loads(line.decode())
                if msg.get("id") == "mcp-state":
                    system_prompts = msg.get("data", {}).get("systemPrompt", [])
                    break

            full_prompt = "\n\n".join(system_prompts)

            # Verify MCP tool route is registered dynamically in project context
            self.assertIn("## MCP Tool Routes", full_prompt)
            self.assertIn('"tijian_problem_get" → `xd://mcp__tijian_problem_get`', full_prompt)

            # Verify xd:// devices instructions are present in prompt[0]
            self.assertIn("# xd:// Tool Devices", full_prompt)
            self.assertIn("Write JSON args as `content` to `xd://<tool>` via `write`.", full_prompt)

            # Core does NOT hardcode tijian_problem_get
            core_content = load_core()
            self.assertNotIn("tijian_problem_get", core_content)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_skills_indexed_and_content_readable_without_full_injection(self):
        """5. Skills regression: paveway-tijian is indexed, body is not embedded, skill:// is readable."""
        create(self.config, "coach", self.workspace, template="coach")
        workflow = WorkflowProvider(self.config.workflow_root).describe("coach")
        self.assertIn("paveway-tijian", workflow.builtin_skills)

        argv = customization.snapshot(
            workflow.customization, self.session_dir, "skill_run", True,
            base=workflow.base, builtin_skills=workflow.builtin_skills
        )
        template_file = argv[argv.index("--system-prompt-template") + 1]
        config_file = argv[argv.index("--config") + 1]

        process = await asyncio.create_subprocess_exec(
            "omp", "--cwd", str(self.workspace), "--session-dir", str(self.session_dir),
            "--mode", "rpc", "--no-session", "--system-prompt-template", str(template_file),
            "--config", str(config_file),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            req = json.dumps({"id": "skill-state", "type": "get_state"}) + "\n"
            process.stdin.write(req.encode())
            await process.stdin.drain()

            system_prompts = []
            while line := await process.stdout.readline():
                msg = json.loads(line.decode())
                if msg.get("id") == "skill-state":
                    system_prompts = msg.get("data", {}).get("systemPrompt", [])
                    break

            full_prompt = "\n\n".join(system_prompts)

            # Skill is indexed in <skills>
            self.assertIn("<skills>", full_prompt)
            self.assertIn("paveway-tijian", full_prompt)

            # Skill full body is NOT injected into system prompt
            self.assertNotIn("tijian_problem_content_ensure", full_prompt)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

        # Verify omp read skill://paveway-tijian:raw can read the skill content
        env = dict(os.environ, PI_CONFIG_FILES=str(config_file))
        read_res = subprocess.run(
            ["omp", "read", "skill://paveway-tijian:raw"],
            cwd=str(self.workspace), env=env,
            capture_output=True, text=True, timeout=10
        )
        self.assertEqual(read_res.returncode, 0, read_res.stderr)
        self.assertIn("paveway-tijian", read_res.stdout)
        self.assertIn("tijian_problem_content_ensure", read_res.stdout)

if __name__ == "__main__":
    unittest.main()
