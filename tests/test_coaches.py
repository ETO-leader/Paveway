import json
from pathlib import Path
import tempfile
import unittest

from oicoach_runtime import coaches, customization, git_sync
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError
from oicoach_runtime.frontend import Frontend
from oicoach_runtime.memory import Memory
from oicoach_runtime.prompt_composer import compose, manifest
from oicoach_runtime.store import Store
from oicoach_runtime.workflow_setup import create
from oicoach_runtime.workflows import WorkflowProvider


class CoachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-profiles-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / "config.toml"
        initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(path)
        create(self.config, "source", self.root, "example")
        self.store = Store(self.config)
        self.addCleanup(self.store.close)

    def test_same_workflow_profiles_keep_prompt_skills_memory_and_sessions_separate(self):
        customization.update(self.config, "source", 0, "skill-put", name="private-source",
                             description="private", content="PRIVATE_SOURCE_SKILL")
        a = coaches.create(self.config, "alpha", "source", "ALPHA_PERSONA {{literal}}")
        b = coaches.create(self.config, "beta", "source", "BETA_PERSONA")
        self.assertNotEqual(a["runtime_workflow_id"], b["runtime_workflow_id"])
        self.assertEqual(a["workflow_source"], b["workflow_source"])
        one = WorkflowProvider(self.config.workflow_root).describe(a["runtime_workflow_id"])
        two = WorkflowProvider(self.config.workflow_root).describe(b["runtime_workflow_id"])
        self.assertEqual(one.customization["skills"], [])
        self.assertIn("ALPHA_PERSONA {{literal}}", compose(one.customization))
        self.assertNotIn("BETA_PERSONA", compose(one.customization))
        self.assertNotIn("ALPHA_PERSONA", compose(two.customization))
        Memory(self.store).put(a["runtime_workflow_id"], "private", "private", "ALPHA_MEMORY")
        self.assertEqual(Memory(self.store).context(b["runtime_workflow_id"], "training")["notes"], [])
        value = coaches.state(self.store, "alpha", "progress", value={"step": "review"}, expected_version=0)
        self.assertEqual(json.loads(value["content"]), {"step": "review"})
        self.assertEqual(coaches.state(self.store, "alpha", "progress")["version"], 1)
        with self.assertRaises(ConflictError):
            coaches.state(self.store, "alpha", "progress", value={"step": "stale"}, expected_version=0)
        self.assertEqual(Memory(self.store).context(b["runtime_workflow_id"], "training")["notes"], [])
        frontend = Frontend(self.store)
        first = frontend.begin(a["runtime_workflow_id"], "training", self.root, settings=one.customization)
        second = frontend.begin(b["runtime_workflow_id"], "training", self.root, settings=two.customization)
        self.assertNotEqual(first["session_dir"], second["session_dir"])
        self.assertEqual(first["coach_id"], "alpha")
        with self.assertRaises(ConflictError): coaches.create(self.config, "alpha", "source")

    def test_persona_update_requires_new_conversation_and_preserves_original_config(self):
        profile = coaches.create(self.config, "alpha", "source", "ORIGINAL_PERSONA")
        workflow_id = profile["runtime_workflow_id"]
        settings = customization.show(self.config, workflow_id)["settings"]
        frontend = Frontend(self.store)
        lease = frontend.begin(workflow_id, "training", self.root, settings=settings)
        owner = {key: lease[key] for key in ("conversation_id", "generation", "owner_token")}
        frontend.finish(**owner, exit_code=0)
        updated = customization.update(self.config, workflow_id, 1, "persona", content="UPDATED_PERSONA")["settings"]
        with self.assertRaises(ConflictError):
            frontend.begin(workflow_id, "training", self.root, conversation_id=lease["conversation_id"], settings=updated)
        newer = frontend.begin(workflow_id, "training", self.root, new=True, settings=updated)
        self.assertNotEqual(newer["conversation_id"], lease["conversation_id"])
        self.assertEqual(self.store.metadata(lease["config_key"])["settings"]["persona"], "ORIGINAL_PERSONA")
        backup = self.config.workflow_root / workflow_id / "customization.before-profile-v1.json"
        self.assertEqual(json.loads(backup.read_text())["persona"], "ORIGINAL_PERSONA")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ConflictError):
            customization.update(self.config, workflow_id, 1, "persona", content="STALE")

    def test_shared_composer_records_source_versions_and_portable_profiles(self):
        profile = coaches.create(self.config, "alpha", "source", "PERSONA_RAW_${event_json}_{{literal}}")
        settings = customization.show(self.config, profile["runtime_workflow_id"])["settings"]
        root = self.root / "snapshot"
        root.mkdir(mode=0o700)
        for mode in ("append", "replace"):
            raw = {**settings, "system_prompt": {"mode": mode, "text": "USER_RAW_{{literal}}"}}
            before = json.dumps(raw)
            argv = customization.snapshot(raw, root, mode, True, workflow_instructions="WORKFLOW_MODE")
            self.assertEqual(argv[0], "--system-prompt")
            self.assertNotIn("--system-prompt-template", argv)
            self.assertIn("USER_RAW_{{literal}}", Path(argv[1]).read_text())
            self.assertEqual(json.dumps(raw), before)
            version = json.loads((Path(argv[1]).parent / "prompt-manifest.json").read_text())
            self.assertEqual(version, manifest(raw, compose(raw, "WORKFLOW_MODE")))
        git_sync.ensure_local(self.config)
        self.assertEqual(git_sync.cycle(self.config)["state"], "ok")
        repository = Path(git_sync.settings(self.config)["repository"])
        portable = git_sync.read_profiles(repository)[profile["runtime_workflow_id"]]
        record = portable[git_sync.heads(portable)[0]]
        self.assertEqual(record["content"]["persona"], settings["persona"])
        self.assertEqual(record["content"]["coach"]["id"], "alpha")
