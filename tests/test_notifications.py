import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from oicoach_runtime import notifications, service
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.inbox import Inbox
from oicoach_runtime.models import utc_text
from oicoach_runtime.store import Store


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_delivery_and_desktop_attempt_upgrade_without_duplicate(self):
        import hashlib
        from oicoach_runtime.store import canonical_json
        notifications.configure(self.config, True)
        event = self.event()
        old_id = hashlib.sha256(canonical_json([event["event_id"], "reminder", None]).encode()).hexdigest()
        legacy = {"delivery_id": old_id, "event_id": event["event_id"], "run_id": None, "workflow_id": "example", "task_id": "training",
            "kind": "reminder", "text": "Old reminder", "state": "acknowledged", "version": 2, "created_at": self.store.timestamp()}
        with self.store.transaction() as db:
            self.store._set_metadata(db, "delivery:v1:" + old_id, legacy)
            self.store._set_metadata(db, notifications.PREFIX + event["event_id"], {"event_id": event["event_id"],
                "not_before_utc": event["not_before_utc"], "delivery_id": old_id, "status": "accepted", "attempted_at": self.store.timestamp()})
        await self.watcher.tick()
        self.sender.assert_not_awaited()
        rows = Inbox(self.store).list("example")["messages"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery_id"], old_id)
        self.assertEqual(rows[0]["state"], "acknowledged")
        self.now += timedelta(seconds=2)
        self.store.modify(event["event_id"], event["version"], at=utc_text(self.now))
        await self.watcher.tick()
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 2)
        self.sender.assert_awaited_once()

    async def test_legacy_inbox_only_unchanged_schedule_is_not_reprojected(self):
        import hashlib
        from oicoach_runtime.store import canonical_json
        event = self.event()
        old_id = hashlib.sha256(canonical_json([event["event_id"], "reminder", None]).encode()).hexdigest()
        with self.store.transaction() as db:
            self.store._set_metadata(db, "delivery:v1:" + old_id, {"delivery_id": old_id, "event_id": event["event_id"], "run_id": None,
                "workflow_id": "example", "task_id": "training", "kind": "reminder", "text": "Old", "state": "created", "version": 1,
                "created_at": self.store.timestamp()})
        Inbox(self.store).project_due()
        Inbox(self.store).project_due()
        rows = Inbox(self.store).list("example")["messages"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery_id"], old_id)

    async def test_legacy_history_only_deduplicates_when_rescheduled_back_to_old_due(self):
        import hashlib
        notifications.configure(self.config, True)
        event = self.event()
        original_due = event["not_before_utc"]
        legacy_history = "desktop-history:v1:" + hashlib.sha256((event["event_id"] + original_due).encode()).hexdigest()
        with self.store.transaction() as db:
            self.store._set_metadata(db, legacy_history, {"event_id": event["event_id"], "not_before_utc": original_due,
                "status": "accepted", "attempted_at": self.store.timestamp()})
        event = self.store.modify(event["event_id"], event["version"], at=utc_text(self.now + timedelta(seconds=2)))
        self.now += timedelta(seconds=3)
        await self.watcher.tick()
        self.sender.assert_awaited_once()
        self.store.modify(event["event_id"], event["version"], at=original_due)
        await notifications.Watcher(self.store, self.sender).tick()
        await self.watcher.tick()
        self.sender.assert_awaited_once()
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 2)

    async def test_acknowledged_occurrence_reschedule_creates_new_unread_inbox_delivery(self):
        notifications.configure(self.config, True)
        event = self.event()
        await self.watcher.tick()
        inbox = Inbox(self.store)
        first = inbox.list("example")["messages"][0]
        inbox.acknowledge("example", first["delivery_id"], first["version"])
        self.now += timedelta(seconds=2)
        self.store.modify(event["event_id"], event["version"], at=utc_text(self.now))
        await self.watcher.tick()
        rows = inbox.list("example")["messages"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["state"] for r in rows}, {"created", "acknowledged"})
        self.assertEqual(len({r["delivery_id"] for r in rows}), 2)
        await notifications.Watcher(self.store, self.sender).tick()
        self.assertEqual(len(inbox.list("example")["messages"]), 2)
        self.assertEqual(self.sender.await_count, 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-notification-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        self.now = datetime.now(timezone.utc) + timedelta(seconds=10)
        self.store = Store(self.config, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.sender = AsyncMock(return_value={"status": "accepted", "backend": "linux", "code": None})
        self.watcher = notifications.Watcher(self.store, self.sender)

    def event(self, key="one", delay=0, intent="reminder"):
        return self.store.enqueue("example", "training", "Fixture due reminder", key,
            {"native_wakeup": True, "intent": intent, "message": "PRIVATE_FIXTURE_REMINDER"},
            at=utc_text(self.now + timedelta(seconds=delay)))

    async def test_due_popup_is_independent_of_busy_model_and_never_marks_seen(self):
        notifications.configure(self.config, True)
        event = self.event()
        # A leased background Run blocks the normal dispatcher, but not the popup.
        busy = self.store.enqueue("example", "busy", "busy Agent", "busy", at=utc_text(self.now))
        claimed = self.store.claim_due("busy-model-fixture")
        self.assertEqual(claimed["event_id"], busy["event_id"])
        await self.watcher.tick()
        self.sender.assert_awaited_once()
        self.assertEqual(self.store.event(busy["event_id"])["status"], "leased")
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")
        self.assertIsNone(self.store.run(claimed["run_id"])["dispatch_intent_at"])
        self.assertEqual(Inbox(self.store).list("example")["messages"][0]["state"], "created")
        await self.watcher.tick()
        reopened = Store(self.config, clock=lambda: self.now)
        try:
            await notifications.Watcher(reopened, self.sender).tick()
        finally:
            reopened.close()
        self.sender.assert_awaited_once()

    async def test_disabled_cancelled_future_old_and_silent_jobs_do_not_popup(self):
        event = self.event()
        await self.watcher.tick()
        self.sender.assert_not_awaited()
        notifications.configure(self.config, True)
        self.store.modify(event["event_id"], event["version"], cancel=True)
        self.event("future", delay=60)
        old = self.event("old", delay=-86400)
        self.event("silent", intent="agent_wake")
        await self.watcher.tick()
        self.sender.assert_not_awaited()
        self.assertEqual({r["event_id"] for r in Inbox(self.store).list("example")["messages"]}, {event["event_id"], old["event_id"]})

    async def test_rescheduled_event_uses_new_due_and_hidden_body(self):
        notifications.configure(self.config, True, show_body=False)
        event = self.event()
        self.store.modify(event["event_id"], event["version"], at=utc_text(self.now + timedelta(seconds=60)))
        await self.watcher.tick()
        self.sender.assert_not_awaited()
        self.now += timedelta(seconds=61)
        await self.watcher.tick()
        self.assertNotIn("PRIVATE_FIXTURE_REMINDER", self.sender.call_args.kwargs["body"])

    async def test_unknown_delivery_is_not_retried_or_reported_seen(self):
        notifications.configure(self.config, True)
        event = self.event()
        async def interrupted(**_):
            raise asyncio.CancelledError()
        watcher = notifications.Watcher(self.store, interrupted)
        with self.assertRaises(asyncio.CancelledError):
            await watcher.tick()
        self.assertEqual(self.store.metadata(notifications.PREFIX + event["event_id"])["status"], "unknown")
        self.assertEqual(Inbox(self.store).list("example")["messages"][0]["state"], "created")
        await self.watcher.tick()
        self.sender.assert_not_awaited()

    async def test_explicit_reschedule_after_first_alarm_creates_one_new_alarm(self):
        notifications.configure(self.config, True)
        event = self.event()
        await self.watcher.tick()
        self.store.modify(event["event_id"], event["version"], at=utc_text(self.now + timedelta(seconds=60)))
        await self.watcher.tick()
        self.sender.assert_awaited_once()
        self.now += timedelta(seconds=61)
        await self.watcher.tick()
        await self.watcher.tick()
        self.assertEqual(self.sender.await_count, 2)
        self.assertNotEqual(self.sender.await_args_list[0].kwargs["tag"], self.sender.await_args_list[1].kwargs["tag"])
        self.assertEqual(self.store.connection.execute("select count(*) from runtime_metadata where key like 'desktop-history:v1:%'").fetchone()[0], 2)

    async def test_unavailable_backend_preserves_inbox_without_replaying_model(self):
        notifications.configure(self.config, True)
        event = self.event()
        self.sender.return_value = {"status": "unavailable", "backend": "linux", "code": "desktop_rejected"}
        await self.watcher.tick()
        self.assertEqual(self.store.event(event["event_id"])["status"], "queued")
        self.assertEqual(self.store.connection.execute("select count(*) from runs").fetchone()[0], 0)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)
        attempt = self.store.metadata(notifications.PREFIX + event["event_id"])
        self.assertNotIn("PRIVATE_FIXTURE_REMINDER", json.dumps(attempt))
        Inbox(self.store).project_due(complete=True)
        self.assertEqual(self.store.event(event["event_id"])["status"], "succeeded")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(len(Inbox(self.store).list("example")["messages"]), 1)

    def test_private_settings_and_separate_service_keep_model_secrets_out(self):
        notifications.configure(self.config, True, backend="windows")
        path = self.config.state_dir / "desktop-notifications.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        unit = service.render(self.path, command=("notifications", "run"))
        self.assertIn('"notifications" "run"', unit)
        self.assertNotIn("EnvironmentFile=", unit)
        self.assertNotIn('"serve"', unit)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "FAKE_KEY_MUST_NOT_LEAVE", "HTTPS_PROXY": "FAKE_PROXY", "WSLENV": "OPENAI_API_KEY"}):
            env = notifications.environment("linux")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertNotIn("WSLENV", env)
        path.unlink()
        path.symlink_to(self.path)
        with self.assertRaises(RuntimeContractError): notifications.settings(self.config)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def process(self, stdout=b"", code=0):
        p = Mock(returncode=code)
        p.communicate = AsyncMock(return_value=(stdout, b""))
        p.wait = AsyncMock()
        return p

    async def test_ubuntu_uses_notify_send_without_shell_and_escapes_markup(self):
        p = self.process()
        with patch.object(notifications, "linux_readiness", new_callable=AsyncMock, return_value=None), patch.object(notifications, "is_wsl", return_value=False), patch.object(notifications.shutil, "which", return_value="/fixture/notify-send"), patch.object(notifications.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=p) as spawn:
            result = await notifications.send(body='<b>"; $(touch /tmp/never)</b>')
            self.assertEqual(result["status"], "accepted")
            self.assertIn("--", spawn.call_args.args)
            self.assertIn("&lt;b&gt;", spawn.call_args.args[-1])
            self.assertNotIn("shell", spawn.call_args.kwargs)

    async def test_wsl_uses_static_powershell_program_and_data_on_stdin(self):
        p = self.process(b"PAVEWAY_NOTIFICATION_ACCEPTED\r\n")
        with patch.object(notifications, "is_wsl", return_value=True), patch.object(notifications.shutil, "which", return_value="/fixture/powershell.exe"), patch.object(notifications.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=p) as spawn:
            body = '中文 " ; $(Get-Process) <script>'
            result = await notifications.send(body=body)
            self.assertEqual(result["status"], "accepted")
            self.assertIn("-EncodedCommand", spawn.call_args.args)
            self.assertNotIn(body, " ".join(spawn.call_args.args))
            self.assertEqual(json.loads(p.communicate.call_args.args[0])["body"], body)

    async def test_missing_command_and_timeout_have_clear_non_seen_outcomes(self):
        with patch.object(notifications.shutil, "which", return_value=None):
            self.assertEqual((await notifications.send("linux"))["code"], "command_missing")
        p = self.process(code=None)
        p.communicate.side_effect = TimeoutError()
        with patch.object(notifications, "linux_readiness", new_callable=AsyncMock, return_value=None), patch.object(notifications.shutil, "which", return_value="/fixture/notify-send"), patch.object(notifications.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=p):
            self.assertEqual((await notifications.send("linux"))["status"], "unknown")
            p.kill.assert_called_once()


