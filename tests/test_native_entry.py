import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from oicoach_runtime.config import Config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.native import recovery_choice, run


class NativeEntryTests(unittest.TestCase):
    config = SimpleNamespace(server=SimpleNamespace(socket_path="/fixture/runtime.sock"))

    def check(self, state, choice):
        row = {"workflow_id": "example", "task_id": "training", "conversation_id": "saved", "status": state}
        def request(_socket, command, args):
            if command == "frontend.list": return [row]
            if command == "frontend.recover":
                if state != "uncertain": self.fail("Live owner must not be recovered")
                return {**row, "status": "released"}
            self.fail("Unexpected Runtime operation: " + command)
        return row, request

    def test_explicit_recover_calls_backend_checks_without_replaying(self):
        row, handler = self.check("uncertain", "recover")
        with patch("oicoach_runtime.native.request", side_effect=handler) as remote, patch("prompt_toolkit.shortcuts.radiolist_dialog") as dialog:
            dialog.return_value.run.return_value = "recover"
            self.assertEqual(recovery_choice(self.config, "example", "training", None, False), ("saved", False, False))
            self.assertEqual([c.args[1] for c in remote.call_args_list], ["frontend.list", "frontend.recover"])

    def test_cancel_and_new_preserve_old_ownership(self):
        for choice, expected in ((None, (None, False, True)), ("new", (None, True, False))):
            with self.subTest(choice=choice):
                row, handler = self.check("uncertain", choice)
                with patch("oicoach_runtime.native.request", side_effect=handler) as remote, patch("prompt_toolkit.shortcuts.radiolist_dialog") as dialog:
                    dialog.return_value.run.return_value = choice
                    self.assertEqual(recovery_choice(self.config, "example", "training", None, False), expected)
                    self.assertEqual(remote.call_count, 1)

    def test_active_owner_has_no_recovery_option(self):
        row, handler = self.check("active", None)
        with patch("oicoach_runtime.native.request", side_effect=handler), patch("prompt_toolkit.shortcuts.radiolist_dialog") as dialog:
            dialog.return_value.run.return_value = None
            recovery_choice(self.config, "example", "training", None, False)
            self.assertNotIn("recover", [value for value, _ in dialog.call_args.kwargs["values"]])

    def test_backend_recovery_refusal_never_falls_back_to_a_new_conversation(self):
        row, handler = self.check("uncertain", "recover")
        with patch("oicoach_runtime.native.request", side_effect=[[row], ConflictError("living owner")]) as remote, patch("prompt_toolkit.shortcuts.radiolist_dialog") as dialog:
            dialog.return_value.run.return_value = "recover"
            with self.assertRaises(ConflictError): recovery_choice(self.config, "example", "training", None, False)
            self.assertEqual(remote.call_count, 2)

    def test_explicit_new_skips_recovery_and_cross_scope_is_rejected(self):
        with patch("oicoach_runtime.native.request") as remote:
            self.assertEqual(recovery_choice(self.config, "example", "training", None, True), (None, True, False))
            remote.assert_not_called()
        with patch("oicoach_runtime.native.request", return_value={"workflow_id": "other", "task_id": "training"}):
            with self.assertRaises(RuntimeContractError): recovery_choice(self.config, "example", "training", "saved", False)

    def test_host_preparation_failure_never_acquires_ownership_or_starts_ui(self):
        with tempfile.TemporaryDirectory(prefix="pw-native-entry-") as directory:
            root = Path(directory)
            config = Config(root / "state", root / "workflows")
            launcher = root / "omp"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o600)
            outcomes = (
                OSError("installer unavailable"),
                subprocess.CalledProcessError(1, ["installer"]),
                subprocess.CompletedProcess(["installer"], 0, stdout="not JSON"),
                subprocess.CompletedProcess(["installer"], 0, stdout="{}"),
                subprocess.CompletedProcess(["installer"], 0,
                    stdout=json.dumps({"launcher": str(root / "missing")})),
                subprocess.CompletedProcess(["installer"], 0,
                    stdout=json.dumps({"launcher": str(launcher)})),
            )
            for outcome in outcomes:
                with self.subTest(outcome=outcome):
                    with patch("oicoach_runtime.native.os.isatty", return_value=True), \
                            patch("oicoach_runtime.proxy.synchronize"), \
                            patch("oicoach_runtime.native_host.subprocess.run", side_effect=[outcome]), \
                            patch("oicoach_runtime.native.request") as remote, \
                            patch("oicoach_runtime.native.run_terminal") as terminal:
                        with self.assertRaises(RuntimeContractError):
                            run(config, root / "config.toml", "example", new=True)
                        remote.assert_not_called()
                        terminal.assert_not_called()
