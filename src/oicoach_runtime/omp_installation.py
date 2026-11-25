"""Locate the user's OMP, without provisioning or pinning an upstream version."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .errors import RuntimeContractError

OMP_PATH_ENV = "PAVEWAY_OMP_PATH"


def resolve_omp(configured="omp") -> str:
    explicit = os.environ.get(OMP_PATH_ENV)
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not explicit or not candidate.is_absolute():
            raise RuntimeContractError(f"{OMP_PATH_ENV} must name an absolute OMP executable path")
        name, source = str(candidate), OMP_PATH_ENV
    else:
        name, source = configured, "agent.executable" if configured != "omp" else "PATH"
    try:
        executable = shutil.which(name)
        if executable:
            return str(Path(executable).resolve(strict=True))
    except (OSError, ValueError, RuntimeError):
        pass
    raise RuntimeContractError(f"OMP executable not found or not executable ({source}): {name}")


def omp_file_identity(executable: str) -> list[int]:
    """Detect upgrades in place without keying compatibility on a version number."""
    try:
        info = os.stat(executable)
    except OSError:
        raise RuntimeContractError(f"Cannot inspect the selected OMP executable: {executable}") from None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def omp_version(executable: str) -> str:
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise RuntimeContractError("OMP --version failed or timed out") from None
    match = re.fullmatch(r"omp(?:/| v?)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", result.stdout.strip())
    if result.returncode or not match:
        raise RuntimeContractError("OMP --version did not report an OMP version")
    return match[1]
