"""Opt-in real OMP SDK reconstruction and synthetic long-session pressure."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from oicoach_runtime.cache_usage import session_usage


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_CACHE_OMP") == "1", "set PAVEWAY_TEST_CACHE_OMP=1 for installed OMP SDK")
class OmpMemoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        manifest = Path.home() / ".cache/paveway/omp/18.16.0/runtime.json"
        if not manifest.is_file():
            self.fail("explicit SDK verification requires the public pinned OMP installation")
        self.runtime = json.loads(manifest.read_text())
        self.project = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory(prefix="paveway-memory-sdk-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.root),
                            "PI_CODING_AGENT_DIR": str(self.root / "agent"), "PYTHON": sys.executable}
        for kind in ("CONFIG", "STATE", "DATA", "CACHE"):
            self.environment[f"XDG_{kind}_HOME"] = str(self.root / kind.lower())

    def run_script(self, name, *args):
        result = subprocess.run([self.runtime["bun"], str(self.project / "examples" / name), *map(str, args)],
                                cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout[-2000:])
        return json.loads(result.stdout)

    def test_real_reset_branch_native_custom_and_soft_compaction_contexts(self):
        report = self.run_script("verify_omp_memory_boundaries.mjs", self.runtime["source"], self.root)
        fixture = report["persisted_usage_fixture"]
        usage = session_usage(fixture["path"], fixture["session_id"])
        self.assertEqual(usage["cumulative"]["call_count"], 3)
        self.assertEqual(usage["cumulative"]["cache_read_tokens"], 240)
        self.assertEqual(usage["last_persisted_entry_ancestry"]["call_count"], 2)
        self.assertNotIn("active_branch", usage)
        self.assertNotEqual(fixture["live_leaf"], fixture["recorded_replay_leaf"])
        self.assertNotEqual(fixture["physical_tail"], fixture["recorded_replay_leaf"])

    def test_thousand_updates_preserve_notes_and_diagnostics_below_archive_limit(self):
        report = self.run_script("stress_memory_context.mjs",
                                 self.project / "src/oicoach_runtime/bundled/paveway-memory-context.mjs", self.root, self.runtime["source"])
        self.assertLessEqual(report["workload"]["selected_context_json_bytes"], 65536)
        last = report["checkpoints"][-1]
        self.assertEqual(last["round"], 1000)
        self.assertLess(last["total_bytes"], 16 * 1024 * 1024)
        self.assertTrue(last["archive"]["usage"]["accepted"])
        self.assertEqual(last["archive"]["usage"]["call_count"], 1001)
        self.assertLess(last["state_json_bytes"], last["model_visible_memory_json_bytes"])
