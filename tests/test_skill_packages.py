"""Complete native Skill packages: install, snapshot, lifecycle and security."""

import hashlib
import json
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import customization as custom
from oicoach_runtime import skill_import, skill_packages
from oicoach_runtime.cli import initialize
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import ConflictError, RuntimeContractError, WorkflowError
from oicoach_runtime.workflow_setup import create

COMPLEX = {
    "complex-skill/SKILL.md": b"---\r\nname: complex-skill\r\ndescription: Use for complex OI reviews\r\ncategory: oi-introduce\r\n---\r\n\r\n# Complex\r\nRead references/a.md and references/nested/b.md.\r\n",
    "complex-skill/references/a.md": b"# A\r\n\r\n  keep indentation\r\n",
    "complex-skill/references/nested/b.md": b"# B\n\n```text\n  exact\n```\n",
    "complex-skill/scripts/check.py": b"#!/usr/bin/env python3\nraise RuntimeError('NEVER_EXECUTE')\n",
    "complex-skill/assets/diagram.png": b"\x89PNG\r\n\x1a\n\x00\x01binary\xff",
}


def write_zip(path, members, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path


def eight_skill_zip(path):
    members, bodies = {}, {}
    for index in range(8):
        name = "bundle-skill-%d" % index
        body = b"---\nname: " + name.encode() + b"\ndescription: Bundle " + name.encode() + b"\n---\nBODY_%d\n" % index
        members[name + "/SKILL.md"] = body
        members[name + "/references/a.md"] = b"REFERENCE_%d\n" % index
        bodies[name] = {name + "/SKILL.md": body, name + "/references/a.md": members[name + "/references/a.md"]}
    write_zip(path, members)
    return bodies


def tree_digest(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


class PackageTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-skill-packages-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        initialize(self.path, str(self.root / "state"), str(self.root / "workflows"))
        self.config = load_config(self.path)
        create(self.config, "coach", self.root)
        create(self.config, "other", self.root)
        self.store = self.config.state_dir / "skill-packages"

    def preview(self, package, workflow="coach"):
        return skill_import.preview(self.config, workflow, package)

    def install(self, package, names=None, replace=(), revision=0, enabled=None, workflow="coach"):
        preview = self.preview(package, workflow)
        wanted = names or [skill["name"] for skill in preview["skills"]]
        batch = {"file": preview["file"], "sha256": preview["sha256"], "replace": sorted(replace),
                 "skills": [{"name": name} | ({"enabled": enabled[name]} if enabled and name in enabled else {})
                            for name in wanted]}
        return skill_import.install(self.config, workflow, revision, batch)

    def settings(self, workflow="coach"):
        return custom.show(self.config, workflow)["settings"]

    def snapshot(self, run_id="run1", session=None, settings=None, **kwargs):
        session = session or self.root / "session"
        session.mkdir(mode=0o700, exist_ok=True)
        argv = custom.snapshot(settings or self.settings(), session, run_id, True,
                               package_root=self.store, **kwargs)
        overlay = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
        return Path(overlay["skills"]["customDirectories"][-1]), overlay


class PackageInstallTests(PackageTestCase):
    def test_complex_skill_is_installed_and_snapshotted_byte_exactly(self):
        package = write_zip(self.root / "complex.zip", COMPLEX)
        preview = self.preview(package)
        self.assertEqual(len(preview["skills"]), 1)
        item = preview["skills"][0]
        self.assertEqual((item["name"], item["file_count"], item["conflict"]), ("complex-skill", 5, False))
        self.assertNotIn("files", item)
        self.assertEqual({entry["path"] for entry in item["resources"]},
                         {"SKILL.md", "references/a.md", "references/nested/b.md", "scripts/check.py", "assets/diagram.png"})

        report = self.install(package)
        record, = self.settings()["skills"]
        self.assertEqual(set(record), {"name", "description", "enabled", "category", "package"})
        self.assertEqual(record["package"], {"digest": item["digest"], "files": 5,
                                            "bytes": sum(len(value) for value in COMPLEX.values())})
        self.assertEqual(report["installed"][0]["digest"], item["digest"])
        self.assertEqual(package.read_bytes(), write_zip(self.root / "again.zip", COMPLEX).read_bytes())

        native, overlay = self.snapshot()
        installed = native / "complex-skill"
        self.assertEqual(tree_digest(installed),
                         {name.split("/", 1)[1]: hashlib.sha256(payload).hexdigest() for name, payload in COMPLEX.items()})
        for path in installed.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(overlay["skills"]["customDirectories"], [str(native)])

    def test_eight_skill_zip_supports_subset_replacement_and_isolation(self):
        package = self.root / "bundle.zip"
        bodies = eight_skill_zip(package)
        custom.update(self.config, "coach", 0, "skill-put", name="bundle-skill-0", description="Old", content="OLD")
        custom.update(self.config, "coach", 1, "skill-disable", name="bundle-skill-0")
        preview = self.preview(package)
        self.assertEqual(len(preview["skills"]), 8)
        self.assertEqual([item["conflict"] for item in preview["skills"]], [True] + [False] * 7)
        self.assertEqual([item["file_count"] for item in preview["skills"]], [2] * 8)
        self.assertTrue(all(item["bytes"] > 0 for item in preview["skills"]))
        self.assertEqual(preview["skills"][0]["enabled"], False)

        self.install(package, names=["bundle-skill-0", "bundle-skill-1", "bundle-skill-7"],
                     replace=["bundle-skill-0"], revision=2)
        settings = self.settings()
        self.assertEqual([skill["name"] for skill in settings["skills"]],
                         ["bundle-skill-0", "bundle-skill-1", "bundle-skill-7"])
        self.assertFalse(settings["skills"][0]["enabled"])
        self.assertEqual({skill["name"]: skill["package"]["files"] for skill in settings["skills"]},
                         {"bundle-skill-0": 2, "bundle-skill-1": 2, "bundle-skill-7": 2})
        self.assertEqual(len(list(self.store.iterdir())), 3)

        native, _ = self.snapshot()
        # The disabled replacement keeps its files and stays out of the launch snapshot.
        self.assertEqual(sorted(path.name for path in native.iterdir()), ["bundle-skill-1", "bundle-skill-7"])
        for name in ("bundle-skill-1", "bundle-skill-7"):
            self.assertEqual(tree_digest(native / name),
                             {path.split("/", 1)[1]: hashlib.sha256(payload).hexdigest()
                              for path, payload in bodies[name].items()})
        custom.update(self.config, "coach", 3, "skill-enable", name="bundle-skill-0")
        native, _ = self.snapshot("enabled")
        self.assertEqual((native / "bundle-skill-0/references/a.md").read_bytes(), b"REFERENCE_0\n")
        self.assertEqual(self.settings("other")["skills"], [])

    def test_reimport_is_idempotent_and_replaces_without_leaking_old_record(self):
        package = write_zip(self.root / "complex.zip", COMPLEX)
        first = self.install(package)
        before = len(list(self.store.iterdir()))
        second = self.install(package, replace=["complex-skill"], revision=1)
        self.assertEqual(len(list(self.store.iterdir())), before)
        self.assertEqual(second["installed"][0]["replaced"], "package")
        record, = self.settings()["skills"]
        self.assertNotIn("content", record)
        changed = dict(COMPLEX)
        changed["complex-skill/references/c.md"] = b"# C\n"
        write_zip(self.root / "complex-v2.zip", changed)
        self.install(self.root / "complex-v2.zip", replace=["complex-skill"], revision=2)
        record, = self.settings()["skills"]
        self.assertEqual(record["package"]["files"], 6)
        native, _ = self.snapshot()
        self.assertEqual((native / "complex-skill/references/c.md").read_bytes(), b"# C\n")

    def test_partial_failure_and_stale_preview_leave_configuration_untouched(self):
        package = self.root / "bundle.zip"
        eight_skill_zip(package)
        preview = self.preview(package)
        batch = {"file": preview["file"], "sha256": preview["sha256"], "replace": [],
                 "skills": [{"name": "bundle-skill-0"}, {"name": "bundle-skill-1"}]}
        before = (self.config.workflow_root / "coach" / "customization.json")
        original = before.read_bytes() if before.exists() else None
        with patch.object(skill_packages, "store", side_effect=RuntimeContractError("disk unavailable")):
            with self.assertRaises(RuntimeContractError):
                skill_import.install(self.config, "coach", 0, batch)
        self.assertEqual(before.read_bytes() if before.exists() else None, original)
        with self.assertRaises(ConflictError):
            skill_import.install(self.config, "coach", 5, batch)
        self.assertEqual(self.settings()["revision"], 0)
        # A changed source after preview must be rejected, not silently installed.
        write_zip(package, {"bundle-skill-0/SKILL.md":
                            b"---\nname: bundle-skill-0\ndescription: changed\n---\nCHANGED\n"})
        with self.assertRaisesRegex(ConflictError, "changed after preview"):
            skill_import.install(self.config, "coach", 0, batch)
        self.assertEqual(self.settings()["skills"], [])
        # Unreferenced immutable objects are harmless: no configuration can see them.
        self.assertFalse(any(json.loads(path.read_text())["skills"] for path in
                             (self.config.workflow_root / "coach").glob("customization*.json")))

    def test_snapshot_is_immutable_after_replace_delete_and_restart(self):
        package = write_zip(self.root / "complex.zip", COMPLEX)
        digest = self.install(package)["installed"][0]["digest"]
        native, _ = self.snapshot("first")
        first = tree_digest(native)
        changed = dict(COMPLEX, **{"complex-skill/references/a.md": b"# A2\n"})
        write_zip(self.root / "v2.zip", changed)
        self.install(self.root / "v2.zip", replace=["complex-skill"], revision=1)
        self.assertEqual(tree_digest(native), first)
        second, _ = self.snapshot("second")
        self.assertEqual((second / "complex-skill/references/a.md").read_bytes(), b"# A2\n")
        custom.update(self.config, "coach", 2, "skill-delete", name="complex-skill")
        self.assertEqual(tree_digest(native), first)
        self.assertEqual(tree_digest(second), tree_digest(second))
        self.assertEqual(self.settings()["skills"], [])
        # Deleting a record never deletes the immutable object itself.
        self.assertTrue((self.store / digest).is_dir())

    def test_missing_or_tampered_package_fails_closed_before_any_snapshot(self):
        package = write_zip(self.root / "complex.zip", COMPLEX)
        report = self.install(package)
        digest = report["installed"][0]["digest"]
        session = self.root / "session"
        session.mkdir(mode=0o700)
        stored = self.store / digest / "files/references/a.md"
        stored.write_bytes(b"tampered")
        with self.assertRaisesRegex(ConflictError, "unavailable"):
            custom.snapshot(self.settings(), session, "tampered", True, package_root=self.store)
        self.assertFalse((session / "customizations/tampered").exists())
        shutil.rmtree(self.store / digest)
        with self.assertRaisesRegex(ConflictError, "unavailable"):
            custom.snapshot(self.settings(), session, "missing", True, package_root=self.store)
        with self.assertRaises(RuntimeContractError):
            custom.update(self.config, "coach", 1, "replace-settings",
                          settings={"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": ""},
                                    "skills": [{"name": "complex-skill", "description": "x", "enabled": True,
                                                "package": {"digest": digest, "files": 5, "bytes": 1}}]})
        self.assertEqual(self.settings()["revision"], 1)

    def test_legacy_inline_skill_still_loads_and_cannot_be_overwritten_by_editing(self):
        legacy = {"name": "old-skill", "description": "legacy", "content": "LEGACY_BODY", "enabled": True}
        path = self.config.workflow_root / "coach" / "customization.json"
        path.write_text(json.dumps(
            {"schema_version": 1, "revision": 3, "system_prompt": {"mode": "append", "text": ""}, "skills": [legacy]}))
        path.chmod(0o600)
        native, _ = self.snapshot()
        self.assertIn("LEGACY_BODY", (native / "old-skill/SKILL.md").read_text())
        package = write_zip(self.root / "complex.zip", COMPLEX)
        self.install(package, revision=3)
        self.assertEqual(sorted(skill["name"] for skill in self.settings()["skills"]), ["complex-skill", "old-skill"])
        with self.assertRaisesRegex(ConflictError, "re-import"):
            custom.update(self.config, "coach", 4, "skill-put", name="complex-skill",
                          description="edited", content="REDUCED")
        self.assertNotIn("content", next(skill for skill in self.settings()["skills"]
                                        if skill["name"] == "complex-skill"))
        custom.update(self.config, "coach", 4, "skill-disable", name="complex-skill")
        native, _ = self.snapshot("disabled-run")
        self.assertFalse((native / "complex-skill").exists())
        custom.update(self.config, "coach", 5, "skill-enable", name="complex-skill")
        native, _ = self.snapshot("enabled-run")
        self.assertTrue((native / "complex-skill/references/a.md").is_file())

    def test_external_omp_skills_are_preserved_while_name_conflicts_are_diagnosable(self):
        external = self.root / "external-skills"
        (external / "external-probe").mkdir(parents=True)
        (external / "external-probe/SKILL.md").write_text(
            "---\nname: external-probe\ndescription: EXTERNAL\n---\nEXTERNAL_BODY\n")
        self.install(write_zip(self.root / "complex.zip", COMPLEX))
        native, overlay = self.snapshot(skill_directories=[str(external)])
        self.assertEqual(overlay["skills"]["customDirectories"], [str(external), str(native)])
        self.assertEqual((external / "external-probe/SKILL.md").read_text().count("EXTERNAL_BODY"), 1)
        self.assertFalse((native / "external-probe").exists())
        (external / "complex-skill").mkdir()
        (external / "complex-skill/SKILL.md").write_text(
            "---\nname: complex-skill\ndescription: EXTERNAL DUP\n---\nEXTERNAL\n")
        session = self.root / "conflict-session"
        session.mkdir(mode=0o700)
        with self.assertRaisesRegex(ConflictError, "complex-skill"):
            custom.snapshot(self.settings(), session, "conflict", True, package_root=self.store,
                            skill_directories=[str(external)])
        self.assertFalse((session / "customizations/conflict").exists())

    def test_packaged_scripts_are_stored_as_data_never_executed(self):
        marker = self.root / "EXECUTED"
        payload = ("import pathlib\npathlib.Path(%r).write_text('ran')\n" % str(marker)).encode()
        members = {"runner-skill/SKILL.md": b"---\nname: runner-skill\ndescription: runs\n---\nRUN\n",
                   "runner-skill/scripts/run.py": payload}
        self.install(write_zip(self.root / "runner.zip", members))
        native, _ = self.snapshot()
        script = native / "runner-skill/scripts/run.py"
        self.assertEqual(script.read_bytes(), payload)
        self.assertEqual(script.stat().st_mode & 0o777, 0o600)
        self.assertFalse(marker.exists())
        self.assertEqual(script.stat().st_mode & stat.S_IXUSR, 0)


class ZipSecurityTests(PackageTestCase):
    def assert_rejected(self, members, *, compression=zipfile.ZIP_DEFLATED, pattern=None, mutate=None):
        package = self.root / ("bad-%d.zip" % len(list(self.root.glob("bad-*.zip"))))
        with zipfile.ZipFile(package, "w", compression=compression) as archive:
            for name, payload in members:
                archive.writestr(name, payload)
        if mutate:
            mutate(package)
        expected = self.assertRaisesRegex(RuntimeContractError, pattern) if pattern else self.assertRaises(RuntimeContractError)
        with expected:
            skill_import.preview(self.config, "coach", package)
        self.assertEqual(self.settings()["skills"], [])
        self.assertFalse(self.store.exists() and any(self.store.iterdir()))
        return package

    def test_path_traversal_absolute_windows_and_control_characters_are_rejected(self):
        skill = "---\nname: probe\ndescription: probe\n---\nbody\n"
        for name in ("../escape/SKILL.md", "/absolute/SKILL.md", "C:/drive/SKILL.md", "bad\\path/SKILL.md",
                     "probe/SKILL.md", "probe/ter\x01rible.md"):
            with self.subTest(name=name):
                with self.assertRaises(RuntimeContractError):
                    skill_import.discover(self._package({"probe/SKILL.md": skill.encode(), name: b"x"}))
        self.assertFalse((self.root.parent / "escape").exists())

    def test_duplicate_case_conflict_symlink_encryption_and_file_directory_conflicts_are_rejected(self):
        skill = b"---\nname: probe\ndescription: probe\n---\nbody\n"
        cases = {
            "case": [("probe/SKILL.md", skill), ("probe/A.md", b"a"), ("probe/a.md", b"a")],
            "file-directory": [("probe/SKILL.md", skill), ("probe/x", b"file"), ("probe/x/y.md", b"nested")],
        }
        for label, members in cases.items():
            with self.subTest(label=label):
                self.assert_rejected(members)
        # zipfile drops exact duplicate names while writing, so exercise the guard directly.
        duplicate = self.root / "duplicate.zip"
        write_zip(duplicate, {"probe/SKILL.md": skill})
        original = zipfile.ZipFile.infolist
        with patch.object(zipfile.ZipFile, "infolist", lambda archive: [*original(archive), original(archive)[-1]]):
            with self.assertRaises(RuntimeContractError):
                skill_import.discover(duplicate)
        symlink = self.root / "symlink.zip"
        with zipfile.ZipFile(symlink, "w") as archive:
            info = zipfile.ZipInfo("probe/SKILL.md")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"---\nname: probe\ndescription: probe\n---\n")
        with self.assertRaises(RuntimeContractError):
            skill_import.preview(self.config, "coach", symlink)
        encrypted = write_zip(self.root / "encrypted.zip", {"probe/SKILL.md": skill})
        data = bytearray(encrypted.read_bytes())
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            data[data.index(signature) + offset] |= 0x01
        encrypted.write_bytes(bytes(data))
        with self.assertRaisesRegex(RuntimeContractError, "encryption"):
            skill_import.preview(self.config, "coach", encrypted)
        self.assert_rejected([("probe/SKILL.md", skill)], compression=zipfile.ZIP_BZIP2)

    def test_size_entry_ratio_and_unpacked_limits_are_enforced(self):
        skill = b"---\nname: probe\ndescription: probe\n---\nbody\n"
        self.assert_rejected([("probe/SKILL.md", skill), ("probe/big.bin", b"\0" * (skill_packages.MAX_FILE_BYTES + 1))],
                             compression=zipfile.ZIP_STORED, pattern="at most 4 MiB")
        self.assert_rejected([("probe/SKILL.md", skill)] +
                             [("probe/f%d.md" % index, b"x") for index in range(skill_packages.MAX_FILES)],
                             pattern="%d resources" % skill_packages.MAX_FILES)
        self.assert_rejected([("probe/SKILL.md", skill), ("probe/bomb.bin", b"\0" * (3 * 1024 * 1024))],
                             pattern="compression ratio")
        package = self.root / "corrupt.zip"
        package.write_bytes(b"PK\x03\x04truncated")
        with self.assertRaises(RuntimeContractError):
            skill_import.preview(self.config, "coach", package)

    def test_ownership_rules_reject_ambiguous_layouts_and_keep_nested_skills_separate(self):
        self.assert_rejected([("probe/SKILL.md", b"---\nname: probe\ndescription: p\n---\n"), ("shared.md", b"x")],
                             pattern="outside every Skill directory")
        self.assert_rejected([("probe/references/a.md", b"x")], pattern="SKILL.md")
        self.assert_rejected([("probe/SKILL.md", b"no frontmatter\n")], pattern="frontmatter")
        self.assert_rejected([("probe/SKILL.md", b"---\nname: probe\n---\nbody\n")], pattern="description")
        self.assert_rejected([("probe/SKILL.md", b"---\nname: Bad Name\ndescription: p\n---\n")], pattern="lowercase")
        self.assert_rejected([("a/SKILL.md", b"---\nname: same\ndescription: p\n---\n"),
                              ("b/SKILL.md", b"---\nname: same\ndescription: p\n---\n")], pattern="unique")
        nested = self.root / "nested.zip"
        write_zip(nested, {
            "outer/SKILL.md": b"---\nname: outer\ndescription: outer\n---\nOUTER\n",
            "outer/references/a.md": b"OUTER_REF\n",
            "outer/inner/SKILL.md": b"---\nname: inner\ndescription: inner\n---\nINNER\n",
            "outer/inner/references/b.md": b"INNER_REF\n",
        })
        preview = self.preview(nested)
        self.assertEqual(sorted((item["name"], item["file_count"]) for item in preview["skills"]),
                         [("inner", 2), ("outer", 2)])
        outer, inner = [next(item for item in preview["skills"] if item["name"] == name) for name in ("outer", "inner")]
        self.assertEqual([entry["path"] for entry in outer["resources"]], ["SKILL.md", "references/a.md"])
        self.assertEqual([entry["path"] for entry in inner["resources"]], ["SKILL.md", "references/b.md"])

    def test_junk_metadata_and_single_file_markdown_are_supported(self):
        package = self.root / "junk.zip"
        write_zip(package, {
            "probe/SKILL.md": b"---\nname: probe\ndescription: p\n---\nPROBE\n",
            "__MACOSX/._probe": b"resource fork",
            "probe/.DS_Store": b"junk",
        })
        preview = self.preview(package)
        self.assertEqual([(item["name"], item["file_count"]) for item in preview["skills"]], [("probe", 1)])
        markdown = self.root / "single-skill.md"
        markdown.write_bytes(b"---\nname: single-skill\ndescription: one file\n---\nSINGLE\n")
        preview = self.preview(markdown)
        self.assertEqual([(item["name"], item["file_count"]) for item in preview["skills"]], [("single-skill", 1)])
        self.assertEqual(markdown.read_bytes(), b"---\nname: single-skill\ndescription: one file\n---\nSINGLE\n")

    def test_store_rejects_symlinked_or_unsafe_objects(self):
        package = write_zip(self.root / "complex.zip", COMPLEX)
        report = self.install(package)
        digest = report["installed"][0]["digest"]
        stored = self.store / digest / "files/references/a.md"
        stored.unlink()
        stored.symlink_to(self.root / "config.toml")
        with self.assertRaises(RuntimeContractError):
            skill_packages.verify(self.config, digest)
        # A symlinked store root never becomes a write or read target.
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "skill-packages"
            link.symlink_to(self.store, target_is_directory=True)
            with self.assertRaises(RuntimeContractError):
                skill_packages.store(link, {"SKILL.md": b"---\nname: x\ndescription: d\n---\n"})
            with self.assertRaises(RuntimeContractError):
                skill_packages.verify(link, digest)

    def _package(self, members):
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        path = self.root / ("direct-%d.zip" % len(list(self.root.glob("direct-*.zip"))))
        path.write_bytes(buffer.getvalue())
        return path


