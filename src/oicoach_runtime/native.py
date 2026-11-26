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

from . import __version__
from .config import ensure_private_directory
from .customization import snapshot
from .diagnostics import NativeStartupFailure
from .errors import ConflictError, RuntimeContractError
from .ipc import request
from .models import validate_id
from .omp_installation import OmpInstallation, OmpInstallationUnavailable, resolve_omp
from .native_terminal import run_terminal
from .processes import InstanceLock
from .proxy import child_environment
from .workflows import WorkflowProvider


SWITCH_FIELDS = {
    "new": (), "legacy": (), "resume": (), "profile": ("coach_id",),
    "cwd": ("cwd", "model", "thinking"), "reopen": ("conversation_id",),
}


def recovery_choice(config, workflow_id, task_id, conversation_id, new):
    """Explicit startup recovery; never infer release from a lost heartbeat."""
    if new:
        return conversation_id, new, False
    if conversation_id:
        record = request(config.server.socket_path, "frontend.show", {"conversation_id": conversation_id})
        if (record["workflow_id"], record["task_id"]) != (workflow_id, task_id):
            raise RuntimeContractError("Native conversation belongs to another scope")
    else:
        rows = request(config.server.socket_path, "frontend.list", {"workflow_id": workflow_id, "task_id": task_id})
        if not rows:
            return conversation_id, new, False
        record = rows[0]
    if record["status"] == "released":
        return record["conversation_id"], False, False
    from prompt_toolkit.shortcuts import radiolist_dialog
    choices = []
    if record["status"] == "uncertain":
        choices.append(("recover", "恢复旧对话（不自动重发请求）"))
    choices += [("new", "新建对话（保留旧历史）"), ("cancel", "取消启动")]
    choice = radiolist_dialog(title="GPT · Session recovery", text=(
        "旧会话尚未释放，状态：" + record["status"] + "。\n"
        "请核对上次操作的结果。恢复不会重发输入；服务会检查旧进程已退出、文件锁已释放及历史完整性。\n"
        "仍在运行的会话不会被接管。方向键／空格选择，Tab 切到确认，Enter 确认。"), values=choices).run()
    if choice == "recover":
        request(config.server.socket_path, "frontend.recover", {"conversation_id": record["conversation_id"]})
        return record["conversation_id"], False, False
    if choice == "new":
        return None, True, False
    return conversation_id, new, True


def run(config, config_path, workflow_id, task_id="training", conversation_id=None, new=False):
    if not os.isatty(0) or not os.isatty(1):
        raise RuntimeContractError("OMP native UI requires an interactive terminal")
    from .proxy import synchronize
    # Runtime owns the admitted snapshot. A source path (including --omp)
    # can be replaced or rejected while the selected snapshot remains usable.
    cwd, inference = None, None
    while True:
        omp = request(config.server.socket_path, "status").get("omp", {})
        if omp.get("installation_status") in ("unavailable", "handoff_required"):
            raise OmpInstallationUnavailable()
        if not omp.get("source_path"):
            executable = resolve_omp(config.agent.executable)
            if omp.get("path") != executable:
                raise RuntimeContractError(
                    "Foreground OMP does not match the Runtime installation. "
                    "Set agent.executable consistently and safely restart Runtime.")
        if omp.get("installation_status") != "selected":
            raise OmpInstallationUnavailable()
        installation = OmpInstallation(omp["path"], tuple(omp["file_identity"]))
        installation.check()
        conversation_id, new, cancelled = recovery_choice(config, workflow_id, task_id, conversation_id, new)
        if cancelled:
            return 0
        synchronize(config_path)
        code, action = _run_once(config, config_path, workflow_id, task_id, conversation_id, new, cwd, inference,
                                 installation=installation, omp_version=omp.get("version"))
        if code or not action:
            return code
        cwd, inference = None, None
        if action["kind"] == "profile":
            from .coaches import resolve
            workflow_id = resolve(config, action["coach_id"])["runtime_workflow_id"]
            conversation_id, new = None, False
        elif action["kind"] in ("cwd", "new"):
            conversation_id, new = None, True
            cwd = action["cwd"]
            if action["kind"] == "cwd":
                inference = {key: action[key] for key in ("model", "thinking")}
        elif action["kind"] == "legacy":
            from .tui import run as run_ui
            run_ui(config_path, workflow=workflow_id, task=task_id)
            conversation_id, new = action["conversation_id"], False
        else:
            conversation_id, new = action["conversation_id"], False


