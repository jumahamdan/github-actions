import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from shared_ci import evidence
from shared_ci.common import PipelineError, sha256_file
from shared_ci.quality import input_digest

LIBRARY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sonar_wrapper', LIBRARY / 'scripts/run-module.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
FETCH_RECEIPT = evidence.fetch_receipt


class SonarWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        (self.root / 'src').mkdir()
        (self.root / 'src/app.js').write_text('export const result = 1;\n')
        self.config = {'version': 1, 'mode': 'branch', 'project_key': 'synthetic', 'host_url': 'https://sonarcloud.io',
                       'source_paths': ['src'], 'quality_receipt': 'reports/quality.json',
                       'coverage': [{'path': 'coverage/lcov.info', 'property': 'sonar.javascript.lcov.reportPaths'}],
                       'branch': 'develop', 'scanner': {'command': ['sonar-scanner'], 'version': '8.3.1.16491'},
                       'allowed_refs': ['refs/heads/develop']}
        self.config_path = self.root / 'sonar.json'
        self.config_path.write_text(json.dumps(self.config))
        for args in (['init', '-q'], ['config', 'user.name', 'Synthetic'], ['config', 'user.email', 'fixture@example.invalid'],
                     ['add', '.'], ['commit', '-qm', 'source']):
            self.git(*args)
        self.source = self.git('rev-parse', 'HEAD')
        self.coverage = b'TN:unit\nSF:src/app.js\nDA:1,1\nend_of_record\n'
        inputs = {'src/app.js': sha256_file(self.root / 'src/app.js')}
        self.receipt = {'schema_version': 1, 'kind': 'quality', 'status': 'passed',
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'provenance': {'repository': 'example/product', 'run_id': '32', 'sha': self.source},
                        'inputs': inputs, 'input_paths': ['src'], 'input_digest': input_digest(inputs),
                        'checks': [{'kind': 'test', 'status': 'passed', 'test_summary': {'executed': 2, 'failed': 0}}],
                        'coverage': {'coverage/lcov.info': hashlib.sha256(self.coverage).hexdigest()}}
        self.metadata = {'id': 32, 'status': 'in_progress', 'conclusion': None, 'head_sha': self.source,
                         'head_branch': 'develop', 'head_repository': {'full_name': 'example/product'},
                         'path': '.github/workflows/ci.yml', 'run_attempt': 2, 'event': 'push',
                         'run_started_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:01:00Z'}
        self.job = {'id': 88, 'run_id': 32, 'run_attempt': 2, 'head_sha': self.source, 'name': 'quality / run',
                    'status': 'completed', 'conclusion': 'success', 'started_at': '2026-01-01T00:02:00Z',
                    'completed_at': '2026-01-01T00:06:00Z'}
        self.item = {'id': 11, 'name': 'quality-run-32-attempt-2', 'expired': False,
                     'created_at': '2026-01-01T00:05:00Z', 'workflow_run': {'id': 32, 'head_sha': self.source}}
        self.requests = []
        self.scanner_calls = []
        self.fetched = []
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'GITHUB_ACTIONS': 'true',
                    'GITHUB_WORKSPACE': str(self.root), 'GITHUB_OUTPUT': str(Path(self.temp.name) / 'outputs'),
                    'RUNNER_TEMP': self.temp.name, 'GITHUB_REPOSITORY': 'example/product', 'GITHUB_RUN_ID': '32',
                    'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/develop',
                    'GITHUB_SHA': self.source, 'GH_TOKEN': 'synthetic-gh', 'SONAR_TOKEN': 'synthetic-sonar',
                    'SHARED_MODULE': 'sonar', 'SHARED_CONFIG': 'sonar.json', 'SHARED_ACTION_REF': 'a' * 40,
                    'QUALITY_RUN_ID': '32', 'QUALITY_RUN_ATTEMPT': '2', 'QUALITY_SOURCE_SHA': self.source,
                    'QUALITY_RUN_HEAD_SHA': self.source, 'QUALITY_WORKFLOW': '.github/workflows/ci.yml',
                    'QUALITY_ARTIFACT': self.item['name'], 'QUALITY_PRODUCER_JOB': 'quality / run',
                    'QUALITY_ALLOWED_BRANCHES': '["develop"]', 'QUALITY_ALLOWED_EVENTS': '["push","workflow_dispatch"]'}
        self.bundle()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def bundle(self, coverage=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('receipt.json', json.dumps(self.receipt))
            for digest in self.receipt['coverage'].values():
                archive.writestr('coverage/' + digest, self.coverage if coverage is None else coverage)
        self.blob = output.getvalue()
        self.item.update(digest='sha256:' + hashlib.sha256(self.blob).hexdigest(), size_in_bytes=len(self.blob))

    def request(self, path, token, binary=False):
        self.assertEqual(token, 'synthetic-gh')
        self.requests.append(path)
        if binary:
            self.assertEqual(path, '/repos/example/product/actions/artifacts/11/zip')
            return self.blob
        if '/jobs?' in path:
            return {'jobs': [self.job]}
        if '/artifacts?' in path:
            return {'artifacts': [self.item]}
        if path == '/repos/example/product/actions/runs/32':
            return self.metadata
        raise AssertionError('Unexpected API call: ' + path)

    def fetch(self, *args, **kwargs):
        self.fetched.append((args, kwargs))
        return FETCH_RECEIPT(*args, **kwargs, request=self.request)

    def scanner(self, command, **kwargs):
        self.scanner_calls.append(command)
        if command[-1] == '--version':
            return subprocess.CompletedProcess(command, 0, 'SonarScanner CLI 8.3.1.16491', '')
        self.assertEqual(kwargs.get('env_names'), ('SONAR_TOKEN',))
        self.assertIn('-Dsonar.qualitygate.wait=true', command)
        return subprocess.CompletedProcess(command, 0, '', '')

    def run_wrapper(self, env=None):
        with patch.dict(os.environ, {**self.env, **(env or {})}, clear=True):
            with patch('shared_ci.evidence.fetch_receipt', side_effect=self.fetch), patch('shared_ci.sonar.run', side_effect=self.scanner):
                wrapper.main()

    def pr(self, fork=False):
        self.config.update(mode='pull_request', pull_request={'key': '17', 'branch': 'feature/coverage', 'base': 'develop'},
                           allowed_refs=['refs/heads/feature/coverage'])
        self.config.pop('branch', None)
        self.config_path.write_text(json.dumps(self.config))
        self.git('add', 'sonar.json')
        self.git('commit', '-qm', 'synthetic PR merge')
        self.source = self.git('rev-parse', 'HEAD')
        head = 'b' * 40
        event = Path(self.temp.name) / 'event.json'
        event.write_text(json.dumps({'pull_request': {'head': {'ref': 'feature/coverage',
                         'repo': {'full_name': 'outsider/product' if fork else 'example/product'}}}}))
        self.env.update(GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event), GITHUB_REF='refs/pull/17/merge',
                        GITHUB_SHA=self.source, QUALITY_SOURCE_SHA=self.source, QUALITY_RUN_HEAD_SHA=head,
                        QUALITY_ALLOWED_BRANCHES='["feature/coverage"]', QUALITY_ALLOWED_EVENTS='["pull_request"]')
        self.metadata.update(head_sha=head, head_branch='feature/coverage', event='pull_request')
        self.job['head_sha'] = head
        self.item['workflow_run']['head_sha'] = head
        self.receipt['provenance']['sha'] = self.source
        self.bundle()

    def test_same_running_push_fetches_completed_quality_job_and_scans_without_retesting(self):
        self.run_wrapper()
        self.assertIn('/repos/example/product/actions/runs/32/attempts/2/jobs?per_page=100&page=1', self.requests)
        args, options = self.fetched[0]
        self.assertEqual(args[3], self.source)
        self.assertEqual(options['run_attempt'], 2)
        self.assertEqual(options['producer_job'], 'quality / run')
        self.assertEqual(options['allowed_branches'], ['develop'])
        self.assertEqual(options['allowed_events'], ['push', 'workflow_dispatch'])
        self.assertEqual((self.root / 'coverage/lcov.info').read_bytes(), self.coverage)
        self.assertEqual(len(self.scanner_calls), 2)
        self.assertIn('-Dsonar.branch.name=develop', self.scanner_calls[-1])

    def test_same_repo_pr_separates_rest_head_from_tested_merge_commit(self):
        self.pr()
        self.run_wrapper()
        self.assertNotEqual(self.env['QUALITY_RUN_HEAD_SHA'], self.source)
        self.assertEqual(self.fetched[0][0][3], self.env['QUALITY_RUN_HEAD_SHA'])
        self.assertIn('-Dsonar.scm.revision=' + self.source, self.scanner_calls[-1])
        self.assertIn('-Dsonar.pullrequest.key=17', self.scanner_calls[-1])

    def test_wrong_branch_producer_attempt_or_event_refuses_before_scanner(self):
        for changed in ({'QUALITY_ALLOWED_BRANCHES': '["main"]'}, {'QUALITY_ALLOWED_BRANCHES': '["feature/*"]'},
                        {'QUALITY_PRODUCER_JOB': 'another / run'}, {'QUALITY_PRODUCER_JOB': ''},
                        {'QUALITY_RUN_ATTEMPT': ''}, {'QUALITY_RUN_ATTEMPT': '0'},
                        {'QUALITY_ALLOWED_EVENTS': '["pull_request"]'}):
            with self.subTest(changed=changed), self.assertRaises(PipelineError):
                self.run_wrapper(changed)
        self.assertEqual(self.scanner_calls, [])
        self.assertFalse((self.root / 'coverage/lcov.info').exists())
        self.assertFalse((self.root / 'reports/quality.json').exists())

    def test_fork_pr_is_refused_before_artifact_token_or_scanner_use(self):
        self.pr(fork=True)
        with self.assertRaisesRegex(PipelineError, 'Fork pull requests'):
            self.run_wrapper()
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.scanner_calls, [])

    def test_pr_coverage_requires_explicit_producer_event_permission(self):
        self.pr()
        with self.assertRaises(PipelineError):
            self.run_wrapper({'QUALITY_ALLOWED_EVENTS': '["push","workflow_dispatch"]'})
        self.assertEqual(self.scanner_calls, [])

    def test_altered_coverage_bytes_fail_authenticated_hash_before_write(self):
        self.bundle(coverage=b'changed bytes')
        with self.assertRaisesRegex(PipelineError, 'Coverage bytes'):
            self.run_wrapper()
        self.assertFalse((self.root / 'coverage/lcov.info').exists())
        self.assertEqual(self.scanner_calls, [])

    def test_coverage_destination_cannot_overwrite_tested_source(self):
        original = (self.root / 'src/app.js').read_bytes()
        self.config['coverage'][0]['path'] = 'src/app.js'
        self.config_path.write_text(json.dumps(self.config))
        self.git('add', 'sonar.json')
        self.git('commit', '-qm', 'bad destination')
        self.receipt['coverage'] = {'src/app.js': hashlib.sha256(self.coverage).hexdigest()}
        self.bundle()
        with self.assertRaisesRegex(PipelineError, 'overwrite source/configuration'):
            self.run_wrapper()
        self.assertEqual((self.root / 'src/app.js').read_bytes(), original)
        self.assertEqual(self.scanner_calls, [])

    def test_workflow_maps_exact_quality_contract_and_keeps_pr_secret_guard(self):
        workflow = yaml.safe_load((LIBRARY / '.github/workflows/v2-sonar.yml').read_text())
        inputs = workflow['on']['workflow_call']['inputs']
        self.assertTrue(inputs['quality-run-attempt']['required'])
        self.assertTrue(inputs['quality-allowed-branches']['required'])
        self.assertEqual(json.loads(inputs['quality-allowed-events']['default']), ['push', 'workflow_dispatch'])
        job = workflow['jobs']['run']
        self.assertIn('inputs.allow-pull-request', job['if'])
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', job['if'])
        execute = next(step for step in job['steps'] if step.get('id') == 'execute')
        for name in ('run-attempt', 'producer-job', 'allowed-branches', 'allowed-events'):
            self.assertEqual(execute['env']['QUALITY_' + name.upper().replace('-', '_')], '${{ inputs.quality-' + name + ' }}')


if __name__ == '__main__':
    unittest.main()
