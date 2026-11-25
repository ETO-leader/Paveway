import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import customization, git_sync
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError
from oicoach_runtime.store import Store
from oicoach_runtime.workflow_setup import create
from oicoach_runtime.workflows import WorkflowProvider


def file_tree(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


class GitSyncTests(unittest.TestCase):
    def test_large_native_export_reports_skip_preserves_backup_and_allows_release_resume(self):
        from oicoach_runtime.frontend import Frontend
        store = Store(self.a)
        try:
            frontend = Frontend(store)
            lease = frontend.begin("coach", "training", self.root / "a")
            owner = {k: lease[k] for k in ("conversation_id", "generation", "owner_token")}
            directory = Path(lease["session_dir"]); directory.mkdir(parents=True, mode=0o700)
            path = directory / "native.jsonl"
            path.write_text(json.dumps({"type": "session", "version": 3, "id": "native", "cwd": str(self.root / "a")}) + "\n")
            record = frontend.get(lease["conversation_id"])
            record.update(session_file=str(path), session_id="native", session_files=[{"session_file": str(path), "session_id": "native"}])
            with store.transaction() as db: frontend.save(db, record)
            frontend.finish(**owner, exit_code=0)
            first = self.cycle(self.a)
            self.assertEqual(first["session_archives"]["exported"], 1)
            repo = Path(git_sync.settings(self.a)["repository"])
            archive = next((repo / "devices").glob("*/sessions/*.json")); original = archive.read_bytes()
            with path.open("a") as stream:
                for _ in range(270): stream.write(json.dumps({"type": "custom", "data": "x" * 65536}) + "\n")
            self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
            resumed = frontend.begin("coach", "training", self.root / "a", lease["conversation_id"])
            owner = {k: resumed[k] for k in owner}
            frontend.finish(**owner, exit_code=0)
            self.edit(self.a, "OTHER_DATA_STILL_SYNCS")
            result = self.cycle(self.a)
            self.assertTrue(result["remote_synced"])
            self.assertEqual(result["session_archives"], {"exported": 0, "skipped": 1, "deferred": 0, "attention": 1, "reasons": {"size_limit": 1}})
            self.assertEqual(git_sync.status(self.a)["last"]["session_archives"], result["session_archives"])
            self.assertEqual(archive.read_bytes(), original)
            self.assertNotIn(str(path), json.dumps(result))
            self.cycle(self.b); self.assertEqual(self.prompt(self.b), "OTHER_DATA_STILL_SYNCS")
            active = frontend.begin("coach", "training", self.root / "a", lease["conversation_id"])
            result = self.cycle(self.a)
            self.assertEqual(result["session_archives"]["reasons"], {"active": 1})
            self.assertEqual(result["session_archives"]["attention"], 0)
            frontend.finish(**{k: active[k] for k in owner}, exit_code=1)
            self.assertEqual(frontend.recover(lease["conversation_id"])["status"], "released")
            header = {"type": "session", "version": 999, "id": "native", "cwd": str(self.root / "a")}
            path.write_text(json.dumps(header) + "\n")
            result = self.cycle(self.a)
            self.assertEqual(result["session_archives"]["reasons"], {"invalid_or_unsupported": 1})
            self.assertEqual(archive.read_bytes(), original)
            header["version"] = 3
            path.write_text(json.dumps(header) + "\n")
            timestamp = git_sync.archive_timestamp
            changed = False
            def append_during_scan(value):
                nonlocal changed
                if not changed:
                    changed = True
                    with path.open("a") as stream: stream.write('{"type":"custom"}\n')
                return timestamp(value)
            with patch.object(git_sync, "archive_timestamp", side_effect=append_during_scan):
                result = self.cycle(self.a)
            self.assertEqual(result["session_archives"]["reasons"], {"changed": 1})
            self.assertEqual(result["session_archives"]["attention"], 0)
            self.assertEqual(archive.read_bytes(), original)
        finally: store.close()

    def test_daemon_auto_sync_rejects_bad_tip_then_recovers_after_remote_fix(self):
        self.cycle(self.a)
        repo = Path(git_sync.settings(self.a)["repository"])
        original = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
        editor = self.root / "automatic-publisher"
        subprocess.run(["git", "clone", str(self.remote), str(editor)], capture_output=True, check=True)
        def publish(version):
            (editor / "format.json").write_text(json.dumps({"format": "paveway-user-data", "version": version}))
            for args in (("add", "format.json"), ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "publish format"), ("push",)):
                subprocess.run(["git", "-C", str(editor), *args], capture_output=True, check=True)
        publish(999)
        config_path = self.root / "a/config.toml"
        home = self.root / "isolated-home"
        home.mkdir()
        log = (self.root / "sync-daemon.log").open("w")
        process = subprocess.Popen([sys.executable, "-m", "oicoach_runtime", "serve", "--config", str(config_path)],
            env={**os.environ, "HOME": str(home)}, stdout=log, stderr=log)
        def wait(state, timeout):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                report = git_sync.read_json(git_sync.local_root(self.a) / "status.json", {})
                if report.get("state") == state: return report
                time.sleep(.03)
            self.fail("automatic Git sync did not reach " + state)
        try:
            wait("error", 6)
            self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]), original)
            publish(1)
            wait("ok", 15)  # No sync.request: the next configured 10s cycle recovers.
            subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", original.decode().strip(), "HEAD"], check=True)
        finally:
            process.terminate(); process.wait(timeout=8); log.close()

    def test_remote_bad_profile_preserves_local_work_and_next_fixed_tip_syncs(self):
        self.edit(self.a, "BASE_PROFILE")
        self.cycle(self.a)
        repo = Path(git_sync.settings(self.a)["repository"])
        editor = self.root / "profile-publisher"
        subprocess.run(["git", "clone", str(self.remote), str(editor)], check=True, capture_output=True)
        def publish():
            subprocess.run(["git", "-C", str(editor), "add", "--all"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(editor), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "publish profile"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(editor), "push"], check=True, capture_output=True)
        record = json.loads(next((editor / "profiles/coach").glob("*.json")).read_text())
        record["content"]["skills"] = [{"name": "bad", "description": None, "content": "SECRET_BAD_PROFILE_CONTENT", "enabled": True}]
        bad = editor / "profiles/coach" / (git_sync.digest(record) + ".json")
        bad.write_text(json.dumps(record))
        publish()
        self.edit(self.a, "LOCAL_UNPUSHED_CONFIGURATION")
        result = git_sync.cycle(self.a)
        self.assertEqual(result["state"], "error")
        self.assertIn("Remote profile validation failed", result["error"])
        self.assertNotIn("SECRET_BAD_PROFILE_CONTENT", result["error"])
        local_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
        self.assertEqual(git_sync.cycle(self.a)["state"], "error")
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip(), local_head)
        self.assertFalse((repo / bad.relative_to(editor)).exists())
        bad.unlink(); publish()
        self.cycle(self.a)
        self.assertEqual(self.prompt(self.a), "LOCAL_UNPUSHED_CONFIGURATION")
        subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", local_head, "HEAD"], check=True)

    def test_incompatible_remote_tip_is_rejected_before_head_update_and_recovers(self):
        self.cycle(self.a)
        repo = Path(git_sync.settings(self.a)["repository"])
        old = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
        editor = self.root / "publisher"
        subprocess.run(["git", "clone", str(self.remote), str(editor)], check=True, capture_output=True)
        def publish(format):
            (editor / "format.json").write_text(json.dumps(format))
            subprocess.run(["git", "-C", str(editor), "add", "format.json"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(editor), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "publish format"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(editor), "push"], check=True, capture_output=True)
        publish({"format": "paveway-user-data", "version": 999})
        result = git_sync.cycle(self.a)
        self.assertEqual(result["state"], "error")
        self.assertIn("format", result["error"])
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]), old)
        self.assertEqual(json.loads((repo / "format.json").read_text()), git_sync.FORMAT)
        publish({"format": "paveway-user-data", "version": True})
        result = git_sync.cycle(self.a)
        self.assertEqual(result["state"], "error")
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]), old)
        publish(git_sync.FORMAT)
        self.cycle(self.a)
        subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", old.decode().strip(), "HEAD"], check=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-git-sync-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], capture_output=True, check=True)
        self.devices = []
        for name in ("a", "b"):
            root = self.root / name
            root.mkdir(mode=0o700)
            path = root / "config.toml"
            initialize(path, str(root / "state"), str(root / "workflows"))
            config = load_config(path)
            create(config, "coach", root)
            git_sync.configure(config, root / "sync", str(self.remote), interval=10)
            self.devices.append(config)
        self.a, self.b = self.devices

    def cycle(self, config):
        result = git_sync.cycle(config)
        self.assertEqual(result["state"], "ok", result)
        return result

    def test_default_archive_is_shared_local_only_and_idempotent(self):
        root = self.root / "fresh"
        path = root / "config.toml"
        initialize(path, str(root / "state"), str(root / "workflows"))
        config = load_config(path)
        create(config, "coach", root)
        create(config, "second-coach", root)
        git_sync.ensure_local(config)
        original = git_sync.settings(config)
        self.assertEqual(original["repository"], str(config.state_dir / "user-data-git"))
        self.assertEqual(original["remote"], "")
        self.edit(config, "first coach")
        customization.update(config, "second-coach", 0, "prompt", mode="append", content="second coach")
        self.finished_run(config)
        commands = []
        run = git_sync.Git.run
        def capture(git, *args, **kwargs):
            commands.append(args[0])
            return run(git, *args, **kwargs)
        with patch.object(git_sync.Git, "run", capture):
            self.cycle(config)
        self.assertNotIn("fetch", commands)
        self.assertNotIn("push", commands)
        repo = Path(original["repository"])
        self.assertTrue((repo / "profiles/coach").is_dir())
        self.assertTrue((repo / "profiles/second-coach").is_dir())
        self.assertEqual(len(git_sync.history(config)["records"]), 1)
        git_sync.ensure_local(config)
        self.assertEqual(git_sync.settings(config), original)
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "remote"], text=True), "")

    def test_remote_changes_preserve_identity_commits_and_pause(self):
        self.cycle(self.a)
        git_sync.set_enabled(self.a, False)
        before = git_sync.settings(self.a)
        repo = before["repository"]
        head = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"])
        git_sync.set_remote(self.a, "")
        self.assertEqual(git_sync.settings(self.a), dict(before, remote=""))
        git_sync.ensure_local(self.a)
        git_sync.configure(self.a, self.a.state_dir / "user-data-git", _if_missing=True)
        self.assertEqual(git_sync.settings(self.a), dict(before, remote=""))
        self.assertEqual(subprocess.check_output(["git", "-C", repo, "remote"], text=True), "")
        git_sync.set_remote(self.a, str(self.remote))
        self.assertEqual(git_sync.settings(self.a), before)
        self.assertEqual(subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"]), head)

    def test_remote_entry_initializes_local_archive_without_daemon(self):
        root = self.root / "offline"
        path = root / "config.toml"
        initialize(path, str(root / "state"), str(root / "workflows"))
        config = load_config(path)
        report = git_sync.set_remote(config, "")
        self.assertTrue(report["configured"])
        self.assertEqual(report["settings"]["remote"], "")

    def install_package(self, config, name, body, revision):
        import zipfile
        from oicoach_runtime import skill_import
        package = self.root / (name + "-" + str(revision) + ".zip")
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name + "/SKILL.md",
                             ("---\nname: " + name + "\ndescription: synced skill\n---\n" + body).encode())
            archive.writestr(name + "/references/a.md", ("# A " + body).encode())
            archive.writestr(name + "/scripts/check.py", b"raise RuntimeError('NEVER_EXECUTE')\n")
            archive.writestr(name + "/assets/blob.bin", bytes(range(32)))
        preview = skill_import.preview(config, "coach", package)
        settings = customization.show(config, "coach")["settings"]
        replace = [name] if any(skill["name"] == name for skill in settings["skills"]) else []
        skill_import.install(config, "coach", settings["revision"], {
            "file": preview["file"], "sha256": preview["sha256"], "replace": replace,
            "skills": [{"name": name}]})
        return customization.show(config, "coach")["settings"]["skills"][0]["package"]["digest"]

    def tree(self, digest, config):
        from oicoach_runtime import skill_packages
        import hashlib
        return {path: hashlib.sha256(payload).hexdigest()
                for path, payload in skill_packages.read(config, digest).items()}

    def snapshot_tree(self, config, run):
        session = self.root / ("snapshot-" + run)
        session.mkdir(mode=0o700, exist_ok=True)
        settings = customization.show(config, "coach")["settings"]
        argv = customization.snapshot(settings, session, run, True, package_root=config.state_dir / "skill-packages")
        native = Path(json.loads(Path(argv[argv.index("--config") + 1]).read_text())["skills"]["customDirectories"][0])
        return native, file_tree(native)

    def publish_from_remote(self, action):
        editor = self.root / ("editor-" + str(len(list(self.root.glob("editor-*")))))
        subprocess.run(["git", "clone", str(self.remote), str(editor)], check=True, capture_output=True)
        action(editor)
        for args in (("add", "--all",), ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "publish"), ("push",)):
            subprocess.run(["git", "-C", str(editor), *args], check=True, capture_output=True)

    def test_complete_skill_package_syncs_between_devices_with_exact_bytes(self):
        digest = self.install_package(self.a, "synced-skill", "VERSION_ONE\n", 0)
        self.edit(self.a, "package profile")
        result = self.cycle(self.a)
        self.assertEqual(result["skill_packages"]["packages"], 1)
        self.cycle(self.b)
        # The remote object and the restored tree are byte-identical.
        self.assertEqual(self.tree(digest, self.a), self.tree(digest, self.b))
        native, restored = self.snapshot_tree(self.b, "after-pull")
        self.assertEqual(restored, {"synced-skill/" + path: sha for path, sha in self.tree(digest, self.b).items()})
        self.assertEqual((native / "synced-skill/references/a.md").read_bytes(), b"# A VERSION_ONE\n")
        self.assertEqual((native / "synced-skill/assets/blob.bin").read_bytes(), bytes(range(32)))
        repo = Path(git_sync.settings(self.b)["repository"])
        tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files"], text=True)
        self.assertIn("packages/" + digest + "/files/references/a.md", tracked)
        self.assertNotIn(str(self.b.state_dir), tracked)

        # A replaces the package; B's old launch snapshot must not change.
        second = self.install_package(self.a, "synced-skill", "VERSION_TWO\n", 2)
        self.assertNotEqual(second, digest)
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(file_tree(native), restored)
        self.assertEqual((native / "synced-skill/references/a.md").read_bytes(), b"# A VERSION_ONE\n")
        _, newest = self.snapshot_tree(self.b, "after-replace")
        self.assertEqual(newest, {"synced-skill/" + path: sha for path, sha in self.tree(second, self.b).items()})

    def test_remote_missing_or_corrupt_package_is_rejected_before_applying_profile(self):
        digest = self.install_package(self.a, "synced-skill", "VERSION_ONE\n", 0)
        self.cycle(self.a)
        self.cycle(self.b)
        before = customization.show(self.b, "coach")["settings"]
        head = subprocess.check_output(["git", "-C", str(git_sync.settings(self.b)["repository"]), "rev-parse", "HEAD"])
        self.publish_from_remote(lambda editor: (editor / "packages" / digest / "files/references/a.md").unlink())
        result = git_sync.cycle(self.b)
        self.assertEqual(result["state"], "error")
        self.assertIn("Skill package", result["error"])
        self.assertEqual(customization.show(self.b, "coach")["settings"], before)
        self.assertEqual(subprocess.check_output(["git", "-C", str(git_sync.settings(self.b)["repository"]), "rev-parse", "HEAD"]), head)

        def corrupt(editor):
            path = editor / "packages" / digest / "files/references/a.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# B VERSION_ONE\n")
        self.publish_from_remote(corrupt)
        result = git_sync.cycle(self.b)
        self.assertEqual(result["state"], "error")
        self.assertIn("digest", result["error"])
        self.assertEqual(customization.show(self.b, "coach")["settings"], before)

    def publish_raw_package(self, editor, files):
        from oicoach_runtime import skill_packages
        digest = skill_packages.digest(files)
        root = editor / "packages" / digest
        (root / "files").mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_bytes(skill_packages.canonical(skill_packages.manifest(files)) + b"\n")
        for path, payload in files.items():
            target = root / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return digest

    def publish_profile(self, editor, skills, prompt="remote binding probe"):
        records = git_sync.read_profiles(editor)
        parent = git_sync.heads(records["coach"])[0]
        record = {"workflow_id": "coach", "parents": [parent], "content": {
            "system_prompt": {"mode": "append", "text": prompt}, "skills": skills},
            "device_id": "f" * 32, "created_at": git_sync.stamp()}
        key = git_sync.digest(record)
        path = editor / "profiles" / "coach" / (key + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(git_sync.canonical(record) + b"\n")
        return key

    def publish_remote(self, action):
        """Clone the remote, let ``action(editor)`` edit it, then commit and push."""
        editor = self.root / ("binding-editor-" + str(len(list(self.root.glob("binding-editor-*")))))
        subprocess.run(["git", "clone", str(self.remote), str(editor)], check=True, capture_output=True)
        action(editor)
        for args in (("add", "--all"), ("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                                       "commit", "-m", "publish"), ("push",)):
            subprocess.run(["git", "-C", str(editor), *args], check=True, capture_output=True)
        return subprocess.check_output(["git", "-C", str(editor), "rev-parse", "HEAD"], text=True).strip()

    def assert_remote_rejected(self, commit, pattern):
        before = customization.show(self.b, "coach")["settings"]
        result = git_sync.cycle(self.b)
        self.assertEqual(result["state"], "error")
        self.assertIn(pattern, result["error"])
        self.assertEqual(customization.show(self.b, "coach")["settings"], before)
        repo = Path(git_sync.settings(self.b)["repository"])
        probe = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True)
        self.assertNotEqual(probe.returncode, 0, "a rejected remote tip must never be merged or applied")
        return result

    def test_remote_package_binding_mismatches_fail_closed_before_apply(self):
        self.edit(self.a, "binding base")
        self.cycle(self.a)
        self.cycle(self.b)
        good = {"SKILL.md": b"---\nname: synced-skill\ndescription: entry\n---\nOK\n"}
        good_reference = {"files": 1, "bytes": len(good["SKILL.md"])}
        renamed = {"SKILL.md": b"---\nname: other-skill\ndescription: entry\n---\nOK\n"}
        valid = {"name": "synced-skill", "description": "entry", "enabled": True, "category": "personal-skill"}
        cases = {
            "files": (good, {"files": 5, "bytes": good_reference["bytes"]}),
            "bytes": (good, {"files": 1, "bytes": 8192}),
            "name": (renamed, {"files": 1, "bytes": len(renamed["SKILL.md"])}),
            "no-entry": ({"references/a.md": b"# A\n\n"}, {"files": 1, "bytes": 6}),
        }
        for label, (files, reference) in cases.items():
            with self.subTest(label=label):
                def action(editor, files=files, reference=reference):
                    digest = self.publish_raw_package(editor, files)
                    self.publish_profile(editor, [{**valid, "package": {"digest": digest, **reference}}],
                                         "remote " + label)
                commit = self.publish_remote(action)
                self.assert_remote_rejected(commit, "binding")
                # A corrected child of the rejected head restores sync; history is kept.
                holder = {}
                def fix(editor):
                    digest = self.publish_raw_package(editor, good)
                    holder["digest"] = digest
                    self.publish_profile(editor, [{**valid, "package": {"digest": digest, **good_reference}}],
                                         "fixed " + label)
                self.publish_remote(fix)
                result = self.cycle(self.b)
                self.assertEqual(result["state"], "ok", result)
                record = next(item for item in customization.show(self.b, "coach")["settings"]["skills"]
                              if item["name"] == "synced-skill")
                self.assertEqual(record["package"], {"digest": holder["digest"], **good_reference})

    def test_remote_bad_binding_preserves_existing_package_snapshot(self):
        digest = self.install_package(self.a, "synced-skill", "VERSION_ONE\n", 0)
        self.edit(self.a, "kept")
        self.cycle(self.a)
        self.cycle(self.b)
        native, snapshot_tree = self.snapshot_tree(self.b, "before-bad-binding")
        before = customization.show(self.b, "coach")["settings"]

        def action(editor):
            self.publish_profile(editor, [{"name": "synced-skill", "description": "entry", "enabled": True,
                                           "package": {"digest": digest, "files": 4, "bytes": 999}}], "bad")
        commit = self.publish_remote(action)
        self.assert_remote_rejected(commit, "binding")
        self.assertEqual(customization.show(self.b, "coach")["settings"], before)
        self.assertEqual(file_tree(native), snapshot_tree)
        self.assertEqual(self.tree(digest, self.b), self.tree(digest, self.a))

    def test_conflicting_same_name_packages_resolve_with_their_profile_version(self):
        first = self.install_package(self.a, "shared-skill", "FROM_A\n", 0)
        self.edit(self.a, "device A base")
        self.cycle(self.a)
        # B never pulled before its own edit, so both heads diverge.
        second = self.install_package(self.b, "shared-skill", "FROM_B\n", 1)
        self.edit(self.b, "device B base")
        result = self.cycle(self.b)
        self.assertEqual(result["state"], "ok")
        self.cycle(self.a)
        conflict = git_sync.conflicts(self.a)[0]
        self.assertEqual(len(conflict["heads"]), 2)
        records = git_sync.read_profiles(Path(git_sync.settings(self.a)["repository"]))["coach"]
        def package_of(head):
            return next(skill for skill in records[head]["content"]["skills"] if skill["name"] == "shared-skill")["package"]["digest"]
        self.assertNotEqual(first, second)
        choice = next(head["head"] for head in conflict["heads"] if package_of(head["head"]) == second)
        git_sync.resolve(self.a, "coach", choice)
        self.cycle(self.a)
        self.cycle(self.b)
        record = next(skill for skill in customization.show(self.a, "coach")["settings"]["skills"]
                      if skill["name"] == "shared-skill")
        self.assertEqual(record["package"]["digest"], second)
        self.assertEqual(customization.show(self.b, "coach")["settings"]["skills"][0]["package"]["digest"], second)
        _, tree = self.snapshot_tree(self.a, "resolved")
        self.assertEqual(tree["shared-skill/references/a.md"], hashlib.sha256(b"# A FROM_B\n").hexdigest())
        self.assertEqual(git_sync.conflicts(self.a), [])

    def edit(self, config, value):
        revision = customization.show(config, "coach")["settings"]["revision"]
        customization.update(config, "coach", revision, "prompt", mode="append", content=value)

    def prompt(self, config):
        return customization.show(config, "coach")["settings"]["system_prompt"]["text"]

    def finished_run(self, config, message="user history", reply="saved reply"):
        store = Store(config)
        try:
            event = store.enqueue("coach", "training", "test", "history-one", payload={"message": message}, at=store.timestamp())
            event = store.claim_due("test")
            store.bind(event, WorkflowProvider(config.workflow_root).describe("coach"))
            store.dispatch(event["run_id"])
            store.evidence(event["run_id"], acknowledged=True)
            store.evidence(event["run_id"], result="completed")
            store.finish(event["run_id"], "succeeded", result="completed", settled=True,
                output={"status": "available", "text": reply, "truncated": False})
            return event["run_id"]
        finally:
            store.close()

    def test_two_devices_pull_portable_settings_and_history_without_queues_or_credentials(self):
        self.edit(self.a, "中文系统提示 ${literal}")
        customization.update(self.a, "coach", 1, "skill-put", name="review-skill", category="oi-introduce", description="复盘", content="检查复杂度")
        customization.update(self.a, "coach", 2, "inference", model="fake/coach", thinking="ultra-v2")
        run = self.finished_run(self.a)
        (self.a.state_dir / "private.service.env").write_text("PRIVATE_SECRET=never-sync")
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(self.prompt(self.b), "中文系统提示 ${literal}")
        self.assertEqual(customization.show(self.b, "coach")["settings"]["skills"][0]["name"], "review-skill")
        self.assertEqual(customization.show(self.b, "coach")["settings"]["skills"][0]["category"], "oi-introduce")
        self.assertEqual(customization.show(self.b, "coach")["settings"]["inference"], {"model": "fake/coach", "thinking": "ultra-v2"})
        page = git_sync.history(self.b, query="saved reply")
        self.assertEqual(page["records"][0]["run_id"], run)
        item = git_sync.history_show(self.b, page["records"][0]["device_id"], run)["record"]
        self.assertEqual(item["reply"], "saved reply")
        with sqlite3.connect(self.b.database_path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        repo = Path(git_sync.settings(self.b)["repository"])
        names = subprocess.check_output(["git", "-C", str(repo), "ls-files"], text=True)
        self.assertNotIn("sqlite", names)
        self.assertNotIn("service.env", names)
        self.assertFalse(any("never-sync" in p.read_text() for p in repo.rglob("*.json") if ".git" not in p.parts))

    def test_concurrent_profile_edits_are_preserved_until_explicit_resolution(self):
        self.edit(self.a, "base")
        self.cycle(self.a)
        self.cycle(self.b)
        self.edit(self.a, "device A edit")
        self.edit(self.b, "device B edit")
        self.cycle(self.a)
        result = self.cycle(self.b)
        self.assertEqual(result["conflict_count"], 1)
        self.cycle(self.a)
        self.assertEqual(self.prompt(self.a), "device A edit")
        self.assertEqual(self.prompt(self.b), "device B edit")
        conflict = git_sync.conflicts(self.a)[0]
        choice = next(h["head"] for h in conflict["heads"] if h["prompt_preview"] == "device B edit")
        git_sync.resolve(self.a, "coach", choice)
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(self.prompt(self.a), "device B edit")
        self.assertEqual(self.prompt(self.b), "device B edit")
        self.assertEqual(git_sync.conflicts(self.b), [])

    def test_offline_failure_keeps_local_configuration_and_future_retry_succeeds(self):
        self.edit(self.a, "offline content")
        with patch.object(git_sync.Git, "run", side_effect=RuntimeContractError("network unavailable")):
            result = git_sync.cycle(self.a)
        self.assertEqual(result["state"], "error")
        self.assertEqual(self.prompt(self.a), "offline content")
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(self.prompt(self.b), "offline content")

    def test_fetch_failure_still_commits_offline_changes_locally(self):
        self.edit(self.a, "commit while offline")
        original = git_sync.Git.run
        def offline(git, *args, **kwargs):
            if args[0] == "fetch":
                raise RuntimeContractError("offline")
            return original(git, *args, **kwargs)
        with patch.object(git_sync.Git, "run", offline):
            self.assertEqual(git_sync.cycle(self.a)["state"], "error")
        repo = Path(git_sync.settings(self.a)["repository"])
        tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files"], text=True)
        self.assertIn("profiles/coach/", tracked)
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(self.prompt(self.b), "commit while offline")

    def test_native_text_history_excludes_thinking_tools_and_preserves_branches(self):
        run = self.finished_run(self.a)
        store = Store(self.a)
        try:
            row = store.run(run)
            binding = store.connection.execute("SELECT * FROM session_bindings WHERE binding_id=?", (row["binding_id"],)).fetchone()
            path = self.a.state_dir / "sessions" / row["binding_id"] / "session.jsonl"
            entries = [
                {"type": "session", "id": "session-id", "version": 3, "cwd": binding["workspace"], "timestamp": "2026-11-09T10:00:00+08:00"},
                {"type": "message", "id": "one", "parentId": None, "timestamp": "2026-11-09T02:01:00Z", "message": {"role": "user", "content": "long user text"}},
                {"type": "message", "id": "two", "parentId": "one", "timestamp": "2026-11-09T10:02:00+08:00", "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "DO_NOT_EXPORT_THINKING"},
                    {"type": "toolCall", "name": "bash", "arguments": {"command": "DO_NOT_EXPORT_COMMAND"}},
                    {"type": "text", "text": "visible assistant text"}]}},
                {"type": "message", "id": "three", "parentId": "two", "message": {"role": "toolResult", "content": "DO_NOT_EXPORT_TOOL_RESULT"}},
            ]
            path.write_text("".join(json.dumps(e) + "\n" for e in entries))
            store.record_session(run, {"sessionFile": str(path), "sessionId": "session-id"})
        finally:
            store.close()
        self.cycle(self.a)
        self.cycle(self.b)
        device = git_sync.settings(self.a)["device_id"]
        page = git_sync.messages(self.b, device, row["binding_id"])
        encoded = json.dumps(page)
        self.assertIn("visible assistant text", encoded)
        self.assertNotIn("DO_NOT_EXPORT", encoded)
        self.assertEqual(page["messages"][1]["parent_id"], "one")
        self.assertEqual(page["messages"][1]["timestamp"], "2026-11-09T02:02:00+00:00")
        session = git_sync.sessions(self.b)["records"][0]
        self.assertEqual(session["created_at"], "2026-11-09T02:00:00+00:00")
        self.assertEqual(session["updated_at"], "2026-11-09T02:02:00+00:00")
        previous = page["messages"]
        with path.open("a") as stream:
            stream.write('{"incomplete":')
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(git_sync.messages(self.b, device, row["binding_id"])["messages"], previous)

    def test_archive_timestamps_and_recent_first_with_legacy_records(self):
        self.assertIsNone(git_sync.archive_timestamp("2026-11-09T10:00:00"))
        self.assertIsNone(git_sync.archive_timestamp(True))
        self.assertIsNone(git_sync.archive_timestamp(float("inf")))
        self.assertEqual(git_sync.archive_timestamp(0), "1970-01-01T00:00:00+00:00")
        values = git_sync.settings(self.a)
        for index, timestamp in enumerate((None, "2026-11-08T23:00:00Z", "2026-11-09T10:00:00+08:00")):
            session = str(index) * 64
            record = {"device_id": values["device_id"], "binding_id": session,
                      "workflow_id": "coach", "task_id": "test", "messages": []}
            if timestamp:
                record["updated_at"] = timestamp
            git_sync.write_json(Path(values["repository"]) / "devices" / values["device_id"] / "sessions" / (session + ".json"), record)
        rows = git_sync.sessions(self.a)["records"]
        self.assertEqual([r["session"] for r in rows], [str(i) * 64 for i in (2, 1, 0)])
        self.assertIsNone(rows[-1]["updated_at"])

    def test_unbound_remote_profile_waits_for_local_workspace(self):
        self.edit(self.a, "portable")
        self.cycle(self.a)
        directory = self.b.workflow_root / "coach"
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
        self.cycle(self.b)
        report = git_sync.conflicts(self.b)[0]
        self.assertTrue(report["needs_binding"])
        self.assertFalse(directory.exists())
        create(self.b, "coach", self.root / "b")
        self.cycle(self.b)
        self.assertEqual(self.prompt(self.b), "portable")

    def test_remote_unexpected_files_are_never_checked_out_or_applied(self):
        self.edit(self.a, "safe")
        self.cycle(self.a)
        source = self.root / "bad"
        subprocess.run(["git", "clone", str(self.remote), str(source)], capture_output=True, check=True)
        (source / ".gitattributes").write_text("* filter=unexpected\n")
        subprocess.run(["git", "-C", str(source), "add", ".gitattributes"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-m", "unexpected file"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(source), "push"], capture_output=True, check=True)
        result = git_sync.cycle(self.b)
        self.assertEqual(result["state"], "error")
        self.assertIn("unexpected", result["error"])
        self.assertFalse((Path(git_sync.settings(self.b)["repository"]) / ".gitattributes").exists())
        self.assertEqual(self.prompt(self.b), "")

    def test_url_validation_and_repository_ownership_prevent_accidental_source_push(self):
        for value in ("https://token@example.com/repo.git", "https://example.com/repo?token=secret", "ext::echo hello", "--upload-pack=bad"):
            with self.assertRaises(RuntimeContractError):
                git_sync.validate_remote(value)
        for config in self.devices:
            values = git_sync.settings(config)
            self.assertEqual((git_sync.local_root(config) / "settings.json").stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(values["device_id"], git_sync.settings(self.b if config == self.a else self.a)["device_id"])
        with self.assertRaises(ConflictError):
            git_sync.configure(self.a, self.root)

    def test_malformed_remote_history_is_an_explicit_validation_error(self):
        run = self.finished_run(self.a)
        self.cycle(self.a)
        self.cycle(self.b)
        device = git_sync.settings(self.a)["device_id"]
        repo = Path(git_sync.settings(self.b)["repository"])
        path = repo / "devices" / device / "runs" / (run + ".json")
        record = json.loads(path.read_text())
        record["started_at"] = {"invalid": "timestamp"}
        git_sync.write_json(path, record)
        with self.assertRaises(RuntimeContractError):
            git_sync.history(self.b)
        with self.assertRaises(RuntimeContractError):
            git_sync.history_show(self.b, device, run)