def _run_once(config, config_path, workflow_id, task_id, conversation_id, new, cwd=None, inference=None, *,
              installation, omp_version=None):
    validate_id(task_id)
    workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
    request(config.server.socket_path, "status")
    from .frontend import session_directory
    if new:
        conversation_id = None
    elif conversation_id is None:
        rows = request(config.server.socket_path, "frontend.list", {"workflow_id": workflow_id, "task_id": task_id})
        conversation_id = rows[0]["conversation_id"] if rows else None
    resuming = conversation_id is not None
    conversation_id = conversation_id or str(uuid.uuid4())
    validate_id(conversation_id)
    root = session_directory(config, workflow_id, task_id, conversation_id)
    ensure_private_directory(root)
    try:
        lock = InstanceLock(root / "owner.lock")
    except RuntimeContractError:
        failure = ConflictError("Native owner.lock is unavailable; no owner generation was created. Check the previous process and lock holder; do not delete the lock file.")
        failure.code = "native_owner_lock_unavailable"
        raise failure from None
    # Admission is serialized by the actual session lock, not by a compensating
    # finish(None) after a failed acquisition. Begin still provides token/CAS fencing.
    try:
        target_cwd = cwd
        if target_cwd is None and resuming:
            target_cwd = request(config.server.socket_path, "frontend.show", {"conversation_id": conversation_id})["cwd"]
        if target_cwd is not None and Path(target_cwd) != workflow.workspace:
            from .workflow_setup import show_tijian
            if show_tijian(config, workflow_id)["configured"]:
                raise RuntimeContractError(
                    "Official OMP cannot safely inherit managed Tijian MCP outside the Workflow Workspace. "
                    "Use a conversation in the Workflow Workspace; no MCP configuration was changed.")
        target = {"cwd": cwd} if cwd is not None else {}
        lease = request(config.server.socket_path, "frontend.begin", {"workflow_id": workflow_id, "task_id": task_id,
                        "conversation_id": conversation_id, "omp_executable": installation.path,
                        "omp_file_identity": list(installation.identity), **target})
    except BaseException:
        lock.close()
        raise
    code = None
    primary_failed = False
    try:
        from .prompt_composer import settings_digest
        if settings_digest(workflow.customization) != lease["configuration_digest"] or workflow.revision != lease["workflow_revision"]:
            workflow = WorkflowProvider(config.workflow_root).describe(workflow_id)
            if settings_digest(workflow.customization) != lease["configuration_digest"] or workflow.revision != lease["workflow_revision"]:
                raise RuntimeContractError("Profile changed during launch; no prompt was sent")
        code, switch_path, nonce = _run_owned(config, config_path, workflow, lease, lock, installation, inference, omp_version)
        action = _read_switch(switch_path, nonce, workflow) if code == 0 and switch_path.exists() else None
        if action and action["kind"] in ("resume", "legacy"):
            action["conversation_id"] = lease["conversation_id"]
        if action and action["kind"] == "new":
            action["cwd"] = lease["cwd"] if lease["cwd"] != lease["workspace"] else None
        return code, action
    except BaseException:
        primary_failed = True
        raise
    finally:
        if lock:
            lock.close()
        try:
            finished = request(config.server.socket_path, "frontend.finish", {
                "conversation_id": lease["conversation_id"], "generation": lease["generation"],
                "owner_token": lease["owner_token"], "exit_code": code})
            if code == 0 and finished["status"] != "released":
                error = RuntimeContractError("Native session release safety checks failed")
                error.code = finished.get("release_diagnostic", "native_session_validation_failed")
                raise error
        except RuntimeContractError as error:
            # Never print remote payloads or replace the original process failure.
            category = error.code if error.code.startswith("native_") or error.code in (
                "daemon_unavailable", "conflict", "internal_error", "native_archive_failed"
            ) else "runtime_error"
            outcome = f"OMP exit_code={code}" if code is not None else "OMP exit_code=unknown"
            failure = RuntimeContractError(
                f"Native ownership release not confirmed (release_diagnostic={category}; {outcome}). "
                "Start Runtime with the same config, inspect frontend show/list, and only use "
                "frontend recover for an uncertain conversation after checking old processes "
                "and side effects; --new preserves the old history. No automatic retry.")
            failure.code = "native_finish_failed"
            if not primary_failed and code == 0:
                raise failure from None
            print(f"{failure.code}: {failure}", file=sys.stderr)


