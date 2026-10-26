"""Opt-in real upstream CLI/use-case verification in isolated harness profiles."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.config import Config, TijianConfig, config_text, load_config
from oicoach_runtime.integrations.tijian import TijianClient, TijianError, configure, mcp_configuration
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.models import utc_text

ENABLED = bool(os.environ.get("OICOACH_TEST_TIJIAN_SOURCE_CLI") and os.environ.get("OICOACH_TEST_TIJIAN_SOURCE_SEED"))


@unittest.skipUnless(ENABLED, "build the documented Tijian source CLI and set SOURCE_CLI/SOURCE_SEED")
class RealSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_missing_problem_has_actionable_error_without_fetching(self):
        with self.assertRaises(TijianError) as caught:
            await self.client.call("problem_get", {"problem": "CF1A"})
        self.assertEqual(caught.exception.code, "tijian_problem_not_found")
        self.assertEqual(caught.exception.outcome, "no_business_mutation")
        self.assertEqual((await self.client.call("problem_stats", {}))["data"]["total"], 2)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-tijian-source-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {"TIJIAN_RUNTIME_MODE": "harness", "TIJIAN_PROFILE_ROOT": str(self.root / "profile")}
        self.environ_patch = patch.dict(os.environ, self.environment)
        self.environ_patch.start()
        self.addCleanup(self.environ_patch.stop)
        process = await asyncio.create_subprocess_exec(os.environ["OICOACH_TEST_TIJIAN_SOURCE_SEED"],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.config = TijianConfig(enabled=True, executable=os.environ["OICOACH_TEST_TIJIAN_SOURCE_CLI"])
        self.client = TijianClient(self.config)

    async def test_real_problem_history_plan_and_query_roundtrip(self):
        self.assertTrue((await self.client.diagnose(with_data=True))["ok"])
        initial = (await self.client.call("problem_get", {"problem": "P1001"}))["data"]
        self.assertEqual(initial["problem"]["status"], "unseen")
        self.assertIn("statementMd", initial["problem"])
        preview = await self.client.call("problem_status", {"problems": ["P1001"], "status": "explored", "dry_run": True})
        self.assertTrue(preview["dryRun"])
        self.assertEqual((await self.client.call("problem_get", {"problem": "P1001"}))["data"], initial)
        for status in ("explored", "attempted", "solved"):
            await self.client.call("problem_status", {"problems": ["P1001"], "status": status})
        metadata = (await self.client.call("problem_get", {"problem": "P1001"}))["data"]
        self.assertEqual(metadata["problem"]["status"], "solved")
        self.assertEqual([item["action"] for item in metadata["statusHistory"]], ["explored", "attempted", "solved"])
        await self.client.call("problem_edit", {"problem": "P1001", "difficulty": 7.5, "quality": 9})
        self.assertEqual((await self.client.call("problem_get", {"problem": "P1001"}))["data"]["problem"]["quality"], 9)
        await self.client.call("history_import", {"problem": "P1002", "at": "2026-10-17 14:30", "status": "explored"})
        await self.client.call("plan_add", {"problems": ["P1001", "P1002"], "target": "solve", "date": "2026-10-18"})
        await self.client.call("plan_target", {"problem": "P1002", "target": "explore", "date": "2026-10-18"})
        plan = await self.client.call("plan_show", {"date": "2026-10-18"})
        self.assertIn("P1002", json.dumps(plan["data"]))
        await self.client.call("plan_remove", {"problem": "P1001", "date": "2026-10-18"})
        await self.client.call("plan_carry", {"date": "2026-10-19"})
        await self.client.call("plan_clear", {"date": "2026-10-18", "confirm": True})
        daily = await self.client.call("daily_show", {"from": "2026-10-17", "to": "2026-10-18"})
        self.assertGreater(len(daily["data"]["days"]), 0)
        found = await self.client.call("search", {"query": "source integration"})
        self.assertEqual(len(found["data"]), 2)
        listed = await self.client.call("problem_list", {"status": "solved", "has_statement": True})
        self.assertEqual([item["id"] for item in listed["data"]], ["P1001"])

    async def test_persisted_statement_reads_and_duplicate_add_need_no_external_fetch(self):
        # The real upstream harness denies external OJ requests. The seeder
        # stores a test statement through UseCases, not an HTML research file.
        # Every call below spawns a new real CLI, proving disk persistence.
        first = await self.client.call("problem_get", {"problem": "P1001"})
        self.assertTrue(first["data"]["problem"]["statementMd"])
        for _ in range(2):
            current = await self.client.call("problem_get", {"problem": "P1001"})
            self.assertEqual(current["data"], first["data"])
            self.assertFalse(current["changed"])
        existing = await self.client.call("problem_add", {"problems": ["P1001"]})
        self.assertFalse(existing["changed"])
        self.assertIn("已在题库", json.dumps(existing, ensure_ascii=False))
        self.assertEqual((await self.client.call("problem_get", {"problem": "P1001"}))["data"], first["data"])
        self.assertEqual((await self.client.call("problem_stats", {}))["data"]["total"], 2)

    @unittest.skipUnless(os.environ.get("OICOACH_TEST_TIJIAN_NETWORK"), "opt in to a real OJ request in isolated normal data")
    async def test_real_oj_first_collection_persists_for_later_local_reads(self):
        # A fresh XDG directory keeps the real network acceptance out of the
        # user's library. Normal mode is necessary: harness blocks real OJs.
        environment = {"XDG_DATA_HOME": str(self.root / "normal-data"), "TIJIAN_RUNTIME_MODE": "normal"}
        with patch.dict(os.environ, environment):
            client = TijianClient(replace(self.config, timeout_seconds=55))
            added = await client.call("problem_add", {"problems": ["P9868"]})
            self.assertTrue(added["changed"], added["message"])
            problem = (await client.call("problem_get", {"problem": "P9868"}))["data"]["problem"]
            self.assertTrue(problem["statementMd"])
            self.assertEqual((await client.call("problem_stats", {}))["data"]["total"], 1)
            with patch.dict(os.environ, {key: "http://127.0.0.1:1" for key in
                                        ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}):
                cached = await client.call("problem_get", {"problem": "P9868"})
                self.assertEqual(cached["data"]["problem"]["statementMd"], problem["statementMd"])
                repeated = await client.call("problem_add", {"problems": ["P9868"]})
                self.assertFalse(repeated["changed"])
                self.assertIn("已在题库", json.dumps(repeated, ensure_ascii=False))

    async def test_setup_initializes_empty_profile_without_fixture_problems(self):
        path = self.root / "fresh-runtime.toml"
        path.touch(mode=0o600)
        path.write_text(config_text(Config(self.root / "fresh-state", self.root / "fresh-workflows")))
        profile = self.root / "fresh-profile"
        with patch.dict(os.environ, {"TIJIAN_PROFILE_ROOT": str(profile)}):
            report = await configure(path, self.config.executable, True)
            self.assertTrue(report["ok"], report)
            self.assertTrue((profile / "data/tijian.db").is_file())
            configured = load_config(path).tijian
            self.assertTrue(configured.enabled)
            self.assertTrue(configured.read_only)
            result = await TijianClient(configured).call("problem_stats", {})
            self.assertEqual(result["data"]["total"], 0)

    async def test_real_articles_export_and_backup_restore(self):
        article = self.root / "fixture.md"
        article.write_text("# Source integration list\n\n[P1001](https://www.luogu.com.cn/problem/P1001)\n")
        await self.client.call("article_import", {"path": str(article)})
        articles = (await self.client.call("article_list", {}))["data"]
        self.assertEqual(len(articles), 1)
        article_id = articles[0]["id"]
        shown = await self.client.call("article_show", {"id": article_id})
        self.assertIn("P1001", json.dumps(shown["data"]))
        output = self.root / "export.md"
        await self.client.call("article_export", {"id": article_id, "output": str(output), "confirm": True})
        self.assertIn("P1001", output.read_text())
        backup = self.root / "backup.zip"
        await self.client.call("backup_export", {"path": str(backup), "format": "native", "confirm": True})
        verified = await self.client.call("backup_verify", {"path": str(backup)})
        self.assertEqual(verified["data"]["problems"], 2)
        await self.client.call("problem_remove", {"problem": "P1002", "confirm": True})
        self.assertEqual((await self.client.call("problem_stats", {}))["data"]["total"], 1)
        await self.client.call("backup_import", {"path": str(backup), "confirm": True})
        self.assertEqual((await self.client.call("problem_stats", {}))["data"]["total"], 2)

    async def test_network_and_desktop_dependencies_fail_without_fake_success(self):
        # Harness network policy prevents contacting real OJs or using user credentials.
        for name, args in (("problem_add", {"problems": ["P1003"]}),
                           ("problem_refetch", {"problems": ["P1001"]}),
                           ("problem_vector_compute", {"problems": ["P1001"]})):
            try:
                result = await self.client.call(name, args)
            except TijianError as error:
                self.assertEqual(error.outcome, "uncertain")
            else:
                # Upstream batch commands can return a valid envelope reporting per-item failure.
                self.assertFalse(result["changed"], (name, result))

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install .[tijian]")
    async def test_sdk_mcp_calls_real_upstream_status_and_statement(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        path = self.root / "runtime.toml"
        path.write_text(config_text(Config(self.root / "state", self.root / "workflows", tijian=self.config)))
        entry = mcp_configuration(path)["mcpServers"]["tijian"]
        parameters = StdioServerParameters(command=entry["command"], args=entry["args"], env=self.environment)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                changed = await session.call_tool("tijian_problem_status", {"problems": ["P1001"], "status": "explored"})
                self.assertFalse(changed.isError)
                read = await session.call_tool("tijian_problem_get", {"problem": "P1001"})
                self.assertFalse(read.isError)
                self.assertEqual(read.structuredContent["data"]["problem"]["status"], "explored")

    @unittest.skipUnless(os.environ.get("OICOACH_TEST_TIJIAN_NETWORK") and
                         os.environ.get("OICOACH_TEST_TIJIAN_OMP_CONFIG"),
                         "opt in to real OJ collection and model report in an isolated empty library")
    async def test_model_collects_missing_statements_and_finishes_report(self):
        from importlib.resources import files
        from oicoach_runtime.adapters.omp_rpc import OmpRpc
        from oicoach_runtime.customization import show
        from oicoach_runtime.proxy import child_environment
        from oicoach_runtime.workflows import Workflow

        source = Path(os.environ["OICOACH_TEST_TIJIAN_OMP_CONFIG"])
        base = load_config(source)
        inference = show(base, os.environ.get("OICOACH_TEST_WORKFLOW", "test01"))["inference"]
        environment = dict(child_environment(source), TIJIAN_RUNTIME_MODE="normal",
                           XDG_DATA_HOME=str(self.root / "normal-data"))
        sessions = self.root / "sessions"
        sessions.mkdir(mode=0o700)
        config = replace(base, state_dir=self.root / "state", workflow_root=self.root / "workflows",
            server=replace(base.server, socket_path=str(self.root / "runtime.sock")),
            tijian=replace(self.config, timeout_seconds=55, read_only=False),
            agent=replace(base.agent, model=inference["model"], thinking=inference["thinking"],
                tools_enabled=True, skills_enabled=False, extensions_enabled=False, rules_enabled=False,
                turn_timeout_seconds=300, settle_timeout_seconds=15, graceful_stop_seconds=1))
        path = self.root / "runtime.toml"
        path.touch(mode=0o600)
        path.write_text(config_text(config))
        template = files("oicoach_runtime").joinpath("bundled/coach.md").read_text()
        workflow = Workflow("test", "1", self.root, "per_task", {"wake": template}, instructions_in_system=True)
        envelope = {"event_id": str(uuid.uuid4()), "run_id": str(uuid.uuid4()), "workflow_id": "test",
            "task_id": "report", "reason": "用户请求", "payload": {
                "message": "根据 Tijian 内的信息分析近几年 NOIP / 联合省选 / NOI 的题目风格，给出报告"},
            "available_tools": [{"name": "tijian", "argv_prefix": [sys.executable, "-m", "oicoach_runtime", "tijian"],
                "config_path": str(path), "commands": ["tools", "doctor", "call"], "read_only": False}]}
        instructions, prompt = workflow.render_parts("wake", envelope)
        with patch.dict(os.environ, environment):
            client = TijianClient(config.tijian)
            # Upstream search initializes a brand-new normal-mode database;
            # stats requires its schema to exist already.
            await client.call("search", {"query": "NOIP"})
            self.assertEqual((await client.call("problem_stats", {}))["data"]["total"], 0)
            adapter = OmpRpc(config, config_path=path)
            try:
                await adapter.start({"workspace": str(self.root), "session_dir": str(sessions), "session_file": None,
                                     "workflow_instructions": instructions}, envelope["run_id"])
                await adapter.ready()
                result = await adapter.prompt(envelope["run_id"], prompt, lambda: None, lambda _: None)
                self.assertEqual(result, "completed")
                self.assertTrue((await adapter.state())["isSettled"])
                messages = (await adapter.command("get_messages"))["messages"]
                report = adapter.assistant_output() or await adapter.command("get_last_assistant_text")
                self.assertGreater(len(report.get("text", "")), 500)
                collected = (await client.call("problem_list", {}))["data"]
                self.assertGreater(len(collected), 0)
                self.assertTrue(any(problem["id"] in report["text"] for problem in collected),
                                "Report must reference a problem actually collected in this run")
                # Confirm statement evidence actually reached the model, rather
                # than accepting a successful add acknowledgement as reading.
                results = [m for m in messages if m.get("role") == "toolResult" and not m.get("isError")]
                statements = []
                for message in results:
                    for block in message.get("content", []):
                        if block.get("type") != "text":
                            continue
                        try:
                            document, _ = json.JSONDecoder().raw_decode(block["text"].lstrip())
                        except (ValueError, KeyError):
                            continue
                        if isinstance(document, dict):
                            data = document.get("result", {}).get("data")
                            if isinstance(data, dict) and data.get("problem", {}).get("statementMd"):
                                statements.append(data["problem"]["id"])
                self.assertTrue(statements, "No actual problem_get statement reached the model")
                print(json.dumps({"acceptance": "tijian-model-report", "collected": len(collected),
                                  "statements_read": sorted(set(statements)), "report_chars": len(report["text"])}))
            finally:
                await adapter.stop(abort=True)

    @unittest.skipUnless(importlib.util.find_spec("mcp") and os.environ.get("OICOACH_TEST_TIJIAN_OMP_CONFIG"),
                         "set OICOACH_TEST_TIJIAN_OMP_CONFIG for an actual model + upstream source roundtrip")
    async def test_real_omp_model_reads_real_tijian_through_mcp(self):
        from oicoach_runtime.adapters.omp_rpc import OmpRpc
        base = load_config(Path(os.environ["OICOACH_TEST_TIJIAN_OMP_CONFIG"]))
        sessions = self.root / "sessions"
        sessions.mkdir(mode=0o700)
        config = replace(base, state_dir=self.root / "runtime-state", workflow_root=self.root / "workflows",
            tijian=replace(self.config, read_only=True), agent=replace(base.agent,
                tools_enabled=False, extensions_enabled=False, skills_enabled=False, rules_enabled=False,
                startup_timeout_seconds=30, turn_timeout_seconds=90, settle_timeout_seconds=15, graceful_stop_seconds=1))
        path = self.root / "runtime.toml"
        path.write_text(config_text(config))
        mcp = mcp_configuration(path)
        mcp["mcpServers"]["tijian"]["env"] = self.environment
        (self.root / ".mcp.json").write_text(json.dumps(mcp))
        adapter = OmpRpc(config, config_path=path)
        try:
            run_id = str(uuid.uuid4())
            await adapter.start({"workspace": str(self.root), "session_dir": str(sessions), "session_file": None}, run_id)
            await adapter.ready()
            tool_names = []
            async with asyncio.timeout(15):
                while not any(name.endswith("tijian_problem_get") for name in tool_names):
                    tool_names = [tool["name"] for tool in (await adapter.state()).get("dumpTools", [])]
                    await asyncio.sleep(0.05)
            result = await adapter.prompt(run_id,
                "Integration test: call only mcp__tijian_problem_get with problem='P1001', exactly once. "
                "Do not use shell, filesystem or any other tools. After receiving its result, reply only DONE.",
                lambda: None, lambda _: None)
            self.assertEqual(result, "completed")
            messages = (await adapter.command("get_messages"))["messages"]
            evidence = [message for message in messages if message.get("role") == "toolResult" and
                        "tijian" in message.get("toolName", "")]
            self.assertEqual(len(evidence), 1, "No unique actual Tijian tool result; history shape=" +
                repr([(m.get("role"), m.get("toolName"), sorted(m)) for m in messages]))
            self.assertFalse(evidence[0].get("isError", False))
            self.assertIn("P1001 source integration", json.dumps(evidence[0], ensure_ascii=False))
        finally:
            await adapter.stop(abort=True)

    @unittest.skipUnless(importlib.util.find_spec("mcp") and os.environ.get("OICOACH_TEST_TIJIAN_OMP_CONFIG"),
                         "set OICOACH_TEST_TIJIAN_OMP_CONFIG for the full daemon/model/Tijian project path")
    async def test_project_cli_daemon_model_tijian_and_persisted_completion(self):
        from oicoach_runtime.adapters.omp_rpc import OmpRpc
        base = load_config(Path(os.environ["OICOACH_TEST_TIJIAN_OMP_CONFIG"]))
        project = self.root / "project"
        script = Path(__file__).resolve().parents[1] / "examples/prepare_tijian.py"
        prepare = await asyncio.create_subprocess_exec(sys.executable, str(script), "--directory", str(project),
            "--tijian-executable", self.config.executable, "--omp-executable", base.agent.executable,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(prepare.communicate(), 15)
        self.assertEqual(prepare.returncode, 0, stderr.decode())
        path = project / "config.toml"
        config = load_config(path)
        config = replace(config, server=replace(config.server, poll_interval_seconds=0.05),
            agent=replace(base.agent, tools_enabled=False, extensions_enabled=False, skills_enabled=False,
                rules_enabled=False, startup_timeout_seconds=30, turn_timeout_seconds=90,
                settle_timeout_seconds=15, graceful_stop_seconds=1))
        path.write_text(config_text(config))
        mcp = mcp_configuration(path)
        mcp["mcpServers"]["tijian"]["env"] = self.environment
        (project / "workspace/.mcp.json").write_text(json.dumps(mcp))
        (project / "workflows/tijian/event.md").write_text(
            "Integration test: call mcp__tijian_problem_get exactly once with problem='P1001'. "
            "Do not use any other tools. After the tool returns, reply only DONE.\n"
            "Runtime event data: ${event_json}\n")

        async def api(command, arguments=None):
            return await asyncio.to_thread(request, config.server.socket_path, command, arguments)

        with (project / "daemon.log").open("w") as log:
            daemon = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "serve",
                "--config", str(path), stdout=log, stderr=log)
            try:
                async with asyncio.timeout(10):
                    while True:
                        self.assertIsNone(daemon.returncode, "Daemon exited before becoming ready")
                        try:
                            if (await api("status"))["daemon"] == "running":
                                break
                        except RuntimeContractError:
                            pass
                        await asyncio.sleep(0.05)
                due = utc_text(datetime.now(timezone.utc) + timedelta(seconds=2))
                cli = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "schedule",
                    "--config", str(path), "--workflow", "tijian", "--task", "whole-project",
                    "--at", due, "--reason", "full project running test", "--dedupe-key", "whole-project-1",
                    "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(cli.communicate(), 10)
                self.assertEqual(cli.returncode, 0, stderr.decode())
                event = json.loads(stdout)["result"]
                # The scheduling CLI has exited; the separate daemon owns execution.
                async with asyncio.timeout(150):
                    while True:
                        current = await api("events.show", {"event_id": event["event_id"]})
                        if current["status"] in ("succeeded", "failed", "blocked", "uncertain", "dead_letter"):
                            break
                        await asyncio.sleep(0.1)
                self.assertEqual(current["status"], "succeeded", current)
                self.assertEqual(current["attempt"], 1)
                run = await api("runs.show", {"run_id": current["run_id"]})
                self.assertEqual(run["prompt_result_status"], "completed")
                self.assertIsNotNone(run["acknowledged_at"])
                self.assertIsNotNone(run["settled_at"])
                self.assertEqual(run["agent_exit_code"], 0)
                self.assertEqual(len(await api("runs.list")), 1)
            finally:
                if daemon.returncode is None:
                    daemon.terminate()
                try:
                    await asyncio.wait_for(daemon.wait(), 10)
                except TimeoutError:
                    daemon.kill()
                    await daemon.wait()

        # Verify the committed session after daemon shutdown, without another prompt.
        with closing(sqlite3.connect(config.database_path)) as database:
            database.row_factory = sqlite3.Row
            binding = dict(database.execute("SELECT * FROM session_bindings WHERE binding_id=?", (run["binding_id"],)).fetchone())
        self.assertTrue(Path(binding["session_file"]).is_file())
        self.assertEqual(binding["last_successful_run_id"], run["run_id"])
        inspector = OmpRpc(config, config_path=path)
        try:
            await inspector.start(binding, str(uuid.uuid4()))
            await inspector.ready()
            state = await inspector.state()
            self.assertEqual(state["sessionId"], binding["session_id"])
            messages = (await inspector.command("get_messages"))["messages"]
            evidence = [message for message in messages if message.get("role") == "toolResult" and
                        "tijian" in message.get("toolName", "")]
            self.assertEqual(len(evidence), 1)
            self.assertFalse(evidence[0].get("isError", False))
            self.assertIn("P1001 source integration", json.dumps(evidence[0], ensure_ascii=False))
        finally:
            await inspector.stop(abort=True)
