import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError, sha256_file
from shared_ci.security import normalize, policy, require_fresh, scan, scanner_intelligence, semgrep_rules


class SecurityTests(unittest.TestCase):
    def test_duplicate_findings_keep_highest_severity_in_either_order(self):
        for levels in (("CRITICAL", "LOW"), ("LOW", "CRITICAL")):
            report = {"Results": [{"Vulnerabilities": [
                {"VulnerabilityID": "CVE-fixture", "Severity": severity, "PkgName": "dependency"}
                for severity in levels]}]}
            with self.subTest(levels=levels):
                findings = normalize("trivy", report)
                self.assertEqual(findings, [{"id": "CVE-fixture", "severity": "critical", "component": "dependency"}])
                self.assertEqual(policy(findings, "critical", [])["blocking"], 1)

    def test_duplicate_findings_across_real_scanner_processes_keep_blocker(self):
        for levels in (("ERROR", "INFO"), ("INFO", "ERROR")):
            with self.subTest(levels=levels), tempfile.TemporaryDirectory() as root:
                rule = Path(root) / "rules.yml"; rule.write_text("rules: []\n")
                scans = []
                for index, level in enumerate(levels):
                    report = {"results": [{"check_id": "shared-rule", "path": "app.py", "extra": {"severity": level}}], "errors": []}
                    tool = Path(root) / str(index) / "semgrep"; tool.parent.mkdir()
                    tool.write_text(f"#!{sys.executable}\nimport sys\nprint('1.0.0' if '--version' in sys.argv else {json.dumps(report)!r})\n")
                    tool.chmod(0o755)
                    scans.append({"id": f"scanner-{index}", "format": "semgrep",
                                  "command": [str(tool), "scan", "--config", "rules.yml", "--json", "."],
                                  "version_command": [str(tool), "--version"], "version": "1.0.0", "output": "stdout",
                                  "rules_identity": {"rules.yml": sha256_file(rule)}, "database_identity": "none"})
                config = {"schema": 1, "threshold": "high", "max_age_hours": 24, "scans": scans}
                result = scan(config, root)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["counts"], {"findings": 1, "blocking": 1, "excepted": 0})
                self.assertEqual(result["findings"], [{"id": "shared-rule", "component": "app.py", "severity": "high"}])
                self.assertEqual([scanner["finding_count"] for scanner in result["scanners"]], [1, 1])

    def test_semgrep_configs_match_all_checksummed_local_rule_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "backend").mkdir(); (root / "rules").mkdir()
            first, second = root / "rules/one.yml", root / "rules/two.yaml"
            first.write_text("rules: []\n"); second.write_text("rules: [] # second\n")
            rules = {"rules/one.yml": sha256_file(first), "rules/two.yaml": sha256_file(second)}
            item = {"rules_identity": rules, "command": ["semgrep", "scan", "--config=../rules/one.yml", "-c", "../rules/two.yaml", "--json", "."],
                    "version_command": ["semgrep", "--version"]}
            self.assertEqual(semgrep_rules(item, root, root / "backend"), rules)
            item["command"] = ["semgrep", "scan", "-f../rules/one.yml", "-c../rules/two.yaml", "."]
            self.assertEqual(semgrep_rules(item, root, root / "backend"), rules)

    def test_semgrep_registry_auto_mismatched_missing_and_mutated_rules_fail_before_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); rule = root / "rules.yml"; rule.write_text("rules: []\n")
            other = root / "other.yml"; other.write_text("rules: [] # other\n")
            base = {"id": "semgrep", "format": "semgrep", "version": "1.0.0", "output": "stdout",
                    "version_command": ["semgrep", "--version"], "rules_identity": {"rules.yml": sha256_file(rule)}, "database_identity": "none"}
            commands = [["semgrep", "scan", "--config", value] for value in
                        ("auto", "p/default", "r/python", "https://example.invalid/rules.yml", "rules", "other.yml")]
            commands += [["semgrep", "scan", "--json"], ["semgrep", "ci", "--config", "rules.yml"],
                         ["semgrep", "scan", "--config", "rules.yml", "--config=p/default"],
                         ["semgrep", "scan", "--config", "rules.yml", "-f", "rules.yml"],
                         ["semgrep", "scan", "--", "--config", "rules.yml"],
                         ["semgrep", "scan", "--config", "rules.yml", "--pattern=x"]]
            for command in commands:
                config = {"schema": 1, "threshold": "high", "max_age_hours": 24, "scans": [{**base, "command": command}]}
                with self.subTest(command=command), patch("shared_ci.security.run") as process, self.assertRaises(PipelineError):
                    scan(config, root)
                process.assert_not_called()
            valid = {**base, "command": ["semgrep", "scan", "--config", "rules.yml"]}
            for declaration in ("builtin:1.0.0", {}, {"rules.yml": "0" * 64}, {"rules.yml": sha256_file(rule), "other.yml": sha256_file(other)}):
                with self.subTest(declaration=declaration), self.assertRaises(PipelineError):
                    semgrep_rules({**valid, "rules_identity": declaration}, root, root)
            alias = root / "alias.yml"; alias.symlink_to(rule)
            with self.assertRaises(PipelineError):
                semgrep_rules({**valid, "command": ["semgrep", "scan", "--config", "alias.yml"]}, root, root)
            rule.write_text("rules: [] # changed\n")
            with self.assertRaisesRegex(PipelineError, "checksums"):
                semgrep_rules(valid, root, root)

    def test_missing_or_malformed_sarif_results_never_mean_clean_scan(self):
        driver = {"driver": {"name": "CodeQL"}}
        runs = [{"tool": driver}, *({"tool": driver, "results": value} for value in (None, {}, "", False))]
        for analysis in runs:
            with self.subTest(analysis=analysis), self.assertRaisesRegex(PipelineError, "results"):
                normalize("sarif", {"version": "2.1.0", "runs": [analysis]})
        self.assertEqual(normalize("sarif", {"version": "2.1.0", "runs": [{"tool": driver, "results": []}]}), [])
        with self.assertRaises(PipelineError):
            normalize("sarif", {"version": "2.1.0", "runs": [{"tool": driver, "results": []}, {"tool": driver}]})
        with self.assertRaises(PipelineError):
            normalize("sarif", {"version": "2.1.0", "runs": [{"tool": driver, "results": [{"ruleId": "fixture", "locations": []}]}]})

    def test_gitleaks_disabled_partial_or_fake_redaction_refused_before_scan(self):
        invalid = [[], ["--redact=0"], ["--redact=false"], ["--redact=20"], ["--redacted"],
                   ["--redact", "--redact=0"], ["--redact=0", "--redact=100"],
                   ["--redact", "0"], ["--redact", "false"], ["--", "--redact"]]
        for flags in invalid + [["--redact"], ["--redact=100"]]:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as root:
                command = [sys.executable, "-c", "from pathlib import Path; Path('ran').touch(); print('[]')", *flags]
                config = {"schema": 1, "threshold": "high", "max_age_hours": 24, "scans": [{
                    "id": "fixture", "format": "gitleaks", "command": command,
                    "version_command": [sys.executable, "-c", "print('1.0.0')"], "version": "1.0.0", "output": "stdout",
                    "rules_identity": "builtin:1.0.0", "database_identity": "none"}]}
                if flags in invalid:
                    with self.assertRaisesRegex(PipelineError, "redact"):
                        scan(config, root)
                    self.assertFalse((Path(root) / "ran").exists())
                else:
                    self.assertEqual(scan(config, root)["status"], "passed")
                    self.assertTrue((Path(root) / "ran").exists())

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
