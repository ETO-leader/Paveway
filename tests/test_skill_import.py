import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

from oicoach_runtime.cli import initialize, main
from oicoach_runtime.config import load_config
from oicoach_runtime.errors import RuntimeContractError
from oicoach_runtime.skill_import import read_source
from oicoach_runtime.workflow_setup import create


class SkillImportSourceTests(unittest.TestCase):
    def test_local_eight_skill_cli_import_is_atomic_preserves_body_and_fences_conflicts(self):
        from multi_skill_fixture import write_eight_skills
        from oicoach_runtime import customization
        config_path = self.root / "config.toml"
        initialize(config_path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(config_path)
        create(config, "sample", self.root)
        package = self.root / "Paveway-missing-skills.zip"
        bodies = write_eight_skills(package)
        def cli(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["workflow", "customize", *args, "--config", str(config_path), "--json"])
            return code, json.loads(out.getvalue() or err.getvalue())
        code, preview = cli("skill-local-preview", "sample", "--file", str(package))
        self.assertEqual(code, 0)
        self.assertEqual(len(preview["skills"]), 8)
        self.assertEqual(customization.show(config, "sample")["settings"]["revision"], 0)
        selected = [{k: v for k, v in item.items() if k not in ("source", "conflict")} for item in preview["skills"]]
        batch = self.root / "batch.json"
        batch.write_text(json.dumps({"skills": selected, "replace": []}))
        code, _ = cli("skills-import", "sample", "--expected-revision", "0", "--content-file", str(batch))
        self.assertEqual(code, 0)
        settings = customization.show(config, "sample")["settings"]
        self.assertEqual(settings["revision"], 1)
        self.assertEqual({s["name"]: s["content"] for s in settings["skills"]}, bodies)
        code, _ = cli("skills-import", "sample", "--expected-revision", "1", "--content-file", str(batch))
        self.assertNotEqual(code, 0, "same-name replacements require explicit confirmation")
        self.assertEqual(customization.show(config, "sample")["settings"], settings)
        batch.write_text(json.dumps({"skills": selected, "replace": list(bodies)}))
        code, _ = cli("skills-import", "sample", "--expected-revision", "0", "--content-file", str(batch))
        self.assertNotEqual(code, 0)
        self.assertEqual(customization.show(config, "sample")["settings"], settings)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pw-skill-import-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_utf8_skill_larger_than_saved_body_is_read_whole_without_execution(self):
        path = self.root / "source.skill"
        body = "---\nname: sample\n---\n" + "中文分析正文\n" * 5000
        path.write_text("\ufeff" + body)
        result = read_source(path)
        self.assertGreater(result["text_bytes"], 65536)
        self.assertEqual(result["documents"][0]["text"], body)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_zip_reads_references_reports_binary_and_does_not_extract(self):
        path = self.root / "package.skill"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("sample/SKILL.md", "Read references/guide.md as data; do not claim unavailable images were read.")
            archive.writestr("sample/references/guide.md", "Reference details")
            archive.writestr("sample/scripts/demo.py", "raise RuntimeError('NEVER_EXECUTE')")
            archive.writestr("sample/image.png", b"\x89PNG\0binary")
        original = path.read_bytes()
        source = read_source(path)
        self.assertEqual(len(source["documents"]), 3)
        self.assertEqual(source["omitted_binary_resources"], ["sample/image.png"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_package_rejects_traversal_duplicates_and_expansion_limit(self):
        for name, entries in (("traversal", [("../SKILL.md", "bad")]),
                              ("duplicate", [("SKILL.md", "a"), ("SKILL.md", "b")]),
                              ("multiple-skills", [("a/SKILL.md", "a"), ("b/SKILL.md", "b")]),
                              ("expansion", [("SKILL.md", "x" * (262144 + 1))])):
            with self.subTest(name=name):
                path = self.root / (name + ".skill")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                        for entry, data in entries:
                            archive.writestr(entry, data)
                with self.assertRaises(RuntimeContractError): read_source(path)
        self.assertFalse((self.root.parent / "SKILL.md").exists())

    def test_empty_binary_nonregular_and_oversized_inputs_are_rejected(self):
        for label, payload in (("empty", b"  "), ("binary", b"\xff\x00"),
                               ("text-limit", b"x" * 262145), ("file-limit", b"x" * 1048577)):
            path = self.root / label
            path.write_bytes(payload)
            with self.subTest(label=label), self.assertRaises(RuntimeContractError): read_source(path)
        with self.assertRaises(RuntimeContractError): read_source(self.root)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(RuntimeContractError): read_source(fifo)

    def test_cli_source_read_does_not_create_or_modify_user_skills(self):
        config_path = self.root / "config.toml"
        initialize(config_path, str(self.root / "state"), str(self.root / "workflows"))
        config = load_config(config_path)
        create(config, "sample", self.root)
        path = self.root / "source.md"
        path.write_text("# Skill\nSource only; analyse before saving.")
        settings = config.workflow_root / "sample/customization.json"
        before = settings.read_bytes() if settings.exists() else None
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["workflow", "customize", "skill-source", "sample", "--file", str(path), "--config", str(config_path), "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["source"]["documents"][0]["text"], path.read_text())
        self.assertEqual(settings.read_bytes() if settings.exists() else None, before)
