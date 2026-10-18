import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oicoach_runtime.config import Config, TijianConfig, config_text, load_config
from oicoach_runtime.errors import ConfigError, RuntimeContractError
from oicoach_runtime.integrations.tijian import TijianClient, TijianError, catalog, mcp_configuration
from oicoach_runtime.integrations.tijian_catalog import OPERATIONS

HAS_MCP = importlib.util.find_spec("mcp") is not None

# Deliberately independent expectations for every exposed operation's native syntax.
CASES = [
    ("help", {}, ["help"]),
    ("problem_get", {"problem": "P1001"}, ["problem", "get", "P1001"]),
    ("problem_list", {"status": "solved", "has_statement": True, "original": "P1", "limit": 3},
     ["problem", "list", "--status", "solved", "--has-statement", "--original", "P1", "--limit", "3"]),
    ("problem_stats", {}, ["problem", "stats"]),
    ("problem_add", {"problems": ["P1", "P2"]}, ["problem", "add", "P1", "P2"]),
    ("problem_status", {"problems": ["P1", "P2"], "status": "胡"}, ["problem", "status", "P1", "P2", "--to", "胡"]),
    ("problem_refetch", {"all": True, "oj": "luogu", "statement_only": True},
     ["problem", "refetch", "--all", "--oj", "luogu", "--statement-only"]),
    ("problem_vector_compute", {"problems": ["P1"]}, ["problem", "vector", "compute", "P1"]),
    ("problem_edit", {"problem": "P1", "quality": 8.5}, ["problem", "edit", "P1", "--quality", "8.5"]),
    ("problem_remove", {"problem": "P1", "confirm": True}, ["problem", "remove", "P1", "--yes"]),
    ("history_import", {"problem": "P1", "at": "2026-10-18 15:30", "status": "过"},
     ["history", "import", "P1", "--at", "2026-10-18 15:30", "--to", "过"]),
    ("search", {"query": "動態規劃 with spaces", "limit": 2}, ["search", "動態規劃 with spaces", "--limit", "2"]),
    ("plan_add", {"problems": ["P1"], "target": "写", "date": "2026-10-18"},
     ["plan", "add", "P1", "--target", "写", "--date", "2026-10-18"]),
    ("plan_remove", {"problem": "P1"}, ["plan", "remove", "P1"]),
    ("plan_target", {"problem": "P1", "target": "explore"}, ["plan", "target", "P1", "explore"]),
    ("plan_show", {"date": "2026-10-18"}, ["plan", "show", "--date", "2026-10-18"]),
    ("plan_carry", {}, ["plan", "carry"]),
    ("plan_clear", {"dry_run": True}, ["plan", "clear", "--yes", "--dry-run"]),
    ("daily_show", {"from": "2026-10-01", "to": "2026-10-18"}, ["daily", "show", "--from", "2026-10-01", "--to", "2026-10-18"]),
    ("backup_export", {"path": "/tmp/tijian backup.zip", "format": "native", "confirm": True},
     ["backup", "export", "/tmp/tijian backup.zip", "--format", "native"]),
    ("backup_import", {"path": "/tmp/backup.zip", "dry_run": True}, ["backup", "import", "/tmp/backup.zip", "--dry-run"]),
    ("backup_verify", {"path": "/tmp/backup.zip"}, ["backup", "verify", "/tmp/backup.zip"]),
    ("article_import", {"path": "/tmp/list.md"}, ["article", "import", "/tmp/list.md"]),
    ("article_list", {}, ["article", "list"]),
    ("article_show", {"id": "abc"}, ["article", "show", "abc"]),
    ("article_export", {"id": "abc", "output": "/tmp/out.md", "confirm": True}, ["article", "show", "abc", "--output", "/tmp/out.md"]),
]


