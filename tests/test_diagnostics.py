import unittest

from oicoach_runtime.diagnostics import prompt_failure


class FailureSummaryTests(unittest.TestCase):
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