class WatcherProcessTests(unittest.TestCase):
    def test_closed_ui_and_no_runtime_still_notify_once_across_process_restart(self):
        with tempfile.TemporaryDirectory(prefix="pw-notification-process-") as temporary:
            root = Path(temporary)
            path = root / "config.toml"
            initialize(path, str(root / "state"), str(root / "workflows"))
            config = load_config(path)
            notifications.configure(config, True, backend="linux")
            capture = root / "notification-calls.jsonl"
            binary = root / "bin/notify-send"
            binary.parent.mkdir()
            binary.write_text('#!' + sys.executable + '\nimport json,sys\nfrom pathlib import Path\np=Path(' + repr(str(capture)) + ')\nwith p.open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
            binary.chmod(0o700)
            probe = binary.parent / "gdbus"
            probe.write_text("#!/bin/sh\nprintf '(true,)\\n'\n")
            probe.chmod(0o700)
            store = Store(config)
            try:
                event = store.enqueue("example", "training", "Process fixture", "process-fixture",
                    {"native_wakeup": True, "intent": "reminder", "message": "Popup while UI closed"},
                    at=utc_text(datetime.now(timezone.utc) + timedelta(seconds=1)))
                environment = dict(os.environ, PATH=str(binary.parent) + os.pathsep + os.environ.get("PATH", os.defpath))
                argv = [sys.executable, "-m", "oicoach_runtime", "notifications", "run", "--config", str(path)]
                with (root / "worker.log").open("w") as log:
                    for index in range(2):
                        process = subprocess.Popen(argv, env=environment, stdout=log, stderr=log)
                        try:
                            deadline = time.monotonic() + 8
                            while time.monotonic() < deadline:
                                attempt = store.metadata(notifications.PREFIX + event["event_id"])
                                if attempt and attempt["status"] == "accepted" and (index == 0 or notifications.status(config)["worker_running"]): break
                                time.sleep(.05)
                            self.assertEqual(attempt["status"], "accepted")
                            if index: time.sleep(1.2)
                        finally:
                            process.terminate(); process.wait(timeout=5)
                        self.assertEqual(process.returncode, 0)
                self.assertEqual(len(capture.read_text().splitlines()), 1)
                self.assertEqual(store.event(event["event_id"])["status"], "queued")
                self.assertEqual(store.connection.execute("select count(*) from runs").fetchone()[0], 0)
                self.assertEqual(Inbox(store).list("example")["messages"][0]["state"], "created")
                # A real Runtime later settles the already-notified event. The
                # worker runs concurrently for the next event; neither owns a
                # model Run, and they share one durable delivery per occurrence.
                second = store.enqueue("example", "training", "Concurrent fixture", "concurrent-fixture",
                    {"native_wakeup": True, "intent": "reminder", "message": "CONCURRENT_BODY"},
                    at=utc_text(datetime.now(timezone.utc) + timedelta(seconds=1)))
                with (root / "concurrent.log").open("w") as log:
                    daemon = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(path)], env=environment, stdout=log, stderr=log)
                    worker = subprocess.Popen(argv, env=environment, stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            if (store.event(second["event_id"])["status"] == "succeeded" and capture.exists() and
                                    len(capture.read_text().splitlines()) == 2): break
                            time.sleep(.05)
                        self.assertEqual(store.event(event["event_id"])["status"], "succeeded")
                        self.assertEqual(store.event(second["event_id"])["status"], "succeeded")
                        self.assertEqual(len(capture.read_text().splitlines()), 2)
                        self.assertEqual(len(Inbox(store).list("example")["messages"]), 2)
                        self.assertEqual(store.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                    finally:
                        worker.terminate(); daemon.terminate()
                        worker.wait(timeout=5); daemon.wait(timeout=8)
            finally:
                store.close()
