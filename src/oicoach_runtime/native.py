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
from .proxy import child_environment
from .workflows import WorkflowProvider


def run(config, config_path, workflow_id, task_id="training"):
    validate_id(task_id)
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    request(config.server.socket_path, "status")
    launch_id = str(uuid.uuid4())
    root = config.state_dir / "native" / launch_id
    ensure_private_directory(root)
    settings = {"socket_path": config.server.socket_path, "workflow_id": workflow_id, "task_id": task_id}
    private = root / "native.json"
    private.write_text(json.dumps(settings), encoding="utf-8")
    private.chmod(0o600)
    extension = Path(str(files("oicoach_runtime").joinpath("bundled/paveway-native.mjs")))
    digest = hashlib.sha256(extension.read_bytes()).hexdigest()
    nonce = secrets.token_hex(32)
    environment = dict(child_environment(config_path), PAVEWAY_NATIVE_DATA=str(private), PAVEWAY_NATIVE_NONCE=nonce)
    argv = [config.agent.executable, "--cwd", str(workflow.workspace), "--session-dir", str(root),
            "--no-extensions", "--trusted-extension", str(extension)]
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

    return run_terminal(argv, environment, ready=ready, timeout=config.agent.startup_timeout_seconds)
