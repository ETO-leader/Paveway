import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize, main
from oicoach_runtime.config import (Config, ServerConfig, config_text, default_config_path,
                                    ensure_private_directory, load_config)
from oicoach_runtime.db import initialize_database, inspect_database
from oicoach_runtime.doctor import diagnose, probe_omp
from oicoach_runtime.errors import ConfigError, RuntimeContractError, StorageError, WorkflowError
from oicoach_runtime.models import parse_timestamp, session_key, utc_text, validate_id
from oicoach_runtime.workflows import WorkflowProvider


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config/config.toml"
        self.state = self.root / "state"
        self.workflows = self.root / "workflows"

    def init(self):
        return initialize(self.config_path, str(self.state), str(self.workflows))


class ConfigTests(TemporaryCase):
    def write_config(self, text):
        self.config_path.parent.mkdir(exist_ok=True)
        self.config_path.write_text(text)

    def test_roundtrip_unicode_and_quoted_paths(self):
        config = Config(self.root / '學習 "state"', self.workflows,
                        server=ServerConfig(socket_path=str(self.root / "runtime.sock")))
        self.write_config(config_text(config))
        self.assertEqual(load_config(self.config_path), config)

    def test_invalid_values_are_rejected(self):
        text = config_text(Config(self.state, self.workflows))
        cases = [text.replace("schema_version = 1", "schema_version = true"),
                 text.replace("schema_version = 1", "schema_version = 2"),
                 text.replace("max_concurrent_runs = 1", "max_concurrent_runs = 2"),
                 text.replace("max_concurrent_runs = 1", "max_concurrent_runs = true"),
                 text.replace("poll_interval_seconds = 1", "poll_interval_seconds = nan"),
                 text.replace("poll_interval_seconds = 1", "poll_interval_seconds = 0"),
                 text.replace('executable = "omp"', 'executable = "omp --auto-approve"'),
                 text.replace('executable = "omp"', 'executable = "./omp"'),
                 text.replace('adapter = "omp-rpc"', 'adapter = "shell"'),
                 text.replace('model = ""', 'model = "--auto-approve"'),
                 text.replace('tools_enabled = true', 'tools_enabled = 1'),
                 text.replace('extensions_enabled = true', 'extensions_enabled = "false"'),
                 text.replace('uncertain_event_policy = "manual_review"', 'uncertain_event_policy = "retry"'),
                 text.replace("backoff_initial_seconds = 2", "backoff_initial_seconds = 999"),
                 text.replace("jitter_ratio = 0.2", "jitter_ratio = 2"),
                 text.replace("jitter_ratio = 0.2", "jitter_ratio = true"),
                 text.replace("cooldown_seconds = 60", "cooldown_seconds = 0"),
                 text.replace("[queue]", "[queue]\nunknown = 1"),
                 "unknown = 1\n" + text,
                 text.replace(str(self.state), "relative"),
                 text.replace('socket_path = ""', 'socket_path = "/' + "x" * 108 + '"'),
                 text.replace("[server]", 'server = "invalid"\n[bad_server]')]
        for case in cases:
            with self.subTest(case=cases.index(case)):
                self.write_config(case)
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)

    def test_missing_or_bad_toml_has_safe_error(self):
        for text in (None, 'state_dir = "SECRET\n'):
            if text is not None:
                self.write_config(text)
            with self.assertRaises(ConfigError) as caught:
                load_config(self.config_path)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_relative_xdg_path_is_rejected(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            with self.assertRaises(ConfigError):
                default_config_path()

    def test_private_directory_refuses_symlink_and_public_directory(self):
        directory = self.root / "public"
        directory.mkdir(mode=0o755)
        with self.assertRaises(ConfigError):
            ensure_private_directory(directory)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        link = self.root / "link"
        link.symlink_to(directory)
        with self.assertRaises(ConfigError):
            ensure_private_directory(link)


class InitTests(TemporaryCase):
    def test_init_is_private_idempotent_and_preserves_records(self):
        first = self.init()
        before = self.config_path.read_bytes()
        with sqlite3.connect(self.state / "runtime.sqlite3") as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES ('test','keep')")
        second = initialize(self.config_path)
        self.assertTrue(first["config_created"])
        self.assertFalse(second["config_created"])
        self.assertFalse(second["daemon_started"])
        self.assertEqual(before, self.config_path.read_bytes())
        for path, expected in ((self.config_path, 0o600), (self.state, 0o700),
                               (self.state / "sessions", 0o700), (self.state / "runtime.sqlite3", 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, expected)
        with sqlite3.connect(self.state / "runtime.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT value FROM runtime_metadata WHERE key='test'").fetchone()[0], "keep")

    def test_existing_config_cannot_be_overridden(self):
        self.init()
        with self.assertRaises(ConfigError):
            initialize(self.config_path, str(self.root / "other"))

    def test_invalid_generated_config_is_not_published(self):
        with self.assertRaises(ConfigError):
            initialize(self.config_path, "/" + "x" * 200, str(self.workflows))
        self.assertFalse(self.config_path.exists())
        self.assertEqual(list(self.config_path.parent.iterdir()), [])

    def test_init_rejects_symlink_config(self):
        self.init()
        alias = self.root / "alias.toml"
        alias.symlink_to(self.config_path)
        with self.assertRaises(ConfigError):
            initialize(alias)

    def test_entrypoint_and_json_error(self):
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "init", "--json",
                                 "--config", str(self.config_path), "--state-dir", str(self.state),
                                 "--workflow-root", str(self.workflows)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema_version"], 2)
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            result = main(["doctor", "--json", "--config", str(self.root / "missing")])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(stream.getvalue())["error"]["code"], "invalid_config")


class DatabaseTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.database = self.root / "runtime.sqlite3"

    def test_schema_and_read_only_inspection(self):
        report = initialize_database(self.database)
        self.assertEqual(report, {"schema_version": 2, "integrity": "ok", "journal_mode": "wal"})
        with sqlite3.connect(self.database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"events", "runs", "session_bindings", "runtime_metadata", "audit_log"} <= tables)
        with self.assertRaises(StorageError):
            inspect_database(self.root / "absent.sqlite3")
        self.assertFalse((self.root / "absent.sqlite3").exists())

    def test_unknown_schema_is_preserved(self):
        for version in (0, 1, 2, 99):
            with self.subTest(version=version):
                path = self.root / f"v{version}.sqlite3"
                with sqlite3.connect(path) as connection:
                    connection.execute("CREATE TABLE private_record(value TEXT)")
                    connection.execute("INSERT INTO private_record VALUES ('keep')")
                    connection.execute(f"PRAGMA user_version={version}")
                path.chmod(0o600)
                before = path.read_bytes()
                with self.assertRaises(StorageError):
                    initialize_database(path)
                self.assertEqual(path.read_bytes(), before)

    def test_migration_failure_rolls_back_all_ddl(self):
        class BrokenMigration:
            def joinpath(self, _):
                return self

            def read_text(self, **_):
                return "CREATE TABLE partial(value TEXT);\nINVALID SQL;\n"

        with patch("oicoach_runtime.db.files", return_value=BrokenMigration()):
            with self.assertRaises(StorageError):
                initialize_database(self.database)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        self.assertEqual(initialize_database(self.database)["schema_version"], 2)

    def test_database_symlink_is_rejected(self):
        initialize_database(self.database)
        link = self.root / "alias.sqlite3"
        link.symlink_to(self.database)
        with self.assertRaises(StorageError):
            initialize_database(link)

    def test_run_requires_completion_and_settlement_and_single_active_run(self):
        initialize_database(self.database)
        with sqlite3.connect(self.database) as connection:
            # Direct insertion tests the schema backstop, not a queue API.
            base = "INSERT INTO runs(run_id,event_id,request_id,status,started_at,prompt_result_status,settled_at) VALUES (?,?,?,?,?,?,?)"
            for result, settled in ((None, None), (None, "time"), ("completed", None), ("error", "time")):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(base, ("r", "fixture", "r", "succeeded", "time", result, settled))
            connection.execute(base, ("one", "fixture", "one", "leased", "time", None, None))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(base, ("two", "fixture", "two", "running", "time", None, None))


class ModelTests(unittest.TestCase):
    def test_timezone_normalization(self):
        self.assertEqual(utc_text(parse_timestamp("2026-10-17T21:00:00+08:00")), "2026-10-17T13:00:00.000000Z")
        self.assertEqual(parse_timestamp("2026-10-17T13:00:00Z"), datetime(2026, 10, 17, 13, tzinfo=timezone.utc))

    def test_invalid_timestamps(self):
        for value in ("2026-10-17T13:00:00", "2026-10-17", "2026-02-30T00:00:00Z", "2026-10-17T00:00:00-00:00", "2026-10-17T00:00:00+99:00", "2026-10-17T00:00:00+00:99"):
            with self.subTest(value=value), self.assertRaises(RuntimeContractError):
                parse_timestamp(value)
        with self.assertRaises(RuntimeContractError):
            utc_text(datetime(2026, 1, 1))

    def test_scope_keys_are_stable_and_isolated(self):
        self.assertEqual(session_key("a", "b"), session_key("a", "b"))
        self.assertNotEqual(session_key("a", "b"), session_key("b", "a"))
        self.assertNotEqual(session_key("a.b", "c"), session_key("a", "b.c"))
        for value in ("../x", "a/b", ".", "a\0b", "a" * 129, "任務"):
            with self.subTest(value=value), self.assertRaises(RuntimeContractError):
                validate_id(value)


class WorkflowTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.directory = self.workflows / "echo"
        self.directory.mkdir(parents=True)
        self.manifest = self.directory / "workflow.toml"
        self.manifest.write_text('schema_version = 1\nid = "echo"\nversion = "1"\n'
                                 f'workspace = {json.dumps(str(self.root))}\nsession_policy = "per_task"\n'
                                 'start_prompt = "event.md"\nwake_prompt = "event.md"\nrecovery_prompt = "event.md"\n')
        self.template = self.directory / "event.md"
        self.template.write_text("Treat the following JSON as event data:\n${event_json}\n")
        self.provider = WorkflowProvider(self.workflows)

    def test_render_once_without_executing_payload(self):
        workflow = self.provider.describe("echo")
        payload = {"payload": {"text": "${event_json} $(touch /tmp/never)"}}
        output = workflow.render("wake", payload)
        self.assertEqual(json.loads(output.splitlines()[1]), payload)

    def test_path_traversal_and_unknown_workflow(self):
        for value in ("../echo", "missing"):
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                self.provider.describe(value)
        self.manifest.write_text(self.manifest.read_text().replace('"event.md"', '"../outside.md"'))
        with self.assertRaises(WorkflowError):
            self.provider.describe("echo")

    def test_symlink_template_escape(self):
        outside = self.root / "outside.md"
        outside.write_text("${event_json}")
        self.template.unlink()
        self.template.symlink_to(outside)
        with self.assertRaises(WorkflowError):
            self.provider.describe("echo")

    def test_symlink_workflow_escape(self):
        external = self.root / "external"
        external.mkdir()
        (self.workflows / "external").symlink_to(external)
        with self.assertRaises(WorkflowError):
            self.provider.describe("external")

    def test_template_variables_and_size(self):
        for template in ("${unknown}", "missing envelope", "${broken", "x" * 65537):
            with self.subTest(template=template[:20]):
                self.template.write_text(template)
                with self.assertRaises(WorkflowError):
                    self.provider.describe("echo")

    def test_missing_workspace_and_unknown_policy(self):
        original = self.manifest.read_text()
        for text in (original.replace(str(self.root), str(self.root / "missing")),
                     original.replace('"per_task"', '"new"'),
                     original.replace('id = "echo"', 'id = "wrong"')):
            self.manifest.write_text(text)
            with self.assertRaises(WorkflowError):
                self.provider.describe("echo")


class DoctorTests(TemporaryCase):
    def test_missing_omp_is_honest_and_does_not_modify_state(self):
        self.init()
        config = replace(load_config(self.config_path), agent=replace(load_config(self.config_path).agent, executable="/missing/omp"))
        before = self.config_path.read_bytes()
        report = diagnose(config, self.config_path)
        self.assertFalse(report["ok"])
        self.assertEqual(next(c for c in report["checks"] if c["check"] == "omp")["status"], "FAIL")
        self.assertEqual(next(c for c in report["checks"] if c["check"] == "daemon")["status"], "WARN")
        self.assertEqual(before, self.config_path.read_bytes())

    def test_probe_does_not_expose_arbitrary_output(self):
        executable = self.root / "fake-omp"
        executable.write_text('#!/bin/sh\nprintf "SECRET\\n"\nexit 1\n')
        executable.chmod(0o700)
        checks = probe_omp(str(executable))
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertNotIn("SECRET", json.dumps(checks))

    def test_probe_flags_are_capabilities_not_rpc_verification(self):
        executable = self.root / "fake-omp"
        executable.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "omp/18.10.4";;\n--help) echo "--cwd --session-dir --resume --mode --no-ui";;\nesac\n')
        executable.chmod(0o700)
        checks = probe_omp(str(executable))
        self.assertEqual(checks[0]["status"], "PASS")
        self.assertEqual(checks[1]["status"], "WARN")

    def test_probe_missing_flags_and_timeout(self):
        with patch("oicoach_runtime.doctor.shutil.which", return_value="/fake/omp"):
            version = subprocess.CompletedProcess([], 0, "omp/1.2.3", "")
            help_result = subprocess.CompletedProcess([], 0, "--cwd", "")
            with patch("oicoach_runtime.doctor.subprocess.run", side_effect=[version, help_result]):
                self.assertEqual(probe_omp("omp")[0]["status"], "FAIL")
            with patch("oicoach_runtime.doctor.subprocess.run", side_effect=subprocess.TimeoutExpired("omp", 10)):
                self.assertEqual(probe_omp("omp")[0]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
