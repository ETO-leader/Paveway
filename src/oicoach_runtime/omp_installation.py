"""Locate the user's OMP, without provisioning or pinning an upstream version."""

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import RuntimeContractError

OMP_PATH_ENV = "PAVEWAY_OMP_PATH"
OMP_IDENTITY_ENV = "PAVEWAY_OMP_FILE_IDENTITY"
REFUSED_FRAME = "paveway_omp_installation_refused"


class OmpInstallationUnavailable(RuntimeContractError):
    code = "omp_installation_unavailable"

    def __init__(self):
        super().__init__("Selected OMP installation is unavailable or changed. "
                         "Safely hand off Runtime before starting new OMP work.")


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
    return list(_file_identity(info))


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass(frozen=True)
class OmpInstallation:
    path: str
    identity: tuple[int, int, int, int]

    @classmethod
    def select(cls, configured="omp"):
        path = resolve_omp(configured)
        return cls(path, tuple(omp_file_identity(path)))

    @classmethod
    def for_command(cls, configured="omp"):
        """An OMP-launched control CLI inherits its owner's selection, not PATH."""
        expected = os.environ.get(OMP_IDENTITY_ENV)
        if expected is None:
            return cls.select(configured)
        path = os.environ.get(OMP_PATH_ENV, "")
        try:
            identity = _parse_identity(expected)
            if not os.path.isabs(path):
                raise ValueError
        except ValueError:
            raise OmpInstallationUnavailable() from None
        return cls(path, identity)

    def export(self, environment):
        environment[OMP_PATH_ENV] = self.path
        environment[OMP_IDENTITY_ENV] = json.dumps(self.identity)

    def check(self):
        try:
            info = os.stat(self.path)
            if stat.S_ISREG(info.st_mode) and os.access(self.path, os.X_OK) and _file_identity(info) == self.identity:
                return
        except OSError:
            pass
        raise OmpInstallationUnavailable()

    def command(self, args, *, rpc=False):
        """Check admission and arrange the final, exec-side identity check."""
        self.check()
        return [sys.executable, "-m", "oicoach_runtime.omp_installation",
                "rpc" if rpc else "cli", json.dumps(self.identity), self.path, *args]


def _parse_identity(text):
    fields = json.loads(text)
    if not isinstance(fields, list) or len(fields) != 4 or any(type(value) is not int for value in fields):
        raise ValueError
    return tuple(fields)


def _exec_selected(mode, expected, path, *args):
    # This process is immediately replaced by OMP; it never holds Runtime locks.
    # Open before inspecting; exec never resolves the unpinned path again.
    refused = OmpInstallationUnavailable()
    installation = OmpInstallation(path, _parse_identity(expected))
    fd = None
    try:
        fd = os.open(path, os.O_PATH)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or _file_identity(info) != installation.identity:
            raise refused
        argv = [path, *args]
        try:
            os.execve(fd, argv, os.environ)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
            # The kernel opens a shebang script through the fd after exec;
            # it returns ENOENT if that fd is close-on-exec. The retry uses
            # the same verified descriptor, never an unchecked pathname.
            if _file_identity(os.fstat(fd)) != installation.identity:
                raise refused
            os.set_inheritable(fd, True)
            os.execve(fd, argv, os.environ)
    except OSError as error:
        try:
            installation.check()
        except OmpInstallationUnavailable:
            pass
        else:
            print(f"Cannot execute selected OMP (errno {error.errno})", file=sys.stderr)
            return 126
    except OmpInstallationUnavailable:
        pass
    finally:
        if fd is not None:
            os.close(fd)
    if mode == "rpc":
        print(json.dumps({"type": REFUSED_FRAME}), flush=True)
    print(str(refused), file=sys.stderr, flush=True)
    return 126


def omp_version(executable: str, *, installation=None) -> str:
    try:
        argv = installation.command(["--version"]) if installation is not None else [executable, "--version"]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise RuntimeContractError("OMP --version failed or timed out") from None
    match = re.fullmatch(r"omp(?:/| v?)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", result.stdout.strip())
    if result.returncode or not match:
        raise RuntimeContractError("OMP --version did not report an OMP version")
    return match[1]


if __name__ == "__main__":
    raise SystemExit(_exec_selected(*sys.argv[1:]))
