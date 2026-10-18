import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime import service
from oicoach_runtime.workflow_setup import create, connect_tijian
from oicoach_runtime.workflows import WorkflowProvider
from dataclasses import replace


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config/config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def test_bundled_workflows_are_editable_private_and_render_data_once(self):
        for template in ("coach", "example"):
            created = create(self.config, template, self.workspace, template)
            workflow = WorkflowProvider(self.config.workflow_root).describe(template)
            text = workflow.render("wake", {"payload": {"message": "literal ${event_json}"}})
            self.assertIn("literal ${event_json}", text)
            directory = Path(created["path"])
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "event.md").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ConflictError):
                create(self.config, template, self.workspace, template)

    def test_invalid_workflows_do_not_overwrite_or_escape(self):
        for identifier, workspace in (("../escape", self.workspace), ("missing", self.root / "missing")):
            with self.assertRaises(RuntimeContractError):
                create(self.config, identifier, workspace)
        occupied = self.config.workflow_root / "occupied"
        occupied.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ConflictError):
            create(self.config, "occupied", self.workspace)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_project_mcp_merge_preserves_other_servers_and_rejects_conflicts(self):
        create(self.config, "coach", self.workspace)
        config = replace(self.config, tijian=replace(self.config.tijian, enabled=True))
        target = self.workspace / ".mcp.json"
        target.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}, "disabledServers": ["other"]}))
        connect_tijian(config, self.path, "coach")
        data = json.loads(target.read_text())
        self.assertEqual(data["mcpServers"]["other"], {"command": "other"})
        self.assertEqual(data["disabledServers"], ["other"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        connect_tijian(config, self.path, "coach")
        data["mcpServers"]["tijian"] = {"command": "existing"}
        target.write_text(json.dumps(data))
        with self.assertRaises(ConflictError):
            connect_tijian(config, self.path, "coach")
        self.assertEqual(json.loads(target.read_text()), data)

    def test_disabled_malformed_and_symlink_mcp_are_not_modified(self):
        create(self.config, "coach", self.workspace)
        with self.assertRaises(RuntimeContractError):
            connect_tijian(self.config, self.path, "coach")
        config = replace(self.config, tijian=replace(self.config.tijian, enabled=True))
        target = self.workspace / ".mcp.json"
        target.write_text("not json")
        with self.assertRaises(RuntimeContractError):
            connect_tijian(config, self.path, "coach")
        target.unlink()
        target.symlink_to(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(config, self.path, "coach")
        self.assertEqual(self.path.read_bytes(), before)

    def test_service_rejects_unit_injection_and_renders_venv_without_enabling(self):
        for name in ("../other.service", "other@instance.service", "x\nExecStart=bad.service"):
            with self.assertRaises(RuntimeContractError):
                service.unit_name(name)
        with patch.dict(os.environ, {"PATH": "/tmp/a%path:/usr/bin"}):
            text = service.render(self.path)
        self.assertIn("KillMode=control-group", text)
        self.assertIn("a%%path", text)
        self.assertIn("[Unit]\nDescription=", text)
        self.assertIn("StartLimitBurst=5", text)
        self.assertIn("-m", text)
        with self.assertRaises(RuntimeContractError):
            service.quote("bad\npath")

    def test_service_install_verifies_before_publish_and_preserves_unowned_units(self):
        unit_root = self.root / "units"
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemd-analyze":
                self.assertFalse((unit_root / service.DEFAULT_NAME).exists())
        with patch.object(service, "unit_directory", return_value=unit_root), patch.object(service, "_run", side_effect=execute):
            installed = service.install(self.path)
            self.assertFalse(installed["enabled_or_started"])
            self.assertFalse(any("enable" in command or "start" in command for command in calls))
            destination = Path(installed["path"])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            destination.write_text("# another application\n")
            with self.assertRaises(ConflictError):
                service.install(self.path, replace=True)
            with self.assertRaises(ConflictError):
                service.control(self.path, "uninstall")
            self.assertEqual(destination.read_text(), "# another application\n")

    def test_failed_service_verification_does_not_publish_a_unit(self):
        root = self.root / "units"
        with patch.object(service, "unit_directory", return_value=root), patch.object(service, "_run", side_effect=RuntimeContractError("invalid unit")):
            with self.assertRaises(RuntimeContractError):
                service.install(self.path)
        self.assertEqual(list(root.iterdir()), [])