class PackageStoreTests(PackageTestCase):
    def test_digest_is_stable_across_zip_order_and_rejects_tampering(self):
        tree = {name.split("/", 1)[1]: payload for name, payload in COMPLEX.items()}
        first = skill_packages.reference(dict(tree))
        reordered = {name: tree[name] for name in reversed(list(tree))}
        self.assertEqual(first, skill_packages.reference(reordered))
        digest = skill_packages.store(self.config, dict(tree))
        self.assertEqual(digest, first["digest"])
        skill_packages.store(self.config, reordered)
        self.assertEqual(len(list(self.store.iterdir())), 1)
        with self.assertRaises(RuntimeContractError):
            skill_packages.store(self.config, dict(COMPLEX), expected="0" * 64)
        self.assertEqual(skill_packages.read(self.config, digest)["references/a.md"], tree["references/a.md"])
        with self.assertRaises(RuntimeContractError):
            skill_packages.verify(self.config, "../escape")
        self.assertEqual(skill_packages.external_skills([str(self.root / "missing")]), {})

    def test_schema_requires_exactly_one_of_content_or_package(self):
        base = {"name": "probe", "description": "d", "enabled": True}
        valid = {"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": ""}}
        custom.validate({**valid, "skills": [dict(base, content="body")]})
        custom.validate({**valid, "skills": [dict(base, package={"digest": "a" * 64, "files": 1, "bytes": 1})]})
        for bad in (dict(base), dict(base, content="body", package={"digest": "a" * 64, "files": 1, "bytes": 1}),
                    dict(base, package={"digest": "A" * 64, "files": 1, "bytes": 1}),
                    dict(base, package={"digest": "a" * 64, "files": 0, "bytes": 1}),
                    dict(base, package={"digest": "a" * 64, "files": 1})):
            with self.subTest(bad=bad), self.assertRaises(WorkflowError):
                custom.validate({**valid, "skills": [bad]})


if __name__ == "__main__":
    unittest.main()
