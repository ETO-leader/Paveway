"""Controlled native OMP entry, with separate physical frontend sessions."""

import hashlib
import json
import os
import secrets
import uuid
from importlib.resources import files
from pathlib import Path

from .config import ensure_private_directory
from .customization import snapshot
from .errors import RuntimeContractError
from .ipc import request
from .models import validate_id
from .native_terminal import run_terminal
from .processes import InstanceLock
from .proxy import child_environment
from .workflows import WorkflowProvider


def run(config, config_path, workflow_id, task_id="training", conversation_id=None, new=False):
    validate_id(task_id)
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    request(config.server.socket_path, "status")
    lease = request(config.server.socket_path, "frontend.begin", {"workflow_id": workflow_id, "task_id": task_id,
                    "conversation_id": conversation_id, "new": new})
    root = Path(lease["session_dir"])
    lock = None
    code = None
    try:
        ensure_private_directory(root)
        lock = InstanceLock(root / "owner.lock")
        code = _run_owned(config, config_path, workflow, lease, lock)
        return code
    finally:
        if lock:
            lock.close()
        try:
            current = request(config.server.socket_path, "frontend.show", {"conversation_id": lease["conversation_id"]})
            request(config.server.socket_path, "frontend.finish", {"conversation_id": lease["conversation_id"],
                "generation": current["generation"], "owner_token": lease["owner_token"], "exit_code": code})
        except RuntimeContractError:
            pass  # An outage or stale owner is never treated as completion.


def _run_owned(config, config_path, workflow, lease, lock):
    workflow_id, task_id = lease["workflow_id"], lease["task_id"]
    root = Path(lease["session_dir"])
    launch_id = str(uuid.uuid4())
    settings = {"socket_path": config.server.socket_path, "workflow_id": workflow_id, "task_id": task_id,
                "conversation_id": lease["conversation_id"], "generation": lease["generation"],
                "owner_token": lease["owner_token"], "tools_enabled": config.agent.tools_enabled}
    private = root / (launch_id + ".json")
    private.write_text(json.dumps(settings), encoding="utf-8")
    private.chmod(0o600)
    extension = Path(str(files("oicoach_runtime").joinpath("bundled/paveway-native.mjs")))
    digest = hashlib.sha256(extension.read_bytes()).hexdigest()
    nonce = secrets.token_hex(32)
    environment = dict(child_environment(config_path), PAVEWAY_NATIVE_DATA=str(private), PAVEWAY_NATIVE_NONCE=nonce)
    argv = [config.agent.executable, "--cwd", str(workflow.workspace), "--session-dir", str(root),
            "--no-extensions", "--trusted-extension", str(extension)]
    if lease["session_file"]:
        path = Path(lease["session_file"])
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeContractError("Native session is missing or unsafe; use --new, preserving the old conversation")
        argv += ["--resume", str(path)]
    if config.agent.profile:
        argv += ["--profile", config.agent.profile]
    inference = workflow.customization.get("inference", {})
    for option in ("model", "thinking"):
        value = inference.get(option) or getattr(config.agent, option)
        if value:
            argv += ["--" + option, value]
    for option in ("tools", "skills", "rules"):
        if not getattr(config.agent, option + "_enabled"):
            argv += ["--no-" + option]
    memory = request(config.server.socket_path, "memory.recall", {"workflow_id": workflow_id, "task_id": task_id}) if workflow.memory_enabled else None
    instructions = workflow.render_parts("wake", {})[0] if workflow.instructions_in_system else ""
    prompt_args = snapshot(workflow.customization, root, launch_id, config.agent.skills_enabled, memory, instructions,
                           builtin_skills=workflow.builtin_skills)
    argv += ["--system-prompt" if item == "--append-system-prompt" else item for item in prompt_args]
    overlay = root / "native.yml"
    overlay.write_text(json.dumps({"autoResume": False, "startup": {"setupWizard": False}}))
    overlay.chmod(0o600)
    argv += ["--config", str(overlay)]

    def ready(frame, guardian):
        pid = frame.get("pid")
        if (frame.get("apiVersion") != 1 or frame.get("nonce") != nonce or frame.get("extensionDigest") != digest or
                frame.get("contextPolicy") != "omp-native" or type(pid) is not int):
            raise RuntimeContractError("Native OMP integration identity mismatch")
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if int(stat[1]) != guardian.pid:
            raise RuntimeContractError("Native OMP is not the owned guardian child")
        session_file = Path(frame.get("sessionFile") or "")
        if not session_file.is_absolute() or not session_file.resolve().is_relative_to(root.resolve()) or not frame.get("sessionId"):
            raise RuntimeContractError("Native OMP session escaped its dedicated directory")
        observed = request(config.server.socket_path, "frontend.attest", {**owner(), "omp_pid": pid,
            "session_id": frame["sessionId"], "session_file": str(session_file)})
        settings["generation"] = observed["generation"]

    def owner():
        return {key: settings[key] for key in ("conversation_id", "generation", "owner_token")}

    def started(process):
        request(config.server.socket_path, "frontend.started", {**owner(), "guardian_pid": process.pid})

    return run_terminal(argv, environment, ready=ready, started=started,
                        timeout=config.agent.startup_timeout_seconds, lock_fds=(lock.fd,))
