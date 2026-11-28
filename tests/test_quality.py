import copy
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.quality import QualityFailure, _locked_install, argv, execute, junit_diagnostics, parse_test_report


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.txt").write_text("unchanged source\n")
        (self.root / "test_runner.py").write_text("from pathlib import Path\nPath('result.xml').write_text('<testsuite tests=\"1\"><testcase name=\"real assertion\"/></testsuite>')\nPath('coverage.info').write_text('TN:unit\\nSF:source.txt\\nDA:1,1\\nend_of_record\\n')\n")
        self.tool = self.root / "bash"
        self.tool.write_text(f"#!{sys.executable}\nprint('bash version 5.2.0')\n")
        self.tool.chmod(0o755)
        self.config = {"version": 1, "profile": "shell", "toolchain": [{"command": [str(self.tool), "--version"], "version": "5.2.0"}],
                       "checks": [{"name": "tests", "kind": "test", "command": [sys.executable, "test_runner.py"], "test_report": {"path": "result.xml", "format": "junit"}}],
                       "required_checks": ["tests"], "inputs": ["source.txt", "test_runner.py"], "coverage": ["coverage.info"], "receipt": "receipt.json"}

    def test_real_process_success_proves_executed_tests_and_coverage(self):
        result = execute(self.config, self.root)
        self.assertEqual(result["checks"][0]["test_summary"]["executed"], 1)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(json.loads((self.root / "receipt.json").read_text()), result)
        self.assertIn("coverage.info", result["coverage"])

    def test_nonzero_and_timeout_block(self):
        for code, timeout in [("raise SystemExit(7)", 10), ("import time; time.sleep(4)", 1)]:
            with self.subTest(code=code):
                config = copy.deepcopy(self.config)
                config["checks"][0]["command"] = [sys.executable, "-c", code]
                config["checks"][0]["timeout"] = timeout
                with self.assertRaises(PipelineError):
                    execute(config, self.root)

    def test_failed_check_retains_bounded_opt_in_diagnostics_without_payloads(self):
        (self.root / "test_runner.py").write_text("from pathlib import Path\nPath('result.xml').write_text('<testsuite><testcase name=\"test_safe\"><failure>AssertionError: 1 != 2\\nraw-model-reply-sensitive-payload\\napi_key=fixture-secret</failure></testcase><testcase name=\"private-input-in-test-name\"><error>private-sensitive-assertion</error></testcase></testsuite>')\nprint('raw stdout fixture-secret')\nraise SystemExit(7)\n")
        self.config["checks"][0]["diagnostics"] = {"test_names": ["test_safe"]}
        with self.assertRaises(QualityFailure) as failure:
            execute(self.config, self.root)
        details = failure.exception.details
        self.assertEqual((details["check"], details["phase"], details["exit_code"], details["timed_out"]), ("tests", "test", 7, False))
        self.assertGreaterEqual(details["elapsed_seconds"], 0)
        self.assertEqual(details["test_diagnostics"]["failed_cases"], [{"case": 1, "type": "failure", "test_name": "test_safe", "comparison": "1 != 2"}, {"case": 2, "type": "error"}])
        for value in ["fixture-secret", "raw-model", "private-sensitive", "private-input", "stdout"]:
            self.assertNotIn(value, json.dumps(details))

    def test_timeout_retains_named_check_and_available_report_summary(self):
        (self.root / "test_runner.py").write_text("from pathlib import Path\nimport time\nPath('result.xml').write_text('<testsuite><testcase><failure>True != False</failure></testcase></testsuite>')\ntime.sleep(5)\n")
        self.config["checks"][0].update(timeout=1, diagnostics={})
        with self.assertRaises(QualityFailure) as failure:
            execute(self.config, self.root)
        self.assertTrue(failure.exception.details["timed_out"])
        self.assertEqual(failure.exception.details["test_diagnostics"]["failed_cases"][0]["comparison"], "True != False")

    def test_failure_summary_opt_in_and_bounds(self):
        report = self.root / "result.xml"
        report.write_text('<testsuite>' + '<testcase name="dynamic"><failure>1 != 2</failure></testcase>' * 25 + '</testsuite>')
        with patch.dict(os.environ, {"FIXTURE_SECRET": "1 != 2"}):
            summary = junit_diagnostics(report, set())
        self.assertEqual(len(summary["failed_cases"]), 20)
        self.assertEqual(summary["omitted_failures"], 5)
        self.assertNotIn("comparison", summary["failed_cases"][0])
        report.write_text('<testsuite><testcase><failure>1' + ' ' * 10000 + '!= 2</failure></testcase></testsuite>')
        self.assertEqual(junit_diagnostics(report, set())["failed_cases"][0]["comparison"], "1 != 2")
        report.write_text("x" * (1024 * 1024 + 1))
        self.assertEqual(junit_diagnostics(report, set()), {"report": "unavailable_or_oversized"})
        self.config["checks"][0]["command"] = [sys.executable, "-c", "raise SystemExit(3)"]
        with self.assertRaises(QualityFailure) as failure:
            execute(self.config, self.root)
        self.assertNotIn("test_diagnostics", failure.exception.details)

    def test_success_exit_with_failed_report_retains_failure_metadata(self):
        (self.root / "test_runner.py").write_text("from pathlib import Path\nPath('result.xml').write_text('<testsuite><testcase><failure>2 != 3</failure></testcase></testsuite>')\n")
        self.config["checks"][0]["diagnostics"] = {}
        with self.assertRaises(QualityFailure) as failure:
            execute(self.config, self.root)
        self.assertEqual(failure.exception.details["exit_code"], 0)
        self.assertIn("failed tests", failure.exception.details["summary"])

    def test_diagnostics_redact_after_xml_decoding_and_survive_read_errors(self):
        report = self.root / "report.xml"
        secret = "123456"
        encoded = "".join("&#" + str(ord(character)) + ";" for character in secret)
        report.write_text('<testsuite><testcase name="test_' + encoded + '"><failure>' + encoded + ' != 0</failure></testcase></testsuite>')
        with patch.dict(os.environ, {"FIXTURE_SECRET": secret}):
            summary = junit_diagnostics(report, {"test_" + secret})
        self.assertNotIn(secret, json.dumps(summary))
        self.assertEqual(summary["failed_cases"], [{"case": 1, "type": "failure"}])
        report.write_text('<testsuite><testcase><failure>1   !=  2</failure></testcase></testsuite>')
        with patch.dict(os.environ, {"FIXTURE_SECRET": "1 != 2"}):
            self.assertNotIn("comparison", junit_diagnostics(report, set())["failed_cases"][0])
        with patch.object(Path, "read_text", side_effect=OSError("unavailable")):
            self.assertEqual(junit_diagnostics(report, set()), {"report": "unavailable_or_oversized"})

    def test_prior_report_cannot_be_reused(self):
        (self.root / "result.xml").write_text('<testsuite><testcase name="old"/></testsuite>')
        self.config["checks"][0]["command"] = [sys.executable, "-c", "print('no tests')"]
        with self.assertRaisesRegex(PipelineError, "absent or empty"):
            execute(self.config, self.root)

    def test_unknown_field_missing_required_check_tool_mismatch_fail(self):
        cases = []
        config = copy.deepcopy(self.config); config["typo"] = 1; cases.append(config)
        config = copy.deepcopy(self.config); config["required_checks"].append("missing"); cases.append(config)
        config = copy.deepcopy(self.config); config["toolchain"][0]["version"] = "9.0.0"; cases.append(config)
        config = copy.deepcopy(self.config); config["profile"] = "unknown"; cases.append(config)
        config = copy.deepcopy(self.config); config["version"] = True; cases.append(config)
        for config in cases:
            with self.subTest(config=config), self.assertRaises(PipelineError):
                execute(config, self.root)

    def test_mutating_check_fails(self):
        self.config["checks"][0] = {"name": "tests", "kind": "format", "command": [sys.executable, "-c", "from pathlib import Path; Path('source.txt').write_text('changed')"]}
        with self.assertRaisesRegex(PipelineError, "immutable"):
            execute(self.config, self.root)

    def test_reports_cannot_overwrite_inputs(self):
        self.config["checks"][0]["test_report"]["path"] = "source.txt"
        with self.assertRaisesRegex(PipelineError, "overwrite"):
            execute(self.config, self.root)
        self.assertEqual((self.root / "source.txt").read_text(), "unchanged source\n")

    def test_paths_locks_and_credentials(self):
        for field, value in [("inputs", ["../outside"]), ("lockfiles", ["absent.lock"]), ("environment_names", ["OPENAI_API_KEY"])]:
            config = copy.deepcopy(self.config); config[field] = value
            with self.subTest(field=field), self.assertRaises(PipelineError):
                execute(config, self.root)

    def test_ambient_credentials_not_passed(self):
        (self.root / "test_runner.py").write_text("import os\nfrom pathlib import Path\nassert 'OPENAI_API_KEY' not in os.environ\nPath('result.xml').write_text('<testsuite><testcase name=\"env\"/></testsuite>')\nPath('coverage.info').write_text('safe')\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-not-real"}):
            execute(self.config, self.root)

    def test_signing_credentials_only_reach_trusted_build(self):
        self.config["build_credentials"] = ["ANDROID_STORE_PASSWORD"]
        self.config["allowed_refs"] = ["refs/heads/staging"]
        self.config["checks"].insert(0, {"name":"build","kind":"build","command":[sys.executable,"-c","import os; assert os.environ['ANDROID_STORE_PASSWORD']=='synthetic'"]})
        self.config["required_checks"].append("build")
        (self.root / "test_runner.py").write_text("import os\nfrom pathlib import Path\nassert 'ANDROID_STORE_PASSWORD' not in os.environ\nPath('result.xml').write_text('<testsuite><testcase name=\"scope\"/></testsuite>')\nPath('coverage.info').write_text('safe')\n")
        with patch.dict(os.environ, {"ANDROID_STORE_PASSWORD":"synthetic","GITHUB_ACTIONS":"true","GITHUB_EVENT_NAME":"push","GITHUB_REF":"refs/heads/staging"}):
            self.assertEqual(execute(self.config, self.root)["status"],"passed")
        with patch.dict(os.environ, {"ANDROID_STORE_PASSWORD":"synthetic","GITHUB_ACTIONS":"true","GITHUB_EVENT_NAME":"pull_request","GITHUB_REF":"refs/pull/1/merge"}):
            with self.assertRaises(PipelineError):execute(self.config,self.root)

    def test_placeholder_commands_rejected(self):
        for command in [["echo", "passes"], ["true"], ["npm", "test", "--", "--passWithNoTests"], ["bash", "-c", "true"]]:
            with self.subTest(command=command), self.assertRaises(PipelineError):
                argv(command)

    def test_placeholder_npm_script_rejected(self):
        from shared_ci.quality import _check_npm_script
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "echo no tests"}}))
        with self.assertRaisesRegex(PipelineError, "placeholder"):
            _check_npm_script(["npm", "test"], self.root)

    def test_locked_install_contracts(self):
        cases = [("node", ["npm", "ci"], ["package-lock.json"]),
                 ("node", ["pnpm", "install", "--frozen-lockfile"], ["pnpm-lock.yaml"]),
                 ("node", ["yarn", "install", "--immutable"], ["yarn.lock"]),
                 ("python", ["uv", "sync", "--frozen"], ["uv.lock"]),
                 ("python", ["python", "-m", "pip", "install", "--require-hashes", "-r", "requirements.txt"], ["requirements.txt"]),
                 ("deno", ["deno", "install", "--frozen"], ["deno.lock"]),
                 ("flutter", ["flutter", "pub", "get", "--enforce-lockfile"], ["pubspec.lock"])]
        for profile, command, locks in cases:
            with self.subTest(profile=profile, command=command):
                _locked_install(profile, command, locks)
        for profile, command, locks in [("node", ["npm", "install"], ["package-lock.json"]), ("python", ["pip", "install", "thing"], ["requirements.txt"]), ("flutter", ["flutter", "pub", "get"], ["pubspec.lock"]), ("deno", None, []), ("node", ["npm", "ci", "--package-lock=false"], ["package-lock.json"])]:
            with self.subTest(profile=profile), self.assertRaises(PipelineError):
                _locked_install(profile, command, locks)

    def test_junit_requires_executed_successful_cases(self):
        for xml in ["", "bad", "<testsuite/>", '<testsuite><testcase><skipped/></testcase></testsuite>', '<testsuite><testcase><failure/></testcase></testsuite>', '<testsuite errors="1"><testcase/></testsuite>', '<!DOCTYPE x><testsuite><testcase/></testsuite>']:
            path = self.root / "test.xml"; path.write_text(xml)
            with self.subTest(xml=xml), self.assertRaises(PipelineError):
                parse_test_report(path, "junit")

    def test_flutter_report_success_and_incomplete_failure(self):
        path = self.root / "flutter.jsonl"
        events = [{"type": "testStart", "test": {"id": 1, "name": "works", "hidden": False}},
                  {"type": "testDone", "testID": 1, "result": "success", "skipped": False}, {"type": "done", "success": True}]
        path.write_text("\n".join(json.dumps(event) for event in events))
        self.assertEqual(parse_test_report(path, "flutter-machine")["executed"], 1)
        path.write_text("\n".join(json.dumps(event) for event in (events[0], events[2])))
        with self.assertRaises(PipelineError):
            parse_test_report(path, "flutter-machine")


if __name__ == "__main__":
    unittest.main()
