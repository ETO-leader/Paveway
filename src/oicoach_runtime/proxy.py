"""Local proxy synchronization; reports contain variable names, never URLs/secrets."""

import os
import queue
import re
import socket
import threading
from urllib.parse import urlsplit

from .config import load_config
from .errors import RuntimeContractError
from .service import read_environment, update_environment

GROUPS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
STANDARD_NAMES = GROUPS + tuple(name.lower() for name in GROUPS)


def is_proxy(key):
    return key in STANDARD_NAMES or key == "PI_PROXY" or bool(re.fullmatch(r"PI_PROXY_[A-Z0-9_]{1,100}", key))


def child_environment(config_path, environment=None):
    result = dict(os.environ if environment is None else environment)
    if config_path is not None:
        result.update({key: value for key, value in read_environment(config_path).items() if is_proxy(key)})
    for key, value in result.items():
        if is_proxy(key) and key.upper() != "NO_PROXY" and value:
            _endpoint(value)
    return result


def _endpoint(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https", "socks4", "socks5", "socks5h") or not parsed.hostname:
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 1080 if parsed.scheme.startswith("socks") else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        return parsed.hostname, port
    except ValueError:
        raise RuntimeContractError("Invalid proxy URL; use an http/https/socks proxy URL with a valid host/port") from None


def _reachable(host, port):
    # DNS lookup itself has no socket timeout, so keep startup bounded as well.
    result = queue.Queue()
    def probe():
        try:
            with socket.create_connection((host, port), timeout=1):
                pass
            result.put(True)
        except OSError:
            result.put(False)
    threading.Thread(target=probe, daemon=True).start()
    try:
        return result.get(timeout=1.5)
    except queue.Empty:
        return False


def status(config_path, *, check=True):
    load_config(config_path)
    environment = child_environment(config_path)
    values = {key: value for key, value in environment.items() if is_proxy(key) and value}
    endpoints = {}
    for key, value in values.items():
        if key.upper() != "NO_PROXY":
            endpoint = _endpoint(value)
            endpoints.setdefault(endpoint, []).append(key)
    checks = []
    if check:
        for (host, port), names in endpoints.items():
            checks.append({"variables": sorted(names), "tcp_reachable": _reachable(host, port)})
    return {"api_version": 1, "mode": "proxy" if endpoints else "direct", "variables": sorted(values),
            "checks": checks, "bypass_all": values.get("NO_PROXY", values.get("no_proxy", "")).strip() == "*",
            "check_scope": "Proxy TCP reachability only; not model authentication or response availability"}


def synchronize(config_path, *, check=True):
    load_config(config_path)
    values = {}
    for upper in GROUPS:
        key = upper if upper in os.environ else upper.lower() if upper.lower() in os.environ else None
        if key is not None:
            values[upper] = values[upper.lower()] = os.environ[key]
    values.update({key: value for key, value in os.environ.items() if key.startswith("PI_PROXY") and is_proxy(key)})
    for key, value in values.items():
        if value and key.upper() != "NO_PROXY":
            _endpoint(value)
    # Absent shell variables preserve saved settings for unattended login startup.
    if values:
        update_environment(config_path, values)
    return {**status(config_path, check=check), "synchronized_variables": sorted(values),
            "effective": "next_agent_start", "running_agent_unchanged": True}


def summary(report):
    if report["mode"] == "direct":
        return "未配置代理，OMP 将直接连接。"
    checks = report["checks"]
    message = "已同步代理，下一次启动 OMP 生效；已运行回合保持原环境。"
    if any(not item["tcp_reachable"] for item in checks):
        message += " 代理端口不可达，请检查本机代理服务。"
    elif checks:
        message += " 代理 TCP 端口可达（不代表模型认证或请求已成功）。"
    if report["bypass_all"]:
        message += " NO_PROXY=* 将绕过代理。"
    return message
