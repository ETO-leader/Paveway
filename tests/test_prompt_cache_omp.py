"""Opt-in integrated OMP cache-prefix capture; loopback usage is synthetic.

PAVEWAY_TEST_CACHE_OMP=1 enables real native PTY and scheduler/OmpRpc processes.
The tests never load the user's profile or call a paid/external provider.
"""

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_CACHE_OMP") == "1",
                     "set PAVEWAY_TEST_CACHE_OMP=1 for isolated real OMP cache capture")
class PromptCacheOmpTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
        from verify_prompt_cache import CacheHarness
        executable = os.environ.get("PAVEWAY_TEST_CACHE_OMP_EXECUTABLE") or shutil.which("omp")
        if not executable:
            self.fail("cache capture was explicitly enabled but omp is missing")
        temporary = tempfile.TemporaryDirectory(prefix="pw-cache-test-")
        self.addCleanup(temporary.cleanup)
        self.fixture = CacheHarness(Path(temporary.name), executable)
        self.addCleanup(self.fixture.close)

    def assert_usage_is_fixture_observation(self, result):
        rows = result["normalized_usage"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("OMP calculated", row["cost_label"])
            self.assertIn("not actual invoice", row["cost_label"])
            self.assertIsNone(row["input_cache_ratio"])
            self.assertEqual(row["cache_write_evidence"], "unavailable_zero_or_missing_provenance")
        self.assertTrue(any(row["omp_normalized_usage"].get("cacheRead", 0) > 0 for row in rows),
                        "the synthetic cached_tokens response was not persisted by real OMP")

    def test_native_unchanged_changed_resume_and_real_compaction(self):
        result = self.fixture.native_scenarios()
        for scenario in ("A", "B", "C"):
            with self.subTest(scenario=scenario):
                self.assertTrue(result[scenario]["system_equal"])
                self.assertTrue(result[scenario]["history_prefix_equal"])
        self.assertTrue(result["E"]["persisted_compaction"])
        self.assertGreater(result["E"]["summary_provider_requests"], 0)
        self.assert_usage_is_fixture_observation(result)

    def test_fresh_rpc_processes_resume_same_session_without_duplicate_memory(self):
        result = self.fixture.rpc_scenario()
        self.assertTrue(result["D"]["fresh_process"])
        self.assertTrue(result["D"]["same_session_file"])
        self.assertTrue(result["D"]["system_equal"])
        self.assertTrue(result["D"]["history_prefix_equal"])
        self.assert_usage_is_fixture_observation(result)

    def test_memory_hook_preserves_authorized_extension_discovery(self):
        from dataclasses import replace
        from oicoach_runtime.config import config_text
        extension = self.fixture.root / "authorized.mjs"
        extension.write_text('export default function(pi) { pi.on("before_agent_start", () => ({message: '
                            '{customType:"authorized-fixture",content:"PAVEWAY_AUTHORIZED_EXTENSION",display:false}})); }')
        path = self.fixture.probe.agent / "config.yml"
        import json
        settings = json.loads(path.read_text())
        settings["extensions"] = [str(extension)]
        path.write_text(json.dumps(settings))
        self.fixture.config = replace(self.fixture.config, agent=replace(self.fixture.config.agent, extensions_enabled=True))
        self.fixture.path.write_text(config_text(self.fixture.config))
        self.fixture.daemon.terminate()
        self.fixture.daemon.wait(timeout=10)
        self.fixture.start_daemon()
        self.fixture.note("AUTHORIZED_EXTENSION_MEMORY")
        wire, _, _ = self.fixture.ask("AUTHORIZED_EXTENSION_REQUEST")
        self.assertIn("PAVEWAY_AUTHORIZED_EXTENSION", json.dumps(wire["messages"]))
        self.assertIn("AUTHORIZED_EXTENSION_MEMORY", json.dumps(wire["messages"]))
        self.assertTrue(any(message["role"] == "user" and "AUTHORIZED_EXTENSION_MEMORY" in json.dumps(message["content"])
                            for message in wire["messages"]))

    def test_provider_without_usage_does_not_fabricate_cache_zeros(self):
        self.fixture.server.expose_usage = False
        self.fixture.ask("NO_PROVIDER_USAGE")
        result = self.fixture.api("cache.stats", {"workflow_id": "cache-probe", "task_id": "background-cache"})
        self.assertIsNone(result["cumulative"]["cache_read_tokens"])
        self.assertIsNone(result["cumulative"]["cache_write_tokens"])
        self.assertIsNone(result["cumulative"]["cache_hit_ratio"])
        self.assertEqual(result["cumulative"]["available_call_counts"]["cache_read_tokens"], 0)

    def test_native_recall_failure_sends_no_model_request_or_false_receipt(self):
        from verify_prompt_cache import NativeConversation, stop_process, read_archive
        conversation = NativeConversation(self.fixture)
        try:
            before = len(self.fixture.server.requests)
            self.fixture.daemon.terminate()
            self.fixture.daemon.wait(timeout=10)
            conversation.send("MEMORY_RECALL_FAILURE\r")
            conversation.until(lambda: conversation.process.poll() is not None)
            self.assertNotEqual(conversation.process.returncode, 0)
            self.assertEqual(len(self.fixture.server.requests), before)
            path = Path(conversation.record["session_file"])
            if path.exists():
                self.assertFalse(any(entry.get("customType") == "paveway-memory-context"
                                     for entry in read_archive(path, self.fixture.root)))
        finally:
            stop_process(conversation.process)
            os.close(conversation.master)


if __name__ == "__main__":
    unittest.main()
