"""Targeted tests for native tijian-mcp configuration and stdio MCP protocol boundary."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize, main
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, WorkflowError
from oicoach_runtime import workflow_setup
from oicoach_runtime.workflow_setup import connect_tijian, create, set_tijian, show_tijian


FAKE_MCP_SERVER_CODE = """#!/usr/bin/env python3
import sys, json

permission = "none" if "--read-only" in sys.argv else "full"
for arg in sys.argv[1:]:
    if arg.startswith("--write-policy="):
        permission = arg.split("=", 1)[1]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-tijian-mcp", "version": "1.0.0"}
            }
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        tools = [
            {"name": "tijian_problem_get", "description": "Read problem", "inputSchema": {"type": "object"}}
        ]
        if permission != "none":
            tools.append(
                {"name": "tijian_problem_ensure", "description": "Ensure problem", "inputSchema": {"type": "object"}}
            )
        if permission == "full":
            tools.append(
                {"name": "tijian_database_reset", "description": "Reset database", "inputSchema": {"type": "object"}}
            )
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools}
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        name = msg.get("params", {}).get("name")
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"ok": True, "tool": name})}]
            }
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
"""


class NativeMcpConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-native-mcp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.config_path)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        (self.workspace / ".omp").mkdir(mode=0o700)
        create(self.config, "coach", self.workspace)
        self.fake_mcp = self.root / "fake_tijian_mcp.py"
        self.fake_mcp.write_text(FAKE_MCP_SERVER_CODE)
        self.fake_mcp.chmod(0o755)

    def test_default_read_only_configuration_includes_flag(self):
        report = connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(report["availability"], "configured_not_verified")
        self.assertTrue(report["read_only"])
        mcp_path = self.workspace / ".omp/mcp.json"
        self.assertTrue(mcp_path.is_file())
        self.assertEqual(mcp_path.stat().st_mode & 0o777, 0o600)
        data = json.loads(mcp_path.read_text())
        server = data["mcpServers"]["tijian"]
        self.assertEqual(server["command"], str(self.fake_mcp.resolve()))
        self.assertEqual(server["args"], ["--read-only"])

    def test_explicit_read_write_configuration_omits_read_only_flag(self):
        report = connect_tijian(self.config, "coach", self.fake_mcp, read_only=False)
        self.assertFalse(report["read_only"])
        data = json.loads((self.workspace / ".omp/mcp.json").read_text())
        server = data["mcpServers"]["tijian"]
        self.assertEqual(server["command"], str(self.fake_mcp.resolve()))
        self.assertEqual(server["args"], [])

    def test_merges_with_existing_servers_and_disabled_servers(self):
        mcp_path = self.workspace / ".omp/mcp.json"
        initial = {
            "mcpServers": {
                "git": {"command": "/usr/bin/git", "args": ["status"]}
            },
            "disabledServers": ["unused"]
        }
        mcp_path.write_text(json.dumps(initial))
        connect_tijian(self.config, "coach", self.fake_mcp)
        data = json.loads(mcp_path.read_text())
        self.assertEqual(data["mcpServers"]["git"], initial["mcpServers"]["git"])
        self.assertEqual(data["disabledServers"], ["unused"])
        self.assertIn("tijian", data["mcpServers"])

    def test_disabled_servers_containing_tijian_raises_conflict_and_preserves_file(self):
        mcp_path = self.workspace / ".omp/mcp.json"
        initial = {
            "mcpServers": {},
            "disabledServers": ["tijian", "other"]
        }
        mcp_path.write_text(json.dumps(initial, indent=2))
        original_bytes = mcp_path.read_bytes()
        with self.assertRaises(ConflictError) as caught:
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertIn("disabledServers", str(caught.exception))
        self.assertEqual(mcp_path.read_bytes(), original_bytes)

    def test_disabled_servers_containing_other_allows_tijian_and_preserves_disabled_list(self):
        mcp_path = self.workspace / ".omp/mcp.json"
        initial = {
            "mcpServers": {},
            "disabledServers": ["other"]
        }
        mcp_path.write_text(json.dumps(initial, indent=2))
        connect_tijian(self.config, "coach", self.fake_mcp)
        data = json.loads(mcp_path.read_text())
        self.assertEqual(data["disabledServers"], ["other"])
        self.assertIn("tijian", data["mcpServers"])
        first_content = mcp_path.read_bytes()
        connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(mcp_path.read_bytes(), first_content)

    def test_idempotent_when_called_repeatedly_with_same_config(self):
        connect_tijian(self.config, "coach", self.fake_mcp)
        first_content = (self.workspace / ".omp/mcp.json").read_bytes()
        connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual((self.workspace / ".omp/mcp.json").read_bytes(), first_content)

    def test_explicit_connect_imports_legacy_config_without_rewriting_it(self):
        legacy = self.workspace / ".mcp.json"
        original = json.dumps({"mcpServers": {"other": {"command": "/fixture/other"}}}).encode()
        legacy.write_bytes(original)
        report = connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertTrue(report["legacy_imported"])
        self.assertEqual(legacy.read_bytes(), original)
        native = json.loads((self.workspace / ".omp/mcp.json").read_text())
        self.assertIn("other", native["mcpServers"])
        self.assertIn("tijian", native["mcpServers"])

    def test_refuses_symlink_native_directory(self):
        directory = self.workspace / ".omp"
        directory.rmdir()
        directory.symlink_to(self.config.workflow_root, target_is_directory=True)
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)

    def test_raises_conflict_when_existing_tijian_differs_and_preserves_file(self):
        mcp_path = self.workspace / ".omp/mcp.json"
        existing = {"mcpServers": {"tijian": {"command": "/different/tijian-mcp", "args": []}}}
        mcp_path.write_text(json.dumps(existing))
        original_bytes = mcp_path.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(mcp_path.read_bytes(), original_bytes)

    def test_rejects_missing_or_non_executable_binary(self):
        missing = self.root / "nonexistent"
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", missing)
        non_exec = self.root / "non_exec"
        non_exec.touch()
        non_exec.chmod(0o644)
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", non_exec)

    def test_rejects_symlink_mcp_config_without_modifying(self):
        mcp_path = self.workspace / ".omp/mcp.json"
        mcp_path.symlink_to(self.config_path)
        before = self.config_path.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_rejects_malformed_existing_json_without_modifying(self):
        mcp_path = self.workspace / ".omp/mcp.json"
        mcp_path.write_text("{not: valid json}")
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(mcp_path.read_text(), "{not: valid json}")

    def test_cli_connect_tijian_and_mutual_exclusion(self):
        # Default read-only via CLI
        code = main([
            "workflow", "connect-tijian", "coach",
            "--executable", str(self.fake_mcp),
            "--config", str(self.config_path),
            "--json"
        ])
        self.assertEqual(code, 0)
        data = json.loads((self.workspace / ".omp/mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["tijian"]["args"], ["--read-only"])

        # Mutually exclusive flags rejected
        with self.assertRaises(SystemExit):
            main([
                "workflow", "connect-tijian", "coach",
                "--executable", str(self.fake_mcp),
                "--read-only", "--read-write",
                "--config", str(self.config_path)
            ])

    @property
    def mcp_path(self):
        return self.workspace / ".omp/mcp.json"

    def _save(self, **values):
        revision = show_tijian(self.config, "coach")["revision"]
        return set_tijian(self.config, "coach", expected_revision=revision, **values)

    def _existing_server(self, args, command=None, **extra):
        raw = {"mcpServers": {"tijian": {
            "command": command or str(self.fake_mcp), "args": args, **extra}}}
        self.mcp_path.write_text(json.dumps(raw))
        return raw

    def _cli(self, *arguments):
        result = subprocess.run([sys.executable, "-m", "oicoach_runtime", "workflow", "tijian", *arguments,
                                 "--config", str(self.config_path), "--json"],
                                capture_output=True, text=True, timeout=30)
        return result.returncode, json.loads(result.stdout if result.returncode == 0 else result.stderr)

    def test_show_missing_is_read_only_and_reports_absent_state(self):
        (self.workspace / ".omp").rmdir()
        legacy = self.workspace / ".mcp.json"
        legacy.write_text("{deliberately invalid legacy JSON")
        before = sorted(p.name for p in self.workspace.iterdir())
        report = show_tijian(self.config, "coach")
        self.assertEqual(report, {
            "api_version": 1, "path": str(self.mcp_path), "configured": False,
            "executable": None, "permission": None, "revision": report["revision"],
            "availability": "not_configured"})
        self.assertEqual(len(report["revision"]), 64)
        self.assertEqual(show_tijian(self.config, "coach"), report)
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), before)
        self.assertFalse((self.workspace / ".omp").exists())

    def test_new_path_defaults_managed_and_creates_private_native_config_only(self):
        (self.workspace / ".omp").rmdir()
        legacy = self.workspace / ".mcp.json"
        legacy.write_text('{"legacy": true}')
        absent = show_tijian(self.config, "coach")["revision"]
        result = self._save(executable=self.fake_mcp)
        self.assertTrue(result["changed"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["permission"], "managed")
        self.assertEqual(result["availability"], "configured_not_verified")
        self.assertNotEqual(result["revision"], absent)
        self.assertEqual(result["revision"], show_tijian(self.config, "coach")["revision"])
        self.assertEqual(json.loads(self.mcp_path.read_text()), {"mcpServers": {"tijian": {
            "command": str(self.fake_mcp), "args": ["--write-policy=managed"]}}})
        self.assertEqual(self.mcp_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.mcp_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(legacy.read_text(), '{"legacy": true}')

    def test_show_recognizes_every_supported_policy_without_executable_probe(self):
        for args, permission in (([], "full"), (["--read-only"], "none"),
                                 (["--write-policy=none"], "none"),
                                 (["--write-policy=managed"], "managed"),
                                 (["--write-policy=full"], "full")):
            with self.subTest(args=args):
                self._existing_server(args, command="/missing/old/tijian-mcp")
                report = show_tijian(self.config, "coach")
                self.assertTrue(report["configured"])
                self.assertEqual(report["permission"], permission)
                self.assertEqual(report["executable"], "/missing/old/tijian-mcp")
                self.assertEqual(report["availability"], "configured_not_verified")

    def test_permission_only_changes_do_not_probe_missing_executable(self):
        self._existing_server(["--read-only"], command="/missing/old/tijian-mcp")
        for permission in ("managed", "full", "none"):
            with self.subTest(permission=permission):
                result = self._save(permission=permission, confirm_full=permission == "full")
                self.assertTrue(result["changed"])
                self.assertEqual(result["permission"], permission)
                server = json.loads(self.mcp_path.read_text())["mcpServers"]["tijian"]
                self.assertEqual(server, {"command": "/missing/old/tijian-mcp",
                                          "args": [f"--write-policy={permission}"]})

    def test_path_only_updates_preserve_all_existing_policy_encodings(self):
        for args in ([], ["--read-only"], ["--write-policy=none"],
                     ["--write-policy=managed"], ["--write-policy=full"]):
            with self.subTest(args=args):
                self._existing_server(args, command="/missing/old/tijian-mcp")
                self.assertTrue(self._save(executable=self.fake_mcp)["changed"])
                server = json.loads(self.mcp_path.read_text())["mcpServers"]["tijian"]
                self.assertEqual(server, {"command": str(self.fake_mcp), "args": args})

    def test_new_and_upgraded_full_require_explicit_confirmation(self):
        absent = show_tijian(self.config, "coach")["revision"]
        with self.assertRaisesRegex(ConflictError, "confirmation"):
            set_tijian(self.config, "coach", expected_revision=absent,
                       executable=self.fake_mcp, permission="full")
        self.assertFalse(self.mcp_path.exists())
        self._save(executable=self.fake_mcp)
        before = self.mcp_path.read_bytes()
        with self.assertRaisesRegex(ConflictError, "confirmation"):
            self._save(permission="full")
        self.assertEqual(self.mcp_path.read_bytes(), before)
        self.assertEqual(self._save(permission="full", confirm_full=True)["permission"], "full")
        self.mcp_path.unlink()
        self.assertEqual(self._save(executable=self.fake_mcp, permission="full", confirm_full=True)["permission"], "full")

    def test_noop_preserves_original_bytes_inode_mtime_and_revision(self):
        for args, permission in (([], "full"), (["--read-only"], "none"),
                                 (["--write-policy=managed"], "managed")):
            with self.subTest(args=args):
                self._existing_server(args)
                os.utime(self.mcp_path, ns=(1_000_000_000, 2_000_000_000))
                content, info = self.mcp_path.read_bytes(), self.mcp_path.stat()
                revision = show_tijian(self.config, "coach")["revision"]
                for values in ({"permission": permission}, {"executable": self.fake_mcp}):
                    result = self._save(**values)
                    self.assertFalse(result["changed"])
                    self.assertEqual(result["revision"], revision)
                    after = self.mcp_path.stat()
                    self.assertEqual((after.st_ino, after.st_mtime_ns), (info.st_ino, info.st_mtime_ns))
                    self.assertEqual(self.mcp_path.read_bytes(), content)

    def test_legacy_connect_noop_preserves_inode_and_mtime(self):
        connect_tijian(self.config, "coach", self.fake_mcp)
        before = self.mcp_path.stat()
        connect_tijian(self.config, "coach", self.fake_mcp)
        after = self.mcp_path.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_revisions_cover_unrelated_json_and_whitespace_and_absent_file(self):
        absent = show_tijian(self.config, "coach")["revision"]
        self.mcp_path.write_text("{}")
        empty = show_tijian(self.config, "coach")["revision"]
        self.assertNotEqual(absent, empty)
        self.mcp_path.write_text("{}\n")
        self.assertNotEqual(empty, show_tijian(self.config, "coach")["revision"])
        self.mcp_path.write_text('{"other": true}')
        unrelated = show_tijian(self.config, "coach")["revision"]
        self.assertNotIn(unrelated, (absent, empty))

    def test_stale_revision_rejects_changed_and_noop_updates(self):
        revision = show_tijian(self.config, "coach")["revision"]
        self._save(executable=self.fake_mcp)
        before = self.mcp_path.read_bytes()
        for values in ({"permission": "none"}, {"permission": "managed"},
                       {"executable": self.fake_mcp}):
            with self.subTest(values=values), self.assertRaisesRegex(ConflictError, "reload"):
                set_tijian(self.config, "coach", expected_revision=revision, **values)
        self.assertEqual(self.mcp_path.read_bytes(), before)

    def test_stale_revision_rejects_deleted_and_unrelated_changed_file(self):
        self._save(executable=self.fake_mcp)
        revision = show_tijian(self.config, "coach")["revision"]
        raw = json.loads(self.mcp_path.read_text())
        raw["unrelated"] = True
        self.mcp_path.write_text(json.dumps(raw))
        with self.assertRaises(ConflictError):
            set_tijian(self.config, "coach", expected_revision=revision, permission="none")
        revision = show_tijian(self.config, "coach")["revision"]
        self.mcp_path.unlink()
        with self.assertRaises(ConflictError):
            set_tijian(self.config, "coach", expected_revision=revision, executable=self.fake_mcp)
        self.assertFalse(self.mcp_path.exists())

    def test_new_api_preserves_other_json_and_disabled_servers(self):
        initial = {"mcpServers": {"other": {"command": "custom", "env": {"KEY": "value"}}},
                   "disabledServers": ["other"], "nested": {"keep": [None, True, "中文"]}}
        self.mcp_path.write_text(json.dumps(initial))
        self._save(executable=self.fake_mcp)
        self._save(permission="none")
        actual = json.loads(self.mcp_path.read_text())
        actual["mcpServers"].pop("tijian")
        self.assertEqual(actual, initial)

    def test_new_api_rejects_disabled_tijian_without_writing(self):
        self.mcp_path.write_text('{"disabledServers": ["tijian"], "mcpServers": {}}')
        before = self.mcp_path.read_bytes()
        for operation in (lambda: show_tijian(self.config, "coach"),
                          lambda: set_tijian(self.config, "coach", expected_revision="irrelevant",
                                             executable=self.fake_mcp)):
            with self.assertRaisesRegex(ConflictError, "disabledServers"):
                operation()
        self.assertEqual(self.mcp_path.read_bytes(), before)

    def test_unrecognized_servers_fail_show_and_set_without_writing(self):
        valid = {"command": str(self.fake_mcp), "args": []}
        invalid = (None, [], "tijian", {}, {"command": str(self.fake_mcp)},
                   {"args": []}, {**valid, "env": {}}, {**valid, "disabled": False},
                   {**valid, "command": "relative/tijian"}, {**valid, "command": 1},
                   {**valid, "command": "/bad\0path"}, {**valid, "args": None},
                   {**valid, "args": "--read-only"}, {**valid, "args": ["--unknown"]},
                   {**valid, "args": ["--write-policy=unknown"]},
                   {**valid, "args": ["--read-only", "--write-policy=managed"]},
                   {**valid, "args": ["--write-policy=none", "--write-policy=none"]},
                   {**valid, "args": [None]}, {**valid, "args": [{}]})
        for server in invalid:
            with self.subTest(server=server):
                self.mcp_path.write_text(json.dumps({"mcpServers": {"tijian": server}}))
                before = self.mcp_path.read_bytes()
                with self.assertRaises(WorkflowError):
                    show_tijian(self.config, "coach")
                with self.assertRaises(WorkflowError):
                    set_tijian(self.config, "coach", expected_revision="irrelevant", executable=self.fake_mcp)
                self.assertEqual(self.mcp_path.read_bytes(), before)

    def test_new_api_rejects_invalid_json_shapes_and_oversized_config(self):
        for content in (b"{bad json", b"\xff", b"[]", b"null",
                        b'{"mcpServers": []}', b'{"mcpServers": null}', b" " * 65537):
            with self.subTest(content=content[:40]):
                self.mcp_path.write_bytes(content)
                with self.assertRaises(WorkflowError):
                    show_tijian(self.config, "coach")
                with self.assertRaises(WorkflowError):
                    set_tijian(self.config, "coach", expected_revision="irrelevant", executable=self.fake_mcp)
                self.assertEqual(self.mcp_path.read_bytes(), content)

    def test_oversized_merge_leaves_original_untouched(self):
        self.mcp_path.write_text(json.dumps({"padding": "x" * 65490}))
        before = self.mcp_path.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "exceeds"):
            self._save(executable=self.fake_mcp)
        self.assertEqual(self.mcp_path.read_bytes(), before)

    def test_new_api_rejects_symlink_file_including_dangling_symlink(self):
        for target in (self.config_path, self.root / "missing"):
            with self.subTest(target=target):
                self.mcp_path.symlink_to(target)
                with self.assertRaises(ConflictError):
                    show_tijian(self.config, "coach")
                with self.assertRaises(ConflictError):
                    set_tijian(self.config, "coach", expected_revision="irrelevant", executable=self.fake_mcp)
                self.assertTrue(self.mcp_path.is_symlink())
                self.mcp_path.unlink()

    def test_new_api_rejects_unsafe_directory(self):
        directory = self.workspace / ".omp"
        directory.rmdir()
        directory.symlink_to(self.config.workflow_root, target_is_directory=True)
        with self.assertRaises(ConflictError):
            show_tijian(self.config, "coach")
        directory.unlink()
        directory.mkdir(mode=0o700)
        for mode in (0o722, 0o770, 0o777):
            with self.subTest(mode=mode):
                directory.chmod(mode)
                with self.assertRaises(ConflictError):
                    show_tijian(self.config, "coach")
        directory.chmod(0o700)
        with patch.object(workflow_setup.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ConflictError):
                show_tijian(self.config, "coach")
        directory.rmdir()
        directory.write_text("not a directory")
        with self.assertRaises(ConflictError):
            show_tijian(self.config, "coach")

    def test_new_api_rejects_nonregular_config(self):
        self.mcp_path.mkdir()
        with self.assertRaises(WorkflowError):
            show_tijian(self.config, "coach")
        self.mcp_path.rmdir()
        os.mkfifo(self.mcp_path)
        with self.assertRaises(WorkflowError):
            show_tijian(self.config, "coach")

    def test_invalid_updates_leave_missing_config_absent(self):
        revision = show_tijian(self.config, "coach")["revision"]
        for values in ({}, {"permission": "managed"}, {"permission": "invalid"},
                       {"executable": self.root / "missing"}, {"executable": self.config_path}):
            with self.subTest(values=values), self.assertRaises(WorkflowError):
                set_tijian(self.config, "coach", expected_revision=revision, **values)
        self.assertFalse(self.mcp_path.exists())

    def test_permission_change_atomically_replaces_file_with_private_mode(self):
        self._existing_server(["--read-only"])
        self.mcp_path.chmod(0o644)
        before = self.mcp_path.stat()
        self.assertTrue(self._save(permission="managed")["changed"])
        after = self.mcp_path.stat()
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mode & 0o777, 0o600)

    def test_atomic_first_write_rejects_file_created_after_comparison(self):
        revision = show_tijian(self.config, "coach")["revision"]
        concurrent = b'{"late": "creation"}'
        original_link = workflow_setup.os.link

        def race_link(source, destination):
            Path(destination).write_bytes(concurrent)
            return original_link(source, destination)

        with patch.object(workflow_setup.os, "link", side_effect=race_link):
            with self.assertRaises(ConflictError):
                set_tijian(self.config, "coach", expected_revision=revision, executable=self.fake_mcp)
        self.assertEqual(self.mcp_path.read_bytes(), concurrent)
        self.assertEqual(list(self.mcp_path.parent.glob(".paveway-mcp-*")), [])

    def test_concurrent_existing_file_edit_is_not_overwritten(self):
        self._save(executable=self.fake_mcp)
        revision = show_tijian(self.config, "coach")["revision"]
        concurrent = b'{"mcpServers": {}, "concurrent": true}'
        original_fsync = workflow_setup.os.fsync

        def edit_after_temp_write(fd):
            original_fsync(fd)
            self.mcp_path.write_bytes(concurrent)

        with patch.object(workflow_setup.os, "fsync", side_effect=edit_after_temp_write):
            with self.assertRaisesRegex(ConflictError, "concurrently"):
                set_tijian(self.config, "coach", expected_revision=revision, permission="none")
        self.assertEqual(self.mcp_path.read_bytes(), concurrent)
        self.assertEqual(list(self.mcp_path.parent.glob(".paveway-mcp-*")), [])

    def test_concurrent_creation_is_not_overwritten(self):
        revision = show_tijian(self.config, "coach")["revision"]
        concurrent = b'{"created": "by another process"}'
        original_fsync = workflow_setup.os.fsync

        def create_after_temp_write(fd):
            original_fsync(fd)
            self.mcp_path.write_bytes(concurrent)

        with patch.object(workflow_setup.os, "fsync", side_effect=create_after_temp_write):
            with self.assertRaises(ConflictError):
                set_tijian(self.config, "coach", expected_revision=revision, executable=self.fake_mcp)
        self.assertEqual(self.mcp_path.read_bytes(), concurrent)
        self.assertEqual(list(self.mcp_path.parent.glob(".paveway-mcp-*")), [])

    def test_legacy_import_rejects_concurrent_source_edit(self):
        legacy = self.workspace / ".mcp.json"
        legacy.write_text("{}")
        concurrent = b'{"legacy": "changed"}'
        original_fsync = workflow_setup.os.fsync

        def edit_after_temp_write(fd):
            original_fsync(fd)
            legacy.write_bytes(concurrent)

        with patch.object(workflow_setup.os, "fsync", side_effect=edit_after_temp_write):
            with self.assertRaises(ConflictError):
                connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(legacy.read_bytes(), concurrent)
        self.assertFalse(self.mcp_path.exists())

    def test_cli_show_set_full_confirmation_noop_and_stale_revision(self):
        code, initial = self._cli("show", "coach")
        self.assertEqual(code, 0)
        self.assertFalse(initial["configured"])
        code, configured = self._cli("set", "coach", "--executable", str(self.fake_mcp),
                                     "--expected-revision", initial["revision"])
        self.assertEqual(code, 0)
        self.assertTrue(configured["changed"])
        self.assertEqual(configured["permission"], "managed")
        code, denied = self._cli("set", "coach", "--permission", "full",
                                 "--expected-revision", configured["revision"])
        self.assertEqual(code, 1)
        self.assertIn("confirmation", denied["error"]["message"])
        code, full = self._cli("set", "coach", "--permission", "full", "--confirm-full",
                              "--expected-revision", configured["revision"])
        self.assertEqual(code, 0)
        self.assertEqual(full["permission"], "full")
        code, noop = self._cli("set", "coach", "--permission", "full", "--expected-revision", full["revision"])
        self.assertEqual(code, 0)
        self.assertFalse(noop["changed"])
        code, stale = self._cli("set", "coach", "--permission", "none",
                               "--expected-revision", configured["revision"])
        self.assertEqual(code, 1)
        self.assertIn("reload", stale["error"]["message"])
        code, shown = self._cli("show", "coach")
        self.assertEqual(code, 0)
        self.assertEqual(shown["revision"], full["revision"])
        self.assertEqual(shown["permission"], "full")

    def test_cli_requires_revision_and_rejects_unknown_permission(self):
        common = ["--config", str(self.config_path), "--json"]
        for arguments in (["set", "coach", "--permission", "none"],
                          ["set", "coach", "--permission", "unknown", "--expected-revision", "value"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                main(["workflow", "tijian", *arguments, *common])

    def test_cli_reports_disabled_and_unrecognized_configuration_without_writing(self):
        for content in ('{"mcpServers": {"tijian": {"command": "/fixture", "args": ["--unknown"]}}}',
                        '{"disabledServers": ["tijian"]}'):
            with self.subTest(content=content):
                self.mcp_path.write_text(content)
                code, error = self._cli("show", "coach")
                self.assertEqual(code, 1)
                self.assertIn("message", error["error"])
                code, error = self._cli("set", "coach", "--executable", str(self.fake_mcp),
                                        "--expected-revision", "irrelevant")
                self.assertEqual(code, 1)
                self.assertIn("message", error["error"])
                self.assertEqual(self.mcp_path.read_text(), content)


class StdioMcpProtocolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-proto-mcp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        initialize(self.config_path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.config_path)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        (self.workspace / ".omp").mkdir(mode=0o700)
        create(self.config, "coach", self.workspace)
        self.fake_mcp = self.root / "fake_tijian_mcp.py"
        self.fake_mcp.write_text(FAKE_MCP_SERVER_CODE)
        self.fake_mcp.chmod(0o755)

    def _interact(self, command, args):
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True
        )
        try:
            # 1. initialize
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
            proc.stdin.flush()
            init_res = json.loads(proc.stdout.readline())
            self.assertEqual(init_res["id"], 1)
            self.assertIn("capabilities", init_res["result"])

            # 2. initialized notification
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()

            # 3. tools/list
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
            proc.stdin.flush()
            tools_res = json.loads(proc.stdout.readline())
            self.assertEqual(tools_res["id"], 2)
            return [t["name"] for t in tools_res["result"]["tools"]]
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.terminate()
            proc.wait(timeout=2)

    def test_configured_read_only_mcp_server_exposes_only_read_tools(self):
        connect_tijian(self.config, "coach", self.fake_mcp, read_only=True)
        mcp_config = json.loads((self.workspace / ".omp/mcp.json").read_text())
        server = mcp_config["mcpServers"]["tijian"]
        tools = self._interact(server["command"], server["args"])
        self.assertIn("tijian_problem_get", tools)
        self.assertNotIn("tijian_problem_ensure", tools)

    def test_configured_read_write_mcp_server_exposes_write_tools(self):
        connect_tijian(self.config, "coach", self.fake_mcp, read_only=False)
        mcp_config = json.loads((self.workspace / ".omp/mcp.json").read_text())
        server = mcp_config["mcpServers"]["tijian"]
        tools = self._interact(server["command"], server["args"])
        self.assertIn("tijian_problem_get", tools)
        self.assertIn("tijian_problem_ensure", tools)

    def test_modern_policy_modes_expose_distinct_tool_sets(self):
        for permission, expected in (
                ("none", {"tijian_problem_get"}),
                ("managed", {"tijian_problem_get", "tijian_problem_ensure"}),
                ("full", {"tijian_problem_get", "tijian_problem_ensure", "tijian_database_reset"})):
            with self.subTest(permission=permission):
                revision = show_tijian(self.config, "coach")["revision"]
                set_tijian(self.config, "coach", expected_revision=revision,
                           executable=self.fake_mcp, permission=permission, confirm_full=permission == "full")
                native = json.loads((self.workspace / ".omp/mcp.json").read_text())
                server = native["mcpServers"]["tijian"]
                self.assertEqual(server["args"], [f"--write-policy={permission}"])
                self.assertEqual(set(self._interact(server["command"], server["args"])), expected)

    def test_paveway_has_no_python_bridge_or_schema_assembly(self):
        # Verify that Python bridge modules cannot be imported
        for mod in (
            "oicoach_runtime.integrations.tijian_catalog",
            "oicoach_runtime.integrations.tijian_mcp",
            "oicoach_runtime.integrations.tijian",
        ):
            with self.subTest(module=mod), self.assertRaises(ModuleNotFoundError):
                __import__(mod)


if __name__ == "__main__":
    unittest.main()
