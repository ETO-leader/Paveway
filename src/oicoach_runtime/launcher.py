"""Launcher-side Runtime lifecycle: reuse, safe handoff restart, owned shutdown.

Ownership is never inferred from a PID file alone. A launcher writes a private
record only after it started an independent daemon and observed the daemon's
instance ID, process identity, build fingerprint and socket through IPC.
"""

import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from . import service
from .compatibility import (
    EXPECTED_LIFECYCLE_PROTOCOL_VERSION,
    compatibility_problem,
    probe_daemon,
    restart_guidance,
)
from .config import ensure_private_directory
from .errors import ConflictError, RuntimeContractError
from .ipc import request
from .omp_installation import OMP_IDENTITY_ENV, OMP_PATH_ENV, OmpInstallation, load_active_omp, prepare_omp
from .processes import InstanceLock, process_identity

STARTUP_TIMEOUT = 20
SHUTDOWN_TIMEOUT = 30
LAUNCHER_LOCK_TIMEOUT = 90
OWNERSHIP_VERSION = 1
# The daemon outlives this process; keeping the Popen handle prevents a
# ResourceWarning on garbage collection without ever waiting on it.
_DETACHED_RUNTIMES = []
LIFECYCLE_TARGET_FIELDS = (
    "runtime_instance", "runtime_build_id", "runtime_version",
    "native_protocol_version", "runtime_pid", "runtime_identity",
)


@dataclass(frozen=True)
class RuntimeHandle:
    reused: bool
    managed: bool
    created: bool
    launch_token: str | None
    status: dict
    service_proxy: dict | None = None


def reap_detached_runtimes(timeout=0):
    """Reap daemon handles that have already exited.

    The daemon normally outlives the launcher process, so handles stay alive
    without being waited on. Once lifecycle shutdown has stopped a daemon, its
    Popen must be polled so an exiting test process does not warn about it.
    The optional timeout waits for a just-signalled daemon to finish exiting.
    """
    deadline = time.monotonic() + timeout
    while True:
        _DETACHED_RUNTIMES[:] = [handle for handle in _DETACHED_RUNTIMES if handle.poll() is None]
        if not _DETACHED_RUNTIMES or time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def ownership_path(config):
    return config.state_dir / "launcher" / "owned-runtime.json"


def read_ownership(config):
    path = ownership_path(config)
    try:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() \
                or info.st_mode & 0o077 or info.st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("api_version") == OWNERSHIP_VERSION else None
    except (OSError, ValueError, UnicodeError):
        return None


def write_ownership(config, config_path, status, token):
    path = ownership_path(config)
    ensure_private_directory(path.parent)
    record = {
        "api_version": OWNERSHIP_VERSION,
        "token": token,
        "config": str(Path(config_path).absolute()),
        "socket": config.server.socket_path,
        "runtime_instance": status.get("runtime_instance"),
        "runtime_build_id": status.get("runtime_build_id"),
        "runtime_version": status.get("runtime_version"),
        "native_protocol_version": status.get("native_protocol_version"),
        "daemon_pid": status.get("runtime_pid"),
        "daemon_identity": status.get("runtime_identity"),
    }
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(record, ensure_ascii=False))
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def clear_ownership(config, token=None):
    record = read_ownership(config)
    if record is None or (token is not None and record.get("token") != token):
        return
    try:
        ownership_path(config).unlink()
    except FileNotFoundError:
        pass


def _lock_free(path):
    try:
        lock = InstanceLock(path)
    except ConflictError:
        return False
    lock.close()
    return True


def _process_running(pid, identity):
    if not pid or not identity or process_identity(pid) != identity:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def _lifecycle_target(status):
    return {key: status.get(key) for key in LIFECYCLE_TARGET_FIELDS}


def _supports_handoff(status):
    return (status.get("lifecycle_protocol_version") == EXPECTED_LIFECYCLE_PROTOCOL_VERSION
            and all(status.get(key) is not None for key in LIFECYCLE_TARGET_FIELDS))


