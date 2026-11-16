import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError, sha256_file
from shared_ci.quality import input_digest
from shared_ci.sonar import execute, validate_coverage_receipt


class SonarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir(); (self.root / "src/app.js").write_text('const result = 1;')
        (self.root / "coverage.info").write_text('TN:unit\nSF:src/app.js\nDA:1,1\nend_of_record\n')
        self.identity = {"repository": "example/product", "run_id": "123", "source_sha": "a" * 40}
        inputs = {"src/app.js": sha256_file(self.root / "src/app.js")}
        self.receipt = {"schema_version": 1, "kind": "quality", "status": "passed", "created_at": datetime.now(timezone.utc).isoformat(),
                        "provenance": {"repository": "example/product", "run_id": "123", "sha": "a" * 40}, "inputs": inputs,
                        "input_paths": ["src"], "input_digest": input_digest(inputs),
                        "checks": [{"kind": "test", "status": "passed", "test_summary": {"executed": 2, "failed": 0}}],
                        "coverage": {"coverage.info": sha256_file(self.root / "coverage.info")}}
        self.coverage = [{"path": "coverage.info", "property": "sonar.javascript.lcov.reportPaths"}]
        self.config = {"version": 1, "mode": "branch", "project_key": "example-product", "host_url": "https://sonarcloud.io",
                       "source_paths": ["src"], "quality_receipt": "quality.json", "coverage": self.coverage, "branch": "develop",
                       "scanner": {"command": ["sonar-scanner"], "version": "6.2.1"}, "allowed_refs": ["refs/heads/develop"], "receipt": "sonar.json"}
        (self.root / "quality.json").write_text(json.dumps(self.receipt))
        self.environment = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/develop",
                            "GITHUB_REPOSITORY": "example/product", "GITHUB_SHA": "a" * 40, "SONAR_TOKEN": "synthetic-only"}

    def validate(self, receipt=None):
        return validate_coverage_receipt(receipt or self.receipt, self.root, self.coverage, **self.identity)

    def test_matching_receipt(self):
        self.assertEqual(self.validate(), self.receipt["coverage"])

    def test_stale_identity_source_coverage_and_new_source_fail(self):
        changed = copy.deepcopy(self.receipt); changed["provenance"]["run_id"] = "999"
        with self.assertRaises(PipelineError): self.validate(changed)
        changed = copy.deepcopy(self.receipt); changed["created_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.assertRaises(PipelineError): self.validate(changed)
        (self.root / "src/new.js").write_text('new input')
        with self.assertRaises(PipelineError): self.validate()
        (self.root / "src/new.js").unlink()
        (self.root / "coverage.info").write_text('replaced coverage')
        with self.assertRaises(PipelineError): self.validate()

    def test_empty_tests_and_forged_fingerprint_fail(self):
        for key, value in [("checks", []), ("input_digest", "fake"), ("created_at", "invalid")]:
            changed = copy.deepcopy(self.receipt); changed[key] = value
            with self.subTest(key=key), self.assertRaises(PipelineError): self.validate(changed)

    def fake_run(self, command, **kwargs):
        if command[-1] == '--version':
            return subprocess.CompletedProcess(command, 0, 'SonarScanner CLI 6.2.1\n', '')
        self.assertEqual(kwargs.get('env_names'), ('SONAR_TOKEN',))
        self.assertFalse(any('synthetic-only' in arg for arg in command))
        self.assertIn('-Dsonar.qualitygate.wait=true', command)
        self.last_command = command
        return subprocess.CompletedProcess(command, 0, '', '')

    def test_branch_analysis_waits_for_gate_without_rerunning_tests(self):
        with patch.dict(os.environ, self.environment), patch('shared_ci.sonar.run', side_effect=self.fake_run) as runner:
            result = execute(self.config, self.root, **self.identity)
        self.assertTrue(result['quality_gate_waited'])
        self.assertEqual(runner.call_count, 2)
        self.assertIn('-Dsonar.branch.name=develop', self.last_command)

    def test_same_repo_allowed_pr_uses_pr_parameters(self):
        config = copy.deepcopy(self.config); config['mode'] = 'pull_request'; config.pop('branch')
        config['pull_request'] = {'key': '17', 'branch': 'feature/checks', 'base': 'develop'}
        config['allowed_refs'] = ['refs/heads/feature/checks']
        event = self.root / 'event.json'; event.write_text(json.dumps({'pull_request': {'head': {'ref': 'feature/checks', 'repo': {'full_name': 'example/product'}}}}))
        environment = dict(self.environment, GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event), GITHUB_REF='refs/pull/17/merge')
        with patch.dict(os.environ, environment), patch('shared_ci.sonar.run', side_effect=self.fake_run):
            execute(config, self.root, **self.identity)
        self.assertIn('-Dsonar.pullrequest.base=develop', self.last_command)
        self.assertFalse(any(arg.startswith('-Dsonar.branch.name=') for arg in self.last_command))

    def test_fork_or_missing_token_never_reports_success(self):
        environment = dict(self.environment, GITHUB_EVENT_NAME='pull_request_target')
        with patch.dict(os.environ, environment), self.assertRaises(PipelineError):
            execute(self.config, self.root, **self.identity)
        environment = dict(self.environment, SONAR_TOKEN='')
        with patch.dict(os.environ, environment), self.assertRaisesRegex(PipelineError, 'absent'):
            execute(self.config, self.root, **self.identity)

    def test_scanner_error_and_version_mismatch_block(self):
        for side_effect in [PipelineError('scanner failed'), lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, '9.9.0', '')]:
            with patch.dict(os.environ, self.environment), patch('shared_ci.sonar.run', side_effect=side_effect), self.assertRaises(PipelineError):
                execute(self.config, self.root, **self.identity)

    def test_invalid_host_config_and_metadata_rejected(self):
        for field, value in [('host_url', 'http://sonarcloud.io'), ('host_url', 'https://token@sonarcloud.io'), ('typo', True), ('source_paths', ['../outside'])]:
            config = copy.deepcopy(self.config); config[field] = value
            with patch.dict(os.environ, self.environment), self.subTest(field=field), self.assertRaises(PipelineError):
                execute(config, self.root, **self.identity)
        with self.assertRaises(PipelineError):
            validate_coverage_receipt(self.receipt, self.root, self.coverage, repository='', run_id='123', source_sha='a'*40)


if __name__ == '__main__':
    unittest.main()
