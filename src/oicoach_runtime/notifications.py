"""Desktop reminders independent of the foreground UI and model execution queue."""

import asyncio
import base64
import html
import hashlib
import json
import os
from pathlib import Path
from datetime import timedelta
import re
import shutil
import signal
import stat
import tempfile
import uuid
from importlib.resources import files

from .config import ensure_private_directory
from .errors import RuntimeContractError
from .inbox import Inbox, occurrence_id
from .models import parse_timestamp, utc_text
from .processes import InstanceLock, process_identity
from .store import Store

UNIT = "paveway-notifications.service"
PREFIX = "desktop-attempt:v1:"


def settings(config):
    path = config.state_dir / "desktop-notifications.json"
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "enabled": False, "backend": "auto", "show_body": True, "since_utc": None}
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
        raise RuntimeContractError("Desktop notification settings must be a private owned JSON file")
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or set(value) != {"schema_version", "enabled", "backend", "show_body", "since_utc"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        if type(value["enabled"]) is not bool or type(value["show_body"]) is not bool or value["backend"] not in ("auto", "linux", "windows"):
            raise ValueError()
        if value["since_utc"] is not None: value["since_utc"] = utc_text(parse_timestamp(value["since_utc"]))
        if value["enabled"] and value["since_utc"] is None: raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError, KeyError):
        raise RuntimeContractError("Invalid desktop notification settings") from None


def configure(config, enabled, backend=None, show_body=None):
    from datetime import datetime, timezone
    if type(enabled) is not bool or (show_body is not None and type(show_body) is not bool):
        raise RuntimeContractError("Notification enabled/show_body must be boolean")
    old = settings(config)
    value = {**old, "enabled": enabled}
    if enabled and not old["enabled"]: value["since_utc"] = utc_text(datetime.now(timezone.utc))
    if backend is not None:
        if backend not in ("auto", "linux", "windows"): raise RuntimeContractError("Choose auto, linux or windows notifications")
        value["backend"] = backend
    if show_body is not None: value["show_body"] = show_body
    ensure_private_directory(config.state_dir)
    with tempfile.NamedTemporaryFile(dir=config.state_dir, prefix="desktop-settings-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value).encode()); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, config.state_dir / "desktop-notifications.json")
        finally: temporary.unlink(missing_ok=True)
    return {"api_version": 1, "settings": value}


def is_wsl():
    try: return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError: return False


def environment(backend):
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
               "XDG_RUNTIME_DIR", "WSL_INTEROP", "WSL_DISTRO_NAME", "SystemRoot", "WINDIR", "TMP", "TEMP", "USER", "LOGNAME")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    if backend == "windows":
        current = Path(env.get("WSL_INTEROP", "/nonexistent"))
        if not current.is_socket():
            candidates = []
            for p in Path("/run/WSL").glob("*_interop"):
                try: info = p.lstat()
                except OSError: continue
                if stat.S_ISSOCK(info.st_mode) and info.st_uid in (0, os.getuid()): candidates.append(p)
            if candidates: env["WSL_INTEROP"] = str(max(candidates, key=lambda p: p.stat().st_mtime_ns))
    elif not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(f"/run/user/{os.getuid()}/bus")
        if bus.exists() and bus.is_socket() and bus.stat().st_uid == os.getuid():
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(bus)
    return env


def selected_backend(value):
    return ("windows" if is_wsl() else "linux") if value == "auto" else value


def clean(value, maximum):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)[:maximum]


