"""Unattended compatibility admission using the actual Runtime and OMP guardian."""

import asyncio
import errno
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import replace
from pathlib import Path

from .diagnostics import NATIVE_FAILURE_FILE, NativeStartupFailure, read_native_startup_failure
from .errors import RuntimeContractError
from .ipc import request
from .omp_installation import OmpCandidateRejected, OmpInstallation, _parse_identity, omp_version

WORKFLOW = "probe"
TASK = "training"
NATIVE_TIMEOUT = 20
STATUS_MARKER = b"PAVEWAY probe/training"
HELP_MARKER = b"Use /paveway to select an action"
CURSOR_QUERY = b"\x1b[6n"
CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


def _environment(root):
    # No OMP, provider, Python, credential, proxy, or project settings inherited.
    environment = {
        "HOME": str(root), "PATH": os.environ.get("PATH", os.defpath),
        "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TMPDIR": str(root / "tmp"), "PI_CODING_AGENT_DIR": str(root / "agent"),
        "PI_AUTO_QA": "0", "PI_NO_TITLE": "1", "PI_MOTION": "none",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    for kind in ("CONFIG", "DATA", "CACHE", "STATE", "RUNTIME"):
        environment[f"XDG_{kind}_HOME" if kind != "RUNTIME" else "XDG_RUNTIME_DIR"] = str(root / kind.lower())
    return environment


def _argv(mode, root, installation):
    return [sys.executable, "-P", "-m", "oicoach_runtime.omp_probe", mode, str(root),
            installation.path, json.dumps(installation.identity)]


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _ready(daemon, socket):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and daemon.poll() is None:
        try:
            return request(socket, "status", timeout=0.3)["omp"]
        except RuntimeContractError as error:
            if error.code != "daemon_unavailable":
                raise
            time.sleep(0.05)
    raise OmpCandidateRejected("OMP candidate rejected: isolated Runtime did not become ready (including OMP --version)")


def _native(root, installation, environment, socket):
    master, slave = pty.openpty()
    try:
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            process = subprocess.Popen(_argv("native", root, installation), env=environment, cwd=root,
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        finally:
            os.close(slave)
        try:
            _drive_terminal(process, master, socket)
            if process.wait(timeout=5) != 0:
                raise OmpCandidateRejected("OMP candidate rejected: native frontend failed during exit")
            rows = request(socket, "frontend.list", {"workflow_id": WORKFLOW, "task_id": TASK}, timeout=1)
            if len(rows) != 1 or rows[0].get("status") != "released":
                raise OmpCandidateRejected("OMP candidate rejected: Runtime did not release the attested frontend")
        finally:
            _stop(process)
    finally:
        os.close(master)


def _drive_terminal(process, master, socket):
    phase = "native extension registration and Runtime attestation"
    deadline = time.monotonic() + NATIVE_TIMEOUT
    seen_status = False
    tail = b""
    while time.monotonic() < deadline and process.poll() is None:
        visible = b""
        if select.select([master], [], [], 0.05)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break  # Slave descriptors have closed.
                raise
            if not chunk:
                break
            data = tail + chunk
            for _ in range(data.count(CURSOR_QUERY) - tail.count(CURSOR_QUERY)):
                os.write(master, b"\x1b[1;1R")
            visible = CSI.sub(b"", data)
            seen_status |= STATUS_MARKER in visible
            tail = data[-2048:]
        if phase == "native extension registration and Runtime attestation" and seen_status:
            omp = request(socket, "status", timeout=1)["omp"]
            rows = request(socket, "frontend.list", {"workflow_id": WORKFLOW, "task_id": TASK}, timeout=1)
            if omp.get("extension") == "attested" and len(rows) == 1 and rows[0]["status"] == "idle" and rows[0]["session_id"]:
                os.write(master, b"/paveway help")
                phase = "composer accepting the management command"
        elif phase == "composer accepting the management command" and b"/paveway help" in visible:
            # Don't combine typing with Enter. The node UI can attest and
            # paint its widgets before the composer has processed the draft.
            os.write(master, b"\r")
            phase = "registered /paveway help command"
        elif phase == "registered /paveway help command" and HELP_MARKER in visible:
            os.write(master, b"/exit")
            phase = "composer accepting the exit command"
        elif phase == "composer accepting the exit command" and b"/exit" in visible:
            os.write(master, b"\r")
            return
    raise OmpCandidateRejected(f"OMP candidate rejected: isolated probe did not verify {phase}")


def probe_omp(installation):
    """Return the version only after isolation, native ownership, local PTY and RPC checks pass."""
    # A short path avoids sockaddr_un limits; don't inherit a possibly non-private TMPDIR.
    with tempfile.TemporaryDirectory(prefix="pw-omp-", dir="/tmp") as directory:
        root = Path(directory)
        for name in ("tmp", "agent", "runtime", "workspace", "workflows"):
            (root / name).mkdir(mode=0o700)
        environment = _environment(root)
        socket = str(root / "runtime.sock")
        try:
            daemon = subprocess.Popen(_argv("runtime", root, installation), env=environment, cwd=root,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                status = _ready(daemon, socket)
                if (status.get("path"), status.get("file_identity")) != (installation.path, list(installation.identity)):
                    raise OmpCandidateRejected("OMP candidate rejected: isolated Runtime did not select the exact executable")
                try:
                    _native(root, installation, environment, socket)
                except (OSError, RuntimeContractError, subprocess.TimeoutExpired) as error:
                    failure = read_native_startup_failure(root / "state" / NATIVE_FAILURE_FILE)
                    failure = failure or NativeStartupFailure(
                        "probe_native", "OMP candidate native compatibility probe did not complete")
                    message = str(error) if isinstance(error, OmpCandidateRejected) else "OMP candidate rejected: native probe failed"
                    raise OmpCandidateRejected(f"{message}: {failure.detail}", native_failure=failure,
                                               version=status["version"]) from None
                result = subprocess.run(_argv("rpc", root, installation), env=environment, cwd=root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, timeout=NATIVE_TIMEOUT)
                if result.returncode:
                    raise OmpCandidateRejected("OMP candidate rejected: isolated RPC ready, get_state or clean disposal failed")
                return status["version"]
            finally:
                _stop(daemon)
        except OmpCandidateRejected:
            raise
        except (OSError, RuntimeContractError, subprocess.TimeoutExpired):
            raise OmpCandidateRejected("OMP candidate rejected: isolated compatibility probe failed or timed out") from None


def _prepare(root, installation):
    from .config import AgentConfig, Config, ServerConfig, config_text
    from .workflow_setup import create

    config = Config(root / "state", root / "workflows", server=ServerConfig(socket_path=str(root / "runtime.sock")),
        agent=AgentConfig(executable=installation.path, model="paveway-admission/local", thinking="off",
            tools_enabled=True, extensions_enabled=False, skills_enabled=False, rules_enabled=False,
            startup_timeout_seconds=15, ack_timeout_seconds=3, graceful_stop_seconds=1))
    agent = root / "agent"
    (agent / "config.yml").write_text(json.dumps({
        "startup": {"setupWizard": False, "showSplash": False, "checkUpdate": False},
        "update": {"auto": False}, "autoResume": False, "worktree": {"onStart": "off"},
    }))
    (agent / "models.yml").write_text(json.dumps({"providers": {"paveway-admission": {
        "baseUrl": "http://127.0.0.1:1/v1", "auth": "none", "api": "openai-completions",
        "models": [{"id": "local", "name": "Probe only", "reasoning": False, "input": ["text"],
                    "contextWindow": 128000, "maxTokens": 64}],
    }}}))
    (root / "config.toml").write_text(config_text(config))
    create(config, WORKFLOW, root / "workspace", "example")
    return config


async def _rpc(root, config, config_path, installation):
    from .adapters.omp_rpc import OmpRpc
    from .processes import enable_subreaper

    enable_subreaper()
    sessions = root / "rpc"
    sessions.mkdir(mode=0o700)
    config = replace(config, agent=replace(config.agent, tools_enabled=False))
    adapter = OmpRpc(config, config_path=config_path, installation=installation)
    try:
        await adapter.start({"workspace": str(root / "workspace"), "session_dir": str(sessions), "session_file": None,
                             "workflow_id": WORKFLOW, "task_id": TASK, "memory_enabled": False}, "compatibility-probe")
        await adapter.ready()
        state = await adapter.state()
        session_id, session_file = state.get("sessionId"), state.get("sessionFile")
        if (state.get("isSettled") is not True or not isinstance(session_id, str) or not session_id
                or not isinstance(session_file, str) or not Path(session_file).is_absolute()
                or not Path(session_file).resolve().is_relative_to(sessions)):
            raise OmpCandidateRejected("OMP RPC get_state did not describe a settled private session")
    finally:
        await adapter.stop()
    return adapter.process.returncode


def _worker(mode, directory, executable, identity):
    # No public CLI option bypasses admission. This worker is launched only with
    # disposable config/state; it injects the candidate to avoid recursive probes.
    os.umask(0o077)
    root = Path(directory)
    config_path = root / "config.toml"
    installation = OmpInstallation(executable, _parse_identity(identity))
    if mode == "runtime":
        from .scheduler import Runtime
        config = _prepare(root, installation)
        version = omp_version(executable, installation=installation)
        asyncio.run(Runtime(config, config_path, omp_installation=installation, probe_version=version).serve())
        return 0
    from .config import load_config
    config = load_config(config_path)
    if mode == "native":
        from .native import run
        return run(config, config_path, WORKFLOW, TASK, new=True)
    if mode == "rpc":
        return asyncio.run(_rpc(root, config, config_path, installation))
    raise ValueError("Unknown compatibility probe worker")


if __name__ == "__main__":
    raise SystemExit(_worker(*sys.argv[1:]))
