import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import customization
from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import config_text, load_config
from oicoach_runtime.errors import ConfigError, ConflictError, RuntimeContractError, WorkflowError
from oicoach_runtime.model_selection import catalog
from oicoach_runtime.workflow_setup import create


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pw-model-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(self.path)
        self.config = replace(config, agent=replace(config.agent,
            executable=str(Path(__file__).with_name("fake_omp.py").resolve()), model="fake/base", thinking="medium"))
        create(self.config, "coach", self.root, "example")
        create(self.config, "other", self.root, "example")

    def test_inference_persists_with_cas_and_preserves_other_settings(self):
        legacy = customization.show(self.config, "coach")
        self.assertNotIn("inference", legacy["settings"])
        self.assertEqual(legacy["inference"], {"model": "fake/base", "thinking": "medium"})
        report = customization.update(self.config, "coach", 0, "inference", model="fake/coach", thinking="high")
        self.assertEqual(report["inference"], {"model": "fake/coach", "thinking": "high"})
        customization.update(self.config, "coach", 1, "prompt", mode="append", content="keep prompt")
        with self.assertRaises(ConflictError):
            customization.update(self.config, "coach", 1, "inference", thinking="low")
        report = customization.update(self.config, "coach", 2, "inference", thinking="low")
        self.assertEqual(report["settings"]["system_prompt"]["text"], "keep prompt")
        self.assertEqual(report["inference"], {"model": "fake/coach", "thinking": "low"})
        self.assertEqual(customization.show(self.config, "other")["inference"], legacy["inference"])
        report = customization.update(self.config, "coach", 3, "inference", model="", thinking="")
        self.assertEqual(report["inference"], legacy["inference"])

    def test_invalid_inference_cannot_modify_saved_settings(self):
        path = self.config.workflow_root / "coach/customization.json"
        customization.update(self.config, "coach", 0, "prompt", mode="append", content="keep")
        before = path.read_bytes()
        for values in ({}, {"model": "--no-tools"}, {"model": "fake/coach --no-tools"},
                       {"model": "x" * 257}, {"thinking": "ultra"}, {"thinking": []}):
            with self.subTest(values=values), self.assertRaises(WorkflowError):
                customization.update(self.config, "coach", 1, "inference", **values)
            self.assertEqual(path.read_bytes(), before)

    def test_config_thinking_roundtrip_and_validation(self):
        self.path.write_text(config_text(self.config))
        self.assertEqual(load_config(self.path).agent.thinking, "medium")
        self.path.write_text(config_text(replace(self.config, agent=replace(self.config.agent, thinking="ultra"))))
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_catalog_uses_native_models_and_supported_thinking_levels(self):
        report = catalog(self.config, "coach")
        self.assertEqual([m["selector"] for m in report["models"]], ["fake/coach", "fake/fast"])
        self.assertEqual(report["models"][0]["thinking"], ["low", "medium", "high"])
        self.assertEqual(report["models"][1]["thinking"], [])
        self.assertEqual(report["settings"]["revision"], 0)

    def test_catalog_errors_are_readonly_and_do_not_expose_process_output(self):
        for process in (subprocess.CompletedProcess([], 1, b"PRIVATE_SECRET", b"PRIVATE_SECRET"),
                        subprocess.CompletedProcess([], 0, b'{"models":null}', b"")):
            with patch("oicoach_runtime.model_selection.subprocess.run", return_value=process):
                with self.assertRaises(RuntimeContractError) as failure:
                    catalog(self.config, "coach")
                self.assertNotIn("PRIVATE_SECRET", str(failure.exception))
        with patch("oicoach_runtime.model_selection.subprocess.run", side_effect=subprocess.TimeoutExpired([], 20)):
            with self.assertRaises(RuntimeContractError):
                catalog(self.config, "coach")
        self.assertEqual(customization.show(self.config, "coach")["settings"]["revision"], 0)


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for native model selection")
class NativeModelSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_and_thinking_flags_reach_native_omp_without_model_turn(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-native-model-") as directory:
            root = Path(directory)
            config = replace(base, state_dir=root / "state", workflow_root=root / "workflows",
                agent=replace(base.agent, tools_enabled=False, skills_enabled=False,
                    extensions_enabled=False, rules_enabled=False, graceful_stop_seconds=1))
            config.workflow_root.mkdir(mode=0o700)
            create(config, "probe", root, "example")
            models = catalog(config, "probe")["models"]
            candidates = [m for m in models if "high" in m["thinking"] and "low" in m["thinking"]]
            self.assertTrue(candidates)
            model = next((m for m in candidates if m["selector"].startswith("oi-relay/")), candidates[0])
            for level in ("high", "low"):
                session = root / level
                session.mkdir(mode=0o700)
                settings = {"schema_version": 1, "revision": 1, "system_prompt": {"mode": "append", "text": ""},
                            "skills": [], "inference": {"model": model["selector"], "thinking": level}}
                adapter = OmpRpc(config)
                try:
                    await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None,
                                         "customization": settings}, "probe-" + level)
                    await adapter.ready()
                    state = await adapter.state()
                    self.assertEqual(state["model"]["provider"] + "/" + state["model"]["id"], model["selector"])
                    self.assertEqual(state["thinkingLevel"], level)
                finally:
                    await adapter.stop()