def _blocker_report(result):
    lines = ["无法安全重启 Runtime：仍有活动工作，旧实例没有被停止。"]
    for item in result.get("blockers", []):
        if item.get("kind") == "frontend":
            lines.append(
                f"- 前台会话 {item.get('conversation_id')}（{item.get('workflow_id')}/{item.get('task_id')}，"
                f"状态 {item.get('status')}）仍在运行。")
        elif item.get("kind") == "agent_restart":
            lines.append("- 后台 agent restart 探测仍在进行。")
        else:
            intent = "已发送模型请求" if item.get("dispatch_intent_at") else "尚未发送模型请求"
            lines.append(
                f"- 后台运行 {item.get('run_id')}（{item.get('workflow_id')}/{item.get('task_id')}，"
                f"状态 {item.get('status')}，{intent}）。")
    handling = [item.get("safe_handling") for item in result.get("blockers", []) if item.get("safe_handling")]
    if handling:
        lines.append("安全处理方式：")
        lines.extend("- " + text for text in dict.fromkeys(handling))
    lines.append("本次未停止 Runtime、未启动第二个实例。")
    return "\n".join(lines)


def _request_handoff(status, socket_path, *, stop, reason):
    payload = {**_lifecycle_target(status), "lifecycle_protocol_version": EXPECTED_LIFECYCLE_PROTOCOL_VERSION,
               "reason": reason}
    command = "lifecycle.shutdown" if stop else "lifecycle.drain"
    return request(socket_path, command, payload, timeout=SHUTDOWN_TIMEOUT + 10)


def _request_resume(status, socket_path):
    return request(socket_path, "lifecycle.resume", {
        **_lifecycle_target(status),
        "lifecycle_protocol_version": EXPECTED_LIFECYCLE_PROTOCOL_VERSION,
        "reason": "launcher resume"}, timeout=5)


