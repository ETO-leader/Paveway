import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError

from oicoach_runtime.cli import initialize, main
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError, WorkflowError
from oicoach_runtime import service
from oicoach_runtime.workflow_setup import create, connect_tijian, describe
from oicoach_runtime.workflows import Workflow, WorkflowProvider
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
        self.fake_mcp = self.root / "tijian-mcp"
        self.fake_mcp.touch()
        self.fake_mcp.chmod(0o755)
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

    def test_builtin_skill_defaults_are_explicit_and_independent_of_tijian(self):
        for template, names in (("coach", ["paveway-tijian"]), ("example", [])):
            with self.subTest(template=template):
                created = create(self.config, template, self.workspace, template)
                workflow = WorkflowProvider(self.config.workflow_root).describe(template)
                self.assertEqual(workflow.builtin_skills, tuple(names))
                self.assertEqual(created["workflow"]["builtin_skills"], names)
                self.assertEqual(describe(workflow)["builtin_skills"], names)
                with self.assertRaises(FrozenInstanceError):
                    workflow.builtin_skills = ()
        positional = Workflow("legacy", "1", self.workspace, "per_task", {}, False, {}, False)
        self.assertEqual(positional.builtin_skills, ())

    def test_legacy_manifest_and_event_bytes_remain_untouched(self):
        for template in ("coach", "example"):
            created = create(self.config, template, self.workspace, template)
            directory = Path(created["path"])
            manifest = directory / "workflow.toml"
            event = directory / "event.md"
            original_manifest = manifest.read_bytes().replace(b'builtin_skills = ["paveway-tijian"]\n', b"")
            manifest.write_bytes(original_manifest)
            original_event = event.read_bytes()
            workflow = WorkflowProvider(self.config.workflow_root).describe(template)
            self.assertEqual(workflow.builtin_skills, ())
            self.assertEqual(describe(workflow)["builtin_skills"], [])
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertEqual(event.read_bytes(), original_event)
    def test_builtin_skill_manifest_validation_rejects_invalid_declarations(self):
        created = create(self.config, "invalid", self.workspace, "example")
        manifest = Path(created["path"]) / "workflow.toml"
        original = manifest.read_text()
        declarations = (
            '"paveway-tijian"', 'true', '1', '{}',
            '[1]', '[true]', '[{}]', '[["paveway-tijian"]]',
            '[""]', '["../paveway-tijian"]', '["bad name"]',
            '["not-a-registered-skill"]', '["paveway-tijian", "paveway-tijian"]',
        )
        for value in declarations:
            with self.subTest(value=value):
                manifest.write_text(original + f"builtin_skills = {value}\n")
                with self.assertRaisesRegex(WorkflowError, "(?i)(builtin|skill)"):
                    WorkflowProvider(self.config.workflow_root).describe("invalid")

    def test_builtin_skills_are_workflow_scoped_and_cli_diagnostics_report_names(self):
        create(self.config, "coach", self.workspace)
        example = create(self.config, "example", self.workspace, "example")
        example_manifest = Path(example["path"]) / "workflow.toml"
        example_manifest.write_text(example_manifest.read_text() + "builtin_skills = []\n")
        provider = WorkflowProvider(self.config.workflow_root)
        self.assertEqual(provider.describe("coach").builtin_skills, ("paveway-tijian",))
        self.assertEqual(provider.describe("example").builtin_skills, ())
        for operation in (["show", "coach"], ["list"]):
            with self.subTest(operation=operation), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["workflow", *operation, "--config", str(self.path), "--json"]), 0)
                report = json.loads(output.getvalue())
                if operation[0] == "show":
                    self.assertEqual(report["workflow"]["builtin_skills"], ["paveway-tijian"])
                else:
                    by_id = {item["id"]: item["builtin_skills"] for item in report["workflows"]}
                    self.assertEqual(by_id, {"coach": ["paveway-tijian"], "example": []})

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
        target = self.workspace / ".omp/mcp.json"
        target.parent.mkdir(mode=0o700, exist_ok=True)
        target.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}, "disabledServers": ["other"]}))
        connect_tijian(self.config, "coach", self.fake_mcp)
        data = json.loads(target.read_text())
        self.assertEqual(data["mcpServers"]["other"], {"command": "other"})
        self.assertEqual(data["disabledServers"], ["other"])
        self.assertEqual(data["mcpServers"]["tijian"], {
            "command": str(self.fake_mcp.resolve()),
            "args": ["--read-only"],
        })
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(json.loads(target.read_text()), data)
        target.write_text(json.dumps({"mcpServers": {"tijian": {"command": "existing"}}}))
        before = target.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(target.read_bytes(), before)
        target.write_text(json.dumps({"mcpServers": {}, "disabledServers": ["tijian"]}))
        before = target.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(target.read_bytes(), before)

    def test_explicit_read_write_omits_read_only_flag(self):
        create(self.config, "coach", self.workspace)
        target = self.workspace / ".omp/mcp.json"
        target.parent.mkdir(mode=0o700, exist_ok=True)
        connect_tijian(self.config, "coach", self.fake_mcp, read_only=False)
        data = json.loads(target.read_text())
        self.assertEqual(data["mcpServers"]["tijian"], {
            "command": str(self.fake_mcp.resolve()),
            "args": [],
        })

    def test_disabled_malformed_and_symlink_mcp_are_not_modified(self):
        create(self.config, "coach", self.workspace)
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", self.root / "missing-mcp")
        non_exec = self.root / "non-exec-mcp"
        non_exec.touch()
        non_exec.chmod(0o644)
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", non_exec)
        target = self.workspace / ".omp/mcp.json"
        target.parent.mkdir(mode=0o700, exist_ok=True)
        target.write_text("not json")
        with self.assertRaises(WorkflowError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(target.read_text(), "not json")
        target.unlink()
        target.symlink_to(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(ConflictError):
            connect_tijian(self.config, "coach", self.fake_mcp)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_connect_tijian_flags_and_mutual_exclusion(self):
        create(self.config, "coach", self.workspace)
        target = self.workspace / ".omp/mcp.json"
        target.parent.mkdir(mode=0o700, exist_ok=True)
        code = main(["workflow", "connect-tijian", "coach", "--executable", str(self.fake_mcp), "--config", str(self.path), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(target.read_text())
        self.assertEqual(data["mcpServers"]["tijian"]["args"], ["--read-only"])
        with self.assertRaises(SystemExit):
            main(["workflow", "connect-tijian", "coach", "--executable", str(self.fake_mcp), "--read-only", "--read-write", "--config", str(self.path)])
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

    def test_service_environment_capture_is_explicit_private_and_preserved(self):
        root = self.root / "units"
        with patch.object(service, "unit_directory", return_value=root), patch.object(service, "_run"), \
             patch.dict(os.environ, {"TEST_MODEL_KEY": 'secret $ % \\ " value', "TEST_MODEL_URL": "https://example.invalid"}):
            installed = service.install(self.path, capture_env=["TEST_MODEL_KEY"])
            env = Path(installed["environment_file"])
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("TEST_MODEL_URL=", env.read_text())
            self.assertNotIn("secret", Path(installed["path"]).read_text() + json.dumps(installed))
            service.install(self.path, capture_env=["TEST_MODEL_URL"])
            self.assertIn("TEST_MODEL_KEY=", env.read_text())
            self.assertIn("TEST_MODEL_URL=", env.read_text())
            before = env.read_bytes()
            with self.assertRaises(RuntimeContractError):
                service.install(self.path, capture_env=["UNSET_PRIVATE_TEST_VARIABLE"])
            self.assertEqual(env.read_bytes(), before)
            env.chmod(0o644)
            with self.assertRaises(ConflictError):
                service.install(self.path, capture_env=["TEST_MODEL_KEY"])

    def test_failed_verification_does_not_capture_credentials(self):
        with patch.object(service, "unit_directory", return_value=self.root / "units"), \
             patch.object(service, "_run", side_effect=RuntimeContractError("invalid unit")), \
             patch.dict(os.environ, {"TEST_MODEL_KEY": "private"}):
            with self.assertRaises(RuntimeContractError):
                service.install(self.path, capture_env=["TEST_MODEL_KEY"])
        self.assertFalse(service.environment_path(self.path).exists())
