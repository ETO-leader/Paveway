"""Opt-in transient user-service test; never installs/enables a persistent unit."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.ipc import request
from oicoach_runtime.models import utc_text
from oicoach_runtime import service


@unittest.skipUnless(os.environ.get("OICOACH_TEST_SYSTEMD") == "1", "Set OICOACH_TEST_SYSTEMD=1 for a transient systemd --user restart test")
class SystemdRestartTests(unittest.TestCase):
    def test_installed_unit_handles_special_paths_restart_and_removal(self):
        repo = Path(__file__).resolve().parents[1]
        name = "paveway-m4-test-" + uuid.uuid4().hex + ".service"
        with tempfile.TemporaryDirectory(prefix="pw-m4-") as directory:
            root = Path(directory) / 'special % $ "paths"'
            root.mkdir(mode=0o700)
            path = root / "config.toml"
            prepared = subprocess.run([sys.executable, str(repo / "examples/prepare_smoke.py"), "--config", str(path)],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            config = load_config(path)
            config = replace(config, server=replace(config.server, poll_interval_seconds=0.05),
                             agent=replace(config.agent, graceful_stop_seconds=1))
            path.write_text(config_text(config))
            installed = False

            def wait_for(callback, timeout=20):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        value = callback()
                        if value:
                            return value
                    except RuntimeContractError:
                        pass
                    time.sleep(0.05)
                self.fail("Installed service did not reach its expected state")

            try:
                # Install validates before publishing and does not activate the unit.
                value = 'private test $ % " \\ value'
                with patch.dict(os.environ, {"PAVEWAY_TEST_MODEL_KEY": value}):
                    report = service.install(path, name=name, capture_env=["PAVEWAY_TEST_MODEL_KEY"])
                installed = True
                self.assertTrue(service.status(path, name=name)["managed"])
                service.control(path, "enable", name=name)
                self.assertEqual(service.status(path, name=name)["UnitFileState"], "enabled")
                service.control(path, "start", name=name)
                wait_for(lambda: request(config.server.socket_path, "status"))
                pid = service.status(path, name=name)["MainPID"]
                self.assertIn(("PAVEWAY_TEST_MODEL_KEY=" + value).encode(), Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"))
                event = request(config.server.socket_path, "schedule", {"workflow_id": "example", "task_id": "installed",
                    "reason": "installed service recovery", "dedupe_key": "installed-1", "payload": {},
                    "at": utc_text(datetime.now(timezone.utc) + timedelta(seconds=5))})
                subprocess.run(["systemctl", "--user", "kill", "--kill-whom=main", "--signal=SIGKILL", name], check=True, timeout=10)
                wait_for(lambda: int(service.status(path, name=name).get("NRestarts", "0")) >= 1)
                current = wait_for(lambda: (lambda e: e if e["status"] == "succeeded" else None)(
                    request(config.server.socket_path, "events.show", {"event_id": event["event_id"]})))
                self.assertEqual(current["attempt"], 1)
                service.control(path, "restart", name=name)
                wait_for(lambda: request(config.server.socket_path, "status"))
                self.assertEqual(len(request(config.server.socket_path, "runs.list")), 1)
                entries = wait_for(lambda: service.logs(path, name=name)["entries"])
                self.assertTrue(any('"component"' in item["message"] for item in entries))
                service.control(path, "disable", name=name)
                self.assertEqual(service.status(path, name=name)["ActiveState"], "active")
            finally:
                # Also handle a manager reload failure after a successfully published file.
                unit_path = service.unit_directory() / name
                if installed or unit_path.exists():
                    service.control(path, "uninstall", name=name)
            self.assertFalse(Path(report["path"]).exists())
            self.assertTrue(config.database_path.exists())

    def test_service_manager_recovers_queue_and_does_not_replay_active_prompt(self):
        repo = Path(__file__).resolve().parents[1]
        unit = "paveway-m3-smoke-" + uuid.uuid4().hex + ".service"
        with tempfile.TemporaryDirectory(prefix="pw-sd-") as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            prepared = subprocess.run([sys.executable, str(repo / "examples/prepare_smoke.py"), "--config", str(config_path)],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            config = load_config(config_path)
            config = replace(config, agent=replace(config.agent, turn_timeout_seconds=30, graceful_stop_seconds=1),
                             restart=replace(config.restart, backoff_initial_seconds=0.05, backoff_max_seconds=0.1, jitter_ratio=0))
            config_path.write_text(config_text(config))
            started = False

            def control(*arguments, check=True):
                return subprocess.run(["systemctl", "--user", *arguments, unit], capture_output=True, text=True, check=check, timeout=15)

            def api(command, arguments=None):
                return request(config.server.socket_path, command, arguments)

            def wait_for(predicate, timeout=12):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        result = predicate()
                        if result:
                            return result
                    except RuntimeContractError:
                        pass
                    time.sleep(0.05)
                self.fail("Transient service did not reach expected runtime state")

            def schedule(key, payload=None, delay=0):
                return api("schedule", {"workflow_id": "example", "task_id": key, "dedupe_key": key, "reason": "systemd verification",
                                        "payload": payload or {}, "at": utc_text(datetime.now(timezone.utc) + timedelta(seconds=delay))})

            def status_is(event, status):
                current = api("events.show", {"event_id": event["event_id"]})
                return current if current["status"] == status else None

            try:
                started = True  # Also clean up if systemd-run times out after submitting the unit.
                launch = subprocess.run(["systemd-run", "--user", "--collect", "--unit", unit,
                                         "--property=Restart=on-failure", "--property=RestartSec=1",
                                         "--property=KillMode=control-group", "--property=TimeoutStopSec=5",
                                         "--working-directory", str(repo), sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(config_path)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(launch.returncode, 0, launch.stderr)
                wait_for(lambda: api("status"))
                future = schedule("future", delay=4)
                first_pid = int(control("show", "--property=MainPID", "--value").stdout.strip())
                self.assertGreater(first_pid, 1)
                control("kill", "--kill-whom=main", "--signal=SIGKILL")
                wait_for(lambda: int(control("show", "--property=NRestarts", "--value").stdout.strip()) >= 1)
                wait_for(lambda: api("status"))
                self.assertNotEqual(int(control("show", "--property=MainPID", "--value").stdout.strip()), first_pid)
                done = wait_for(lambda: status_is(future, "succeeded"))
                self.assertEqual(done["attempt"], 1)
                active = schedule("active", {"fake_mode": "hold"})
                wait_for(lambda: (root / "fake-entered").exists())
                control("kill", "--kill-whom=main", "--signal=SIGKILL")
                wait_for(lambda: int(control("show", "--property=NRestarts", "--value").stdout.strip()) >= 2)
                uncertain = wait_for(lambda: status_is(active, "uncertain"))
                self.assertEqual(uncertain["attempt"], 1)
                self.assertEqual(len(api("runs.list")), 2)
                healthy = schedule("healthy")
                wait_for(lambda: status_is(healthy, "succeeded"))
                self.assertEqual(len(api("runs.list")), 3)
            finally:
                if started:
                    stopped = control("stop", check=False)
                    if stopped.returncode != 0:
                        control("kill", "--kill-whom=all", "--signal=SIGKILL", check=False)
                        control("stop", check=False)


if __name__ == "__main__":
    unittest.main()