class CatalogTests(unittest.TestCase):
    def test_all_operation_mappings_and_schemas(self):
        self.assertEqual({name for name, _, _ in CASES}, set(OPERATIONS))
        for name, arguments, expected in CASES:
            with self.subTest(name=name):
                self.assertEqual(OPERATIONS[name].arguments(arguments), expected)
                if HAS_MCP:
                    import jsonschema
                    jsonschema.Draft202012Validator.check_schema(OPERATIONS[name].schema())
                    jsonschema.validate(arguments, OPERATIONS[name].schema())
        self.assertEqual(len(catalog()["tools"]), 26)

    def test_untrusted_flags_and_invalid_types_fail_before_spawn(self):
        for name, args in [
            ("problem_get", {"problem": "--dry-run"}), ("problem_get", {"problem": "x\nPRIVATE"}),
            ("problem_get", {"problem": "P1", "other": True}), ("problem_get", {}),
            ("problem_status", {"problems": ["P1", "--yes"], "status": "solved"}),
            ("problem_status", {"problems": ["P1"], "status": "read_solution"}),
            ("problem_list", {"limit": True}), ("problem_list", {"limit": 10001}),
            ("problem_edit", {"problem": "P1", "difficulty": float("nan")}),
            ("problem_edit", {"problem": "P1", "difficulty": 10 ** 400}),
            ("problem_edit", {"problem": "P1"}), ("problem_remove", {"problem": "P1"}),
            ("problem_remove", {"problem": "P1", "confirm": "true"}),
            ("problem_refetch", {"all": True, "problems": ["P1"]}),
            ("problem_refetch", {"problems": ["P1"], "oj": "luogu"}),
            ("problem_vector_compute", {}), ("plan_add", {"problems": []}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(RuntimeContractError):
                OPERATIONS[name].arguments(args)

    def test_config_compatibility_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            config = Config(root / "state", root / "workflows")
            text = config_text(config)
            path.write_text(text.split("[tijian]")[0])
            self.assertFalse(load_config(path).tijian.enabled)
            for change in ('enabled = "true"', 'executable = "./tijian"', 'max_output_bytes = 1', 'timeout_seconds = nan'):
                lines = text.splitlines()
                key = change.split(" =")[0]
                section = lines.index("[tijian]")
                for i in range(section + 1, len(lines)):
                    if lines[i].startswith(key + " ="):
                        lines[i] = change
                path.write_text("\n".join(lines))
                with self.subTest(change=change), self.assertRaises(ConfigError):
                    load_config(path)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake_tijian.py"
        shutil.copyfile(Path(__file__).with_name("fake_tijian.py"), self.fake)
        self.fake.chmod(0o700)
        self.config = TijianConfig(enabled=True, executable=str(self.fake), timeout_seconds=3)
        self.client = TijianClient(self.config)

    async def test_all_operations_reach_json_cli_with_exact_tokens(self):
        for name, args, expected in CASES:
            await self.client.call(name, args)
            actual = json.loads((self.root / "trace.jsonl").read_text().splitlines()[-1])
            self.assertEqual(actual, expected)

    async def test_status_roundtrip_dry_run_and_metadata_preservation(self):
        await self.client.call("problem_status", {"problems": ["P1"], "status": "explored", "dry_run": True})
        self.assertEqual((await self.client.call("problem_get", {"problem": "P1"}))["data"]["problem"]["status"], "unseen")
        for status in ("explored", "attempted", "solved"):
            await self.client.call("problem_status", {"problems": ["P1"], "status": status})
        result = await self.client.call("tijian_problem_get", {"problem": "P1"})
        self.assertEqual(result["data"]["statusHistory"], ["explored", "attempted", "solved"])
        self.assertTrue(result["data"]["futureUpstreamField"]["preserved"])
        self.assertEqual(result["data"]["translations"][0]["language"], "en")

    async def test_errors_are_redacted_and_writes_are_never_retried(self):
        for problem, code in (("FAIL", "tijian_command_failed"), ("MALFORMED", "tijian_protocol_error"),
                              ("BAD_ENVELOPE", "tijian_protocol_error"), ("WRONG_DRY_RUN", "tijian_protocol_error")):
            with self.subTest(problem=problem), self.assertRaises(TijianError) as caught:
                await self.client.call("problem_add", {"problems": [problem]})
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.outcome, "uncertain")
            self.assertNotIn("PRIVATE_SECRET", str(caught.exception))
        self.assertEqual(len((self.root / "trace.jsonl").read_text().splitlines()), 4)

    async def test_missing_disabled_read_only_and_unknown_operation(self):
        for config, code in ((replace(self.config, enabled=False), "tijian_disabled"),
                             (replace(self.config, executable="/no/such/tijian"), "tijian_unavailable"),
                             (replace(self.config, read_only=True), "tijian_read_only")):
            with self.assertRaises(TijianError) as caught:
                await TijianClient(config).call("problem_add", {"problems": ["P1"]})
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(RuntimeContractError):
            await self.client.call("direct_sql", {})
        with self.assertRaises(RuntimeContractError):
            await self.client.call("problem_add", {"problems": ["中" * 8000] * 10})
        self.assertFalse((self.root / "trace.jsonl").exists())

    async def test_stderr_is_drained_and_output_is_bounded(self):
        await self.client.call("problem_get", {"problem": "STDERR"})
        with self.assertRaises(TijianError) as caught:
            await TijianClient(replace(self.config, max_output_bytes=1024)).call("problem_get", {"problem": "FLOOD"})
        self.assertEqual(caught.exception.code, "tijian_output_limit")

    async def test_timeout_and_cancellation_cleanup_owned_descendants(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                config = replace(self.config, timeout_seconds=0.3 if not cancel else 10)
                task = asyncio.create_task(TijianClient(config).call("problem_add", {"problems": ["HANG"]}))
                marker = self.root / "child.pid"
                for _ in range(100):
                    if marker.exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(TijianError) as caught:
                        await task
                    self.assertEqual(caught.exception.code, "tijian_timeout")
                    self.assertEqual(caught.exception.outcome, "uncertain")
                # A container's PID 1 may defer zombie reaping; no executable remains.
                status = Path(f"/proc/{pid}/stat")
                for _ in range(100):
                    if not status.exists() or status.read_text().split()[2] == "Z":
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")
                marker.unlink()

    async def test_killing_bridge_client_also_stops_the_native_tool_descendants(self):
        path = self.root / "runtime.toml"
        path.write_text(config_text(Config(self.root / "state", self.root / "workflows", tijian=self.config)))
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "tijian", "call", "problem_add",
            "--config", str(path), "--arguments", '{"problems":["HANG"]}', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            marker = self.root / "child.pid"
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            process.kill()
            await asyncio.wait_for(process.communicate(), 5)
            status = Path(f"/proc/{pid}/stat")
            for _ in range(100):
                if not status.exists() or status.read_text().split()[2] == "Z":
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")
        finally:
            if process.returncode is None:
                process.kill()
            await process.communicate()

    async def test_parallel_calls_are_serialized_and_doctor_omits_data(self):
        await asyncio.gather(*[self.client.call("problem_status", {"problems": ["P1"], "status": "explored"}) for _ in range(8)])
        result = await self.client.call("problem_get", {"problem": "P1"})
        self.assertEqual(len(result["data"]["statusHistory"]), 8)
        report = await self.client.diagnose(with_data=True)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["checks"]), 4)
        self.assertNotIn("題面", json.dumps(report, ensure_ascii=False))

    async def test_cli_uses_files_and_emits_uncertain_without_raw_errors(self):
        config_path = self.root / "config.toml"
        config_path.write_text(config_text(Config(self.root / "state", self.root / "workflows", tijian=self.config)))
        args_path = self.root / "arguments.json"
        args_path.write_text(json.dumps({"problems": ["FAIL"]}))
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "oicoach_runtime", "tijian", "call", "problem_add",
            "--config", str(config_path), "--arguments-file", str(args_path), "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 1)
        result = json.loads(stderr)
        self.assertEqual(result["error"]["outcome"], "uncertain")
        self.assertNotIn("PRIVATE_SECRET", stderr.decode())

    async def test_example_prepares_a_separate_workspace_and_refuses_overwrite(self):
        script = Path(__file__).resolve().parents[1] / "examples/prepare_tijian.py"
        destination = self.root / "integration"
        command = [sys.executable, str(script), "--directory", str(destination),
                   "--tijian-executable", str(self.fake), "--omp-executable", sys.executable]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        result = json.loads(stdout)
        config = load_config(Path(result["config"]))
        self.assertTrue(config.tijian.read_only)
        from oicoach_runtime.workflows import WorkflowProvider
        self.assertEqual(WorkflowProvider(config.workflow_root).describe("tijian").workspace, destination / "workspace")
        self.assertEqual(json.loads((destination / "workspace/.mcp.json").read_text()), mcp_configuration(Path(result["config"])))
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        self.assertEqual(process.returncode, 2)

    @unittest.skipUnless(HAS_MCP, "install .[tijian] to test MCP protocol")
    async def test_real_sdk_stdio_initialize_discovery_calls_and_error_semantics(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        for read_only in (False, True):
            config_path = self.root / "config.toml"
            config_path.write_text(config_text(Config(self.root / "state", self.root / "workflows", tijian=replace(self.config, read_only=read_only))))
            entry = mcp_configuration(config_path)["mcpServers"]["tijian"]
            parameters = StdioServerParameters(command=entry["command"], args=entry["args"])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, "paveway-tijian")
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    self.assertIn("tijian_problem_get", names)
                    self.assertEqual("tijian_problem_status" in names, not read_only)
                    result = await session.call_tool("tijian_problem_get", {"problem": "P1"})
                    self.assertFalse(result.isError)
                    self.assertEqual(result.structuredContent["data"]["problem"]["id"], "P1")
                    result = await session.call_tool("tijian_problem_status", {"problems": ["P1"], "status": "explored"})
                    self.assertEqual(result.isError, read_only)
                    result = await session.call_tool("tijian_problem_get", {"problem": "--PRIVATE_SECRET"})
                    self.assertTrue(result.isError)
                    self.assertNotIn("PRIVATE_SECRET", result.content[0].text)


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_TIJIAN"), "set OICOACH_TEST_REAL_TIJIAN to a real executable")
class RealTijianTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_json_cli_and_local_read_queries(self):
        config = TijianConfig(enabled=True, executable=os.environ["OICOACH_TEST_REAL_TIJIAN"], read_only=True)
        self.assertTrue((await TijianClient(config).diagnose(with_data=True))["ok"])


