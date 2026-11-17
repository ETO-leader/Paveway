"""Real OMP acceptance for complete Skill packages.

The flow mirrors the user story: install from a ZIP, drop the ZIP, restart
Paveway, open a new session, and let OMP read the Skill and relative
references through its own ``skill://`` protocol. No model turn is submitted.

Two host modes:

* ``OICOACH_TEST_REAL_OMP_CONFIG`` — an existing config (local verification).
* ``PAVEWAY_TEST_NATIVE=1`` — the isolated loopback-provider host used by the
  GitHub Actions native job; no credentials or user OMP configuration.

CI sets ``PAVEWAY_REQUIRE_REAL_OMP_PACKAGE=1`` so a missing host fails the
import (and therefore the job) instead of silently skipping.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

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

CONFIG = os.environ.get("OICOACH_TEST_REAL_OMP_CONFIG")
NATIVE = os.environ.get("PAVEWAY_TEST_NATIVE") == "1" and shutil.which("omp")
REQUIRED = os.environ.get("PAVEWAY_REQUIRE_REAL_OMP_PACKAGE") == "1"
if REQUIRED and not (CONFIG or NATIVE):
    raise RuntimeError("Complete Skill package acceptance requires a real OMP host: set "
                       "OICOACH_TEST_REAL_OMP_CONFIG or PAVEWAY_TEST_NATIVE=1 with omp on PATH")


@unittest.skipUnless(CONFIG or NATIVE, "set PAVEWAY_TEST_NATIVE=1 or OICOACH_TEST_REAL_OMP_CONFIG for native Skill acceptance")
class RealSkillPackageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-package-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = None
        self.base = self.host_config()

    def host_config(self):
        if CONFIG:
            return load_config(Path(CONFIG))
        # Same isolated loopback host as the native CI job: allowlisted env,
        # fixture provider, no user profile, auth or global OMP settings.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
        from verify_omp_m0 import Probe, Provider
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.server.requests = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)
        probe = Probe(self.root, shutil.which("omp"), self.server)
        environment = dict(probe.env)
        cache = self.omp_cache()
        if cache is not None:
            environment["PAVEWAY_OMP_CACHE"] = cache
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        base = load_config(self.config_path())
        return replace(base, agent=replace(base.agent, executable=shutil.which("omp"),
                                          model="paveway-m0/probe", thinking="off"))

    def omp_cache(self):
        """The prepared host the CI launcher resolves; never download in a test."""
        explicit = os.environ.get("PAVEWAY_OMP_CACHE")
        if explicit:
            return explicit
        root = Path.home() / ".cache/paveway/omp"
        if not root.is_dir():
            return None
        prepared = [entry for entry in sorted(root.iterdir()) if (entry / "runtime.json").is_file()]
        return str(prepared[-1]) if prepared else None

    def config_path(self):
        path = self.root / "config.toml"
        if not path.exists():
            initialize(path, str(self.root / "state"), str(self.root / "workflows"))
        return path

    def runtime(self):
        config = load_config(self.config_path())
        return replace(config, agent=replace(self.base.agent, tools_enabled=True, skills_enabled=True,
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
        config = self.runtime()
        create(config, "coach", self.root)
        digest = self.install_zip(config, self.root)
        self.assertFalse((self.root / "bundle.zip").exists())

        # An OMP-managed external Skill must stay visible and untouched; the
        # package directory is appended after it, never merged into it.
        external = self.root / "external-skills" / "external-probe"
        external.mkdir(parents=True)
        external_entry = "---\nname: external-probe\ndescription: EXTERNAL_INDEX\n---\nEXTERNAL_BODY\n"
        (external / "SKILL.md").write_text(external_entry, encoding="utf-8")
        ambient = self.root / "ambient.yml"
        ambient.write_text(json.dumps({"skills": {"customDirectories": [str(external.parent)]}}), encoding="utf-8")
        prior = os.environ.get("PI_CONFIG_FILES")
        patcher = patch.dict(os.environ, {
            "PI_CONFIG_FILES": os.pathsep.join(filter(None, (prior, str(ambient))))})
        patcher.start()
        self.addCleanup(patcher.stop)

        # Fresh launch from persisted configuration only.
        session = self.root / "sessions"
        session.mkdir(mode=0o700)
        adapter = OmpRpc(config)
        try:
            await adapter.start({"workspace": str(self.root), "session_dir": str(session), "session_file": None,
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
        if self.server is not None:
            # OMP may generate routing hints through the loopback fixture at
            # startup (local, credential-free, no cost). Its own turn and the
            # private Skill bodies must never reach a provider.
            transcript = json.dumps(self.server.requests, ensure_ascii=False)
            self.assertNotIn("Only state verified facts", transcript)
            self.assertNotIn("NEVER_EXECUTE_IN_TEST", transcript)

        snapshot = session / "customizations" / "package-acceptance"
        overlay = json.loads((snapshot / "omp.yml").read_text())
        directories = overlay["skills"]["customDirectories"]
        self.assertEqual(directories[-1], str(snapshot / "skills"))
        self.assertIn(str(external.parent), directories)
        native = Path(directories[-1])
        for relative, payload in RESOURCES.items():
            installed = native / "plain-oi-editorials" / relative
            self.assertTrue(installed.is_file(), relative)
            self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(),
                             hashlib.sha256(payload).hexdigest())

        # OMP itself resolves skill://<name> and relative resource paths.
        external_body = await self.native_read(config, self.root, snapshot / "omp.yml", "skill://external-probe:raw")
        self.assertIn(b"EXTERNAL_BODY", external_body)
        self.assertEqual((external / "SKILL.md").read_text(encoding="utf-8"), external_entry)
        self.assertFalse((native / "external-probe").exists())
        body = await self.native_read(config, self.root, snapshot / "omp.yml", "skill://plain-oi-editorials:raw")
        self.assertIn(SKILL_MD, body)
        reference = await self.native_read(config, self.root, snapshot / "omp.yml",
                                           "skill://plain-oi-editorials/references/a.md:raw")
        self.assertIn(RESOURCES["references/a.md"], reference)
        nested = await self.native_read(config, self.root, snapshot / "omp.yml",
                                        "skill://plain-oi-editorials/references/nested/b.md:raw")
        self.assertIn(b"- correctness", nested)
        # The profile still references the same immutable object after restart.
        settings = customization.show(config, "coach")["settings"]
        self.assertEqual(settings["skills"][0]["package"]["digest"], digest)
