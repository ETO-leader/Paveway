import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.omp_installation import (
    OmpCandidateRejected, OmpInstallation, OmpInstallationUnavailable, load_active_omp,
    prepare_omp, resolve_omp, startup_omp,
)


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

    def test_project_cwd_cannot_shadow_the_checked_exec_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shadow = root / "oicoach_runtime"
            shadow.mkdir()
            (shadow / "__init__.py").touch()
            (shadow / "omp_installation.py").write_text("raise SystemExit(17)\n")
            selected = root / "omp"
            selected.write_text("#!/bin/sh\nexit 42\n")
            selected.chmod(0o700)
            installed = OmpInstallation.select(str(selected))
            result = subprocess.run(installed.command([]), cwd=root, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 42, result.stderr)

    def test_activation_retains_exact_snapshot_and_rejection_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            candidate = root / "omp"
            shutil.copy2(shutil.which("true"), candidate)
            first = OmpInstallation.select(str(candidate), environment={})
            with patch("oicoach_runtime.omp_probe.probe_omp", return_value="19.1.1") as probe:
                with prepare_omp(state, first) as activation:
                    self.assertIsNone(load_active_omp(state))
                    snapshot = activation.admission.installed
                    self.assertNotEqual(snapshot, first)
                    self.assertEqual(Path(snapshot.path).read_bytes(), candidate.read_bytes())
                    probe.assert_called_once_with(snapshot)
                    activation.commit()
            initial = load_active_omp(state)
            self.assertEqual(initial.source, first)
            self.assertEqual(initial.status()["pinning"], "snapshot")

            staged = root / "replacement"
            shutil.copy2(shutil.which("false"), staged)
            staged.replace(candidate)
            second = OmpInstallation.select(str(candidate), environment={})
            self.assertNotEqual(first, second)
            with patch("oicoach_runtime.omp_probe.probe_omp", side_effect=OmpCandidateRejected("registration failed")):
                with self.assertRaisesRegex(OmpCandidateRejected, "registration failed"):
                    with prepare_omp(state, second):
                        self.fail("Rejected probe entered activation context")
                self.assertEqual(startup_omp(state, str(candidate)), initial)
            self.assertEqual(load_active_omp(state), initial)
            self.assertEqual(list((state / "omp").glob("snapshot-*")), [Path(snapshot.path).parent])
            self.assertEqual(subprocess.run(snapshot.command([]), check=False, timeout=5).returncode, 0)

    def test_probe_does_not_admit_an_executable_with_version_but_no_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "version-only"
            attempted = root / "executed-in-real-environment"
            candidate.write_text(
                f"#!/bin/sh\nif [ \"$OMP_TEST_CREDENTIAL\" ]; then touch '{attempted}'; fi\n"
                "if [ \"$1\" = '--version' ]; then printf 'omp/19.2.1\\n'; fi\n"
            )
            candidate.chmod(0o700)
            installation = OmpInstallation.select(str(candidate), environment={})
            state = root / "state"
            with patch.dict(os.environ, {"OMP_TEST_CREDENTIAL": "must-not-inherit"}):
                with self.assertRaisesRegex(OmpCandidateRejected, "native extension registration"):
                    with prepare_omp(state, installation):
                        self.fail("Version-only executable entered activation")
            self.assertFalse(attempted.exists())
            self.assertIsNone(load_active_omp(state))
            failure = state / "native-startup-failure.json"
            self.assertEqual(failure.stat().st_mode & 0o777, 0o600)
            report = json.loads(failure.read_bytes())
            self.assertEqual(report["omp_version"], "19.2.1")
            self.assertNotIn("must-not-inherit", json.dumps(report))
