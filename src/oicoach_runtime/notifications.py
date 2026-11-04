"""Desktop reminders independent of the foreground UI and model execution queue."""

import asyncio
import base64
import html
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import tempfile
import uuid
from importlib.resources import files

from .config import ensure_private_directory
from .errors import RuntimeContractError
from .inbox import Inbox
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


async def send(backend="auto", title="Paveway · 定时提醒", body="定时任务已到期，请打开 Paveway /inbox 查看。", tag=None):
    backend = selected_backend(backend)
    executable = shutil.which("powershell.exe" if backend == "windows" else "notify-send")
    if not executable:
        return {"status": "unavailable", "backend": backend, "code": "command_missing"}
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
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=environment(backend))
        stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=8)
        accepted = process.returncode == 0 and (backend != "windows" or b"PAVEWAY_NOTIFICATION_ACCEPTED" in stdout)
        return {"status": "accepted" if accepted else "unavailable", "backend": backend, "code": None if accepted else "desktop_rejected"}
    except TimeoutError:
        return {"status": "unknown", "backend": backend, "code": "notification_timeout"}
    except OSError:
        return {"status": "unavailable", "backend": backend, "code": "desktop_unreachable"}
    finally:
        if process is not None and process.returncode is None:
            process.kill(); await process.wait()


class Watcher:
    def __init__(self, store, sender=send):
        self.store, self.sender = store, sender

    async def tick(self, stop=None):
        values = settings(self.store.config)
        if not values["enabled"]: return
        rows = self.store.connection.execute("""SELECT event_id FROM events e
            WHERE json_extract(payload_json,'$.native_wakeup')=1 AND json_extract(payload_json,'$.intent')='reminder'
            AND status!='cancelled' AND not_before_utc>=? AND not_before_utc<=?
            AND NOT EXISTS(SELECT 1 FROM runtime_metadata m WHERE m.key=? || e.event_id
                AND json_extract(m.value,'$.not_before_utc')=e.not_before_utc)
            ORDER BY not_before_utc,created_at,event_id LIMIT 20""", (values["since_utc"], self.store.timestamp(), PREFIX)).fetchall()
        for row in rows:
            values = settings(self.store.config)
            if not values["enabled"] or (stop is not None and stop.is_set()):
                return
            event = self.store.event(row[0])
            with self.store.transaction() as db:
                # Recheck cancellation and fencing immediately before intent.
                current = self.store.event(event["event_id"])
                previous = self.store.metadata(PREFIX + event["event_id"])
                if current["status"] == "cancelled" or not values["since_utc"] <= current["not_before_utc"] <= self.store.timestamp() or (previous and previous["not_before_utc"] == current["not_before_utc"]): continue
                event = current
                delivery = Inbox(self.store).due(event, db=db)
                identity = hashlib.sha256((event["event_id"] + event["not_before_utc"]).encode()).hexdigest()
                history_key = "desktop-history:v1:" + identity
                attempt = {"event_id": event["event_id"], "not_before_utc": event["not_before_utc"], "status": "unknown", "attempted_at": self.store.timestamp(), "code": "notification_interrupted"}
                attempt["delivery_id"] = delivery["delivery_id"]
                self.store._set_metadata(db, PREFIX + event["event_id"], attempt)
                self.store._set_metadata(db, history_key, attempt)
            body = delivery["text"] if values["show_body"] else "定时任务已到期，请打开 Paveway /inbox 查看。"
            result = await self.sender(backend=values["backend"], body=body, tag=identity)
            with self.store.transaction() as db:
                outcome = {**attempt, **result, "completed_at": self.store.timestamp()}
                self.store._set_metadata(db, PREFIX + event["event_id"], outcome)
                self.store._set_metadata(db, history_key, outcome)
            print(json.dumps({"component": "desktop-notifications", "transition": result["status"],
                "backend": result.get("backend"), "code": result.get("code")}), flush=True)


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
