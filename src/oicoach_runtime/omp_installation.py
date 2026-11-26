"""Select, admit and execute exact OMP installations."""

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .build_id import runtime_build_id
from .config import ensure_private_directory
from .errors import RuntimeContractError
from .processes import InstanceLock

OMP_PATH_ENV = "PAVEWAY_OMP_PATH"
OMP_IDENTITY_ENV = "PAVEWAY_OMP_FILE_IDENTITY"
REFUSED_FRAME = "paveway_omp_installation_refused"


class OmpInstallationUnavailable(RuntimeContractError):
    code = "omp_installation_unavailable"

    def __init__(self):
        super().__init__("Selected OMP installation is unavailable or changed. "
                         "Safely hand off Runtime before starting new OMP work.")


class OmpCandidateRejected(RuntimeContractError):
    code = "omp_candidate_rejected"

    def __init__(self, message, *, native_failure=None, version=None):
        super().__init__(message)
        self.native_failure = native_failure
        self.version = version


def resolve_omp(configured="omp", *, environment=None) -> str:
    environment = os.environ if environment is None else environment
    explicit = environment.get(OMP_PATH_ENV)
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not explicit or not candidate.is_absolute():
            raise RuntimeContractError(f"{OMP_PATH_ENV} must name an absolute OMP executable path")
        name, source = str(candidate), OMP_PATH_ENV
    else:
        name, source = configured, "agent.executable" if configured != "omp" else "PATH"
    try:
        executable = shutil.which(name, path=environment.get("PATH", os.defpath))
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
    def select(cls, configured="omp", *, environment=None):
        path = resolve_omp(configured, environment=environment)
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
        # Config/catalog probes run with the project's cwd. Don't import a
        # project module in place of this installed execution boundary.
        return [sys.executable, "-P", "-m", "oicoach_runtime.omp_installation",
                "rpc" if rpc else "cli", json.dumps(self.identity), self.path, *args]


def _identity(fields):
    if not isinstance(fields, (list, tuple)) or len(fields) != 4 or any(type(value) is not int for value in fields):
        raise ValueError
    return tuple(fields)


def _parse_identity(text):
    return _identity(json.loads(text))


def _omp_root(state_dir):
    return Path(state_dir) / "omp"


@dataclass(frozen=True)
class AdmittedOmp:
    source: OmpInstallation
    installed: OmpInstallation
    version: str
    build_id: str

    def status(self):
        return {
            "path": self.installed.path, "file_identity": list(self.installed.identity),
            "source_path": self.source.path, "source_file_identity": list(self.source.identity),
            "pinning": "snapshot" if self.source.path != self.installed.path else "executable_only",
            "installation_status": "selected", "version": self.version,
        }


