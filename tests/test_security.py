import datetime as dt
import json
import sys
import tempfile
import unittest

from shared_ci.common import PipelineError
from shared_ci.security import normalize, policy, require_fresh, scan, scanner_intelligence


class SecurityTests(unittest.TestCase):
    def test_stale_actual_database_is_refused(self):
        item={"format":"trivy","version":"1.0.0","rules_identity":"builtin:1.0.0","database_identity":{
            "command":[sys.executable,"-c","print('{\"VulnerabilityDB\":{\"Version\":2,\"UpdatedAt\":\"2000-01-01T00:00:00Z\"}}')"],"max_age_hours":24}}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(PipelineError,"stale"):
                scanner_intelligence(item,root,root)
    def test_codeql_cvss_severity_overrides_sarif_display_level(self):
        report = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "CodeQL", "rules": [
            {"id": "example", "properties": {"security-severity": "9.8"}}]}},
            "results": [{"ruleId": "example", "level": "warning"}]}]}
        self.assertEqual(normalize("sarif", report)[0]["severity"], "critical")

    def test_stale_security_receipt_cannot_be_reused(self):
        receipt = {"status": "passed", "policy_identity": "same", "checked_at": "2026-01-01T00:00:00+00:00", "max_age_hours": 24}
        with self.assertRaises(PipelineError):
            require_fresh(receipt, "same", at=dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc))
    def test_secret_value_never_leaves_normalizer(self):
        result = normalize("gitleaks", [{"RuleID": "api-key", "Fingerprint": "f1", "File": "app.py", "Secret": "synthetic-secret"}])
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertEqual(policy(result, "high", [])["blocking"], 1)

    def test_error_schema_is_not_clean_scan(self):
        for kind, data in (("npm", {"error": {}}), ("trivy", {}), ("sarif", {"version": "2.1.0", "runs": []}), ("semgrep", {"results": [], "errors": ["bad"]})):
            with self.assertRaises(PipelineError):
                normalize(kind, data)

    def test_unknown_severity_blocks_and_exceptions_expire(self):
        findings = normalize("pip-audit", {"dependencies": [{"name": "example", "vulns": [{"id": "PYSEC-example"}]}]})
        self.assertEqual(policy(findings, "critical", [])["blocking"], 1)
        exception = {"id": "PYSEC-example", "component": "example", "owner": "maintainer", "reason": "isolated fixture",
                     "expires_at": "2030-01-01T00:00:00+00:00"}
        self.assertEqual(policy(findings, "high", [exception], dt.datetime(2029, 1, 1, tzinfo=dt.timezone.utc))["blocking"], 0)
        with self.assertRaises(PipelineError):
            policy(findings, "high", [exception], dt.datetime(2031, 1, 1, tzinfo=dt.timezone.utc))

    def test_actual_command_failure_cannot_return_clean(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = {"schema": 1, "threshold": "high", "max_age_hours": 24, "scans": [{
                "id": "fixture", "format": "gitleaks", "command": [sys.executable, "-c", "print('[]'); raise SystemExit(1)", "--redact"],
                "version_command": [sys.executable, "-c", "print('1.0.0')"], "version": "1.0.0", "output": "stdout",
                "rules_identity": "builtin:1.0.0", "database_identity": "none"}]}
            with self.assertRaises(PipelineError):
                scan(cfg, root)
            cfg["scans"][0]["command"][2] = "print('[]')"
            self.assertEqual(scan(cfg, root)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
