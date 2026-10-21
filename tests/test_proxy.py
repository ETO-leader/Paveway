import concurrent.futures
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import proxy, service
from oicoach_runtime.cli import initialize
from oicoach_runtime.errors import RuntimeContractError


class ProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pw-proxy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        environment = patch.dict(os.environ, {k: v for k, v in os.environ.items() if not proxy.is_proxy(k)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_sync_normalizes_cases_keeps_credentials_and_reports_no_values(self):
        os.environ["PRIVATE_MODEL_KEY"] = "preserved-api-key"
        service.capture_environment(self.path, ["PRIVATE_MODEL_KEY"])
        os.environ.update(https_proxy="http://private-user:private-pass@127.0.0.1:7890", NO_PROXY="localhost,127.0.0.1",
                          PI_PROXY_OPENAI_CODEX="http://127.0.0.1:7891")
        report = proxy.synchronize(self.path, check=False)
        saved = service.read_environment(self.path)
        self.assertEqual(saved["HTTPS_PROXY"], os.environ["https_proxy"])
        self.assertEqual(saved["https_proxy"], saved["HTTPS_PROXY"])
        self.assertEqual(saved["NO_PROXY"], saved["no_proxy"])
        self.assertEqual(saved["PRIVATE_MODEL_KEY"], "preserved-api-key")
        self.assertEqual(saved["PI_PROXY_OPENAI_CODEX"], "http://127.0.0.1:7891")
        self.assertEqual(service.environment_path(self.path).stat().st_mode & 0o777, 0o600)
        for secret in ("private-user", "private-pass", "127.0.0.1", "7890", "preserved-api-key"):
            self.assertNotIn(secret, json.dumps(report))

    def test_absent_proxy_preserves_saved_settings_and_empty_clears_both_cases(self):
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        proxy.synchronize(self.path, check=False)
        path = service.environment_path(self.path)
        before = path.stat().st_mtime_ns
        os.environ.pop("HTTPS_PROXY")
        proxy.synchronize(self.path, check=False)
        self.assertEqual(path.stat().st_mtime_ns, before)
        environment = proxy.child_environment(self.path, {"HTTPS_PROXY": "http://old.invalid", "PRIVATE_MODEL_KEY": "current-key"})
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(environment["PRIVATE_MODEL_KEY"], "current-key")
        os.environ["https_proxy"] = ""
        report = proxy.synchronize(self.path, check=False)
        self.assertEqual(report["mode"], "direct")
        environment = proxy.child_environment(self.path, {"HTTPS_PROXY": "http://old.invalid"})
        self.assertEqual(environment["HTTPS_PROXY"], "")
        self.assertEqual(environment["https_proxy"], "")

    def test_concurrent_environment_updates_do_not_drop_keys(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda i: service.update_environment(self.path, {"KEY_" + str(i): str(i)}), range(24)))
        saved = service.read_environment(self.path)
        self.assertEqual(saved, {"KEY_" + str(i): str(i) for i in range(24)})

    def test_tcp_check_reports_failure_without_exposing_proxy_auth(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        os.environ["HTTPS_PROXY"] = "http://user:password@127.0.0.1:" + str(listener.getsockname()[1])
        report = proxy.synchronize(self.path)
        self.assertTrue(report["checks"][0]["tcp_reachable"])
        listener.close()
        report = proxy.status(self.path)
        self.assertFalse(report["checks"][0]["tcp_reachable"])
        self.assertIn("端口不可达", proxy.summary(report))
        self.assertNotIn("password", json.dumps(report))

    def test_dns_probe_has_an_overall_timeout(self):
        release = threading.Event()
        def slow(*args, **kwargs):
            release.wait(4)
            raise OSError
        try:
            with patch.object(proxy.socket, "create_connection", side_effect=slow):
                start = time.monotonic()
                self.assertFalse(proxy._reachable("proxy.invalid", 8080))
                self.assertLess(time.monotonic() - start, 2.5)
        finally:
            release.set()

    def test_invalid_or_unsafe_environment_is_rejected_without_overwrite(self):
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        proxy.synchronize(self.path, check=False)
        path = service.environment_path(self.path)
        before = path.read_bytes()
        for value in ("PRIVATE_INVALID_PROXY", "http://127.0.0.1:99999", "http://127.0.0.1:7890\nPRIVATE_SECRET"):
            os.environ["HTTPS_PROXY"] = value
            with self.assertRaises(RuntimeContractError) as failure:
                proxy.synchronize(self.path, check=False)
            self.assertNotIn("PRIVATE", str(failure.exception))
            self.assertEqual(path.read_bytes(), before)
        path.chmod(0o644)
        with self.assertRaises(RuntimeContractError):
            proxy.child_environment(self.path)
