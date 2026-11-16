import unittest
from shared_ci.common import PipelineError
from shared_ci.plan import decide


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.config={"schema":1,"shared_paths":[".github/**"],"products":{"app":{"paths":["app/**"]},"site":{"paths":["site/**"]}}}

    def test_feature_checks_only_changed_product(self):
        event={"pull_request":{"head":{"ref":"feature/test"},"base":{"ref":"develop"}}}
        result=decide(self.config,["site/index.html"],event,event_name="pull_request",ref="refs/pull/1/merge")
        self.assertTrue(result["products"]["site"]["check"])
        self.assertFalse(result["products"]["app"]["check"])

    def test_promotion_verifies_and_does_not_request_duplicate_checks(self):
        for head,base in [("develop","staging"),("staging","main")]:
            event={"pull_request":{"head":{"ref":head},"base":{"ref":base}}}
            result=decide(self.config,[".github/workflows/ci.yml"],event,event_name="pull_request",ref="refs/pull/1/merge")
            self.assertTrue(result["products"]["app"]["verify"])
            self.assertFalse(result["products"]["app"]["check"])

    def test_invalid_route_and_privileged_pr_event_fail(self):
        event={"pull_request":{"head":{"ref":"feature/skip"},"base":{"ref":"main"}}}
        for name in ["pull_request","pull_request_target"]:
            with self.assertRaises(PipelineError):decide(self.config,["app/a"],event,event_name=name,ref="refs/heads/main")