def _wait_ready(config, *, old_instance=None, timeout=STARTUP_TIMEOUT, pid=None, identity=None):
    deadline = time.monotonic() + timeout
    while True:
        try:
            status = probe_daemon(config.server.socket_path, timeout=1)
        except RuntimeContractError as error:
            if error.code != "daemon_unavailable":
                raise
            status = None
        if status is not None and (old_instance is None or status.get("runtime_instance") != old_instance):
            return status
        if pid is not None and not _process_running(pid, identity) and status is None:
            raise RuntimeContractError("Runtime 启动进程已退出；请检查 startup.log 后重试。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeContractError("Runtime 启动未就绪；请使用 doctor 或 service logs 检查，不会自动重发请求。")
        time.sleep(min(0.1, remaining))


def _wait_released(config, expected, timeout=SHUTDOWN_TIMEOUT):
    socket_path = config.server.socket_path
    socket_lock = socket_path + ".lock"
    pid, identity = expected.get("runtime_pid"), expected.get("runtime_identity")
    deadline = time.monotonic() + timeout
    while True:
        identity_gone = not _process_running(pid, identity)
        locks_free = _lock_free(config.state_dir / "runtime.lock") and _lock_free(socket_lock)
        try:
            status = probe_daemon(socket_path, timeout=1)
        except RuntimeContractError as error:
            if error.code != "daemon_unavailable":
                raise
            status = None
        if status is not None and status.get("runtime_instance") != expected.get("runtime_instance"):
            raise RuntimeContractError(
                "另一个 Runtime 实例已在旧实例退出前启动；已停止自动操作，不会启动竞争实例。")
        if identity_gone and locks_free and status is None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeContractError(
                "旧 Runtime 未在限定时间内退出并释放锁；已停止自动操作，不会启动第二个实例。"
                "请检查 startup.log 或 systemd 日志后重试。")
        time.sleep(min(0.1, remaining))

def _restore_failed_activation(config, activation):
    """Restore the prior pointer only while excluding every Runtime startup."""
    if not activation.changed:
        return ""
    try:
        ensure_private_directory(Path(config.server.socket_path).parent)
        with ExitStack() as locks:
            for path in (config.state_dir / "runtime.lock", config.server.socket_path + ".lock"):
                locks.callback(InstanceLock(path).close)
            if probe_daemon(config.server.socket_path, timeout=1) is None:
                activation.rollback()
                return "已恢复上次 OMP 活动引用。"
    except (OSError, RuntimeContractError):
        pass
    return "无法确认 Runtime 已释放；保留已验证的候选引用，请先检查状态，未自动启动第二个实例。"


def _start_standalone(config, config_path):
    ensure_private_directory(config.state_dir)
    log_path = config.state_dir / "startup.log"
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        info = os.fstat(log_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeContractError("后台启动日志必须是当前用户拥有的私密普通文件。")
        argv = [sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(config_path)]
        environment = {**os.environ, **service.read_environment(config_path)}
        for key in ("PAVEWAY_SHUTDOWN_ON_EXIT", "PAVEWAY_LAUNCH_TOKEN", "PAVEWAY_RESTART_RUNTIME", OMP_IDENTITY_ENV):
            environment.pop(key, None)
        # Popen with close_fds keeps Python 3.11 working (POSIX_SPAWN_CLOSEFROM
        # only exists from 3.12) and closes every inherited descriptor except the
        # three explicitly redirected ones, so Runtime/socket/credential locks
        # never leak into the daemon.
        process = subprocess.Popen(
            argv, stdin=null_fd, stdout=log_fd, stderr=log_fd,
            close_fds=True, start_new_session=True, env=environment)
        reap_detached_runtimes()
        _DETACHED_RUNTIMES.append(process)
        pid = process.pid
        (config.state_dir / "daemon.pid").write_text(str(pid), encoding="utf-8")
    finally:
        os.close(null_fd)
        os.close(log_fd)
    print("正在启动后台 Runtime…", flush=True)
    return _wait_ready(config, pid=pid, identity=process_identity(pid))


def _start_managed(config, config_path, name):
    print("正在启动已有的 systemd 用户服务…", flush=True)
    result = service.control(config_path, "start", name=name)
    return _wait_ready(config), result["proxy"]


def _new_handle(status, *, reused, managed, created, service_proxy=None):
    token = secrets.token_hex(32) if created else None
    return RuntimeHandle(reused=reused, managed=managed, created=created, launch_token=token, status=status,
                         service_proxy=service_proxy)


def _problem_text(problem):
    return {
        "native_protocol_version": "原生协议版本不匹配",
        "runtime_version": "运行时代码版本不一致",
        "runtime_build_id_missing": "旧 Runtime 缺少构建指纹",
        "runtime_build_id": "代码构建指纹不一致",
        "omp_executable": "OMP 安装已变更",
    }.get(problem, "兼容性检查未通过")


def ensure_runtime(config, config_path, *, restart=False, omp_path=None, service_name=service.DEFAULT_NAME,
                   managed_only=False):
    """Reuse Runtime or attest the next installation before attempting handoff."""
    ensure_private_directory(config.state_dir)
    try:
        launcher_lock = InstanceLock(config.state_dir / "launcher.lock", timeout=LAUNCHER_LOCK_TIMEOUT)
    except ConflictError:
        raise RuntimeContractError(
            "另一个 Paveway 启动器正在检查或重启 Runtime；请等待其完成后重试，不会启动第二个实例。") from None

    with ExitStack() as stack:
        stack.callback(launcher_lock.close)
        manager = service.status(config_path, name=service_name)
        managed = manager.get("managed", False)
        if managed_only and not managed:
            raise RuntimeContractError("未安装该配置的 systemd 用户服务；不会启动独立 Runtime。")
        saved_environment = service.read_environment(config_path)
        environment = {**os.environ, **saved_environment}
        explicit = omp_path
        if explicit is None and OMP_IDENTITY_ENV not in os.environ:
            explicit = os.environ.get(OMP_PATH_ENV)
        if explicit is not None:
            environment[OMP_PATH_ENV] = explicit
        elif OMP_IDENTITY_ENV in os.environ and OMP_PATH_ENV not in saved_environment:
            environment.pop(OMP_PATH_ENV, None)
        source = OmpInstallation.select(config.agent.executable, environment=environment)
        active = load_active_omp(config.state_dir)
        status = probe_daemon(config.server.socket_path)
        def require_managed_owner(current, unit):
            if str(current.get("runtime_pid")) != unit.get("MainPID"):
                raise RuntimeContractError("systemd 服务未拥有当前 Runtime；未执行交接，也不会启动第二个实例。")

        if managed and status is not None:
            require_managed_owner(status, manager)

        def check(current, admission):
            problem = compatibility_problem(current)
            if problem:
                return problem
            if admission is None or admission.source != source:
                return "omp_executable"
            observed = current.get("omp", {})
            expected = admission.status()
            if any(observed.get(field) != expected[field] for field in (
                    "path", "file_identity", "source_path", "source_file_identity", "installation_status", "version")):
                return "omp_executable"
            return None

        problem = check(status, active) if status is not None else None
        if status and not status.get("stopping") and problem is None and not restart:
            if status.get("draining"):
                try:
                    _request_resume(status, config.server.socket_path)
                except RuntimeContractError:
                    raise RuntimeContractError(
                        "Runtime 处于 draining 且无法恢复；请检查 daemon 日志并等待其退出后重试，"
                        "不会启动第二个实例。") from None
                status = probe_daemon(config.server.socket_path)
                if status is None or status.get("draining") or check(status, active) is not None:
                    raise RuntimeContractError(
                        "Runtime 未能恢复；请等待其安全收尾完成后重试，不会启动第二个实例。")
            return _new_handle(status, reused=True, managed=managed, created=False)

        if status and not _supports_handoff(status):
            raise RuntimeContractError(
                "运行中的 Runtime 是升级前的旧实例，不支持空闲检查与安全交接协议，无法证明可以安全停止。\n"
                "未发送终止信号，也未启动第二个实例。\n\n" + restart_guidance(config_path))
        if managed and status and status.get("stopping"):
            raise RuntimeContractError(
                "systemd 托管的 Runtime 正在安全退出；请等待其完成并用 service status 检查后重试，不会启动第二个实例。")

        # No production lifecycle request, environment synchronization, or
        # active-reference change is allowed before this isolated probe passes.
        activation = stack.enter_context(prepare_omp(config.state_dir, source, recheck_wrapper=restart))
        if status is not None:
            if status.get("stopping"):
                print("检测到旧 Runtime 正在安全退出；等待其释放后启动…", flush=True)
                _wait_released(config, status)
            else:
                print(f"检测到{_problem_text(problem)}；正在检查活动状态并安全重启…" if problem
                      else "正在安全重启 Runtime…", flush=True)
                handoff = _request_handoff(status, config.server.socket_path, stop=not managed, reason="launcher restart")
                if not handoff.get("accepted"):
                    raise RuntimeContractError(_blocker_report(handoff))
                if not managed:
                    _wait_released(config, status)

        service_proxy = None
        try:
            activation.commit()
            if managed and status is not None:
                result = service.control(config_path, "restart", name=service_name)
                service_proxy = result["proxy"]
                new_status = _wait_ready(config, old_instance=status.get("runtime_instance"))
            elif managed:
                new_status, service_proxy = _start_managed(config, config_path, service_name)
            else:
                new_status = _start_standalone(config, config_path)
            if managed:
                require_managed_owner(new_status, service.status(config_path, name=service_name))
            problem = check(new_status, activation.admission)
            if problem:
                raise RuntimeContractError(
                    f"Runtime 已启动，但{_problem_text(problem)}；未进入界面。\n\n{restart_guidance(config_path)}")
        except (OSError, RuntimeContractError) as error:
            if managed and status is not None:
                try:
                    _request_resume(status, config.server.socket_path)
                except RuntimeContractError:
                    pass
            recovery = _restore_failed_activation(config, activation)
            if recovery:
                message = str(error) if isinstance(error, RuntimeContractError) else "Runtime 启动失败；请检查文件权限。"
                raise RuntimeContractError(f"{message}\n{recovery}") from None
            raise
        activation.retire_previous_snapshot()
        handle = _new_handle(new_status, reused=False, managed=managed, created=not managed, service_proxy=service_proxy)
        if handle.created:
            write_ownership(config, config_path, new_status, handle.launch_token)
        if status is not None:
            print("Runtime 已使用当前代码重启。", flush=True)
        return handle


def shutdown_owned_runtime(config, config_path):
    """Best-effort safe shutdown for an independent Runtime created by this launch.

    Called after the foreground UI exits normally. Any doubt leaves the Runtime
    running and reports why; this never kills by PID or removes lock files.
    """
    if os.environ.get("PAVEWAY_SHUTDOWN_ON_EXIT") != "1":
        return
    token = os.environ.get("PAVEWAY_LAUNCH_TOKEN")
    if not token:
        print("--shutdown-on-exit：Runtime 不是由本次启动创建（或属于共享/托管实例），已保留。", flush=True)
        return
    try:
        launcher_lock = InstanceLock(config.state_dir / "launcher.lock", timeout=10)
    except ConflictError:
        print("--shutdown-on-exit：另一个启动器正在检查或重启 Runtime，已保留。", flush=True)
        return
    try:
        _shutdown_owned_runtime_locked(config, config_path, token)
    finally:
        launcher_lock.close()


def _shutdown_owned_runtime_locked(config, config_path, token):
    record = read_ownership(config)
    if record is None or record.get("token") != token:
        print("--shutdown-on-exit：找不到本次启动的所有权记录，已保留 Runtime。", flush=True)
        return
    if record.get("config") != str(Path(config_path).absolute()) or record.get("socket") != config.server.socket_path:
        print("--shutdown-on-exit：所有权记录与当前配置不匹配，已保留 Runtime。", flush=True)
        return
    if service.status(config_path).get("managed"):
        print("--shutdown-on-exit：Runtime 由 systemd 托管，不由本次启动器拥有，已保留。", flush=True)
        return
    status = probe_daemon(config.server.socket_path)
    if status is None:
        clear_ownership(config, token)
        print("--shutdown-on-exit：Runtime 已退出，清理所有权记录。", flush=True)
        return
    if status.get("runtime_instance") != record.get("runtime_instance") \
            or not _process_running(record.get("daemon_pid"), record.get("daemon_identity")):
        print("--shutdown-on-exit：当前 Runtime 进程身份已变化，已保留。", flush=True)
        return
    try:
        result = _request_handoff(status, config.server.socket_path, stop=True, reason="shutdown on exit")
    except RuntimeContractError as error:
        print(f"--shutdown-on-exit：无法安全关闭 Runtime（{error}），已保留。", flush=True)
        return
    if not result.get("accepted"):
        print(_blocker_report(result), file=sys.stderr)
        print("--shutdown-on-exit：仍有活动工作，已保留 Runtime。", file=sys.stderr)
        return
    try:
        _wait_released(config, status, timeout=SHUTDOWN_TIMEOUT)
    except RuntimeContractError as error:
        print(f"--shutdown-on-exit：{error}", file=sys.stderr)
        return
    clear_ownership(config, token)
    print("--shutdown-on-exit：已安全关闭本次启动的独立 Runtime。", flush=True)
