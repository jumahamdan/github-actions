import os
import unittest
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.gate import evaluate


class GateTests(unittest.TestCase):
    def setUp(self):
        self.staging = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
                        "GITHUB_REF": "refs/heads/staging"}
        self.policy = {"schema": 1, "checks": {"distribute": True}, "staging_submissions": ["distribute"]}
        self.submitted = {"distribute": {"result": "success", "outputs": {"status": "pending", "state": "submitted"}}}

    def test_missing_cancelled_skipped_required_checks_fail(self):
        policy = {"schema": 1, "checks": {"test": True}}
        for result in ({}, {"test": {"result": "cancelled"}}, {"test": {"result": "skipped"}}, {"test": {"result": "failure"}}):
            with self.assertRaises(PipelineError):
                evaluate(policy, result)

    def test_explicit_docs_only_native_skip(self):
        result = evaluate({"schema": 1, "checks": {"docs": True, "native": False}},
                          {"docs": {"result": "success"}, "native": {"result": "skipped"}})
        self.assertEqual(result["checks"]["native"], "not-applicable")

    def test_explicit_staging_submission_is_reported_truthfully(self):
        with patch.dict(os.environ, self.staging):
            result = evaluate(self.policy, self.submitted)
        self.assertEqual(result, {"schema": 1, "status": "passed", "checks": {"distribute": "submitted"}})

    def test_submission_policy_cannot_be_used_in_release_pr_or_local_context(self):
        for change in ({"GITHUB_REF": "refs/heads/main"}, {"GITHUB_REF": "refs/heads/develop"},
                       {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_EVENT_NAME": "pull_request_target"},
                       {"GITHUB_ACTIONS": "false"}):
            with self.subTest(change=change), patch.dict(os.environ, {**self.staging, **change}):
                with self.assertRaises(PipelineError):
                    evaluate(self.policy, self.submitted)

    def test_only_successful_exact_pending_submitted_is_accepted(self):
        cases = [{"result": value, "outputs": {"status": "pending", "state": "submitted"}}
                 for value in ("failure", "cancelled", "skipped")]
        cases += [{"result": "success", "outputs": outputs} for outputs in (
            {}, {"status": "pending"}, {"status": "advisory", "state": "submitted"},
            *({"status": "pending", "state": value} for value in
              ("draft", "console_review_required", "processing", "processing_timeout", "beta_processing_or_review")),
        )]
        with patch.dict(os.environ, self.staging):
            for value in cases:
                with self.subTest(value=value), self.assertRaises(PipelineError):
                    evaluate(self.policy, {"distribute": value})

    def test_default_and_unnamed_checks_keep_rejecting_pending(self):
        for policy in ({"schema": 1, "checks": {"distribute": True}},
                       {"schema": 1, "checks": {"distribute": True, "other": True}, "staging_submissions": ["other"]}):
            results = {**self.submitted, **({"other": {"result": "success", "outputs": {"status": "passed"}}}
                                           if "other" in policy["checks"] else {})}
            with patch.dict(os.environ, self.staging), self.assertRaisesRegex(PipelineError, "pending"):
                evaluate(policy, results)

    def test_malformed_or_inapplicable_submission_policies_fail(self):
        for selection in ([], "distribute", {}, [None], [["distribute"]], ["distribute", "distribute"], ["unknown"]):
            with self.subTest(selection=selection), self.assertRaises(PipelineError):
                evaluate({**self.policy, "staging_submissions": selection}, self.submitted)
        with self.assertRaises(PipelineError):
            evaluate({**self.policy, "checks": {"distribute": False}}, self.submitted)


if __name__ == "__main__":
    unittest.main()
