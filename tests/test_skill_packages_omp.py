"""Opt-in: prove a complete installed package is discovered and read by real OMP.

The flow mirrors the user story: install from a ZIP, drop the ZIP, restart
Paveway, open a new session, and let OMP read the Skill and a relative
reference through its own ``skill://`` protocol. No model turn is submitted.
"""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from oicoach_runtime import customization, skill_import
from oicoach_runtime.adapters.omp_rpc import OmpRpc
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.proxy import child_environment
from oicoach_runtime.workflow_setup import create

SKILL_MD = (b"---\nname: plain-oi-editorials\ndescription: Write OI editorials with a verified rubric\n"
            b"category: oi-introduce\n---\n\n# Plain OI editorials\n\n"
            b"Read references/a.md before drafting and run scripts/check.py only if the user asks.\n")
RESOURCES = {
    "SKILL.md": SKILL_MD,
    "references/a.md": b"# Task contract\n\nOnly state verified facts.\n",
    "references/nested/b.md": b"# Rubric\n\n- clarity\n- correctness\n",
    "scripts/check.py": b"raise RuntimeError('NEVER_EXECUTE_IN_TEST')\n",
    "assets/diagram.png": b"\x89PNG\r\n\x1a\n\x00fixture-binary\xff",
}


@unittest.skipUnless(os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG"), "set OICOACH_TEST_REAL_OMP_CONFIG for native Skill acceptance")
class RealSkillPackageTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, root):
        config_path = root / "config.toml"
        initialize(config_path, str(root / "state"), str(root / "workflows"))
        base = load_config(Path(os.environ["OICOACH_TEST_REAL_OMP_CONFIG"]))
        config = load_config(config_path)
        return replace(config, agent=replace(base.agent, tools_enabled=True, skills_enabled=True,
                                             extensions_enabled=False, rules_enabled=False,
                                             startup_timeout_seconds=30, ack_timeout_seconds=20,
                                             graceful_stop_seconds=1))

    def install_zip(self, config, root):
        package = root / "bundle.zip"
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative, payload in RESOURCES.items():
                archive.writestr("plain-oi-editorials/" + relative, payload)
        preview = skill_import.preview(config, "coach", package)
        skill_import.install(config, "coach", preview["revision"], {
            "file": preview["file"], "sha256": preview["sha256"], "replace": [],
            "skills": [{"name": "plain-oi-editorials"}]})
        package.unlink()  # The authored ZIP is never needed again.
        return preview["skills"][0]["digest"]

    async def native_read(self, config, workspace, overlay, url):
        environment = child_environment(None)
        prior = os.environ.get("PI_CONFIG_FILES")
        environment["PI_CONFIG_FILES"] = os.pathsep.join(filter(None, (prior, str(overlay))))
        process = await asyncio.create_subprocess_exec(
            config.agent.executable, "read", url, cwd=workspace, env=environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace")[-2000:])
        return stdout

    async def test_installed_package_survives_restart_and_is_read_by_real_omp(self):
        with tempfile.TemporaryDirectory(prefix="pw-package-native-") as directory:
            root = Path(directory)
            config = self.runtime(root)
            create(config, "coach", root)
            digest = self.install_zip(config, root)
            self.assertFalse((root / "bundle.zip").exists())

            # Fresh launch from persisted configuration only.
            session = root / "sessions"
            session.mkdir(mode=0o700)
            adapter = OmpRpc(config)
            try:
                await adapter.start({"workspace": str(root), "session_dir": str(session), "session_file": None,
                                     "customization": customization.show(config, "coach")["settings"]},
                                    "package-acceptance")
                await adapter.ready()
                prompt = json.dumps((await adapter.state())["systemPrompt"], ensure_ascii=False)
                self.assertIn("plain-oi-editorials", prompt)
                self.assertIn("verified rubric", prompt)
                self.assertNotIn("Only state verified facts", prompt)
                self.assertEqual((await adapter.command("get_messages"))["messages"], [])
            finally:
                await adapter.stop()

            snapshot = session / "customizations" / "package-acceptance"
            overlay = json.loads((snapshot / "omp.yml").read_text())
            native = Path(overlay["skills"]["customDirectories"][-1])
            for relative, payload in RESOURCES.items():
                installed = native / "plain-oi-editorials" / relative
                self.assertTrue(installed.is_file(), relative)
                self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(),
                                 hashlib.sha256(payload).hexdigest())

            # OMP itself resolves skill://<name> and relative resource paths.
            body = await self.native_read(config, root, snapshot / "omp.yml", "skill://plain-oi-editorials:raw")
            self.assertIn(SKILL_MD, body)
            reference = await self.native_read(config, root, snapshot / "omp.yml",
                                               "skill://plain-oi-editorials/references/a.md:raw")
            self.assertIn(RESOURCES["references/a.md"], reference)
            nested = await self.native_read(config, root, snapshot / "omp.yml",
                                            "skill://plain-oi-editorials/references/nested/b.md:raw")
            self.assertIn(b"- correctness", nested)
            # The profile still references the same immutable object after restart.
            settings = customization.show(config, "coach")["settings"]
            self.assertEqual(settings["skills"][0]["package"]["digest"], digest)
