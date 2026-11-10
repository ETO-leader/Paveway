"""Opt-in real raw RPC boundaries; all HTTP/cache responses are synthetic.

Native/TUI owns /clear. Pinned raw RPC treats that spelling as user text;
this proof must never manufacture a reset receipt or reset_boundary for it.
"""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_CACHE_OMP") == "1",
                     "set PAVEWAY_TEST_CACHE_OMP=1 for isolated real OMP RPC capture")
class RpcMemoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
        from verify_prompt_cache import CacheHarness

        temporary = tempfile.TemporaryDirectory(prefix="paveway-test-rpc-boundaries-")
        self.addCleanup(temporary.cleanup)
        self.fixture = CacheHarness(Path(temporary.name))
        self.addCleanup(self.fixture.close)

    def test_raw_clear_input_real_compaction_resume_and_frozen_memory_prefix(self):
        from verify_prompt_cache import canonical, history_blocks, memory_deltas
        from verify_rpc_memory_boundaries import effective_notes, verify_rpc_memory_boundaries

        result = asyncio.run(verify_rpc_memory_boundaries(self.fixture))
        self.assertTrue(Path(result["build"]["executable"]).is_file())
        self.assertTrue(Path(result["build"]["source"]).is_dir())
        self.assertTrue(Path(result["build"]["bun"]).is_file())
        self.assertEqual(len(result["build"]["commit"]), 40)
        self.assertIn("synthetic", result["usage"])
        self.assertIn("real Runtime IPC", result["transport"])

        clear = result["clear"]
        self.assertIn("Native/TUI-only", clear["rpc_behavior"])
        self.assertTrue(clear["agent_invoked"])
        self.assertEqual(clear["status"], "completed")
        self.assertEqual(clear["reset_boundaries"], 0)
        self.assertEqual(clear["unchanged_receipts_before"], 1)
        self.assertEqual(clear["unchanged_receipts_after"], 1)
        expected_comparisons = {
            "append", "clear_is_ordinary_input", "unchanged", "mutation", "resume",
            "compacted_unchanged", "frozen_restore_after_changes", "final_clear_is_ordinary_input",
        }
        self.assertEqual(set(result["comparisons"]), expected_comparisons)
        for name, comparison in result["comparisons"].items():
            with self.subTest(boundary=name):
                self.assertTrue(comparison["system_equal"])
                self.assertTrue(comparison["history_prefix_equal"])
                self.assertGreater(comparison["canonical_request_common_prefix_bytes"], 0)
        self.assertTrue(result["resume"]["fresh_process"])
        self.assertTrue(result["resume"]["same_session_file"])
        self.assertTrue(result["resume"]["same_session_id"])
        self.assertTrue(result["archive"]["original_disk_prefix_preserved"])
        self.assertTrue(result["archive"]["original_user_input_retained"])
        self.assertEqual(result["archive"]["memory_receipts"], 3)

        wire = result["wire"]
        ordinary_clear = canonical(history_blocks(wire["clear_input"]))
        self.assertIn("RPC_BOUNDARY_BEFORE", ordinary_clear)
        self.assertIn("/clear", ordinary_clear)
        self.assertEqual(ordinary_clear.count("RPC_BOUNDARY_CURRENT_KEEP"), 1)
        self.assertEqual(ordinary_clear.count("RPC_BOUNDARY_DELETED_OLD"), 1)
        self.assertEqual(ordinary_clear.count("RPC_BOUNDARY_UPDATE_OLD"), 1)
        self.assertTrue(any(note["key"] == "rpc-delete" and note["reason"] == "deleted"
                            for delta in memory_deltas(wire["mutated"]) for note in delta["withdrawals"]))

        compaction = result["compaction"]
        self.assertEqual(compaction["persisted_entries"], 1)
        self.assertTrue(compaction["id"])
        self.assertGreater(compaction["summary_provider_requests"], 0)
        self.assertTrue(compaction["response"]["success"])
        self.assertEqual(compaction["response"]["command"], "compact")
        restores = compaction["frozen_restore"]
        self.assertEqual(len(restores), 1)
        self.assertTrue(restores[0]["restore"])
        self.assertEqual({note["key"] for note in restores[0]["current_keys_at_compaction"]},
                         {"cache-note", "rpc-update", "rpc-add"})
        self.assertEqual([delta for delta in memory_deltas(wire["after_compaction_changes"])
                          if delta.get("restore")], restores)
        self.assertTrue(any(note["key"] == "rpc-add" and note["reason"] == "deleted"
                            for delta in memory_deltas(wire["after_compaction_changes"])
                            for note in delta["withdrawals"]))
        expected = {"cache-note": "RPC_BOUNDARY_CURRENT_KEEP",
                    "rpc-update": "RPC_BOUNDARY_UPDATE_AFTER_COMPACTION"}
        self.assertEqual(result["current_notes"], expected)
        self.assertEqual(effective_notes(wire["final_clear_input"]), expected)
        self.assertEqual(effective_notes(wire["resumed"]), {
            "cache-note": "RPC_BOUNDARY_CURRENT_KEEP", "rpc-update": "RPC_BOUNDARY_UPDATE_CURRENT",
            "rpc-add": "RPC_BOUNDARY_ADDED_CURRENT"})


if __name__ == "__main__":
    unittest.main()
