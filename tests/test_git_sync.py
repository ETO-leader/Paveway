import json
import sqlite3
import subprocess
import tempfile
import unittest
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


class GitSyncTests(unittest.TestCase):
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
        run = self.finished_run(self.a)
        (self.a.state_dir / "private.service.env").write_text("PRIVATE_SECRET=never-sync")
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(self.prompt(self.b), "中文系统提示 ${literal}")
        self.assertEqual(customization.show(self.b, "coach")["settings"]["skills"][0]["name"], "review-skill")
        self.assertEqual(customization.show(self.b, "coach")["settings"]["skills"][0]["category"], "oi-introduce")
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
                {"type": "session", "id": "session-id", "version": 3, "cwd": binding["workspace"]},
                {"type": "message", "id": "one", "parentId": None, "message": {"role": "user", "content": "long user text"}},
                {"type": "message", "id": "two", "parentId": "one", "message": {"role": "assistant", "content": [
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
        previous = page["messages"]
        with path.open("a") as stream:
            stream.write('{"incomplete":')
        self.cycle(self.a)
        self.cycle(self.b)
        self.assertEqual(git_sync.messages(self.b, device, row["binding_id"])["messages"], previous)

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
