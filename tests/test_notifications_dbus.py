"""Real notify-send/gdbus on a private bus: acceptance is not a visible banner."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from oicoach_runtime import notifications, service
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.inbox import Inbox
from oicoach_runtime.models import utc_text
from oicoach_runtime.store import Store


@unittest.skipUnless(all(shutil.which(x) for x in ("dbus-daemon", "gdbus", "notify-send")), "requires dbus-daemon, gdbus and notify-send")
class NotificationDBusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if subprocess.run(["/usr/bin/python3", "-c", "from gi.repository import Gio,GLib"], capture_output=True).returncode:
            self.skipTest("real private-bus fixture requires system python3-gi")
        self.temp = tempfile.TemporaryDirectory(prefix="pw-dbus-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        notifications.configure(self.config, True, backend="linux")
        self.now = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        bus_config = self.root / "bus.conf"
        bus_config.write_text(f'<busconfig><type>session</type><listen>unix:tmpdir={self.root}</listen><auth>EXTERNAL</auth><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>')
        self.bus = subprocess.Popen(["dbus-daemon", "--nofork", "--nopidfile", "--print-address=1", "--config-file=" + str(bus_config)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop, self.bus)
        self.address = self.bus.stdout.readline().strip()
        self.assertTrue(self.address)
        self.env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": self.address}
        environment = patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": self.address})
        environment.start(); self.addCleanup(environment.stop)
        self.capture = self.root / "notify-calls.jsonl"
        self.event = self.store.enqueue("example", "training", "Private bus fixture", "fixture",
            {"native_wakeup": True, "intent": "reminder", "message": "REAL_DBUS_FIXTURE"}, at=utc_text(self.now))

    @staticmethod
    def stop(process):
        if process.poll() is None: process.terminate()
        process.wait(timeout=5)
        if process.stdout: process.stdout.close()

    def start_service(self, mode="accept"):
        ready = self.root / ("ready-" + uuid.uuid4().hex)
        process = subprocess.Popen(["/usr/bin/python3", str(Path(__file__).with_name("fake_notifications_dbus.py")), str(ready), str(self.capture), mode], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, process)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(ready.exists(), "private notification service did not acquire its name")
        return process

    async def test_service_appears_after_due_and_retry_accepts_once(self):
        watcher = notifications.Watcher(self.store)
        await watcher.tick()
        first = self.store.metadata(notifications.PREFIX + self.event["event_id"])
        self.assertEqual(first["status"], "retry_wait")
        self.assertEqual(first["code"], "notification_service_not_ready")
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        self.start_service()
        self.now += timedelta(seconds=3)
        await watcher.tick()
        self.assertEqual(self.store.metadata(notifications.PREFIX + self.event["event_id"])["status"], "accepted")
        await notifications.Watcher(self.store).tick()
        self.assertEqual(len(self.capture.read_text().splitlines()), 1)
        self.assertEqual(self.store.event(self.event["event_id"])["status"], "queued")

    async def test_absent_service_exhausts_bounded_retry_and_keeps_inbox(self):
        watcher = notifications.Watcher(self.store, retry_window_seconds=3)
        await watcher.tick()
        self.now += timedelta(seconds=2); await watcher.tick()
        self.now += timedelta(seconds=2); await watcher.tick()
        attempt = self.store.metadata(notifications.PREFIX + self.event["event_id"])
        self.assertEqual(attempt["status"], "unavailable")
        self.assertEqual(attempt["code"], "notification_retry_expired")
        self.assertFalse(self.capture.exists())
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)

    async def test_notify_timeout_is_unknown_and_not_retried(self):
        self.start_service("hang")
        await notifications.Watcher(self.store).tick()
        attempt = self.store.metadata(notifications.PREFIX + self.event["event_id"])
        self.assertEqual(attempt["status"], "unknown")
        self.assertEqual(attempt["code"], "notification_timeout")
        self.now += timedelta(seconds=20)
        await notifications.Watcher(self.store).tick()
        self.assertEqual(len(self.capture.read_text().splitlines()), 1)

    async def test_bus_unreachable_and_notification_rejected_are_distinct(self):
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(self.root / "absent-bus")}):
            failure = await notifications.send("linux")
        self.assertEqual(failure["code"], "session_bus_unreachable")
        self.assertTrue(failure["retryable"])
        self.start_service("reject")
        failure = await notifications.send("linux")
        self.assertEqual(failure["code"], "desktop_rejected")
        self.assertFalse(failure.get("retryable", False))

    @unittest.skipUnless(os.environ.get("PAVEWAY_TEST_SYSTEMD") == "1", "set PAVEWAY_TEST_SYSTEMD=1 for a temporary enabled user unit")
    async def test_enabled_user_unit_real_worker_survives_restart_without_duplicate(self):
        if subprocess.run(["systemctl", "--user", "show-environment"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            self.skipTest("no reachable systemd user manager")
        unit = "paveway-test-notifications-" + uuid.uuid4().hex + ".service"
        unit_path = Path.home() / ".config/systemd/user" / unit
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        body = service.render(self.path, command=("notifications", "run"))
        body = body.replace("[Service]\n", "[Service]\nEnvironment=" + service.quote("DBUS_SESSION_BUS_ADDRESS=" + self.address) + "\nEnvironment=" + service.quote("PYTHONPATH=" + str(Path(__file__).resolve().parents[1] / "src")) + "\n")
        unit_path.write_text(body)
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", unit], check=True, capture_output=True)
            self.assertEqual(subprocess.run(["systemctl", "--user", "is-enabled", unit], capture_output=True).returncode, 0)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                attempt = self.store.metadata(notifications.PREFIX + self.event["event_id"])
                if attempt and attempt["status"] == "retry_wait": break
                await asyncio.sleep(.05)
            self.assertEqual(attempt["status"], "retry_wait", "login-time worker should wait for the notification service")
            self.start_service()
            for _ in range(2):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    attempt = self.store.metadata(notifications.PREFIX + self.event["event_id"])
                    if attempt and attempt["status"] == "accepted": break
                    await asyncio.sleep(.05)
                self.assertEqual(attempt["status"], "accepted")
                subprocess.run(["systemctl", "--user", "restart", unit], check=True)
                await asyncio.sleep(1.2)
            self.assertEqual(len(self.capture.read_text().splitlines()), 1)
            self.assertEqual(self.store.event(self.event["event_id"])["status"], "queued")
        finally:
            subprocess.run(["systemctl", "--user", "disable", "--now", unit], capture_output=True)
            unit_path.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