@unittest.skipUnless(HAS_MCP and os.environ.get("OICOACH_TEST_TIJIAN_OMP_CONFIG"),
                     "set OICOACH_TEST_TIJIAN_OMP_CONFIG for a real model + fixture MCP roundtrip")
class OmpTijianBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_omp_model_calls_the_bridge_in_an_isolated_workspace(self):
        from oicoach_runtime.adapters.omp_rpc import OmpRpc
        import uuid
        base = load_config(Path(os.environ["OICOACH_TEST_TIJIAN_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-tijian-omp-") as directory:
            root = Path(directory)
            fake = root / "fake_tijian.py"
            shutil.copyfile(Path(__file__).with_name("fake_tijian.py"), fake)
            fake.chmod(0o700)
            sessions = root / "sessions"
            sessions.mkdir(mode=0o700)
            config = replace(base, state_dir=root / "state", workflow_root=root / "workflows",
                tijian=TijianConfig(enabled=True, executable=str(fake), read_only=True),
                agent=replace(base.agent, tools_enabled=False, extensions_enabled=False,
                    skills_enabled=False, rules_enabled=False, startup_timeout_seconds=30,
                    turn_timeout_seconds=90, settle_timeout_seconds=15, graceful_stop_seconds=1))
            config_path = root / "config.toml"
            config_path.write_text(config_text(config))
            config_path.chmod(0o600)
            (root / ".mcp.json").write_text(json.dumps(mcp_configuration(config_path)))
            adapter = OmpRpc(config, config_path=config_path)
            try:
                run_id = str(uuid.uuid4())
                await adapter.start({"workspace": str(root), "session_dir": str(sessions), "session_file": None}, run_id)
                await adapter.ready()
                async with asyncio.timeout(15):
                    while not any(tool["name"].endswith("tijian_problem_get") for tool in (await adapter.state()).get("dumpTools", [])):
                        await asyncio.sleep(0.05)
                result = await adapter.prompt(run_id,
                    "Integration test: use mcp__tijian_problem_get exactly once with problem='P1001'. "
                    "Do not use shell, filesystem or other tools. After reading the result, reply only DONE. "
                    "This is a fake Tijian fixture; no actual OJ access is needed.", lambda: None, lambda _: None)
                self.assertEqual(result, "completed")
                trace = root / "trace.jsonl"
                self.assertTrue(trace.exists(), "Model completed without calling the Tijian MCP bridge")
                self.assertEqual([json.loads(line) for line in trace.read_text().splitlines()], [["problem", "get", "P1001"]])
            finally:
                await adapter.stop(abort=True)
