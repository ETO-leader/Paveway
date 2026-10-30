import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize, main
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.integrations.vjudge_auth import SameOriginRedirect, VjudgeAuth, VjudgeAuthError, TijianError, read_secret_file


class VjudgeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-vjudge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        initialize(self.config, str(self.root / "state"), str(self.root / "workflows"))
        environment = {"TIJIAN_RUNTIME_MODE": "normal", "XDG_DATA_HOME": str(self.root / "data")}
        self.environ = patch.dict(os.environ, environment)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def auth(self, transport=None):
        return VjudgeAuth(self.config, transport=transport)

    def seed(self):
        auth = self.auth()
        auth.root.mkdir(mode=0o700, parents=True)
        auth.path.write_text(json.dumps({"cookie": "JSESSIONID=OLD_SESSION", "updatedAt": None}))
        auth.path.chmod(0o600)
        return auth.path.read_bytes()

    def test_login_merges_cookies_checks_before_save_and_never_stores_password(self):
        calls = []
        def transport(endpoint, cookie, fields):
            calls.append(endpoint)
            if endpoint == "/":
                return "landing", ["JSESSIONID=initial; Path=/; HttpOnly", "theme=light; Path=/"]
            if endpoint == "/user/login":
                self.assertEqual(fields, {"username": "test-user", "password": " TEST_PASSWORD "})
                self.assertIn("JSESSIONID=initial", cookie)
                return "success", ["JSESSIONID=VALID_SESSION; Secure; HttpOnly"]
            self.assertIn("JSESSIONID=VALID_SESSION", cookie)
            self.assertIn("theme=light", cookie)
            return "true", []
        auth = self.auth(transport)
        report = auth.login(" test-user ", " TEST_PASSWORD ")
        self.assertEqual(calls, ["/", "/user/login", "/user/checkLogInStatus"])
        self.assertTrue(report["logged_in"])
        self.assertTrue(report["live_checked"])
        self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)
        self.assertIn("VALID_SESSION", auth.path.read_text())
        for file in auth.root.iterdir():
            self.assertNotIn("TEST_PASSWORD", file.read_text())
        self.assertNotIn("VALID_SESSION", json.dumps(report))
        self.assertNotIn("test-user", json.dumps(report))
        self.assertFalse((auth.root / "vjudge_credentials.json").exists())

    def test_failed_login_or_challenge_preserves_existing_cookie(self):
        original = self.seed()
        for response in ("false", "<html>verification required</html>"):
            auth = self.auth(lambda endpoint, cookie, fields: (response, ["JSESSIONID=UNVERIFIED"]))
            with self.assertRaises(TijianError):
                auth.login("test", "SECRET_PASSWORD")
            self.assertEqual(auth.path.read_bytes(), original)

    def test_https_protocol_uses_encoded_form_cookie_headers_and_verified_status(self):
        requests = []
        class Response:
            def __init__(self, cookie=None, body=None):
                self.headers = Message()
                if cookie:
                    self.headers.add_header("Set-Cookie", cookie)
                self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                if self.body is None:
                    raise AssertionError("Login must not read an unnecessary landing or login body")
                return self.body
        def open_request(request, timeout):
            requests.append(request)
            self.assertLessEqual(timeout, 7)
            if request.full_url == "https://vjudge.net/":
                return Response("JSESSIONID=landing; Path=/")
            if request.full_url == "https://vjudge.net/user/login":
                self.assertEqual(urllib.parse.parse_qs(request.data.decode()),
                                 {"username": ["test-user"], "password": ["p&=秘密"]})
                self.assertIn("JSESSIONID=landing", request.get_header("Cookie"))
                return Response("JSESSIONID=verified; HttpOnly; Secure")
            self.assertEqual(request.full_url, "https://vjudge.net/user/checkLogInStatus")
            self.assertIn("JSESSIONID=verified", request.get_header("Cookie"))
            return Response(body=b"true")
        auth = self.auth()
        with patch("urllib.request.OpenerDirector.open", side_effect=open_request):
            result = auth.login("test-user", "p&=秘密")
        self.assertTrue(result["logged_in"])
        self.assertEqual(len(requests), 3)
        self.assertNotIn("秘密", auth.path.read_text())

    def test_cookie_import_is_verified_and_clear_preserves_account_binding(self):
        auth = self.auth(lambda endpoint, cookie, fields: ("true", []))
        report = auth.import_cookie("Cookie: JSESSIONID=SESSION_SECRET")
        self.assertTrue(report["logged_in"])
        binding = auth.root / "vjudge_credentials.json"
        binding.write_text("OPAQUE_UPSTREAM_CREDENTIALS")
        self.assertTrue(auth.test()["logged_in"])
        result = auth.clear()
        self.assertFalse(result["configured"])
        self.assertTrue(result["has_bound_account"])
        self.assertEqual(binding.read_text(), "OPAQUE_UPSTREAM_CREDENTIALS")
        self.assertFalse(auth.path.exists())

    def test_rejects_header_injection_and_leaves_old_session_unchanged(self):
        original = self.seed()
        for value in ("", "JSESSIONID=x\r\nAuthorization: secret", "x=" + "a" * 20000):
            auth = self.auth(lambda *args: self.fail("Invalid header reached network"))
            with self.assertRaises(RuntimeContractError):
                auth.import_cookie(value)
            self.assertEqual(auth.path.read_bytes(), original)

    def test_concurrent_external_change_is_not_overwritten(self):
        self.seed()
        auth = self.auth()
        def transport(*args):
            auth.path.write_text(json.dumps({"cookie": "JSESSIONID=EXTERNAL", "updatedAt": None}))
            return "true", []
        auth.transport = transport
        with self.assertRaises(ConflictError):
            auth.import_cookie("JSESSIONID=NEW")
        self.assertIn("EXTERNAL", auth.path.read_text())

    def test_auth_uses_tijian_harness_session_directory_when_selected(self):
        with patch.dict(os.environ, {"TIJIAN_RUNTIME_MODE": "harness", "TIJIAN_PROFILE_ROOT": str(self.root / "profile")}):
            auth = self.auth(lambda *args: ("true", []))
            auth.import_cookie("JSESSIONID=HARNESS")
            self.assertEqual(auth.path, self.root / "profile/sessions/vjudge_session.json")
        self.assertFalse((self.root / "data/tijian/vjudge_session.json").exists())

    def test_secret_file_requires_private_owned_regular_file(self):
        path = self.root / "input.json"
        path.write_text("SECRET_INPUT")
        path.chmod(0o644)
        with self.assertRaises(RuntimeContractError):
            read_secret_file(path)
        path.chmod(0o600)
        self.assertEqual(read_secret_file(path), "SECRET_INPUT")
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            read_secret_file(link)

    def test_redirect_cannot_send_cookie_or_password_to_other_origins(self):
        handler = SameOriginRedirect()
        request = urllib.request.Request("https://vjudge.net/user/login", data=b"password=SECRET")
        for url in ("https://other.example/user/login", "http://vjudge.net/", "https://vjudge.net:444/", "https://evil@vjudge.net/",
                    "https://:password@vjudge.net/", "https://vjudge.net:INVALID/"):
            with self.subTest(url=url), self.assertRaises(TijianError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)

    def test_unconfigured_session_does_not_claim_a_live_check(self):
        auth = self.auth(lambda *args: self.fail("No cookie should not make a network request"))
        self.assertFalse(auth.test()["live_checked"])
        self.assertFalse(auth.status()["configured"])

    def test_no_proxy_can_bypass_a_proxy_type_not_supported_by_login(self):
        from email.message import Message
        class Response:
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b"false"
        auth = self.auth()
        auth.environment = {"HTTPS_PROXY": "socks5://proxy.invalid:1", "NO_PROXY": "vjudge.net"}
        auth.deadline = float("inf")
        with patch("urllib.request.OpenerDirector.open", return_value=Response()):
            self.assertFalse(auth._check("JSESSIONID=dummy"))
        auth.environment["NO_PROXY"] = ""
        with self.assertRaises(TijianError) as caught:
            auth._check("JSESSIONID=dummy")
        self.assertEqual(caught.exception.code, "vjudge_proxy_unsupported")

    def test_network_error_does_not_echo_server_body_or_credentials(self):
        auth = self.auth()
        auth.deadline = float("inf")
        failure = urllib.error.HTTPError("https://vjudge.net/", 403, "SECRET_RESPONSE", {}, io.BytesIO(b"SECRET_RESPONSE"))
        with patch("urllib.request.OpenerDirector.open", side_effect=failure):
            with self.assertRaises(TijianError) as caught:
                auth._request("/", "JSESSIONID=SECRET_COOKIE", {"password": "SECRET_PASSWORD"})
        self.assertEqual(caught.exception.code, "vjudge_access_blocked")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_cli_private_file_inputs_and_results_do_not_echo_credentials(self):
        from contextlib import redirect_stdout
        secret = self.root / "login.json"
        secret.write_text(json.dumps({"username": "TEST_USER", "password": "TEST_PASSWORD"}))
        secret.chmod(0o600)
        output = io.StringIO()
        with patch.object(VjudgeAuth, "login", return_value={"logged_in": True}) as login, redirect_stdout(output):
            code = main(["vjudge", "auth", "login", "--credentials-file", str(secret), "--config", str(self.config), "--json"])
        self.assertEqual(code, 0)
        login.assert_called_once_with("TEST_USER", "TEST_PASSWORD")
        self.assertNotIn("TEST_PASSWORD", output.getvalue())
        self.assertNotIn("TEST_USER", output.getvalue())

    def test_ui_transport_never_puts_password_in_argv_and_removes_temporary_input(self):
        from oicoach_runtime.ui_client import CliClient
        paths = []
        class Process:
            returncode = 0
            def communicate(self, timeout):
                return b'{"api_version":1,"configured":true}', b''
        def spawn(argv, **kwargs):
            self.assertNotIn("SECRET_PASSWORD", " ".join(argv))
            path = Path(argv[argv.index("--credentials-file") + 1])
            paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("SECRET_PASSWORD", path.read_text())
            return Process()
        with patch("subprocess.Popen", side_effect=spawn):
            result = CliClient(self.config).call("vjudge", "auth", "login",
                text_files={"--credentials-file": '{"username":"user","password":"SECRET_PASSWORD"}'})
        self.assertTrue(result["configured"])
        self.assertFalse(paths[0].exists())
