"""Synthetic long-session pressure and opt-in OMP source reconstruction audit."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from oicoach_runtime.cache_usage import session_usage


class BunMemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.bun = shutil.which("bun")
        if not self.bun:
            self.skipTest("requires standalone bun on PATH")
        self.project = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory(prefix="paveway-memory-sdk-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.root),
                            "PI_CODING_AGENT_DIR": str(self.root / "agent"), "PYTHON": sys.executable}
        for kind in ("CONFIG", "STATE", "DATA", "CACHE"):
            self.environment[f"XDG_{kind}_HOME"] = str(self.root / kind.lower())

    def run_script(self, name, *args):
        result = subprocess.run([self.bun, str(self.project / "examples" / name), *map(str, args)],
                                cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout[-2000:])
        return json.loads(result.stdout)


class OmpMemoryBoundarySourceAudits(BunMemoryTestCase):
    """SessionManager is an upstream implementation detail, not a binary API."""

    def setUp(self):
        source = os.environ.get("PAVEWAY_OMP_SOURCE")
        if not source:
            self.skipTest("set PAVEWAY_OMP_SOURCE to an OMP checkout with dependencies for the SessionManager audit")
        self.source = Path(source).expanduser().resolve()
        session_manager = self.source / "packages/coding-agent/src/session/session-manager.ts"
        self.assertTrue(session_manager.is_file(), f"OMP checkout is missing SessionManager: {session_manager}")
        self.assertIsNotNone(shutil.which("bun"), "the SessionManager audit requires bun on PATH")
        super().setUp()

    def test_real_reset_branch_native_custom_and_soft_compaction_contexts(self):
        report = self.run_script("verify_omp_memory_boundaries.mjs", self.source, self.root)
        fixture = report["persisted_usage_fixture"]
        usage = session_usage(fixture["path"], fixture["session_id"])
        self.assertEqual(usage["cumulative"]["call_count"], 3)
        self.assertEqual(usage["cumulative"]["cache_read_tokens"], 240)
        self.assertEqual(usage["last_persisted_entry_ancestry"]["call_count"], 2)
        self.assertNotIn("active_branch", usage)
        self.assertNotEqual(fixture["live_leaf"], fixture["recorded_replay_leaf"])
        self.assertNotEqual(fixture["physical_tail"], fixture["recorded_replay_leaf"])


class MemoryPressureTests(BunMemoryTestCase):
    def test_thousand_updates_preserve_notes_and_diagnostics_below_archive_limit(self):
        report = self.run_script("stress_memory_context.mjs",
                                 self.project / "src/oicoach_runtime/bundled/paveway-memory-context.mjs", self.root)
        self.assertLessEqual(report["workload"]["selected_context_json_bytes"], 65536)
        last = report["checkpoints"][-1]
        self.assertEqual(last["round"], 1000)
        self.assertLess(last["total_bytes"], 16 * 1024 * 1024)
        self.assertTrue(last["archive"]["usage"]["accepted"])
        self.assertEqual(last["archive"]["usage"]["call_count"], 1001)
        self.assertLess(last["state_json_bytes"], last["model_visible_memory_json_bytes"])
