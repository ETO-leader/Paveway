"""Legacy runtime configuration must not block startup or lose local settings."""
from dataclasses import replace
from pathlib import Path
import tempfile
import tomllib
import unittest

from oicoach_runtime.config import config_text, defaults, load_config
from oicoach_runtime.errors import ConfigError


class ConfigCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-config-compat-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.path = root / "config.toml"
        self.base = config_text(replace(defaults(), state_dir=root / "state", workflow_root=root / "workflows",
            server=replace(defaults().server, socket_path=str(root / "runtime.sock"))))

    def test_retired_tijian_settings_are_preserved_without_reactivating_bridge(self):
        original = self.base + '\n[tijian]\nenabled = true\nexecutable = "/fixture/old-tijian"\nread_only = false\ntimeout_seconds = 120\nmax_output_bytes = 65536\n'
        self.path.write_text(original)
        config = load_config(self.path)
        self.assertEqual(self.path.read_text(), original)
        self.assertFalse(hasattr(config, "tijian"))
        self.assertNotIn("/fixture/old-tijian", repr(config))
        self.assertEqual(tomllib.loads(config_text(config))["tijian"], tomllib.loads(original)["tijian"])
        self.assertEqual(config.agent, defaults().agent)

    def test_empty_or_partial_legacy_tables_roundtrip_without_adding_defaults(self):
        for table in ('[tijian]\n', '[tijian]\nenabled = false\n'):
            with self.subTest(table=table):
                self.path.write_text(self.base + '\n' + table)
                config = load_config(self.path)
                self.assertEqual(tomllib.loads(config_text(config))["tijian"], tomllib.loads(table)["tijian"])

    def test_unknown_fields_and_malformed_legacy_settings_remain_errors(self):
        for extra in ('[unknown]\n', '[legacy_tijian]\n', '[tijian]\nenabled = "yes"\n',
                      '[tijian]\npassword = "not-a-supported-setting"\n', 'tijian = "invalid"\n'):
            with self.subTest(extra=extra):
                # A scalar top-level assignment must precede other tables.
                self.path.write_text(extra + self.base if extra.startswith('tijian =') else self.base + '\n' + extra)
                with self.assertRaises(ConfigError):
                    load_config(self.path)
