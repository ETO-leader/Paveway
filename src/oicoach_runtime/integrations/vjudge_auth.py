"""VJudge HTTPS login and Tijian-compatible local sessions; no upstream edits.

Follows Tijian's landing -> user/login -> checkLogInStatus protocol. Passwords
are transient; only its native cookie JSON is saved, outside Runtime archives.
"""

import fcntl
import http.client
import json
import os
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from ..errors import ConflictError, RuntimeContractError
from ..proxy import child_environment
from .tijian import TijianError

ORIGIN = "https://vjudge.net"
MAX_COOKIE = 16384


def read_secret_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeContractError("登录输入文件必须是当前用户拥有的私密普通文件（0600）")
        raw = stream.read(65537)
    try:
        if len(raw) > 65536:
            raise ValueError()
        return raw.decode("utf-8")
    except (UnicodeError, ValueError):
        raise RuntimeContractError("登录输入文件必须为 UTF-8，最多 64 KiB") from None


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parsed = urllib.parse.urlsplit(newurl)
            allowed = (parsed.scheme == "https" and parsed.hostname == "vjudge.net" and
                       parsed.port in (None, 443) and parsed.username is None and parsed.password is None)
        except ValueError:
            allowed = False
        if not allowed:
            raise TijianError("vjudge_redirect_blocked", "VJudge 登录被重定向到其他站点，已停止，未保存会话。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize_cookie(value):
    if not isinstance(value, str):
        raise RuntimeContractError("Cookie 必须为文本")
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if not value or len(value.encode()) > MAX_COOKIE or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise RuntimeContractError("Cookie 需为非空单行 HTTP Cookie，最多 16 KiB；不接受换行或控制字符")
    return value


class VjudgeAuth:
    def __init__(self, config_path, *, transport=None):
        self.environment = child_environment(config_path)
        mode = self.environment.get("TIJIAN_RUNTIME_MODE", "normal")
        if mode == "harness":
            root = Path(self.environment.get("TIJIAN_PROFILE_ROOT", ""))
            if not root.is_absolute():
                raise RuntimeContractError("Tijian harness 需要绝对 profile 路径")
            self.root = root / "sessions"
        elif mode == "normal":
            root = Path(self.environment.get("XDG_DATA_HOME") or Path.home() / ".local/share")
            if not root.is_absolute():
                raise RuntimeContractError("XDG_DATA_HOME 必须为绝对路径")
            self.root = root / "tijian"
        else:
            raise RuntimeContractError("无效的 Tijian 运行模式")
        self.path = self.root / "vjudge_session.json"
        self.transport = transport

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeContractError("Tijian 会话必须是当前用户拥有的普通文件")
            raw = stream.read(65537)
        try:
            value = json.loads(raw)
            if len(raw) > 65536 or not isinstance(value, dict):
                raise ValueError()
            cookie = value.get("cookie", "")
            return raw, normalize_cookie(cookie) if cookie.strip() else None
        except (ValueError, AttributeError, TypeError):
            raise RuntimeContractError("Tijian 会话文件无法读取；未覆盖现有内容") from None

    def status(self):
        _, cookie = self._read()
        return {"api_version": 1, "configured": bool(cookie), "has_bound_account":
                (self.root / "vjudge_credentials.json").is_file(), "password_saved": False,
                "scope": "tijian_shared", "live_checked": False}

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeContractError("Tijian 会话目录必须由当前用户拥有且不可被其他用户写入")
        fd = os.open(self.root / ".paveway-vjudge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RuntimeContractError("VJudge 会话锁必须是私密普通文件")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError:
            raise ConflictError("另一个 VJudge 会话操作正在执行，请稍后核对") from None
        finally:
            os.close(fd)

    def _request(self, endpoint, cookie="", fields=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TijianError("vjudge_timeout", "VJudge 登录或连接测试超时，未保存新会话")
        if self.transport:
            return self.transport(endpoint, cookie, fields)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "Referer": ORIGIN + "/",
                   "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}
        if cookie:
            headers["Cookie"] = cookie
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        proxies = {}
        bypass = urllib.request.proxy_bypass_environment("vjudge.net", {"no":
            self.environment.get("no_proxy", self.environment.get("NO_PROXY", ""))})
        for scheme in ("http", "https"):
            value = self.environment.get(scheme + "_proxy", self.environment.get(scheme.upper() + "_PROXY"))
            value = value or self.environment.get("all_proxy", self.environment.get("ALL_PROXY"))
            if value and not bypass:
                if not value.startswith(("http://", "https://")):
                    raise TijianError("vjudge_proxy_unsupported", "VJudge 登录需要 HTTP/HTTPS 代理；当前代理类型不受此登录入口支持")
                proxies[scheme] = value
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), SameOriginRedirect())
        try:
            with opener.open(urllib.request.Request(ORIGIN + endpoint, data=data, headers=headers), timeout=min(7, remaining)) as response:
                if response.headers.get("cf-mitigated") == "challenge":
                    raise TijianError("vjudge_access_blocked", "VJudge 要求浏览器验证；请在浏览器登录后使用 Cookie 导入")
                if endpoint in ("/", "/user/login"):
                    return "", response.headers.get_all("Set-Cookie", [])
                body = response.read(262145)
                if len(body) > 262144:
                    raise TijianError("vjudge_response_limit", "VJudge 响应过大，未保存会话")
                return body.decode("utf-8", errors="replace"), response.headers.get_all("Set-Cookie", [])
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                raise TijianError("vjudge_access_blocked", "VJudge 拒绝访问或要求浏览器验证；请在浏览器登录后使用 Cookie 导入") from None
            if error.code == 429:
                raise TijianError("vjudge_rate_limited", "VJudge 请求频率受限，请稍后重试") from None
            raise TijianError("vjudge_http_error", "VJudge 服务返回错误，未保存新会话") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise TijianError("vjudge_network_error", "无法连接 VJudge，请检查网络或代理；未保存新会话") from None

    def _check(self, cookie):
        body, _ = self._request("/user/checkLogInStatus", cookie)
        if body.strip() not in ("true", "false"):
            raise TijianError("vjudge_invalid_response", "VJudge 未返回登录状态，可能要求浏览器验证；未保存新会话")
        return body.strip() == "true"

    def _save(self, cookie, original):
        if self._read()[0] != original:
            raise ConflictError("Tijian 会话在登录期间已改变，未覆盖，请重新核对")
        fd, name = tempfile.mkstemp(prefix=".paveway-vjudge-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"cookie": cookie, "updatedAt": datetime.now(timezone.utc).isoformat()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
        return {**self.status(), "logged_in": True, "live_checked": True}

    def test(self):
        _, cookie = self._read()
        self.deadline = time.monotonic() + 20
        return {**self.status(), "logged_in": self._check(cookie) if cookie else False, "live_checked": bool(cookie)}

    def import_cookie(self, cookie):
        cookie = normalize_cookie(cookie)
        with self._lock():
            original, _ = self._read()
            self.deadline = time.monotonic() + 20
            if not self._check(cookie):
                raise TijianError("vjudge_not_logged_in", "Cookie 未通过 VJudge 登录检查，原会话保持不变")
            return self._save(cookie, original)

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password or len(username.encode()) > 512 or len(password.encode()) > 4096:
            raise RuntimeContractError("请输入用户名和密码；用户名最多 512 字节，密码最多 4096 字节")
        with self._lock():
            original, _ = self._read()
            self.deadline = time.monotonic() + 20
            _, initial = self._request("/")
            cookies = {}
            def merge(values):
                for value in values:
                    parsed = SimpleCookie()
                    try:
                        parsed.load(value)
                    except CookieError:
                        raise TijianError("vjudge_invalid_cookie", "VJudge 返回的会话格式无效，未保存") from None
                    cookies.update({key: item.coded_value for key, item in parsed.items()})
                return "; ".join(key + "=" + value for key, value in sorted(cookies.items()))
            cookie = merge(initial)
            _, values = self._request("/user/login", cookie, {"username": username.strip(), "password": password})
            cookie = merge(values)
            if not cookie:
                raise TijianError("vjudge_login_failed", "VJudge 未返回登录会话，请检查账号密码或改用浏览器 Cookie；原会话保持不变")
            cookie = normalize_cookie(cookie)
            if not self._check(cookie):
                raise TijianError("vjudge_login_failed", "VJudge 登录未成功，请检查账号密码或改用浏览器 Cookie；原会话保持不变")
            return self._save(cookie, original)

    def clear(self):
        with self._lock():
            self._read()
            self.path.unlink(missing_ok=True)
            return self.status()
