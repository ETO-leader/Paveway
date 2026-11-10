"""Real Linux descendants, including tools that start their own session."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from oicoach_runtime.frontend import alive
from oicoach_runtime.processes import enable_subreaper, process_identity


class ChildLifecycleTests(unittest.TestCase):
    def test_guardian_reaps_setsid_descendant_and_preserves_unrelated_child(self):
        enable_subreaper()
        with tempfile.TemporaryDirectory(prefix="pw-child-lifecycle-") as directory:
            path = Path(directory) / "descendant"
            unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
            descendant = None
            try:
                script = ("import subprocess,sys;from pathlib import Path;"
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                    "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                    f"Path({str(path)!r}).write_text(str(p.pid))")
                guardian = subprocess.Popen([sys.executable, "-m", "oicoach_runtime.child", str(os.getpid()),
                    sys.executable, "-c", script], start_new_session=True,
                    env={**os.environ, "OICOACH_LOCK_FDS": ""})
                self.assertEqual(guardian.wait(timeout=5), 0)
                descendant = int(path.read_text())
                self.assertFalse(alive(descendant, process_identity(descendant)), "setsid tool survived normal guardian exit")
                self.assertFalse(Path(f"/proc/{descendant}").exists(), "tool was not reaped")
                self.assertIsNone(unrelated.poll(), "unrelated child was killed")
            finally:
                if descendant and Path(f"/proc/{descendant}").exists():
                    try: os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError: pass
                    try: os.waitpid(descendant, 0)
                    except ChildProcessError: pass
                unrelated.terminate()
                unrelated.wait()
