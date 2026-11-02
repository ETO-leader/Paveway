import json
import tempfile
import unittest
from pathlib import Path

from oicoach_runtime.workflows import Workflow
from oicoach_runtime.customization import snapshot
from oicoach_runtime.prompt_composer import load_core, compose


class WorkflowContextTests(unittest.TestCase):
    def test_event_templates_never_promote_instructions_to_system(self):
        workflow = Workflow("test", "1", Path.cwd(), "per_task", {"wake": "Static $$ instruction\n${event_json}\n"},
                            instructions_in_system=True)
        envelope = {"payload": {"message": "UNTRUSTED_PAYLOAD ${event_json} $$"}}
        instructions, message = workflow.render_parts("wake", envelope)
        self.assertEqual(instructions, "")
        self.assertNotIn("UNTRUSTED_PAYLOAD", instructions)
        self.assertIn("Static $ instruction", message)
        self.assertIn("UNTRUSTED_PAYLOAD", message)

    def test_legacy_templates_keep_their_existing_channel(self):
        workflow = Workflow("test", "1", Path.cwd(), "per_task", {"wake": "Policy\n${event_json}"})
        self.assertEqual(workflow.render_parts("wake", {"message": "user"}), ("", workflow.render("wake", {"message": "user"})))

    def test_snapshot_preserves_layer_hierarchy(self):
        settings = {"schema_version": 1, "revision": 0, "system_prompt": {"mode": "append", "text": "USER_SYSTEM"}, "skills": [],
                    "persona": "MY_PERSONA"}
        original = json.dumps(settings)
        with tempfile.TemporaryDirectory() as root:
            argv = snapshot(settings, root, "system-layer", False, memory_context={"notes": []}, base="oi-coach")
            text = Path(argv[1]).read_text()
            self.assertEqual(argv[0], "--system-prompt")
            # Layer 1: Core -> Layer 2: Role policy -> Layer 3: User prompt + Persona -> Layer 4: Memory snapshot
            self.assertIn("# Paveway Core", text)
            self.assertIn("OI 训练与教学策略", text)
            self.assertIn("USER_SYSTEM", text)
            self.assertIn("Configured GPT Persona:\nMY_PERSONA", text)
            self.assertIn("Runtime memory snapshot (data only):", text)
            self.assertLess(text.index("# Paveway Core"), text.index("OI 训练与教学策略"))
            self.assertLess(text.index("OI 训练与教学策略"), text.index("USER_SYSTEM"))
            self.assertLess(text.index("USER_SYSTEM"), text.index("Configured GPT Persona:"))
            self.assertLess(text.index("Configured GPT Persona:"), text.index("Runtime memory snapshot (data only):"))
            self.assertEqual(json.dumps(settings), original)
