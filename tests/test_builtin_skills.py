import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from oicoach_runtime import builtin_skills
from oicoach_runtime.errors import WorkflowError


class BuiltinSkillTests(unittest.TestCase):
    def test_invalid_or_missing_packaged_resources_are_rejected(self):
        valid = "---\nname: paveway-test\ndescription: valid description\n---\nValid body\n"
        cases = (
            ("paveway-..-escape", valid),
            ("not-official", valid),
            ("paveway-test", None),
            ("paveway-test", "no metadata"),
            ("paveway-test", "---\nname: paveway-test\n"),
            ("paveway-test", valid.replace("name: paveway-test", "name: paveway-other")),
            ("paveway-test", valid.replace("description: valid description", "description: ''")),
            ("paveway-test", valid.replace("description: valid description", "description: []")),
            ("paveway-test", valid.replace("Valid body", "")),
            ("paveway-test", valid.replace("name: paveway-test", "name: paveway-test\nname: paveway-test")),
            ("paveway-test", valid.replace("Valid body", "\0")),
            ("paveway-test", valid.replace("Valid body", "x" * 65536)),
            ("paveway-test", valid.replace("description: valid description", "description: !!python/object:builtins.object {}")),
        )
        for name, markdown in cases:
            with self.subTest(name=name, markdown=markdown and markdown[:100]), tempfile.TemporaryDirectory() as directory:
                package = Path(directory)
                resource = package / "bundled" / "skills" / name
                resource.mkdir(parents=True)
                if markdown is not None:
                    (resource / "SKILL.md").write_text(markdown, encoding="utf-8")
                with patch.object(builtin_skills, "files", return_value=package), self.assertRaises(WorkflowError):
                    builtin_skills.registry()

    def test_duplicate_resource_registration_is_rejected(self):
        # A malformed Traversable listing must not silently replace a registration.
        package = files("oicoach_runtime")
        resource = package.joinpath("bundled", "skills", "paveway-tijian")
        with patch.object(builtin_skills, "files") as root:
            root.return_value.joinpath.return_value.iterdir.return_value = [resource, resource]
            with self.assertRaisesRegex(WorkflowError, "Duplicate"):
                builtin_skills.registry()

    def test_unknown_and_unsafe_declarations_are_rejected(self):
        for names in (["paveway-unknown"], ["paveway-tijian"] * 2, ["paveway-../escape"],
                      ["paveway-UPPER"], ["paveway-" + "x" * 64], [42], "paveway-tijian"):
            with self.subTest(names=names), self.assertRaises(WorkflowError):
                builtin_skills.validate_names(names)


@unittest.skipUnless(os.environ.get("PAVEWAY_TEST_WHEEL"), "set PAVEWAY_TEST_WHEEL to a real built wheel")
class InstalledWheelTests(unittest.TestCase):
    def test_noneditable_wheel_reads_and_snapshots_resources_outside_repository(self):
        wheel = Path(os.environ["PAVEWAY_TEST_WHEEL"]).resolve(strict=True)
        expected = builtin_skills.registry()["paveway-tijian"].markdown
        with tempfile.TemporaryDirectory(prefix="paveway-wheel-") as directory:
            root = Path(directory)
            environment = root / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True, text=True)
            python = str(environment / "bin" / "python")
            install = subprocess.run([python, "-m", "pip", "install", str(wheel)], capture_output=True, text=True)
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            script = '''
import json, sys
from pathlib import Path
from importlib.resources import files
import oicoach_runtime
from oicoach_runtime.builtin_skills import registry
from oicoach_runtime.customization import snapshot
assert Path(oicoach_runtime.__file__).is_relative_to(Path(sys.prefix))
skill = registry()["paveway-tijian"]
assert skill.markdown == files("oicoach_runtime").joinpath("bundled", "skills", skill.name, "SKILL.md").read_text(encoding="utf-8")
settings = {"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": ""}, "skills": []}
argv = snapshot(settings, Path.cwd() / "sessions", "wheel-run", True, builtin_skills=[skill.name])
root = Path(json.loads(Path(argv[argv.index("--config") + 1]).read_text())["skills"]["customDirectories"][0])
assert (root / skill.name / "SKILL.md").read_text() == skill.markdown
assert skill.body not in Path(argv[1]).read_text()
print(json.dumps({"markdown": skill.markdown, "package": oicoach_runtime.__file__}))
'''
            result = subprocess.run([python, "-I", "-c", script], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["markdown"], expected)
