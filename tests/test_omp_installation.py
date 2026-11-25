import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.omp_installation import resolve_omp


class OmpInstallationTests(unittest.TestCase):
    def test_explicit_path_takes_precedence_without_falling_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configured"
            selected = root / "selected"
            for path in (configured, selected):
                path.touch(mode=0o700)
            with patch.dict(os.environ, {"PAVEWAY_OMP_PATH": str(selected)}):
                self.assertEqual(resolve_omp(str(configured)), str(selected))
                selected.chmod(0o600)
                with self.assertRaisesRegex(RuntimeContractError, "PAVEWAY_OMP_PATH"):
                    resolve_omp(str(configured))

    def test_configured_command_and_path_default_resolve_to_absolute_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configured"
            configured.touch(mode=0o700)
            (root / "omp").symlink_to(configured)
            with patch.dict(os.environ, {"PATH": directory}, clear=True):
                self.assertEqual(resolve_omp("configured"), str(configured))
                self.assertEqual(resolve_omp(), str(configured))
                with self.assertRaisesRegex(RuntimeContractError, "agent.executable"):
                    resolve_omp("missing")