def load_active_omp(state_dir):
    """Read the committed admission, without resolving the mutable source."""
    root = _omp_root(state_dir)
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OmpCandidateRejected("OMP admission directory is not private")
    try:
        fd = os.open(root / "active.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise OmpCandidateRejected("Cannot read the private OMP active reference") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            value = json.loads(stream.read(8193))
        if value["schema"] != 1 or not re.fullmatch(r"[a-f0-9]{64}", value["build_id"]):
            raise ValueError
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", value["version"]):
            raise ValueError
        source = OmpInstallation(value["source_path"], _identity(value["source_file_identity"]))
        installed = OmpInstallation(value["path"], _identity(value["file_identity"]))
        if not os.path.isabs(source.path) or not os.path.isabs(installed.path):
            raise ValueError
        if installed != source:
            path = Path(installed.path)
            if path.name != "omp" or path.parent.parent != root or not path.parent.name.startswith("snapshot-"):
                raise ValueError
        return AdmittedOmp(source, installed, value["version"], value["build_id"])
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        raise OmpCandidateRejected("Invalid OMP active reference; no candidate was executed") from None


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _snapshot(source, root):
    """Copy standalone ELF bytes; wrappers retain their original package layout."""
    source.check()
    try:
        fd = os.open(source.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except PermissionError:
        return source, None  # The checked exec path can execute execute-only ELF.
    with os.fdopen(fd, "rb") as stream:
        if _file_identity(os.fstat(stream.fileno())) != source.identity:
            raise OmpInstallationUnavailable()
        if stream.read(4) != b"\x7fELF":
            return source, None
        stream.seek(0)
        directory = Path(tempfile.mkdtemp(prefix="snapshot-", dir=root))
        path = directory / "omp"
        try:
            with path.open("xb") as destination:
                shutil.copyfileobj(stream, destination, 1024 * 1024)
                if _file_identity(os.fstat(stream.fileno())) != source.identity:
                    raise OmpInstallationUnavailable()
                os.fchmod(destination.fileno(), 0o500)
                destination.flush()
                os.fsync(destination.fileno())
            _sync_directory(directory)
            return OmpInstallation(str(path), tuple(omp_file_identity(str(path)))), directory
        except BaseException:
            shutil.rmtree(directory)
            raise


def _publish_active(root, admission):
    if admission is None:
        (root / "active.json").unlink(missing_ok=True)
    else:
        payload = {**admission.status(), "schema": 1, "build_id": admission.build_id}
        fd, temporary = tempfile.mkstemp(prefix=".active-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / "active.json")
        finally:
            Path(temporary).unlink(missing_ok=True)


class _Activation:
    def __init__(self, root, admission, previous):
        self.root = root
        self.admission = admission
        self.previous = previous
        self.committed = previous == admission

    @property
    def changed(self):
        return self.committed and self.admission != self.previous

    def _swap(self, expected, replacement):
        lock = InstanceLock(self.root / "activation.lock", timeout=5)
        try:
            if load_active_omp(self.root.parent) != expected:
                raise OmpCandidateRejected("OMP active reference changed during admission; inspect the current Runtime")
            if expected != replacement:
                _publish_active(self.root, replacement)
                # Preserve either snapshot if publishing succeeded but the
                # directory could not be synchronized. A rollback only earns
                # cleanup of the candidate once its reference is durable too.
                if replacement == self.admission:
                    self.committed = True
                _sync_directory(self.root)
        finally:
            lock.close()

    def commit(self):
        self.admission.installed.check()
        self._swap(self.previous, self.admission)
        self.committed = True

    def rollback(self):
        if self.changed:
            self._swap(self.admission, self.previous)
            self.committed = False

    def retire_previous_snapshot(self):
        if not self.changed or self.previous is None:
            return
        previous = self.previous
        if previous.installed.path in (previous.source.path, self.admission.installed.path):
            return
        # Never sweep arbitrary paths. We retire only the exact prior snapshot
        # after the launcher confirms the replacement Runtime owns its locks.
        try:
            previous.installed.check()
            path = Path(previous.installed.path)
            path.unlink()
            path.parent.rmdir()
        except (OSError, OmpInstallationUnavailable):
            pass


def _attest_candidate(state_dir, installed):
    from .omp_probe import probe_omp

    try:
        return probe_omp(installed)
    except OmpCandidateRejected as error:
        if error.native_failure is not None:
            try:
                error.native_failure.persist(Path(state_dir), error.version)
            except OSError:
                pass  # The refusal still takes precedence over diagnostic I/O.
        raise


@contextmanager
def prepare_omp(state_dir, source, *, recheck_wrapper=False):
    """Attest the candidate without moving the active reference or draining Runtime."""

    root = _omp_root(state_dir)
    ensure_private_directory(root)
    active = load_active_omp(state_dir)
    build = runtime_build_id()
    if (active and active.source == source and active.build_id == build
            and not (recheck_wrapper and active.source == active.installed)):
        try:
            active.installed.check()
        except OmpInstallationUnavailable:
            pass
        else:
            yield _Activation(root, active, active)
            return
    installed, directory = _snapshot(source, root)
    activation = None
    try:
        version = _attest_candidate(state_dir, installed)
        source.check()
        activation = _Activation(root, AdmittedOmp(source, installed, version, build), active)
        yield activation
    finally:
        if directory is not None and (activation is None or not activation.committed):
            shutil.rmtree(directory)


def startup_omp(state_dir, configured):
    """Serve the committed installation, including after a systemd auto-restart."""
    root = _omp_root(state_dir)
    active = load_active_omp(state_dir)
    if active is not None:
        active.installed.check()
        build = runtime_build_id()
        if active.build_id != build:
            version = _attest_candidate(state_dir, active.installed)
            admission = AdmittedOmp(active.source, active.installed, version, build)
            _Activation(root, admission, active).commit()
            return admission
        return active
    with prepare_omp(state_dir, OmpInstallation.select(configured)) as activation:
        activation.commit()
        return activation.admission


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
