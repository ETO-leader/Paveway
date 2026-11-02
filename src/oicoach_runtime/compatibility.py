"""Compatibility checks between client launcher and running runtime daemon."""

import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .build_id import runtime_build_id
from .errors import RuntimeContractError
from .ipc import request
from . import service

EXPECTED_NATIVE_PROTOCOL_VERSION = 1


def check_runtime_compatibility(status: dict[str, Any], config_path: Path) -> None:
    """Validate that the running daemon matches the current client's version and build.

    Raises RuntimeContractError with explicit, actionable instructions if incompatible.
    """
    daemon_version = status.get("runtime_version")
    current_version = __version__

    daemon_build_id = status.get("runtime_build_id")
    current_build_id = runtime_build_id()

    daemon_protocol = status.get("native_protocol_version")

    # 1. Native protocol version check (Case F)
    if daemon_protocol != EXPECTED_NATIVE_PROTOCOL_VERSION:
        guidance = restart_guidance(config_path)
        raise RuntimeContractError(
            f"运行中的 Paveway daemon 原生协议版本 ({daemon_protocol}) 与当前客户端要求 ({EXPECTED_NATIVE_PROTOCOL_VERSION}) 不匹配。\n\n"
            f"已有任务不会被中断或修改。\n\n"
            f"{guidance}"
        )

    # 2. Version mismatch check (Case C)
    if daemon_version != current_version:
        guidance = restart_guidance(config_path)
        raise RuntimeContractError(
            f"运行中的 Paveway daemon 版本与当前客户端不一致：\n"
            f"  - 当前 Paveway 版本：{current_version}\n"
            f"  - 运行中 daemon 版本：{daemon_version}\n\n"
            f"需要重启 Runtime 才能进入界面。已有任务不会被中断或修改。\n\n"
            f"{guidance}"
        )

    # 3. Missing runtime_build_id (Case E)
    if not daemon_build_id:
        guidance = restart_guidance(config_path)
        raise RuntimeContractError(
            "运行中的 Paveway daemon 不支持当前版本的构建指纹检查，\n"
            "可能是升级前启动的旧进程。\n\n"
            "请重启 Runtime 后重试。\n\n"
            f"{guidance}"
        )

    # 4. Same version but different build ID (Case D)
    if daemon_build_id != current_build_id:
        guidance = restart_guidance(config_path)
        raise RuntimeContractError(
            "检测到运行中的 Paveway daemon 与当前代码不一致。\n\n"
            "当前版本与 daemon 版本相同，但代码构建指纹不同。\n"
            "这通常发生在 Git pull、源码修改或更新安装包后未重启 daemon。\n\n"
            "请确认没有正在执行的任务，然后重启 Paveway Runtime。\n\n"
            f"{guidance}"
        )


def restart_guidance(config_path: Path) -> str:
    """Return safe restart instructions based on whether the service is managed by systemd."""
    try:
        is_managed = service.status(config_path).get("managed", False)
    except Exception:
        is_managed = False

    python_bin = sys.executable

    if is_managed:
        return (
            "【安全重启指引（systemd 托管）】\n"
            f"请执行以下命令重启 daemon 服务：\n"
            f"  {python_bin} -m oicoach_runtime service restart --config \"{config_path}\""
        )

    return (
        "【安全重启指引（独立后台进程）】\n"
        "1. 确认没有正在执行的任务。\n"
        "2. 查询实际运行的 daemon 进程：\n"
        "     pgrep -fa \"oicoach_runtime serve\"\n"
        "3. 对确认无误的 daemon 进程发送终止信号：\n"
        "     kill <PID>\n"
        "4. 等待进程退出后，重新执行启动命令：\n"
        "     ./paveway.sh"
    )


def probe_daemon(socket_path: str) -> dict[str, Any] | None:
    """Query daemon status via IPC.

    Returns the status dict if running, or None if the daemon is unavailable.
    Raises RuntimeContractError on unexpected IPC failures.
    """
    try:
        data = request(socket_path, "status")
        if data.get("daemon") == "running":
            return data
        return None
    except RuntimeContractError as error:
        if error.code == "daemon_unavailable":
            return None
        raise
