"""Read-only diagnostics; never start an agent turn or inspect credentials."""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .config import Config
from .db import inspect_database
from .errors import RuntimeContractError
from .ipc import request
from .workflows import WorkflowProvider

REQUIRED_FLAGS = ("--cwd", "--session-dir", "--resume", "--mode", "--no-ui")


def probe_omp(executable: str) -> list[dict]:
    resolved = shutil.which(executable)
    if not resolved:
        return [{"check": "omp", "status": "FAIL", "detail": "Executable not found; storage remains usable"}]
    try:
        version = subprocess.run([resolved, "--version"], capture_output=True, text=True, timeout=10, check=False)
        help_result = subprocess.run([resolved, "--help"], capture_output=True, text=True, timeout=10, check=False)
        matched = re.search(r"\bomp(?:/| v?)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", version.stdout)
        # Do not echo arbitrary stdout/stderr; extensions may expose private data there.
        if version.returncode or help_result.returncode or not matched:
            return [{"check": "omp", "status": "FAIL", "detail": "OMP version/help probe failed or returned an unrecognized version"}]
        missing = [flag for flag in REQUIRED_FLAGS if not re.search(re.escape(flag) + r"(?=[=\s,]|$)", help_result.stdout)]
        return [{"check": "omp", "status": "FAIL" if missing else "PASS",
                 "detail": f"OMP {matched[1]}; missing flags: {', '.join(missing) if missing else 'none'}"},
                {"check": "omp_rpc", "status": "WARN", "detail": "CLI flags verified only; real-provider RPC and session integration still require M2 verification"}]
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return [{"check": "omp", "status": "FAIL", "detail": "OMP version/help probe failed or timed out"}]


def _private(path: Path, directory: bool) -> bool:
    try:
        info = path.lstat()
        return (not path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o077
                and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))
    except OSError:
        return False


def diagnose(config: Config, config_path: Path) -> dict:
    checks = [
        {"check": "python", "status": "PASS", "detail": sys.version.split()[0]},
        {"check": "config", "status": "PASS", "detail": "Config schema v1 is valid"},
    ]
    for name, path, directory in (("config_permissions", config_path, False),
                                   ("state_permissions", config.state_dir, True),
                                   ("database_permissions", config.database_path, False)):
        checks.append({"check": name, "status": "PASS" if _private(path, directory) else "FAIL",
                       "detail": "Private and owned" if _private(path, directory) else "Missing, unsafe ownership/type, or group/other permissions"})
    checks.append({"check": "state_writable", "status": "PASS" if os.access(config.state_dir, os.W_OK | os.X_OK) else "FAIL",
                   "detail": "Filesystem access check; no files written"})
    try:
        schema = inspect_database(config.database_path)
        checks.append({"check": "database", "status": "PASS" if schema["journal_mode"] == "wal" else "FAIL", "detail": schema})
    except RuntimeContractError as error:
        checks.append({"check": "database", "status": "FAIL", "detail": str(error)})
    if config.workflow_root.is_dir():
        provider = WorkflowProvider(config.workflow_root)
        directories = sorted(p for p in config.workflow_root.iterdir() if p.is_dir())
        if not directories:
            checks.append({"check": "workflows", "status": "WARN", "detail": "No workflows installed"})
        for directory in directories:
            try:
                workflow = provider.describe(directory.name)
                checks.append({"check": "workflow", "status": "PASS", "detail": f"{workflow.workflow_id}@{workflow.revision}"})
            except RuntimeContractError as error:
                checks.append({"check": "workflow", "status": "FAIL", "detail": str(error)})
    else:
        checks.append({"check": "workflows", "status": "FAIL", "detail": "Configured workflow root is missing"})
    checks.extend(probe_omp(config.agent.executable))
    try:
        runtime = request(config.server.socket_path, "status")
        checks.append({"check": "daemon", "status": "PASS", "detail": runtime})
    except RuntimeContractError:
        checks.append({"check": "daemon", "status": "WARN", "detail": "Daemon is offline or its private socket is unavailable; run serve"})
    return {"api_version": 1, "milestone": "M1", "ok": not any(c["status"] == "FAIL" for c in checks), "checks": checks}
