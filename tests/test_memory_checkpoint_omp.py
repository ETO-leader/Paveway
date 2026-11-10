"""Opt-in real OMP checkpoint crash/abort/recovery proof; loopback only.

PAVEWAY_TEST_CACHE_OMP=1 enables the pinned prepared host in a temporary profile.
The seed is 63 completed RPC model turns, never synthetic session-manager rows.
"""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_CACHE_OMP") == "1",
                     "set PAVEWAY_TEST_CACHE_OMP=1 for isolated real OMP checkpoint proof")
class MemoryCheckpointOmpTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
        from verify_prompt_cache import CacheHarness

        temporary = tempfile.TemporaryDirectory(prefix="pw-checkpoint-test-")
        self.addCleanup(temporary.cleanup)
        # Deliberately use CacheHarness's pinned native host, not a PATH OMP or
        # PAVEWAY_TEST_CACHE_OMP_EXECUTABLE inherited from another acceptance run.
        self.fixture = CacheHarness(Path(temporary.name))
        self.addCleanup(self.fixture.close)

    def test_checkpoint_preparation_is_not_receipt_until_real_prompt_commit(self):
        from verify_memory_checkpoint import NOTE_64, SCOPE, verify_memory_checkpoint

        result = asyncio.run(verify_memory_checkpoint(self.fixture))
        self.assertEqual(result["seed_receipts"], 63)
        self.assertEqual(result["seed_checkpoints"], 0)
        for stage in ("crash", "cancel"):
            with self.subTest(stage=stage):
                observation = result[stage]
                state = observation["prepared"]["state"]
                self.assertEqual(state["schema"], 2)
                self.assertEqual(state["scope"], SCOPE)
                self.assertEqual(len(state["notes"]), 1)
                self.assertEqual(state["notes"][0]["version"], 64)
                self.assertEqual(state["notes"][0]["content"], NOTE_64)
                self.assertEqual(observation["accepted_receipts"], 63)
                self.assertEqual(observation["checkpoints"], 0)
                self.assertEqual(observation["model_requests"], 0)
                self.assertTrue(observation["original_receipts_unchanged"])
        self.assertEqual(result["crash"]["exit_code"], 23)
        self.assertTrue(result["cancel"]["paused"])
        self.assertTrue(result["cancel"]["abort_acknowledged"])
        self.assertEqual(result["cancel"]["status"], "aborted")
        recovery = result["recovery"]
        self.assertEqual(recovery["accepted_receipts"], 64)
        self.assertEqual(recovery["committed_checkpoints"], 1)
        self.assertEqual(recovery["normal_model_requests"], 1)
        self.assertEqual(recovery["current_note_version"], 64)
        self.assertEqual(recovery["current_note_occurrences"], 1)
        self.assertEqual(recovery["checkpoint"]["type"], "custom_message")
        self.assertEqual(recovery["checkpoint"]["details"]["state"], result["crash"]["prepared"]["state"])
        self.assertEqual(recovery["checkpoint"]["details"]["state"], result["cancel"]["prepared"]["state"])
        unchanged = result["unchanged"]
        self.assertTrue(unchanged["system_equal"])
        self.assertTrue(unchanged["history_prefix_equal"])
        self.assertEqual(unchanged["normal_model_requests"], 1)
        self.assertEqual(unchanged["accepted_receipts"], 64)
        self.assertEqual(unchanged["committed_checkpoints"], 1)
        self.assertEqual(unchanged["current_note_occurrences"], 1)
        self.assertEqual(result["total_loopback_requests"], 65)


if __name__ == "__main__":
    unittest.main()
