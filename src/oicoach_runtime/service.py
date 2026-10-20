"""Owned systemd user units. Installing never enables or starts a service."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import load_config, xdg_path
from .errors import ConflictError, RuntimeContractError

DEFAULT_NAME = "oicoach-runtime.service"
MARKER = "# Managed by oicoach-runtime; config="


def environment_path(config_path):
    return Path(config_path).absolute().with_suffix(".service.env")


def capture_environment(config_path, names):
    """Persist only explicitly selected variables, never values in argv/unit/output."""
    path = environment_path(config_path)
    marker = MARKER + json.dumps(str(Path(config_path).absolute()))
    values = {}
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
            raise ConflictError("Service environment file must be private, owned and not a symlink")
        lines = path.read_text().splitlines()
        if not lines or lines[0] != marker:
            raise ConflictError("Refusing to overwrite an unmanaged service environment file")
        try:
            for line in lines[1:]:
                key, value = line.split("=", 1)
                values[key] = json.loads(value)
        except (ValueError, TypeError):
            raise ConflictError("Invalid managed service environment file") from None
    for key in names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key not in os.environ:
            raise RuntimeContractError("Captured environment names must exist in the invoking shell")
        values[key] = os.environ[key]
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise RuntimeContractError("Service environment values must be single-line strings without controls")
    text = marker + "\n" + "".join(key + "=" + json.dumps(value, ensure_ascii=False) + "\n" for key, value in sorted(values.items()))
    if len(text.encode()) > 65536:
        raise RuntimeContractError("Service environment file exceeds 64 KiB")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def unit_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.service", name):
        raise RuntimeContractError("Use a simple systemd .service name without paths, templates or specifiers")
    return name


def unit_directory():
    return xdg_path("XDG_CONFIG_HOME", ".config") / "systemd/user"


def quote(value, *, expand_environment=False):
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RuntimeContractError("Systemd paths and environment values cannot contain control characters")
    value = value.replace("%", "%%")
    if expand_environment:
        value = value.replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(config_path, *, working_directory=None):
    path = Path(config_path).absolute()
    load_config(path)
    directory = Path(working_directory).absolute() if working_directory else path.parent
    if not directory.is_dir():
        raise RuntimeContractError("Service working directory must exist")
    quote(directory)  # Validate controls; WorkingDirectory does not use ExecStart quoting.
    if "\\" in str(directory) or str(directory)[-1].isspace():
        raise RuntimeContractError("Choose a service working directory without backslashes or trailing whitespace")
    interpreter = str(Path(sys.executable).absolute())  # Do not resolve away the venv symlink.
    search_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    argv = [interpreter, "-m", "oicoach_runtime", "serve", "--config", str(path)]
    text = MARKER + json.dumps(str(path)) + "\n" + "\n".join([
        "[Unit]", "Description=Paveway OI Agent Runtime", "StartLimitIntervalSec=60", "StartLimitBurst=5", "",
        "[Service]", "Type=simple", "ExecStart=" + " ".join(quote(arg, expand_environment=True) for arg in argv),
        "WorkingDirectory=" + str(directory).replace("%", "%%"), 'Environment="PYTHONUNBUFFERED=1"',
        # EnvironmentFile takes a literal path, not ExecStart-style quoted words.
        "EnvironmentFile=-" + str(environment_path(path)).replace("%", "%%"),
        "Environment=" + quote("PATH=" + search_path), "UMask=0077", "Restart=on-failure", "RestartSec=3",
        "TimeoutStopSec=60", "KillMode=control-group", "StandardOutput=journal", "StandardError=journal", "",
        "[Install]", "WantedBy=default.target", ""])
    return text


def _run(argv, *, check=True):
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise RuntimeContractError("Systemd command unavailable or timed out; check the user manager") from None
    if check and completed.returncode:
        raise RuntimeContractError("Systemd command failed; inspect service status or journal locally")
    return completed


def _owned(path, config_path):
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_size > 65536:
            return False
        first = path.read_text().splitlines()[0]
        return first == MARKER + json.dumps(str(Path(config_path).absolute()))
    except (OSError, UnicodeError, IndexError):
        return False


def install(config_path, *, name=DEFAULT_NAME, working_directory=None, replace=False, capture_env=()):
    name = unit_name(name)
    text = render(config_path, working_directory=working_directory)
    directory = unit_directory()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    exists = destination.exists() or destination.is_symlink()
    if exists and not _owned(destination, config_path):
        raise ConflictError("Refusing to replace an unowned unit or a unit for another configuration")
    if exists and destination.read_text() != text and not replace:
        raise ConflictError("Managed unit differs; use service install --replace to update it")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                     prefix="paveway-verify-", suffix=".service", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            _run(["systemd-analyze", "--user", "verify", str(temporary)])
            if capture_env:
                capture_environment(config_path, capture_env)
            if exists:
                if destination.read_text() != text:
                    os.replace(temporary, destination)
            else:
                os.link(temporary, destination)  # Atomic no-overwrite publication.
        finally:
            temporary.unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return {"api_version": 1, "unit": name, "path": str(destination), "installed": True,
            "environment_file": str(environment_path(config_path)), "captured_environment": list(capture_env),
            "enabled_or_started": False, "next_commands": [
                ["systemctl", "--user", "enable", "--now", name],
                ["systemctl", "--user", "status", name],
                ["journalctl", "--user", "-u", name, "-f"]]}


def require_owned(config_path, name):
    path = unit_directory() / unit_name(name)
    if not _owned(path, config_path):
        raise ConflictError("No managed unit for this name/configuration; install it first")
    return path


def control(config_path, action, *, name=DEFAULT_NAME, now=False):
    if action not in ("start", "stop", "restart", "enable", "disable", "uninstall"):
        raise RuntimeContractError("Unsupported service action")
    if now and action not in ("enable", "disable"):
        raise RuntimeContractError("--now is only valid for enable/disable")
    path = require_owned(config_path, name)
    argv = ["systemctl", "--user", "disable", "--now", name] if action == "uninstall" else (
        ["systemctl", "--user", action] + (["--now"] if now else []) + [name])
    _run(argv)
    if action == "uninstall":
        # Recheck ownership after waiting on the manager, before deleting anything.
        require_owned(config_path, name).unlink()
        _run(["systemctl", "--user", "daemon-reload"])
    return {"api_version": 1, "unit": name, "action": action, "completed": True,
            "runtime_state_preserved": True, "unit_path": str(path)}


def status(config_path, *, name=DEFAULT_NAME):
    path = unit_directory() / unit_name(name)
    if not path.exists() and not path.is_symlink():
        return {"api_version": 1, "unit": name, "installed": False, "managed": False}
    managed = _owned(path, config_path)
    if not managed:
        return {"api_version": 1, "unit": name, "installed": True, "managed": False}
    result = _run(["systemctl", "--user", "show", name, "--no-pager",
        "--property=ActiveState,SubState,UnitFileState,MainPID,NRestarts,ExecMainStatus"])
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"api_version": 1, "unit": name, "installed": True, "managed": True, **properties}


def logs(config_path, *, name=DEFAULT_NAME, lines=50):
    require_owned(config_path, name)
    if type(lines) is not int or not 1 <= lines <= 1000:
        raise RuntimeContractError("Journal lines must be between 1 and 1000")
    result = _run(["journalctl", "--user", "--unit", name, "--lines", str(lines), "--no-pager", "--output=json"])
    entries = []
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
            message = record.get("MESSAGE")
            if isinstance(message, str):
                entries.append({"timestamp_us": record.get("__REALTIME_TIMESTAMP"), "message": message[:4096]})
        except (ValueError, AttributeError):
            continue
    return {"api_version": 1, "unit": name, "entries": entries}
