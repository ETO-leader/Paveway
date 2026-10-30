"""Controlled native OMP entry, with separate physical frontend sessions."""

import asyncio
import hashlib
import json
import os
import secrets
import stat
import sys
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
    if not os.isatty(0) or not os.isatty(1):
        raise RuntimeContractError("OMP native UI requires an interactive terminal")
    from .proxy import synchronize
    synchronize(config_path)
    while True:
        code, action = _run_once(config, config_path, workflow_id, task_id, conversation_id, new)
        if code or not action:
            return code
        if action["kind"] == "profile":
            from .coaches import resolve
            workflow_id = resolve(config, action["coach_id"])["runtime_workflow_id"]
            conversation_id, new = None, False
        elif action["kind"] == "new":
            conversation_id, new = None, True
        elif action["kind"] == "legacy":
            from .tui import run as run_ui
            run_ui(config_path, workflow=workflow_id, task=task_id)
            conversation_id, new = action["conversation_id"], False
        else:
            conversation_id, new = action["conversation_id"], False


def _run_once(config, config_path, workflow_id, task_id, conversation_id, new):
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
        from .prompt_composer import settings_digest
        if settings_digest(workflow.customization) != lease["configuration_digest"] or workflow.revision != lease["workflow_revision"]:
            workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
            if settings_digest(workflow.customization) != lease["configuration_digest"] or workflow.revision != lease["workflow_revision"]:
                raise RuntimeContractError("Profile changed during launch; no prompt was sent")
        code, action = _run_owned(config, config_path, workflow, lease, lock)
        if action and action["kind"] in ("resume", "legacy"):
            action["conversation_id"] = lease["conversation_id"]
        return code, action
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
    nonce = secrets.token_hex(32)
    switch_path = root / ("switch-" + launch_id + ".json")
    settings = {"socket_path": config.server.socket_path, "workflow_id": workflow_id, "task_id": task_id,
                "coach_id": lease["coach_id"],
                "background_task_id": lease["background_task_id"],
                "conversation_id": lease["conversation_id"], "generation": lease["generation"],
                "owner_token": lease["owner_token"], "tools_enabled": config.agent.tools_enabled,
                "memory_enabled": workflow.memory_enabled,
                "cli_prefix": [sys.executable, "-m", "oicoach_runtime"], "config_path": str(config_path),
                "switch_path": str(switch_path)}
    private = root / (launch_id + ".json")
    private.write_text(json.dumps(settings), encoding="utf-8")
    private.chmod(0o600)
    extension = Path(str(files("oicoach_runtime").joinpath("bundled/paveway-native.mjs")))
    digest = hashlib.sha256(extension.read_bytes()).hexdigest()
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
    if not lease["session_file"] or lease["inference_changed"]:
        for option in ("model", "thinking"):
            value = inference.get(option) or getattr(config.agent, option)
            if value:
                argv += ["--" + option, value]
    for option in ("tools", "skills", "rules"):
        if not getattr(config.agent, option + "_enabled"):
            argv += ["--no-" + option]
    memory = request(config.server.socket_path, "memory.recall", {"workflow_id": workflow_id, "task_id": task_id}) if workflow.memory_enabled else None
    instructions = workflow.render_parts("wake", {})[0] if workflow.instructions_in_system else ""
    directories = ()
    if config.agent.skills_enabled and (workflow.builtin_skills or any(skill["enabled"] for skill in workflow.customization["skills"])):
        from .omp_config import skill_directories
        directories = asyncio.run(skill_directories(config, str(workflow.workspace), environment))
    argv += snapshot(workflow.customization, root, launch_id, config.agent.skills_enabled, memory, instructions,
                     builtin_skills=workflow.builtin_skills, skill_directories=directories)
    overlay = root / "native.yml"
    overlay.write_text(json.dumps({"autoResume": False, "startup": {"setupWizard": False}}))
    overlay.chmod(0o600)
    argv += ["--config", str(overlay)]

    def ready(frame, guardian):
        pid = frame.get("pid")
        if (type(frame.get("apiVersion")) is not int or frame.get("apiVersion") != 1 or frame.get("nonce") != nonce or frame.get("extensionDigest") != digest or
                frame.get("contextPolicy") != "omp-native" or type(pid) is not int):
            raise RuntimeContractError("Native OMP integration identity mismatch")
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            raise RuntimeContractError("Native OMP exited before admission") from None
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

    code = run_terminal(argv, environment, ready=ready, started=started,
                        timeout=config.agent.startup_timeout_seconds, lock_fds=(lock.fd,))
    action = None
    if code == 0 and switch_path.exists():
        info = switch_path.lstat()
        if switch_path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise RuntimeContractError("Native switch request is not private")
        action = json.loads(switch_path.read_text())
        switch_path.unlink()
        if action.pop("nonce", None) != nonce or action.get("kind") not in ("new", "profile", "resume", "legacy") or \
                set(action) != ({"kind", "coach_id"} if action.get("kind") == "profile" else {"kind"}):
            raise RuntimeContractError("Native switch request identity mismatch")
        if "coach_id" in action:
            validate_id(action["coach_id"])
    return code, action
