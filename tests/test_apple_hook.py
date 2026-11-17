import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.assurance import stamp
from shared_ci.common import PipelineError, atomic_json, sha256_file
from shared_ci import evidence

spec = importlib.util.spec_from_file_location('apple_hook', Path(__file__).resolve().parents[1] / 'scripts/apple/run-hook.py')
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class AppleHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.archive = self.root / 'Archive.xcarchive'
        self.archive.mkdir()
        self.library = self.root / 'library'
        self.library.mkdir()
        (self.source / 'config.json').write_text('{}')
        (self.library / 'module.py').write_text('# synthetic reviewed helper\n')
        for root in (self.source, self.library):
            self.git(root, 'init', '-q')
            self.git(root, 'config', 'user.name', 'Synthetic Fixture')
            self.git(root, 'config', 'user.email', 'fixture@example.invalid')
            self.git(root, 'add', '.')
            self.git(root, 'commit', '-qm', 'fixture')
        self.revision = self.git(self.library, 'rev-parse', 'HEAD')
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'CI_XCODE_CLOUD': 'true',
               'CI_COMMIT': self.git(self.source, 'rev-parse', 'HEAD'),
               'CI_PRIMARY_REPOSITORY_PATH': str(self.source), 'CI_ARCHIVE_PATH': str(self.archive),
               'CI_XCODEBUILD_ACTION': 'archive', 'CI_XCODEBUILD_EXIT_CODE': '0',
               'SHARED_LIBRARY_REVISION': self.revision, 'SHARED_APPLE_CONFIG': 'config.json',
               'SHARED_APPLE_OUTPUT': '.shared-ci-evidence/receipt.json'}
        for patched in (patch.dict(os.environ, env, clear=True),
                        patch.object(hook.platform, 'system', return_value='Darwin'),
                        patch.object(hook, 'LIBRARY', self.library)):
            patched.start()
            self.addCleanup(patched.stop)

    def git(self, root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

    def configure(self, config):
        atomic_json(self.source / 'config.json', config)
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '--allow-empty', '-qm', 'configure')
        os.environ['CI_COMMIT'] = self.git(self.source, 'rev-parse', 'HEAD')

    def candidate_fixture(self):
        (self.source / 'check-config.json').write_text('{"schema":1}')
        config = {'schema': 1, 'product': 'synthetic-ios', 'version': '1.0', 'build': '1',
                  'library_sha': 'workflow', 'inputs': ['check-config.json'], 'artifacts': ['app.bin'],
                  'toolchain': {'compiler': 'synthetic'},
                  'checks': [{'path': '.shared-ci-evidence/check.json', 'kind': 'mobile', 'config': 'check-config.json'}]}
        self.configure(config)
        shutil.copyfile(self.source / 'check-config.json', self.archive / 'check-config.json')
        (self.archive / 'app.bin').write_bytes(b'synthetic archive product')
        receipt = stamp({'status': 'passed'}, 'mobile', self.source / 'check-config.json', self.source)
        path = self.archive / '.shared-ci-evidence/check.json'
        atomic_json(path, receipt)
        os.environ.update(SHARED_APPLE_MODULE='evidence', SHARED_APPLE_ROOT='archive',
                          SHARED_APPLE_CHECK_DIGESTS=json.dumps({'.shared-ci-evidence/check.json': sha256_file(path)}))
        return path

    def test_archive_monitoring_uses_checkout_config_and_current_archive_root(self):
        os.environ.update(SHARED_APPLE_MODULE='monitoring', SHARED_APPLE_ROOT='archive')
        seen = []
        module = types.SimpleNamespace(execute=lambda config, root: seen.append(root) or {'status': 'passed'})
        with patch.object(hook.importlib, 'import_module', return_value=module):
            hook.execute_hook()
        self.assertEqual(seen, [self.archive])
        self.assertTrue((self.archive / '.shared-ci-evidence/receipt.json').is_file())

    def test_archive_candidate_binds_verified_source_library_and_trusted_check_output(self):
        self.candidate_fixture()
        with patch.object(hook.importlib, 'import_module', return_value=evidence):
            candidate = hook.execute_hook()
        self.assertEqual(candidate['source_sha'], os.environ['CI_COMMIT'])
        self.assertEqual(candidate['library_sha'], self.revision)
        self.assertEqual(candidate['checks'], json.loads(os.environ['SHARED_APPLE_CHECK_DIGESTS']))
        self.assertEqual(evidence.verify(candidate, self.archive, source_root=self.source)['status'], 'passed')

    def test_candidate_refuses_missing_malformed_or_forged_check_digests(self):
        receipt = self.candidate_fixture()
        trusted = os.environ['SHARED_APPLE_CHECK_DIGESTS']
        for value in ('{}', '[]', 'invalid', '{"check":"bad-hash"}'):
            with self.subTest(value=value), patch.dict(os.environ, {'SHARED_APPLE_CHECK_DIGESTS': value}):
                with patch.object(hook.importlib, 'import_module', return_value=evidence), self.assertRaises(PipelineError):
                    hook.execute_hook()
        os.environ['SHARED_APPLE_CHECK_DIGESTS'] = trusted
        forged = json.loads(receipt.read_text())
        forged['invented_result'] = 'passed'
        atomic_json(receipt, forged)
        with patch.object(hook.importlib, 'import_module', return_value=evidence):
            with self.assertRaisesRegex(PipelineError, 'trusted workflow'):
                hook.execute_hook()

    def test_candidate_refuses_caller_library_revision(self):
        self.candidate_fixture()
        config = json.loads((self.source / 'config.json').read_text())
        config['library_sha'] = 'b' * 40
        self.configure(config)
        with patch.object(hook.importlib, 'import_module', return_value=evidence):
            with self.assertRaisesRegex(PipelineError, 'executing Apple helper'):
                hook.execute_hook()

    def test_archive_verify_uses_source_checkout_and_rejects_changed_source(self):
        self.candidate_fixture()
        with patch.object(hook.importlib, 'import_module', return_value=evidence):
            candidate = hook.execute_hook()
        atomic_json(self.source / 'candidate.json', candidate)
        os.environ.update(SHARED_APPLE_OPERATION='verify', SHARED_APPLE_CONFIG='candidate.json')
        with patch.object(hook.importlib, 'import_module', return_value=evidence):
            self.assertEqual(hook.execute_hook()['status'], 'passed')
        (self.source / 'check-config.json').write_text('changed tracked source')
        with self.assertRaisesRegex(PipelineError, 'Working source'):
            evidence.verify(candidate, self.archive, source_root=self.source)
        self.git(self.source, 'add', 'check-config.json')
        self.git(self.source, 'commit', '-qm', 'different tree')
        with self.assertRaisesRegex(PipelineError, 'source tree differs'):
            evidence.verify(candidate, self.archive, source_root=self.source, require_tree=False)

    def test_changed_or_unpinned_library_fails_before_module_execution(self):
        module = types.SimpleNamespace(execute=lambda *args: self.fail('module must not run'))
        for revision in ('main', 'b' * 40):
            with patch.dict(os.environ, {'SHARED_LIBRARY_REVISION': revision}):
                with patch.object(hook.importlib, 'import_module', return_value=module), self.assertRaises(PipelineError):
                    hook.execute_hook()
        (self.library / 'module.py').write_text('# changed helper')
        with patch.object(hook.importlib, 'import_module', return_value=module):
            with self.assertRaisesRegex(PipelineError, 'uncommitted changes'):
                hook.execute_hook()

    def test_library_modified_during_operation_cannot_emit_success(self):
        def change(*args):
            (self.library / 'module.py').write_text('# changed during operation')
            return {'status': 'passed'}
        with patch.object(hook.importlib, 'import_module', return_value=types.SimpleNamespace(execute=change)):
            with self.assertRaisesRegex(PipelineError, 'uncommitted changes'):
                hook.execute_hook()
        self.assertFalse((self.source / '.shared-ci-evidence/receipt.json').exists())

    def test_summary_contains_only_trusted_receipt_selection(self):
        os.environ.update(SHARED_APPLE_MODULE='monitoring', SHARED_APPLE_ROOT='archive')
        stream = io.StringIO()
        with patch.object(hook.importlib, 'import_module', return_value=types.SimpleNamespace(execute=lambda *args: {'status': 'passed'})):
            with contextlib.redirect_stdout(stream):
                hook.execute_hook(emit_summary=True)
        result = json.loads(stream.getvalue())
        self.assertEqual(set(result), {'status', 'receipt_path', 'receipt_sha256', 'root', 'library_sha', 'source_sha'})
        self.assertEqual(result['receipt_sha256'], sha256_file(self.archive / result['receipt_path']))
        self.assertEqual(result['library_sha'], self.revision)

    def test_bridge_export_is_native_archive_only(self):
        os.environ.update(SHARED_APPLE_MODULE='apple_bridge', SHARED_APPLE_ROOT='archive')
        self.configure({'operation': 'apple-export'})
        module = types.SimpleNamespace(execute=lambda config, root: {'status': 'passed', 'operation': config['operation']})
        with patch.object(hook.importlib, 'import_module', return_value=module):
            self.assertEqual(hook.execute_hook()['operation'], 'apple-export')
        self.configure({'operation': 'apple-import'})
        with patch.object(hook.importlib, 'import_module', return_value=module), self.assertRaises(PipelineError):
            hook.execute_hook()

    def test_failed_result_has_receipt_and_fails(self):
        os.environ.update(SHARED_APPLE_MODULE='monitoring', SHARED_APPLE_ROOT='archive')
        with patch.object(hook.importlib, 'import_module', return_value=types.SimpleNamespace(execute=lambda *args: {'status': 'pending'})):
            with self.assertRaises(PipelineError):
                hook.execute_hook()
        self.assertEqual(json.loads((self.archive / '.shared-ci-evidence/receipt.json').read_text())['status'], 'pending')

    def test_wrong_host_context_root_module_and_payload_output_refused(self):
        for values in [{'GITHUB_ACTIONS': 'true'}, {'CI_XCODE_CLOUD': 'false'}, {'SHARED_APPLE_MODULE': 'os'},
                       {'SHARED_APPLE_ROOT': 'anywhere'}, {'SHARED_APPLE_ROOT': 'archive', 'CI_XCODEBUILD_EXIT_CODE': '1'},
                       {'SHARED_APPLE_ROOT': 'archive', 'SHARED_APPLE_OUTPUT': 'Products/payload.json'}]:
            with self.subTest(values=values), patch.dict(os.environ, values), self.assertRaises(PipelineError):
                hook.execute_hook()
        with patch.object(hook.platform, 'system', return_value='Linux'), self.assertRaises(PipelineError):
            hook.execute_hook()


if __name__ == '__main__':
    unittest.main()
