"""Opt-in: prove custom settings reach native OMP, rather than the user envelope."""

import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.config import load_config
from oicoach_runtime.customization import IDENTITY_PROMPT


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for native customization acceptance")
class RealCustomizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_system_prompt_append_replace_and_skill_discovery_without_model(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-custom-native-") as directory:
            root = Path(directory)
            config = replace(base, agent=replace(base.agent, tools_enabled=True, skills_enabled=True,
                extensions_enabled=False, rules_enabled=False, startup_timeout_seconds=30,
                ack_timeout_seconds=20, graceful_stop_seconds=1))
            marker = "SYSTEM_" + uuid.uuid4().hex
            for mode in ("append", "replace"):
                with self.subTest(mode=mode):
                    session = root / mode
                    session.mkdir(mode=0o700)
                    settings = {"schema_version": 1, "revision": 1, "system_prompt": {"mode": mode, "text": marker},
                                "skills": [{"name": category, "category": category, "description": "NATIVE_SKILL_DISCOVERY",
                                            "content": "BODY_" + category, "enabled": True}
                                           for category in ("oi-introduce", "user-information", "personal-skill")]}
                    settings["skills"].append({"name": "disabled-skill", "description": "DISABLED_DISCOVERY",
                                               "content": "DISABLED_BODY", "enabled": False})
                    adapter = OmpRpc(config)
                    try:
                        await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None,
                                             "customization": settings}, "native-" + mode)
                        await adapter.ready()
                        state = await adapter.state()
                        prompt = json.dumps(state["systemPrompt"], ensure_ascii=False)
                        self.assertIn(marker, prompt)
                        self.assertIn(IDENTITY_PROMPT, prompt)
                        self.assertIn("NATIVE_SKILL_DISCOVERY", prompt)
                        self.assertNotIn("DISABLED_DISCOVERY", prompt)
                        self.assertNotIn("DISABLED_BODY", prompt)
                        for category in ("oi-introduce", "user-information", "personal-skill"):
                            self.assertIn(category, prompt)
                            self.assertNotIn("BODY_" + category, prompt)
                        for label in ("OI Introduce", "user information", "personal skill"):
                            self.assertIn("[" + label + "]", prompt)
                    finally:
                        await adapter.stop()

    async def test_disabled_tools_or_skills_do_not_inject_a_fallback_body(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-custom-permissions-") as directory:
            root = Path(directory)
            for tools_enabled, skills_enabled in ((False, True), (True, False)):
                with self.subTest(tools=tools_enabled, skills=skills_enabled):
                    session = root / (str(tools_enabled) + str(skills_enabled))
                    session.mkdir(mode=0o700)
                    config = replace(base, agent=replace(base.agent, tools_enabled=tools_enabled,
                        skills_enabled=skills_enabled, extensions_enabled=False, rules_enabled=False,
                        startup_timeout_seconds=30, ack_timeout_seconds=20, graceful_stop_seconds=1))
                    settings = {"schema_version": 1, "revision": 1, "system_prompt": {"mode": "append", "text": "CUSTOM_ONLY"},
                                "skills": [{"name": "permission-probe", "description": "PERMISSION_SKILL_INDEX",
                                            "content": "PERMISSION_SKILL_BODY", "enabled": True}]}
                    adapter = OmpRpc(config)
                    try:
                        await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None,
                                             "customization": settings}, "permission-probe")
                        await adapter.ready()
                        prompt = json.dumps((await adapter.state())["systemPrompt"])
                        self.assertIn("CUSTOM_ONLY", prompt)
                        self.assertNotIn("PERMISSION_SKILL_BODY", prompt)
                        self.assertNotIn("PERMISSION_SKILL_INDEX", prompt)
                    finally:
                        await adapter.stop()

    async def test_real_model_follows_system_prompt_and_reads_user_skill_body(self):
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-custom-model-") as directory:
            root = Path(directory)
            session = root / "sessions"
            session.mkdir(mode=0o700)
            config = replace(base, agent=replace(base.agent, tools_enabled=True, skills_enabled=True,
                extensions_enabled=False, rules_enabled=False, startup_timeout_seconds=30,
                ack_timeout_seconds=20, turn_timeout_seconds=120, settle_timeout_seconds=30, graceful_stop_seconds=1))
            system_marker = "SYS_" + uuid.uuid4().hex
            skill_marker = "SKILL_" + uuid.uuid4().hex
            unrelated_marker = "UNRELATED_" + uuid.uuid4().hex
            settings = {"schema_version": 1, "revision": 1,
                "system_prompt": {"mode": "append", "text": "For this connection check, your final answer must include " + system_marker},
                "skills": [{"name": "acceptance-skill", "description": "Use when the user requests the customization acceptance check. Read the SKILL.md for the required verification token.",
                            "content": "For the customization acceptance check, reply with the following exact token in your final answer: " + skill_marker + ". Do not call any other tools or schedule reminders.", "enabled": True},
                           {"name": "unrelated-skill", "description": "Only use for an unrelated database migration task. Not applicable to customization acceptance checks.",
                            "content": "Unrelated task token: " + unrelated_marker, "enabled": True}]}
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None,
                                     "customization": settings}, "real-model")
                await adapter.ready()
                native_prompt = json.dumps((await adapter.state())["systemPrompt"])
                self.assertIn("acceptance-skill", native_prompt)
                self.assertNotIn(skill_marker, native_prompt)
                self.assertNotIn(unrelated_marker, native_prompt)
                status = await adapter.prompt("real-model", "Please use acceptance-skill to perform the customization acceptance check. Read the skill file and follow its instructions. Do not change files, save memories or schedule reminders.", lambda: None, lambda _: None)
                self.assertEqual(status, "completed", getattr(adapter, "failure_message", None))
                response = await adapter.command("get_last_assistant_text")
                self.assertIn(system_marker, response["text"])
                self.assertIn(skill_marker, response["text"])
                messages = await adapter.command("get_messages")
                self.assertTrue(any(m.get("role") == "toolResult" and skill_marker in json.dumps(m)
                                    for m in messages.get("messages", [])))
                self.assertFalse(any(m.get("role") == "toolResult" and unrelated_marker in json.dumps(m)
                                     for m in messages.get("messages", [])))
                session_file = (await adapter.state())["sessionFile"]
            finally:
                await adapter.stop()
            # Existing history stays, but the next process receives the new system prompt.
            second_marker = "UPDATED_" + uuid.uuid4().hex
            settings["revision"] = 2
            settings["system_prompt"] = {"mode": "replace", "text": "For this acceptance check return exactly " + second_marker + ". Do not use tools."}
            settings["skills"][0]["enabled"] = False
            settings["skills"][1]["enabled"] = False
            resumed = OmpRpc(config)
            try:
                await resumed.start({"workspace": str(root), "session_dir": str(session), "session_file": session_file,
                                     "customization": settings}, "real-model-updated")
                await resumed.ready()
                native_prompt = json.dumps((await resumed.state())["systemPrompt"])
                self.assertIn(second_marker, native_prompt)
                self.assertNotIn(system_marker, native_prompt)
                status = await resumed.prompt("real-model-updated", "Please perform the next check according to the current system prompt.", lambda: None, lambda _: None)
                self.assertEqual(status, "completed", getattr(resumed, "failure_message", None))
                response = await resumed.command("get_last_assistant_text")
                self.assertEqual(response["text"].strip(), second_marker)
            finally:
                await resumed.stop()