async def linux_readiness(env):
    """No Notify call is made here: temporary failures are provably unsent."""
    probe = shutil.which("gdbus", path=env.get("PATH", os.defpath))
    if not probe:
        return {"status": "unavailable", "backend": "linux", "code": "notification_probe_missing", "retryable": False}
    process = None
    try:
        base = [probe, "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus", "--method"]
        process = await asyncio.create_subprocess_exec(*base, "org.freedesktop.DBus.NameHasOwner", "org.freedesktop.Notifications",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
        output, _ = await asyncio.wait_for(process.communicate(), 2)
        if process.returncode:
            return {"status": "unavailable", "backend": "linux", "code": "session_bus_unreachable", "retryable": True}
        if output.strip() == b"(true,)":
            return None
        process = await asyncio.create_subprocess_exec(*base, "org.freedesktop.DBus.StartServiceByName", "org.freedesktop.Notifications", "0",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
        await asyncio.wait_for(process.communicate(), 2)
        if process.returncode == 0:
            return None
        return {"status": "unavailable", "backend": "linux", "code": "notification_service_not_ready", "retryable": True}
    except (TimeoutError, OSError):
        return {"status": "unavailable", "backend": "linux", "code": "session_bus_unreachable", "retryable": True}
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def send(backend="auto", title="Paveway · 定时提醒", body="定时任务已到期，请打开 Paveway /inbox 查看。", tag=None):
    backend = selected_backend(backend)
    env = environment(backend)
    executable = shutil.which("powershell.exe" if backend == "windows" else "notify-send", path=env.get("PATH", os.defpath))
    if not executable:
        return {"status": "unavailable", "backend": backend, "code": "command_missing"}
    if backend == "linux":
        unavailable = await linux_readiness(env)
        if unavailable:
            return unavailable
    title, body = clean(title, 128), clean(body, 320)
    payload = None
    if backend == "windows":
        program = files("oicoach_runtime").joinpath("bundled/desktop-notification.ps1").read_text()
        encoded = base64.b64encode(program.encode("utf-16le")).decode()
        argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
        payload = json.dumps({"title": title, "body": body, "tag": (tag or uuid.uuid4().hex)[:16]}).encode()
    else:
        argv = [executable, "--app-name=Paveway", "--icon=appointment-soon", "--expire-time=15000", "--", title, html.escape(body, quote=False)]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if payload else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=8)
        accepted = process.returncode == 0 and (backend != "windows" or b"PAVEWAY_NOTIFICATION_ACCEPTED" in stdout)
        if not accepted and backend == "linux" and not any(marker in stderr for marker in (b".Rejected", b"AccessDenied", b"NotAuthorized")):
            return {"status": "unknown", "backend": backend, "code": "notification_transport_unknown"}
        return {"status": "accepted" if accepted else "unavailable", "backend": backend, "code": None if accepted else "desktop_rejected"}
    except TimeoutError:
        return {"status": "unknown", "backend": backend, "code": "notification_timeout"}
    except OSError:
        if process is not None:
            return {"status": "unknown", "backend": backend, "code": "notification_transport_unknown"}
        return {"status": "unavailable", "backend": backend, "code": "desktop_unreachable"}
    finally:
        if process is not None and process.returncode is None:
            process.kill(); await process.wait()


class Watcher:
    def __init__(self, store, sender=send, *, gate=None, retry_window_seconds=120):
        self.store, self.sender, self.gate = store, sender, gate
        self.retry_window_seconds = retry_window_seconds

    async def tick(self, stop=None):
        Inbox(self.store).project_due()
        values = settings(self.store.config)
        if not values["enabled"]: return
        rows = self.store.connection.execute("""SELECT e.event_id FROM events e
            LEFT JOIN wake_bindings b ON b.event_id=e.event_id
            WHERE json_extract(e.payload_json,'$.native_wakeup')=1 AND json_extract(e.payload_json,'$.intent')='reminder'
            AND e.status!='cancelled' AND e.not_before_utc>=? AND e.not_before_utc<=?
            AND (json_extract(e.payload_json,'$.wake_binding_id') IS NULL
                OR (b.binding_id IS NOT NULL AND b.status='active' AND b.event_id=e.event_id))
            AND NOT EXISTS(SELECT 1 FROM runtime_metadata m WHERE m.key=? || e.event_id
                AND json_extract(m.value,'$.not_before_utc')=e.not_before_utc
                AND (json_extract(m.value,'$.status')!='retry_wait' OR json_extract(m.value,'$.next_attempt_at')>?))
            ORDER BY e.not_before_utc,e.created_at,e.event_id LIMIT 20""", (values["since_utc"], self.store.timestamp(), PREFIX, self.store.timestamp())).fetchall()
        from .wakebinding import WakeBindings
        service = WakeBindings(self.store)
        for row in rows:
            values = settings(self.store.config)
            if not values["enabled"] or (stop is not None and stop.is_set()):
                return
            event = self.store.event(row[0])
            if event["payload"].get("wake_binding_id"):
                gate = self.gate or service.gate
                try:
                    allowed = await gate(event)
                except Exception:
                    allowed = False
                if not allowed:
                    continue
            with self.store.transaction() as db:
                # Recheck cancellation, binding fencing and due time immediately before intent.
                current = self.store.event(event["event_id"])
                if current["status"] == "cancelled" or not service.local_gate(current) or not values["since_utc"] <= current["not_before_utc"] <= self.store.timestamp(): continue
                event = current
                identity = occurrence_id(event)
                history_key = "desktop-history:v1:" + identity
                legacy_key = "desktop-history:v1:" + hashlib.sha256((event["event_id"] + event["not_before_utc"]).encode()).hexdigest()
                previous = self.store.metadata(history_key) or self.store.metadata(legacy_key)
                indexed = self.store.metadata(PREFIX + event["event_id"])
                if previous is None and indexed and indexed.get("not_before_utc") == event["not_before_utc"]:
                    previous = indexed
                if previous and (previous["status"] != "retry_wait" or previous["next_attempt_at"] > self.store.timestamp()):
                    previous = {**previous, "occurrence_id": identity}
                    self.store._set_metadata(db, PREFIX + event["event_id"], previous)
                    self.store._set_metadata(db, history_key, previous)
                    continue
                if previous and previous["retry_until"] <= self.store.timestamp():
                    terminal = {**previous, "status": "unavailable", "code": "notification_retry_expired"}
                    self.store._set_metadata(db, PREFIX + event["event_id"], terminal)
                    self.store._set_metadata(db, history_key, terminal)
                    continue
                delivery = Inbox(self.store).due(event, db=db)
                attempt = {"event_id": event["event_id"], "occurrence_id": identity, "not_before_utc": event["not_before_utc"],
                    "status": "unknown", "attempted_at": self.store.timestamp(), "code": "notification_interrupted",
                    "delivery_id": delivery["delivery_id"], "attempt": (previous or {}).get("attempt", 0) + 1,
                    "retry_until": (previous or {}).get("retry_until") or utc_text(self.store.clock() + timedelta(seconds=self.retry_window_seconds))}
                self.store._set_metadata(db, PREFIX + event["event_id"], attempt)
                self.store._set_metadata(db, history_key, attempt)
            body = delivery["text"] if values["show_body"] else "定时任务已到期，请打开 Paveway /inbox 查看。"
            result = await self.sender(backend=values["backend"], body=body, tag=identity)
            outcome = {**attempt, **result, "completed_at": self.store.timestamp()}
            if result.get("retryable") is True and result["status"] == "unavailable" and self.store.timestamp() < attempt["retry_until"]:
                next_attempt = min(self.store.clock() + timedelta(seconds=min(30, 2 ** min(attempt["attempt"], 5))), parse_timestamp(attempt["retry_until"]))
                outcome.update(status="retry_wait", next_attempt_at=utc_text(next_attempt))
            with self.store.transaction() as db:
                self.store._set_metadata(db, PREFIX + event["event_id"], outcome)
                self.store._set_metadata(db, history_key, outcome)
            print(json.dumps({"component": "desktop-notifications", "transition": outcome["status"],
                "backend": result.get("backend"), "code": outcome.get("code")}), flush=True)


async def run(config):
    from . import __version__
    ensure_private_directory(config.state_dir)
    lock = InstanceLock(config.state_dir / "desktop-notifications.lock")
    store = Store(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    try:
        with store.transaction() as db:
            store._set_metadata(db, "desktop-worker:v1", {"pid": os.getpid(), "identity": process_identity(os.getpid()), "version": __version__})
        print(json.dumps({"component": "desktop-notifications", "transition": "ready"}), flush=True)
        watcher = Watcher(store)
        while not stop.is_set():
            await watcher.tick(stop)
            try: await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError: pass
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM): loop.remove_signal_handler(sig)
        store.close(); lock.close()


def status(config):
    from .frontend import alive
    store = Store(config)
    try:
        worker = store.metadata("desktop-worker:v1") or {}
        values = settings(config)
        row = store.connection.execute("SELECT value FROM runtime_metadata WHERE key LIKE ? ORDER BY json_extract(value,'$.attempted_at') DESC LIMIT 1", (PREFIX + "%",)).fetchone()
        return {"api_version": 1, "settings": values, "backend": selected_backend(values["backend"]),
            "worker_running": alive(worker.get("pid"), worker.get("identity")),
            "worker_version": worker.get("version"),
            "last_attempt": json.loads(row[0]) if row else None,
            "accepted_is_not_user_seen": True}
    finally: store.close()
