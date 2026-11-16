import unittest

from shared_ci.common import PipelineError
from shared_ci.gate import evaluate


class GateTests(unittest.TestCase):
    def test_missing_cancelled_skipped_required_checks_fail(self):
        policy = {"schema": 1, "checks": {"test": True}}
        for result in ({}, {"test": {"result": "cancelled"}}, {"test": {"result": "skipped"}}, {"test": {"result": "failure"}}):
            with self.assertRaises(PipelineError):
                evaluate(policy, result)

    def test_explicit_docs_only_native_skip(self):
        result = evaluate({"schema": 1, "checks": {"docs": True, "native": False}},
                          {"docs": {"result": "success"}, "native": {"result": "skipped"}})
        self.assertEqual(result["checks"]["native"], "not-applicable")


if __name__ == "__main__":
    unittest.main()