def _run_owned(config, config_path, workflow, lease, lock, installation, inference, omp_version):
    workflow_id, task_id = lease["workflow_id"], lease["task_id"]
    root = Path(lease["session_dir"])
    cwd = lease["cwd"]
    launch_id = str(uuid.uuid4())
    nonce = secrets.token_hex(32)
    switch_path = root / ("switch-" + launch_id + ".json")
    settings = {"socket_path": config.server.socket_path, "workflow_id": workflow_id, "task_id": task_id,
                "coach_id": lease["coach_id"],
                "workflow_source": workflow.customization.get("coach", {}).get("workflow_source", workflow_id),
                "background_task_id": lease["background_task_id"],
                "conversation_id": lease["conversation_id"], "generation": lease["generation"],
                "cwd": cwd, "workspace": lease["workspace"], "session_dir": str(root),
                "materialize": inference is not None,
                "owner_token": lease["owner_token"], "tools_enabled": config.agent.tools_enabled,
                "memory_enabled": workflow.memory_enabled,
                "paveway_version": __version__,
                "omp_version": omp_version,
                "cli_prefix": [sys.executable, "-m", "oicoach_runtime"], "config_path": str(config_path),
                "switch_path": str(switch_path)}
    private = root / (launch_id + ".json")
    private.write_text(json.dumps(settings), encoding="utf-8")
    private.chmod(0o600)
    extension = Path(str(files("oicoach_runtime").joinpath("bundled/paveway-native.mjs")))
    digest = hashlib.sha256(extension.read_bytes()).hexdigest()
    environment = dict(child_environment(config_path), PAVEWAY_NATIVE_DATA=str(private), PAVEWAY_NATIVE_NONCE=nonce)
    installation.export(environment)
    argv = ["--cwd", cwd, "--session-dir", str(root),
            "--no-extensions", "--trusted-extension", str(extension)]
    if lease["session_file"]:
        from .git_sync import validate_session_header
        validate_session_header(lease, root)
        argv += ["--resume", lease["session_file"]]
    if config.agent.profile:
        argv += ["--profile", config.agent.profile]
    preference = workflow.customization.get("inference", {})
    if not lease["session_file"] or lease["inference_changed"]:
        for option in ("model", "thinking"):
            value = (inference or {}).get(option) or preference.get(option) or getattr(config.agent, option)
            if value:
                argv += ["--" + option, value]
    for option in ("skills", "rules"):
        if not getattr(config.agent, option + "_enabled"):
            argv += ["--no-" + option]
    directories = ()
    if config.agent.skills_enabled and (workflow.builtin_skills or any(skill["enabled"] for skill in workflow.customization["skills"])):
        from .omp_config import skill_directories
        directories = asyncio.run(skill_directories(config, installation, cwd, environment))
    argv += snapshot(workflow.customization, root, launch_id, config.agent.skills_enabled,
                     base=workflow.base, builtin_skills=workflow.builtin_skills, skill_directories=directories,
                     package_root=config.state_dir / "skill-packages")
    overlay = {
        "autoResume": False,
        "startup": {"setupWizard": False, "showSplash": False, "checkUpdate": False},
        "update": {"auto": False},  # Only the installation gate may replace the selected binary.
        "worktree": {"onStart": "off"},
        "composer": {"shape": "paveway.chrome"},
    }
    if not config.agent.tools_enabled:
        from .omp_config import disabled_tool_args
        argv += disabled_tool_args(overlay, environment)
    overlay_path = root / "native.yml"
    overlay_path.write_text(json.dumps(overlay))
    overlay_path.chmod(0o600)
    argv += ["--config", str(overlay_path)]

    admitted = False
    expected_tools_disabled = not config.agent.tools_enabled
    pending_pid = None

    def ready(frame, guardian):
        nonlocal admitted, pending_pid
        if frame.get("nonce") == nonce and "failure" in frame:
            raise NativeStartupFailure.from_extension(frame)
        pid = frame.get("pid")
        if (type(frame.get("apiVersion")) is not int or frame.get("apiVersion") != 1 or frame.get("nonce") != nonce or
                type(pid) is not int or pid < 2):
            raise NativeStartupFailure("attestation", "Native OMP integration identity mismatch")
        if frame.get("ready") is True and pending_pid == pid:
            admitted, pending_pid = True, None
            return True
        if (pending_pid is not None or "ready" in frame or frame.get("extensionDigest") != digest or
                frame.get("contextPolicy") != "omp-native" or frame.get("toolsDisabled") is not expected_tools_disabled):
            raise NativeStartupFailure("attestation", "Native OMP integration identity mismatch")
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            raise NativeStartupFailure("attestation", "Native OMP exited before admission") from None
        if len(stat) < 2 or stat[1] != str(guardian.pid):
            raise NativeStartupFailure("attestation", "Native OMP is not the owned guardian child")
        try:
            matches_cwd = Path(f"/proc/{pid}/cwd").samefile(cwd)
        except OSError:
            matches_cwd = False
        if not matches_cwd:
            raise NativeStartupFailure("attestation", "Native OMP process cwd does not match its conversation")
        path, session_id = frame.get("sessionFile"), frame.get("sessionId")
        try:
            contained = isinstance(path, str) and Path(path).is_absolute() and Path(path).resolve().is_relative_to(root.resolve())
        except (OSError, UnicodeError, ValueError, RuntimeError):
            contained = False
        if not contained or not isinstance(session_id, str) or not session_id:
            raise NativeStartupFailure("attestation", "Native OMP session escaped its dedicated directory")
        if not admitted and lease["session_file"] and (path, session_id) != (lease["session_file"], lease["session_id"]):
            raise NativeStartupFailure("attestation", "Native OMP did not open the requested physical session")
        try:
            observed = request(config.server.socket_path, "frontend.attest", {**owner(), "omp_pid": pid,
                "session_id": session_id, "session_file": path})
        except RuntimeContractError:
            raise NativeStartupFailure("runtime_attest", "Runtime refused Native Extension attestation") from None
        # Keep the final owner identity from our own authenticated attestation,
        # including OMP /new switches; finish needs no extra frontend.show round trip.
        lease["generation"] = settings["generation"] = observed["generation"]
        pending_pid = pid
        return False

    def owner():
        return {key: settings[key] for key in ("conversation_id", "generation", "owner_token")}

    def started(process):
        try:
            request(config.server.socket_path, "frontend.started", {**owner(), "guardian_pid": process.pid})
        except RuntimeContractError:
            raise NativeStartupFailure("runtime_started", "Runtime did not register the Native guardian") from None

    try:
        code = run_terminal(installation.command(argv), environment, ready=ready, started=started,
                            timeout=config.agent.startup_timeout_seconds, lock_fds=(lock.fd,))
    except (NativeStartupFailure, OSError, OmpInstallationUnavailable) as error:
        failure = error if isinstance(error, NativeStartupFailure) else NativeStartupFailure(
            "guardian", "Cannot launch or communicate with the owned OMP process", exception_type=type(error).__name__)
        try:
            failure.persist(config.state_dir, omp_version)
        except OSError:
            print("Cannot persist the Native startup diagnostic; inspect state directory permissions and storage.", file=sys.stderr)
        raise failure from None
    return code, switch_path, nonce


def _read_switch(switch_path, nonce, workflow):
    info = switch_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
        raise RuntimeContractError("Native switch request is not private")
    action = json.loads(switch_path.read_text())
    switch_path.unlink()
    if not isinstance(action, dict) or action.pop("nonce", None) != nonce:
        raise RuntimeContractError("Native switch request identity mismatch")
    kind = action.get("kind")
    if not isinstance(kind, str) or kind not in SWITCH_FIELDS or set(action) != {"kind", *SWITCH_FIELDS[kind]}:
        raise RuntimeContractError("Native switch request identity mismatch")
    if kind in ("profile", "reopen"):
        validate_id(action[SWITCH_FIELDS[kind][0]])
    if kind == "cwd":
        from .frontend import check_directory
        from .customization import validate
        check_directory(action["cwd"], canonical=True)
        validate({**workflow.customization, "inference": {key: action[key] for key in ("model", "thinking")}})
    return action
