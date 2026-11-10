import unittest

from oicoach_runtime.diagnostics import prompt_failure
from oicoach_runtime.diagnostics import failure_hint


class FailureSummaryTests(unittest.TestCase):
    def test_recovery_hints_distinguish_stage_and_do_not_authorize_replay(self):
        for code, stage in (("agent_auth_failed", "model_authentication"),
                            ("agent_rpc_ready_missing", "host_startup"),
                            ("agent_session_persistence_error", "persistence"),
                            ("session_uncertain", "binding"),
                            ("native_wakeup_snapshot_missing", "configuration"),
                            ("memory_projection_failed", "memory")):
            with self.subTest(code=code):
                result = failure_hint(code, True, "uncertain")
                self.assertEqual(result["stage"], stage)
                self.assertTrue(result["outcome_unknown"])
                self.assertFalse(result["automatic_replay"])
                self.assertTrue(result["next_action"])

    def test_provider_failures_have_safe_actionable_summaries(self):
        for http, code in ((401, "agent_auth_failed"), (403, "agent_access_denied"),
                           (404, "agent_model_not_found"), (429, "agent_rate_limited"),
                           (408, "agent_provider_timeout"), (503, "agent_provider_unavailable")):
            with self.subTest(http=http):
                actual, message = prompt_failure("error", {"httpStatus": http, "message": "private-provider-payload"})
                self.assertEqual(actual, code)
                self.assertNotIn("private-provider-payload", message)
        self.assertEqual(prompt_failure("error", {"message": "401 Invalid API key sk-private"})[0], "agent_auth_failed")
        self.assertEqual(prompt_failure("error", None)[0], "agent_turn_failed")
        self.assertEqual(prompt_failure("error", {"message": {"untrusted": True}})[0], "agent_turn_failed")
        self.assertEqual(prompt_failure("aborted", None)[0], "agent_aborted")
        self.assertEqual(prompt_failure("completed", {"httpStatus": 401}), (None, None))
