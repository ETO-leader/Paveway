import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from oicoach_runtime import customization as custom
from oicoach_runtime.context_budget import CONTEXT_PROMPT

RUNTIME_PROMPT = custom.IDENTITY_PROMPT + "\n\n" + CONTEXT_PROMPT
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, WorkflowError
from oicoach_runtime.workflow_setup import create
from oicoach_runtime.workflows import WorkflowProvider


class CustomizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        create(self.config, "coach", self.root)
        create(self.config, "other", self.root)
        self.directory = self.config.workflow_root / "coach"

    def update(self, operation, **values):
        revision = custom.show(self.config, "coach")["settings"]["revision"]
        return custom.update(self.config, "coach", revision, operation, **values)["settings"]

    def test_settings_persist_scope_and_do_not_change_coach_template_or_revision(self):
        template = (self.directory / "event.md").read_bytes()
        value = "用户原文 $HOME ${event_json}\n第二行 `no shell`"
        settings = self.update("prompt", mode="append", content=value)
        self.assertEqual(settings["system_prompt"]["text"], value)
        self.assertEqual((self.directory / "customization.json").stat().st_mode & 0o777, 0o600)
        loaded = WorkflowProvider(self.config.workflow_root).describe("coach")
        self.assertEqual(loaded.customization, settings)
        self.assertEqual(loaded.revision, "1")
        self.assertEqual((self.directory / "event.md").read_bytes(), template)
        self.assertEqual(custom.show(self.config, "other")["settings"]["revision"], 0)

    def test_skills_crud_keeps_disabled_state_and_reset_keeps_skills(self):
        self.update("skill-put", name="my-skill", description="什么时候使用", content="正文")
        self.update("skill-disable", name="my-skill")
        settings = self.update("skill-put", name="my-skill", description="新用途", content="新正文")
        self.assertFalse(settings["skills"][0]["enabled"])
        self.update("skill-enable", name="my-skill")
        self.update("prompt", mode="replace", content="替换系统提示")
        settings = self.update("prompt", mode="append", content="")
        self.assertEqual(len(settings["skills"]), 1)
        self.assertTrue(settings["skills"][0]["enabled"])
        self.assertEqual(self.update("skill-delete", name="my-skill")["skills"], [])

    def test_concurrent_saves_use_revision_cas(self):
        def save(value):
            try:
                custom.update(self.config, "coach", 0, "prompt", mode="append", content=value)
                return "saved"
            except ConflictError:
                return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(save, ("first", "second"))), ["saved", "conflict"])

    def test_invalid_content_and_symlinks_never_overwrite_settings(self):
        self.update("prompt", mode="append", content="保留")
        before = (self.directory / "customization.json").read_bytes()
        for operation, values in (("prompt", {"mode": "replace", "content": ""}),
                                  ("prompt", {"mode": "append", "content": "x" * 65537}),
                                  ("skill-put", {"name": "../escape", "description": "desc", "content": "body"}),
                                  ("skill-put", {"name": "valid", "description": "", "content": "body"})):
            with self.subTest(operation=operation, values=list(values)):
                with self.assertRaises(WorkflowError):
                    self.update(operation, **values)
                self.assertEqual((self.directory / "customization.json").read_bytes(), before)
        path = self.directory / "customization.json"
        path.unlink()
        path.symlink_to(self.path)
        with self.assertRaises(WorkflowError):
            custom.load(self.directory)

    def test_dispatch_snapshots_are_immutable_and_native_loading_respects_disabled_skills(self):
        self.update("prompt", mode="append", content="旧系统提示")
        settings = self.update("skill-put", name="native-skill", description="原生用途", content="原生正文")
        session = self.root / "session"
        session.mkdir(mode=0o700)
        argv = custom.snapshot(settings, session, "run1", True)
        self.assertIn("--append-system-prompt", argv)
        old_prompt = Path(argv[1]).read_text()
        self.assertEqual(old_prompt, "旧系统提示\n\n" + RUNTIME_PROMPT)
        self.assertNotIn("原生正文", old_prompt)
        overlay = json.loads(Path(argv[3]).read_text())
        skill = Path(overlay["skills"]["customDirectories"][0]) / "native-skill/SKILL.md"
        self.assertIn("原生正文", skill.read_text())
        self.update("prompt", mode="replace", content="新系统提示")
        self.assertEqual(Path(argv[1]).read_text(), old_prompt)
        self.assertEqual(skill.stat().st_mode & 0o777, 0o600)
        latest = custom.show(self.config, "coach")["settings"]
        disabled = custom.snapshot(latest, session, "run2", False)
        self.assertEqual(disabled[0], "--system-prompt")
        self.assertNotIn("--config", disabled)
        self.assertEqual(len(latest["skills"]), 1)

    def test_default_identity_does_not_modify_settings_or_event_template(self):
        settings = custom.show(self.config, "coach")["settings"]
        self.assertEqual(settings["revision"], 0)
        before = json.dumps(settings)
        event = self.directory / "event.md"
        original = event.read_bytes()
        session = self.root / "identity-session"
        session.mkdir(mode=0o700)
        argv = custom.snapshot(settings, session, "identity", False)
        self.assertEqual(argv[0], "--append-system-prompt")
        self.assertEqual(Path(argv[1]).read_text(), RUNTIME_PROMPT)
        self.assertEqual(event.read_bytes(), original)
        self.assertEqual(json.dumps(settings), before)

    def test_three_categories_offer_native_files_without_inlining_bodies(self):
        for index, category in enumerate(reversed(custom.SKILL_CATEGORIES)):
            self.update("skill-put", name="skill-" + str(index), category=category,
                        description="category description", content="BODY_" + category + " ${literal}")
        self.update("skill-put", name="disabled", category="oi-introduce", description="disabled", content="DO_NOT_LOAD")
        self.update("skill-disable", name="disabled")
        session = self.root / "all-categories"
        session.mkdir(mode=0o700)
        for mode in ("append", "replace"):
            settings = self.update("prompt", mode=mode, content="CUSTOM_SYSTEM")
            argv = custom.snapshot(settings, session, mode, True)
            self.assertEqual(argv[0], "--system-prompt" if mode == "replace" else "--append-system-prompt")
            prompt = Path(argv[1]).read_text()
            self.assertEqual(prompt, "CUSTOM_SYSTEM\n\n" + RUNTIME_PROMPT)
            for category in custom.SKILL_CATEGORIES:
                self.assertNotIn("BODY_" + category, prompt)
            self.assertNotIn("DO_NOT_LOAD", prompt)
            native = Path(json.loads(Path(argv[3]).read_text())["skills"]["customDirectories"][0])
            self.assertEqual(len(list(native.glob("*/SKILL.md"))), 3)
            for item in settings["skills"]:
                path = native / item["name"] / "SKILL.md"
                if item["enabled"]:
                    body = path.read_text()
                    self.assertIn("[" + custom.SKILL_CATEGORIES[item["category"]] + "] category description", body)
                    self.assertTrue(body.endswith(item["content"]))
                else:
                    self.assertFalse(path.exists())
        disabled = custom.snapshot(settings, session, "globally-disabled", False)
        self.assertEqual(Path(disabled[1]).read_text(), "CUSTOM_SYSTEM\n\n" + RUNTIME_PROMPT)

    def test_legacy_category_reading_preserves_records_and_edits_preserve_category(self):
        settings = self.update("skill-put", name="old-skill", description="old", content="legacy body")
        settings["skills"][0].pop("category")
        before = json.dumps(settings, ensure_ascii=False)
        custom.validate(settings)
        self.assertEqual(json.dumps(settings, ensure_ascii=False), before)
        self.assertEqual(custom.skill_category(settings["skills"][0]), "personal-skill")
        path = self.directory / "customization.json"
        path.write_text(before)
        loaded = custom.load(self.directory)
        session = self.root / "legacy-session"
        session.mkdir(mode=0o700)
        argv = custom.snapshot(loaded, session, "legacy", True)
        self.assertIn("--append-system-prompt", argv)
        native = Path(json.loads(Path(argv[3]).read_text())["skills"]["customDirectories"][0])
        self.assertIn("[personal skill] old", (native / "old-skill/SKILL.md").read_text())
        self.assertEqual((session / "customizations/legacy/system.md").read_text(), RUNTIME_PROMPT)
        self.assertEqual(path.read_text(), before)
        self.update("skill-disable", name="old-skill")
        settings = self.update("skill-put", name="old-skill", category="user-information", description="profile", content="new")
        self.assertFalse(settings["skills"][0]["enabled"])
        settings = self.update("skill-put", name="old-skill", description="edit", content="edited")
        self.assertEqual(settings["skills"][0]["category"], "user-information")
        saved = path.read_bytes()
        with self.assertRaises(WorkflowError):
            self.update("skill-put", name="old-skill", category="../../escape", description="bad", content="bad")
        self.assertEqual(path.read_bytes(), saved)
