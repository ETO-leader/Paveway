import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.omp_installation import OmpInstallation, OmpInstallationUnavailable, resolve_omp


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

    def test_exec_side_rejects_replacement_after_command_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "omp"
            shutil.copy2(shutil.which("true"), selected)
            old = OmpInstallation.select(str(selected))
            command = old.command([], rpc=True)
            replacement = Path(directory) / "replacement"
            replacement.write_text("#!/bin/sh\nprintf 'REPLACEMENT WAS EXECUTED\\n'\n")
            replacement.chmod(0o700)
            replacement.replace(selected)
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 126)
            self.assertIn("paveway_omp_installation_refused", result.stdout)
            self.assertNotIn("REPLACEMENT WAS EXECUTED", result.stdout)
            self.assertIn("Safely hand off Runtime", result.stderr)

    def test_model_cli_inherited_selection_cannot_switch_to_new_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "omp"
            shutil.copy2(shutil.which("true"), selected)
            old = OmpInstallation.select(str(selected))
            environment = {}
            old.export(environment)
            replacement = Path(directory) / "replacement"
            replacement.write_text("#!/bin/sh\nexit 42\n")
            replacement.chmod(0o700)
            replacement.replace(selected)
            with patch.dict(os.environ, environment):
                with self.assertRaises(OmpInstallationUnavailable):
                    OmpInstallation.for_command().command(["models", "--json"])
            self.assertEqual(subprocess.run(OmpInstallation.select(str(selected)).command([]), timeout=5).returncode, 42)

    def test_exec_accepts_execute_only_elf(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "omp"
            shutil.copy2(sys.executable, selected)
            selected.chmod(0o111)
            result = subprocess.run(OmpInstallation.select(str(selected)).command(
                ["-c", "print('EXECUTED_SELECTED_ELF')"]), capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "EXECUTED_SELECTED_ELF"), result.stderr)
