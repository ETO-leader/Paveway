import json
import tempfile
import unittest
from pathlib import Path

from oicoach_runtime.workflows import Workflow
from oicoach_runtime.errors import WorkflowError
from oicoach_runtime.customization import snapshot


class WorkflowContextTests(unittest.TestCase):
    def test_only_trusted_template_prefix_moves_to_system(self):
        workflow = Workflow("test", "1", Path.cwd(), "per_task", {"wake": "Static $$ instruction\n${event_json}\n"},
                            instructions_in_system=True)
        envelope = {"payload": {"message": "UNTRUSTED_PAYLOAD ${event_json} $$"}}
        instructions, message = workflow.render_parts("wake", envelope)
        self.assertEqual(instructions, "Static $ instruction")
        self.assertNotIn("UNTRUSTED_PAYLOAD", instructions)
        self.assertEqual(json.loads(message), envelope)

    def test_legacy_templates_keep_their_existing_channel(self):
        workflow = Workflow("test", "1", Path.cwd(), "per_task", {"wake": "Policy\n${event_json}"})
        self.assertEqual(workflow.render_parts("wake", {"message": "user"}), ("", workflow.render("wake", {"message": "user"})))

    def test_ambiguous_or_nontrailing_payload_cannot_be_promoted(self):
        for text in ("${event_json}\n${event_json}", "${event_json}\nsuffix", "$event_json"):
            workflow = Workflow("test", "1", Path.cwd(), "per_task", {"wake": text}, instructions_in_system=True)
            with self.subTest(text=text), self.assertRaises(WorkflowError):
                workflow.render_parts("wake", {"message": "private"})

    def test_snapshot_preserves_user_settings_and_identity_after_workflow_rules(self):
        settings = {"schema_version": 1, "revision": 0, "system_prompt": {"mode": "replace", "text": "USER_SYSTEM"}, "skills": []}
        original = json.dumps(settings)
        with tempfile.TemporaryDirectory() as root:
            argv = snapshot(settings, root, "system-layer", False, workflow_instructions="WORKFLOW_RULES")
            text = Path(argv[1]).read_text()
            self.assertEqual(argv[0], "--system-prompt")
            self.assertLess(text.index("WORKFLOW_RULES"), text.index("USER_SYSTEM"))
            self.assertLess(text.index("USER_SYSTEM"), text.index("Identity rule:"))
            self.assertEqual(json.dumps(settings), original)
