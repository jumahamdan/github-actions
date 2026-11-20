"""Exercise the actual embedded workflow scripts without downloading/running CodeQL."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from shared_ci.common import PipelineError
from shared_ci.security import scan

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/v2-codeql.yml').read_text())


def script(name):
    step = next(step for step in WORKFLOW['jobs']['analyze']['steps'] if step.get('name') == name)
    return step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]


class CodeQLWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = json.loads((ROOT / 'examples/security-codeql.json').read_text())
        self.write_policy()
        self.env = {'GITHUB_WORKSPACE': str(self.root), 'RUNNER_TEMP': str(self.root),
                    'GITHUB_OUTPUT': str(self.root / 'output'), 'PRODUCT_ROOT': '.', 'SECURITY_CONFIG': 'security.json',
                    'CODEQL_LANGUAGE': 'javascript', 'BUILD_MODE': 'none', 'BUILD_CONFIG': '',
                    'CODEQL_ENTITLED': 'true', 'ADVANCED_CONFIRMED': 'true',
                    'GO_VERSION': '', 'JAVA_VERSION': '', 'DOTNET_VERSION': '',
                    'PATH': os.environ['PATH'], 'HOME': str(self.root)}
        self.prepare = script('Validate scope and download checksum-pinned language tools')
        self.build = script('Run explicit compiled-language build')

    def tearDown(self): self.temp.cleanup()

    def write_policy(self): (self.root / 'security.json').write_text(json.dumps(self.policy))

    def execute(self, body):
        with patch.dict(os.environ, self.env, clear=True): exec(compile(body, '<workflow>', 'exec'), {})

    def test_scope_policy_and_entitlement_fail_before_any_download(self):
        for field, bad in [('CODEQL_ENTITLED', 'false'), ('ADVANCED_CONFIRMED', 'false'),
                           ('CODEQL_LANGUAGE', 'swift'), ('CODEQL_LANGUAGE', 'dart'), ('CODEQL_LANGUAGE', 'php'),
                           ('BUILD_MODE', 'autobuild'), ('PRODUCT_ROOT', '../outside')]:
            previous = self.env[field]; self.env[field] = bad
            with self.subTest(field=field, bad=bad), patch('urllib.request.urlopen') as fetch, self.assertRaises(SystemExit):
                self.execute(self.prepare)
            fetch.assert_not_called(); self.env[field] = previous
        self.policy['scans'][0]['command'] = ['cat', 'old-report.sarif']; self.write_policy()
        with patch('urllib.request.urlopen') as fetch, self.assertRaises(SystemExit): self.execute(self.prepare)
        fetch.assert_not_called()

    def test_kotlin_none_and_unpinned_manual_sdk_fail(self):
        self.env.update(CODEQL_LANGUAGE='java')
        self.policy['scans'][0]['id'] = 'codeql-java-none'
        self.policy['scans'][0]['command'][1] = '.shared-codeql-results/java.sarif'; self.write_policy()
        (self.root / 'Example.kt').write_text('class Example')
        with patch('urllib.request.urlopen') as fetch, self.assertRaisesRegex(SystemExit, 'Kotlin'): self.execute(self.prepare)
        fetch.assert_not_called()
        self.env.update(CODEQL_LANGUAGE='go', BUILD_MODE='manual', GO_VERSION='latest')
        with self.assertRaisesRegex(SystemExit, 'exact compiler'): self.execute(self.prepare)

    def test_bundle_checksum_is_enforced_and_matching_pin_emits_paths(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'wrong')), self.assertRaisesRegex(SystemExit, 'checksum'):
            self.execute(self.prepare)
        expected = '082e798c97831283a2a5f69e9198c51270e5767013d5333daeeab6520af9ce72'
        class Digest:
            def update(self, value): pass
            def hexdigest(self): return expected
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'fixture')), patch('hashlib.sha256', return_value=Digest()):
            self.execute(self.prepare)
        result = (self.root / 'output').read_text()
        self.assertIn('source-root=.\n', result)
        self.assertIn('results=' + str(self.root / '.shared-codeql-results'), result)

    def build_config(self, command):
        config = {'directory': '.', 'timeout_seconds': 3,
                  'toolchain': [{'command': [sys.executable, '--version'],
                                 'version': '.'.join(map(str, sys.version_info[:3]))}], 'commands': [command]}
        (self.root / 'build.json').write_text(json.dumps(config)); self.env['BUILD_CONFIG'] = 'build.json'
        return config

    def test_manual_build_executes_argv_with_tracing_and_without_tokens(self):
        command = [sys.executable, '-c', "import json,os;open('build-env.json','w').write(json.dumps(dict(os.environ)))"]
        self.build_config(command)
        self.env.update(CODEQL_TRACER_CONFIGURATION='trace-fixture', CODEQL_ACTION_TOKEN='synthetic-secret',
                        OPENAI_API_KEY='synthetic-secret', GITHUB_TOKEN='synthetic-secret')
        self.execute(self.build)
        env = json.loads((self.root / 'build-env.json').read_text())
        self.assertEqual(env['CODEQL_TRACER_CONFIGURATION'], 'trace-fixture')
        self.assertFalse({'CODEQL_ACTION_TOKEN', 'OPENAI_API_KEY', 'GITHUB_TOKEN'} & set(env))

    def test_manual_build_missing_compiler_bad_argv_and_timeout_fail(self):
        for scenario in ('compiler', 'argv', 'timeout'):
            command = [sys.executable, '-c', 'pass']
            config = self.build_config(command)
            if scenario == 'compiler': config['toolchain'][0]['version'] = '0.0.0'
            elif scenario == 'argv': config['commands'] = ['not-an-argv-array']
            else:
                config['timeout_seconds'] = 1
                config['commands'] = [[sys.executable, '-c', 'import time;time.sleep(10)']]
            (self.root / 'build.json').write_text(json.dumps(config))
            with self.subTest(scenario=scenario), self.assertRaises(SystemExit): self.execute(self.build)

    def test_generated_sarif_uses_actual_security_gate_for_cvss_and_errors(self):
        tools = self.root / 'tools'; tools.mkdir()
        executable = tools / 'codeql'; executable.write_text('#!/bin/sh\nprintf \'{"version":"2.27.4"}\\n\'\n'); executable.chmod(0o755)
        self.env['PATH'] = str(tools) + ':' + self.env['PATH']
        reports = self.root / '.shared-codeql-results'; reports.mkdir()
        report = {'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'CodeQL', 'rules': [
            {'id': 'example/rule', 'properties': {'security-severity': '9.8'}}]}},
            'invocations': [{'executionSuccessful': True}], 'results': []}]}
        path = reports / 'javascript.sarif'; path.write_text(json.dumps(report))
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(scan(self.policy, self.root)['status'], 'passed')
            report['runs'][0]['results'] = [{'ruleId': 'example/rule', 'level': 'warning'}]
            path.write_text(json.dumps(report))
            self.assertEqual(scan(self.policy, self.root)['status'], 'failed')
            report['runs'][0]['invocations'][0]['executionSuccessful'] = False
            path.write_text(json.dumps(report))
            with self.assertRaises(PipelineError): scan(self.policy, self.root)

    def test_upload_is_separate_opt_in_and_does_not_define_gate_success(self):
        analyze = WORKFLOW['jobs']['analyze']; upload = WORKFLOW['jobs']['upload']
        self.assertEqual(analyze['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(upload['permissions']['security-events'], 'write')
        self.assertIn('inputs.upload-to-github', upload['if'])
        self.assertIn('head.repo.full_name == github.repository', upload['if'])
        action = next(step for step in analyze['steps'] if step.get('name') == 'Analyze to local SARIF')
        self.assertEqual(action['with']['upload'], 'never')
        self.assertFalse(action['with']['upload-database'])
        self.assertEqual(next(step for step in analyze['steps'] if step.get('id') == 'gate')['with']['module'], 'security')


if __name__ == '__main__': unittest.main()
