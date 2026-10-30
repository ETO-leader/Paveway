"""Opt-in: prove custom settings reach native OMP, rather than the user envelope."""

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.builtin_skills import registry
from oicoach_runtime.config import load_config
from oicoach_runtime.customization import IDENTITY_PROMPT
from oicoach_runtime.proxy import child_environment


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for native customization acceptance")
class RealCustomizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_template_expands_dynamic_capabilities_without_model(self):
        """M0 D5: inspect OMP's own template expansion, not a fake renderer."""
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        with tempfile.TemporaryDirectory(prefix="pw-template-native-") as directory:
            root = Path(directory)
            skill = root / "skills" / "template-probe"
            skill.mkdir(parents=True, mode=0o700)
            (skill / "SKILL.md").write_text("---\nname: template-probe\n"
                "description: M0_TEMPLATE_SKILL_INDEX\n---\nM0_TEMPLATE_SKILL_PRIVATE_BODY\n")
            template = root / "system.hbs"
            template.write_text("You are GPT. M0_TEMPLATE_IDENTITY\n"
                "{{#each toolInfo}}M0_TOOL_{{name}}\n{{/each}}"
                "{{#each skills}}M0_SKILL_{{name}}: {{description}}\n{{/each}}")
            overlay = root / "config.yml"
            overlay.write_text(json.dumps({"skills": {"customDirectories": [str(skill.parent)]},
                "startup": {"setupWizard": False}, "autoResume": False}))
            for enabled in (True, False):
                with self.subTest(capabilities_enabled=enabled):
                    argv = [base.agent.executable, "--cwd", str(root), "--session-dir", str(root / str(enabled)),
                        "--mode", "rpc", "--no-ui", "--no-title", "--no-extensions", "--no-rules",
                        "--system-prompt-template", str(template), "--config", str(overlay)]
                    argv += ["--tools", "read"] if enabled else ["--no-tools", "--no-skills"]
                    if base.agent.profile:
                        argv += ["--profile", base.agent.profile]
                    process = await asyncio.create_subprocess_exec(*argv, cwd=root, env=child_environment(None),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, limit=1048576)
                    stderr = asyncio.create_task(process.stderr.read())
                    try:
                        async with asyncio.timeout(40):
                            async def frame_until(predicate):
                                while line := await process.stdout.readline():
                                    frame = json.loads(line)
                                    if predicate(frame):
                                        return frame
                                self.fail("native template probe closed before state was available")
                            await frame_until(lambda frame: frame.get("type") == "ready")
                            process.stdin.write(b'{"type":"get_state","id":"template-state"}\n')
                            await process.stdin.drain()
                            response = await frame_until(lambda frame: frame.get("id") == "template-state")
                            self.assertTrue(response.get("success"))
                            prompt = json.dumps(response["data"]["systemPrompt"], ensure_ascii=False)
                            self.assertIn("M0_TEMPLATE_IDENTITY", prompt)
                            self.assertNotIn("You are omp's trusted coding assistant", prompt)
                            self.assertNotIn("M0_TEMPLATE_SKILL_PRIVATE_BODY", prompt)
                            self.assertNotIn("M0_TOOL_bash", prompt)
                            self.assertNotIn("{{#", prompt)
                            for marker in ("M0_TOOL_read", "M0_SKILL_template-probe", "M0_TEMPLATE_SKILL_INDEX"):
                                self.assertEqual(marker in prompt, enabled, marker)
                    finally:
                        if process.returncode is None:
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                        await stderr

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
                        self.assertNotIn("You are omp's trusted coding assistant", prompt)
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

    async def test_native_builtin_and_user_skills_read_without_model(self):
        """Only boot, inspect, and invoke `omp read`; never submit a prompt."""
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        builtin = registry()["paveway-tijian"]
        with tempfile.TemporaryDirectory(prefix="pw-builtin-native-") as directory:
            root = Path(directory)
            session = root / "sessions"
            session.mkdir(mode=0o700)
            config = replace(base, agent=replace(base.agent, tools_enabled=True, skills_enabled=True,
                extensions_enabled=False, rules_enabled=False, startup_timeout_seconds=30,
                ack_timeout_seconds=20, graceful_stop_seconds=1))
            # This earlier overlay must survive the generated snapshot's array.
            ambient = root / "ambient-skills" / "ambient-native-probe"
            ambient.mkdir(parents=True, mode=0o700)
            (ambient / "SKILL.md").write_text(
                "---\nname: ambient-native-probe\ndescription: AMBIENT_NATIVE_INDEX\n---\nAMBIENT_NATIVE_BODY\n",
                encoding="utf-8")
            overlay = root / "ambient.yml"
            overlay.write_text(json.dumps({"skills": {"customDirectories": [str(ambient.parent)]}}),
                               encoding="utf-8")
            prior_overlays = os.pathsep.join(filter(None, (os.environ.get("PI_CONFIG_FILES"), str(overlay))))
            settings = {"schema_version": 1, "revision": 1,
                "system_prompt": {"mode": "append", "text": "BUILTIN_NATIVE_SYSTEM"},
                "skills": [{"name": "builtin-user-probe", "description": "BUILTIN_USER_NATIVE_INDEX",
                            "content": "BUILTIN_USER_NATIVE_BODY", "enabled": True},
                           {"name": "builtin-disabled-probe", "description": "BUILTIN_DISABLED_NATIVE_INDEX",
                            "content": "BUILTIN_DISABLED_NATIVE_BODY", "enabled": False}]}
            adapter = OmpRpc(config)
            with patch.dict(os.environ, {"PI_CONFIG_FILES": prior_overlays}):
                try:
                    await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None,
                        "customization": settings, "builtin_skills": ("paveway-tijian",)}, "native-builtin")
                    await adapter.ready()
                    prompt = json.dumps((await adapter.state())["systemPrompt"], ensure_ascii=False)
                    self.assertIn(builtin.name, prompt)
                    self.assertIn("BUILTIN_USER_NATIVE_INDEX", prompt)
                    self.assertIn("AMBIENT_NATIVE_INDEX", prompt)
                    self.assertNotIn(json.dumps(builtin.body.strip(), ensure_ascii=False)[1:-1], prompt)
                    self.assertNotIn("BUILTIN_USER_NATIVE_BODY", prompt)
                    self.assertNotIn("AMBIENT_NATIVE_BODY", prompt)
                    self.assertNotIn("BUILTIN_DISABLED_NATIVE_INDEX", prompt)
                    self.assertNotIn("BUILTIN_DISABLED_NATIVE_BODY", prompt)
                    # `read` is OMP's real native tool entry point, not a model
                    # prompt or a filesystem read pretending to be native.
                    snapshot = session / "customizations" / "native-builtin"
                    environment = child_environment(None)
                    environment["PI_CONFIG_FILES"] = prior_overlays + os.pathsep + str(snapshot / "omp.yml")
                    if config.agent.profile:
                        environment["OMP_PROFILE"] = config.agent.profile
                    for name, expected in (
                        (builtin.name, builtin.markdown),
                        ("builtin-user-probe", (snapshot / "skills" / "builtin-user-probe" / "SKILL.md").read_text(encoding="utf-8")),
                    ):
                        process = await asyncio.create_subprocess_exec(
                            config.agent.executable, "read", "skill://" + name + ":raw",
                            cwd=root, env=environment, stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        try:
                            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
                        finally:
                            if process.returncode is None:
                                process.kill()
                                await process.wait()
                        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                        self.assertIn(expected, stdout.decode("utf-8"))
                    self.assertEqual((await adapter.command("get_messages"))["messages"], [])
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
                                             "customization": settings, "builtin_skills": ("paveway-tijian",)}, "permission-probe")
                        await adapter.ready()
                        prompt = json.dumps((await adapter.state())["systemPrompt"], ensure_ascii=False)
                        self.assertIn("CUSTOM_ONLY", prompt)
                        self.assertNotIn("PERMISSION_SKILL_BODY", prompt)
                        self.assertNotIn("PERMISSION_SKILL_INDEX", prompt)
                        builtin = registry()["paveway-tijian"]
                        self.assertNotIn(builtin.name, prompt)
                        self.assertNotIn(json.dumps(builtin.body.strip(), ensure_ascii=False)[1:-1], prompt)
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
