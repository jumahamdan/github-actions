import copy
import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError, sha256_file
from shared_ci.mobile import execute, _ddp_results, _testlab_results, GCLOUD_VERSION, GCLOUD_BETA_VERSION

SHA = 'a' * 40
CERT = 'b' * 64
UUID = '11111111-2222-3333-4444-555555555555'
GH = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/staging',
      'GITHUB_REPOSITORY': 'example/app', 'GITHUB_SHA': SHA}


def completed(argv, stdout='', stderr='', code=0):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def ddp_fixture(name, devices):
    passed = {'status': {'statusType': 'DONE'}, 'result': {'resultType': 'PASSED'}}
    return {'name': name,
            'sessionConfig': {'jobConfigs': [
                {'allocationConfig': {'deviceConfigs': [{'requirement': {'deviceId': device}}]}}
                for device in devices]},
            'sessionReport': {**copy.deepcopy(passed), 'jobReports': [
                {**copy.deepcopy(passed), 'id': str(i), 'executionReports': [copy.deepcopy(passed)]}
                for i, device in enumerate(devices)]}}


class MobileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, GH, clear=True)
        self.env.start()
        self.mac = patch("shared_ci.mobile.platform.system", return_value="Darwin")
        self.mac.start()

    def tearDown(self):
        self.mac.stop()
        self.env.stop()
        self.tmp.cleanup()

    def write(self, name, value=b'fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def cloud(self, archive=None):
        os.environ.pop("GITHUB_ACTIONS", None)
        os.environ.update(CI_XCODE_CLOUD='TRUE', CI_COMMIT=SHA, CI_BUILD_ID='cloud-build',
                          CI_BUILD_NUMBER='42', CI_PRIMARY_REPOSITORY_PATH=str(self.root),
                          CI_GIT_REF='refs/heads/staging', CI_START_CONDITION='push',
                          CI_XCODEBUILD_ACTION='archive', CI_XCODEBUILD_EXIT_CODE='0')
        if archive:
            os.environ['CI_ARCHIVE_PATH'] = str(archive)

    def apple(self, signed=False):
        archive = self.root / 'Candidate.xcarchive'
        self.cloud(archive)
        config = {'operation': 'apple-verify', 'archive': '$CI_ARCHIVE_PATH',
                  'signing': 'signed' if signed else 'unsigned', 'version': '1.2.0', 'build': '42',
                  'allowed_refs': ['refs/heads/staging'], 'targets': []}
        paths = [('Products/Applications/Example.app', 'com.example.mobile', 'Example', {}),
                 ('Products/Applications/Example.app/PlugIns/Widget.appex', 'com.example.mobile.widget', 'Widget',
                  {'NSExtension': {'NSExtensionPointIdentifier': 'com.apple.widgetkit-extension'}}),
                 ('Products/Applications/Example.app/Watch/Watch.app', 'com.example.mobile.watch', 'Watch',
                  {'WKApplication': True, 'WKCompanionAppBundleIdentifier': 'com.example.mobile',
                   'WKRunsIndependentlyOfCompanionApp': True})]
        for path, bundle, executable, extras in paths:
            info = {'CFBundleIdentifier': bundle, 'CFBundleVersion': '42', 'CFBundleShortVersionString': '1.2.0',
                    'CFBundleExecutable': executable, **extras}
            self.write('Candidate.xcarchive/' + path + '/Info.plist', plistlib.dumps(info))
            self.write('Candidate.xcarchive/' + path + '/' + executable).chmod(0o755)
            symbol = 'dSYMs/' + executable + '.app.dSYM'
            self.write('Candidate.xcarchive/' + symbol + '/Contents/Resources/DWARF/' + executable)
            config['targets'].append({'path': path, 'bundle_id': bundle, 'executable': executable, 'plist': extras,
                                      'entitlements': {'get-task-allow': False}, 'dsym': symbol})
        return config

    def apple_run(self, argv, **kwargs):
        if argv[0] == 'dwarfdump':
            return completed(argv, f'UUID: {UUID} (arm64) {argv[-1]}\n')
        if '--entitlements' in argv:
            return completed(argv, plistlib.dumps({'get-task-allow': False}).decode())
        return completed(argv)

    def test_unsigned_archive_proves_identity_symbols_and_is_not_distributable(self):
        config = self.apple()
        with patch('shared_ci.mobile.run', side_effect=self.apple_run):
            result = execute(config, self.root)
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['distribution_ready'])
        self.assertEqual(result['application'], {'id': 'com.example.mobile', 'version': '1.2.0', 'build': '42'})
        self.assertEqual(len(result['debug_symbols']), 3)
        self.assertFalse(Path(result['debug_symbols'][0]['binary_path']).is_absolute())

    def test_signed_archive_checks_every_target(self):
        config = self.apple(signed=True)
        with patch('shared_ci.mobile.run', side_effect=self.apple_run) as run:
            result = execute(config, self.root)
        self.assertTrue(result['distribution_ready'])
        self.assertEqual(sum('--verify' in call.args[0] for call in run.call_args_list), 3)

    def test_archive_negative_identity_executable_extension_and_extra_bundle(self):
        for scenario in ('version', 'widget', 'watch', 'executable', 'missing-symbols', 'extra'):
            with self.subTest(scenario=scenario):
                config = self.apple()
                target = config['targets'][1 if scenario == 'widget' else 2 if scenario == 'watch' else 0]
                path = self.root / 'Candidate.xcarchive' / target['path']
                if scenario in ('version', 'widget', 'watch'):
                    value = plistlib.loads((path / 'Info.plist').read_bytes())
                    key = {'version': 'CFBundleVersion', 'widget': 'NSExtension', 'watch': 'WKCompanionAppBundleIdentifier'}[scenario]
                    value[key] = 'wrong'
                    (path / 'Info.plist').write_bytes(plistlib.dumps(value))
                elif scenario == 'executable': (path / target['executable']).chmod(0o644)
                elif scenario == 'missing-symbols':
                    (self.root / 'Candidate.xcarchive' / target['dsym'] / 'Contents/Resources/DWARF' / target['executable']).unlink()
                else: (path / 'PlugIns/Unexpected.appex').mkdir(parents=True)
                with patch('shared_ci.mobile.run', side_effect=self.apple_run), self.assertRaises(PipelineError):
                    execute(config, self.root)
                if scenario == 'extra': (path / 'PlugIns/Unexpected.appex').rmdir()

    def test_symbol_mismatch_or_bad_signing_blocks(self):
        config = self.apple(True)
        for scenario in ('uuid', 'entitlements', 'codesign'):
            def fake(argv, **kwargs):
                if scenario == 'uuid' and 'dSYMs' in argv[-1]:
                    return completed(argv, f'UUID: {"f" * 36} (arm64) symbol')
                if scenario == 'entitlements' and '--entitlements' in argv:
                    return completed(argv, plistlib.dumps({'get-task-allow': True}).decode())
                if scenario == 'codesign' and '--verify' in argv:
                    raise PipelineError('signature invalid')
                return self.apple_run(argv, **kwargs)
            with self.subTest(scenario=scenario), patch('shared_ci.mobile.run', side_effect=fake), self.assertRaises(PipelineError):
                execute(config, self.root)

    def test_signed_archive_refuses_pr_failed_build_or_wrong_build_number(self):
        for key, value in (('CI_START_CONDITION', 'pr_open'), ('CI_XCODEBUILD_EXIT_CODE', '1'), ('CI_BUILD_NUMBER', '43')):
            config = self.apple(True)
            os.environ[key] = value
            with self.subTest(key=key), self.assertRaises(PipelineError): execute(config, self.root)

    def bootstrap(self, flutter=False):
        self.cloud()
        (self.root / 'App.xcworkspace').mkdir(exist_ok=True)
        locks = {'Package.resolved': sha256_file(self.write('Package.resolved'))}
        config = {'operation': 'apple-bootstrap', 'kind': 'native', 'xcode': {'version': '26.1', 'build': '17B55'},
                  'locks': locks, 'project': 'App.xcworkspace', 'scheme': 'App'}
        if flutter:
            config['kind'] = 'flutter'
            config['flutter'] = {'version': '3.50.0', 'revision': 'c' * 40, 'directory': '.'}
            config['pods'] = {'version': '1.16.2', 'directory': '.'}
            for name in ('pubspec.lock', 'Podfile.lock', 'Gemfile.lock'):
                locks[name] = sha256_file(self.write(name))
        return config

    def bootstrap_run(self, argv, **kwargs):
        if argv == ['xcodebuild', '-version']: return completed(argv, 'Xcode 26.1\nBuild version 17B55\n')
        if '--machine' in argv: return completed(argv, json.dumps({'frameworkVersion': '3.50.0', 'frameworkRevision': 'c' * 40}))
        if argv == ['bundle', 'exec', 'pod', '--version']: return completed(argv, '1.16.2\n')
        return completed(argv)

    def test_flutter_bootstrap_uses_enforced_locks_and_no_archive_compile(self):
        config = self.bootstrap(True)
        with patch('shared_ci.mobile.run', side_effect=self.bootstrap_run) as run:
            execute(config, self.root)
        args = [call.args[0] for call in run.call_args_list]
        self.assertIn(['flutter', 'pub', 'get', '--enforce-lockfile'], args)
        self.assertIn(['bundle', 'exec', 'pod', 'install', '--deployment'], args)
        self.assertIn('-onlyUsePackageVersionsFromResolvedFile', args[-1])
        self.assertTrue(all('archive' not in arg for arg in args))

    def test_native_bootstrap_checks_tool_and_locks_before_and_after(self):
        config = self.bootstrap()
        for scenario in ('tool', 'lock-before', 'lock-after'):
            self.write('Package.resolved')
            if scenario == 'lock-before': self.write('Package.resolved', b'changed')
            def fake(argv, **kwargs):
                if scenario == 'tool': return completed(argv, 'Xcode unknown')
                if scenario == 'lock-after' and '-resolvePackageDependencies' in argv:
                    self.write('Package.resolved', b'changed')
                return self.bootstrap_run(argv, **kwargs)
            with self.subTest(scenario=scenario), patch('shared_ci.mobile.run', side_effect=fake), self.assertRaises(PipelineError):
                execute(config, self.root)

    def nextbuild(self):
        config = {'operation': 'next-build', 'product': 'example-ios', 'remote_highest': 'remote.json',
                  'ledger': 'numbers.json', 'serialization_key': 'example-ios-builds'}
        os.environ['MOBILE_BUILD_SERIALIZATION_KEY'] = config['serialization_key']
        self.write('remote.json', json.dumps({'product': config['product'], 'highest': 41,
                                            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}).encode())
        return config

    def test_build_number_is_monotonic_and_keeps_persistent_ledger(self):
        config = self.nextbuild()
        self.assertEqual(execute(config, self.root)['build_number'], 42)
        self.assertEqual(execute(config, self.root)['build_number'], 43)
        config = self.nextbuild()
        self.assertEqual(execute(config, self.root)['build_number'], 44)

    def test_build_number_rejects_stale_cross_product_or_unserialized(self):
        for scenario in ('stale', 'product', 'lock', 'overflow'):
            config = self.nextbuild()
            value = json.loads((self.root / 'remote.json').read_text())
            if scenario == 'stale': value['checked_at'] = '2020-01-01T00:00:00+00:00'
            elif scenario == 'product': value['product'] = 'other'
            elif scenario == 'lock': del os.environ['MOBILE_BUILD_SERIALIZATION_KEY']
            else: value['highest'] = 2100000000
            self.write('remote.json', json.dumps(value).encode())
            with self.subTest(scenario=scenario), self.assertRaises(PipelineError): execute(config, self.root)

    def android(self):
        jar = self.write('tools/bundletool.jar')
        self.write('app.aab')
        os.environ.update(KEYSTORE_PATH=str(self.write('upload.jks')), STORE_PASSWORD='synthetic-store-secret',
                          KEY_PASSWORD='synthetic-key-secret')
        return {'operation': 'android-derive', 'aab': 'app.aab', 'output_apk': 'out/app.apk',
                'allowed_refs': ['refs/heads/staging'],
                'bundletool': {'jar': 'tools/bundletool.jar', 'sha256': sha256_file(jar), 'version': '1.18.3'},
                'apksigner': {'command': '/tools/apksigner', 'version': '0.9'},
                'signing': {'keystore_env': 'KEYSTORE_PATH', 'store_password_env': 'STORE_PASSWORD',
                            'key_password_env': 'KEY_PASSWORD', 'alias': 'upload', 'sha256': CERT}}

    def android_run(self, argv, **kwargs):
        if argv[-1] == 'version': return completed(argv, '1.18.3\n' if argv[0] == 'java' else '0.9\n')
        if 'manifest' in argv:
            return completed(argv, '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.mobile" android:versionName="1.2.0" android:versionCode="42"/>')
        if 'build-apks' in argv:
            self.assertFalse(any('synthetic' in arg for arg in argv))
            path = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--output='))
            for prefix in ('--ks-pass=file:', '--key-pass=file:'):
                password = Path(next(arg[len(prefix):] for arg in argv if arg.startswith(prefix)))
                self.assertEqual(password.stat().st_mode & 0o777, 0o600)
                self.assertTrue(password.read_text().startswith('synthetic'))
                self.password_path = password
            with zipfile.ZipFile(path, 'w') as archive: archive.writestr('universal.apk', b'installed apk')
        if 'verify' in argv: return completed(argv, f'Signer #1 certificate SHA-256 digest: {CERT}\n')
        return completed(argv)

    def test_derive_apk_once_preserves_aab_identity_and_cleans_passwords(self):
        config = self.android()
        with patch('shared_ci.mobile.run', side_effect=self.android_run): result = execute(config, self.root)
        self.assertEqual(result['application']['build'], '42')
        self.assertEqual(result['artifacts']['app.aab']['sha256'], result['aab_sha256'])
        self.assertFalse(self.password_path.exists())
        self.assertFalse(result['play_app_signing_verified'])

    def test_derive_uses_checksummed_installed_jar_outside_product(self):
        config = self.android()
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / 'bundle tool.jar'
            jar.write_bytes(b'fixture')
            config['bundletool'].pop('jar')
            config['bundletool']['jar_env'] = 'SHARED_BUNDLETOOL_JAR'
            config['apksigner']['command'] = 'apksigner'
            with patch.dict(os.environ, {'SHARED_BUNDLETOOL_JAR': str(jar)}), patch('shared_ci.mobile.run', side_effect=self.android_run) as commands:
                result = execute(config, self.root)
            self.assertEqual(result['bundletool_version'], '1.18.3')
            self.assertIn(['java', '-jar', str(jar), 'version'], [call.args[0] for call in commands.call_args_list])
            self.assertIn(['apksigner', 'version'], [call.args[0] for call in commands.call_args_list])

    def test_installed_jar_requires_one_explicit_nonlinked_checksum_bound_file(self):
        for scenario in ('both', 'neither', 'missing-env', 'relative', 'directory', 'link', 'parent-link', 'bad-hash', 'changed-bytes'):
            config = self.android()
            with tempfile.TemporaryDirectory() as directory:
                installed = Path(directory) / 'bundletool.jar'
                installed.write_bytes(b'fixture')
                config['bundletool'].pop('jar')
                config['bundletool']['jar_env'] = 'SHARED_BUNDLETOOL_JAR'
                value = str(installed)
                if scenario == 'both': config['bundletool']['jar'] = 'tools/bundletool.jar'
                elif scenario == 'neither': config['bundletool'].pop('jar_env')
                elif scenario == 'missing-env': value = ''
                elif scenario == 'relative': value = 'tools/bundletool.jar'
                elif scenario == 'directory': value = directory
                elif scenario == 'link':
                    link = Path(directory) / 'linked.jar'; link.symlink_to(installed); value = str(link)
                elif scenario == 'parent-link':
                    link = self.root / 'installed-alias'; link.symlink_to(Path(directory), target_is_directory=True); value = str(link / 'bundletool.jar')
                elif scenario == 'bad-hash': config['bundletool']['sha256'] = 'not-a-sha256'
                elif scenario == 'changed-bytes': installed.write_bytes(b'modified')
                with self.subTest(scenario=scenario), patch.dict(os.environ, {'SHARED_BUNDLETOOL_JAR': value}), patch('shared_ci.mobile.run') as commands:
                    with self.assertRaises(PipelineError): execute(config, self.root)
                    commands.assert_not_called()

    def test_derive_rejects_wrong_multi_signer_missing_apk_or_tool_checksum(self):
        for scenario in ('wrong-signer', 'multi-signer', 'missing-apk', 'jar', 'aab-mutated'):
            config = self.android()
            if scenario == 'jar': config['bundletool']['sha256'] = '0' * 64
            def fake(argv, **kwargs):
                if scenario == 'missing-apk' and 'build-apks' in argv: return completed(argv)
                if 'verify' in argv and scenario in ('wrong-signer', 'multi-signer'):
                    return completed(argv, f'Signer #1 certificate SHA-256 digest: {"d" * 64}\n' if scenario == 'wrong-signer'
                                     else f'Signer #1 certificate SHA-256 digest: {CERT}\nSigner #2 certificate SHA-256 digest: {CERT}\n')
                if scenario == 'aab-mutated' and 'build-apks' in argv: self.write('app.aab', b'changed')
                return self.android_run(argv, **kwargs)
            with self.subTest(scenario=scenario), patch('shared_ci.mobile.run', side_effect=fake), self.assertRaises(PipelineError):
                execute(config, self.root)
            self.assertFalse((self.root / 'out/app.apk').exists())

    def device(self, ddp=False):
        self.write('app.apk'); self.write('tests.apk')
        os.environ['GOOGLE_DEVICE_TOKEN'] = 'synthetic-access-token'
        return {'operation': 'device-test', 'provider': 'ddp-instrumentation' if ddp else 'testlab-robo',
                'app': 'app.apk', 'live_enabled': True, 'allowed_refs': ['refs/heads/staging'],
                'project': 'example-project', 'bucket': 'example-results', 'access_token_env': 'GOOGLE_DEVICE_TOKEN',
                'timeout_seconds': 300, 'deadline_seconds': 900,
                'devices': ['pixel-device'] if ddp else [{'model': 'pixel', 'version': '35', 'locale': 'en', 'orientation': 'portrait'}],
                **({'test': 'tests.apk', 'location': 'us-central1'} if ddp else {})}

    def device_run(self, argv, **kwargs):
        self.assertNotIn('env_names', kwargs)
        self.assertEqual(kwargs['extra_env']['CLOUDSDK_CORE_DISABLE_USAGE_REPORTING'], 'true')
        token = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--access-token-file=')))
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.token_path = token
        if 'version' in argv: return completed(argv, json.dumps({'Google Cloud SDK': GCLOUD_VERSION, 'beta': GCLOUD_BETA_VERSION}))
        if 'firebase' in argv: return completed(argv, json.dumps([{'outcome': 'Passed', 'axis_value': 'pixel-35-en-portrait', 'test_details': ''}]))
        if 'submit' in argv: return completed(argv, stderr='Creating session [synthetic-session] in location [us-central1].\n')
        if 'describe' in argv:
            return completed(argv, json.dumps(ddp_fixture('projects/example-project/locations/us-central1/sessions/synthetic-session', ['pixel-device'])))
        return completed(argv, '[]')

    def test_real_device_command_contracts_and_private_token_cleanup(self):
        for ddp in (False, True):
            config = self.device(ddp)
            with self.subTest(ddp=ddp), patch('shared_ci.mobile.run', side_effect=self.device_run) as run:
                result = execute(config, self.root)
            self.assertEqual(result['executions'], 1)
            self.assertFalse(self.token_path.exists())
            args = [call.args[0] for call in run.call_args_list]
            if ddp:
                self.assertIn('--flaky-test-attempts=1', args[1])
                self.assertIn('--full', args[-1])
            else: self.assertIn('--num-flaky-test-attempts=0', args[1])

    def test_testlab_unknown_partial_flaky_cancelled_duplicate_or_wrong_matrix_fails(self):
        devices = self.device()['devices']
        for rows in ([], [{'outcome': 'Inconclusive'}], [{'outcome': 'Flaky'}], [{'outcome': 'Cancelled'}],
                     [{'outcome': 'Passed', 'axis_value': 'wrong'}],
                     [{'outcome': 'Passed', 'axis_value': 'pixel-35-en-portrait'}] * 2):
            with self.subTest(rows=rows), self.assertRaises(PipelineError): _testlab_results(rows, devices)

    def test_ddp_requires_all_nested_states_and_exact_devices(self):
        for scenario in ('running', 'cancelled', 'error', 'empty', 'wrong-device', 'wrong-session', 'no-jobs'):
            value = ddp_fixture('session', ['pixel-device'])
            report = value['sessionReport']
            if scenario == 'running': report['status']['statusType'] = 'RUNNING'
            if scenario == 'cancelled': report['result']['resultType'] = 'CANCELLED'
            if scenario == 'error': report['jobReports'][0]['executionReports'][0]['result']['resultType'] = 'ERROR'
            if scenario == 'empty': report['jobReports'][0]['executionReports'] = []
            if scenario == 'wrong-device': value['sessionConfig']['jobConfigs'][0]['allocationConfig']['deviceConfigs'][0]['requirement']['deviceId'] = 'other'
            if scenario == 'wrong-session': value['name'] = 'other'
            if scenario == 'no-jobs': report['jobReports'] = []
            with self.subTest(scenario=scenario), self.assertRaises(PipelineError): _ddp_results(value, 'session', ['pixel-device'])

    def test_device_disallows_implicit_live_or_forks_and_retires_testlab(self):
        config = self.device()
        config['live_enabled'] = False
        with self.assertRaises(PipelineError): execute(config, self.root)
        config['live_enabled'] = True
        os.environ['GITHUB_EVENT_NAME'] = 'pull_request_target'
        with self.assertRaises(PipelineError): execute(config, self.root)
        os.environ['GITHUB_EVENT_NAME'] = 'push'
        class Future(dt.datetime):
            @classmethod
            def now(cls, tz=None): return cls(2027, 9, 30, tzinfo=dt.timezone.utc)
        with patch('shared_ci.mobile.dt.datetime', Future), self.assertRaisesRegex(PipelineError, 'retired'):
            execute(config, self.root)

    def test_device_tool_mismatch_submission_unknown_and_cli_errors_fail(self):
        for scenario in ('version', 'beta-missing', 'session-id', 'cli'):
            config = self.device(True)
            def fake(argv, **kwargs):
                if scenario == 'version': return completed(argv, '{"Google Cloud SDK":"1.0"}')
                if scenario == 'beta-missing': return completed(argv, json.dumps({'Google Cloud SDK': GCLOUD_VERSION}))
                if scenario == 'session-id' and 'submit' in argv: return completed(argv, '{}')
                if scenario == 'cli' and 'wait' in argv: raise PipelineError('provider failed')
                return self.device_run(argv, **kwargs)
            with self.subTest(scenario=scenario), patch('shared_ci.mobile.run', side_effect=fake), self.assertRaises(PipelineError): execute(config, self.root)

    def test_loader_uses_exact_commit_cleans_checkout_and_rejects_changed_revision(self):
        loader = Path(__file__).resolve().parents[1] / 'scripts/apple/run-hook.sh'
        bindir = self.root / 'bin'
        bindir.mkdir()
        git = self.write('bin/git', b"#!/usr/bin/python3\nimport os,sys,json\nfrom pathlib import Path\nwith Path(os.environ['TRACE']).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\nif 'rev-parse' in sys.argv: print(os.environ['OBSERVED_REVISION'])\n")
        python = self.write('bin/python3', b"#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\nif '-c' in sys.argv:\n    sys.exit(0)\nassert sys.argv[1:3] == ['-I', '-B']\nPath(os.environ['PROOF']).write_text(str(Path(sys.argv[3]).parents[2]))\n")
        git.chmod(0o755); python.chmod(0o755)
        env = {'PATH': str(bindir) + ':/usr/bin:/bin', 'TMPDIR': str(self.root),
               'CI_PRIMARY_REPOSITORY_PATH': str(self.root), 'CI_XCODE_CLOUD': 'TRUE',
               'SHARED_LIBRARY_REPOSITORY': 'https://github.com/example/shared-workflows.git',
               'SHARED_LIBRARY_REVISION': SHA, 'OBSERVED_REVISION': SHA,
               'SHARED_MOBILE_CONFIG': 'ci/mobile.json', 'SHARED_MOBILE_OUTPUT': 'evidence/mobile.json',
               'TRACE': str(self.root / 'trace'), 'PROOF': str(self.root / 'proof'),
               'SHARED_PYTHON': str(python), 'SHARED_PYTHON_VERSION': '3.12.3'}
        result = subprocess.run(['bash', str(loader)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        checkout = Path((self.root / 'proof').read_text())
        self.assertFalse(checkout.exists())
        calls = [json.loads(line) for line in (self.root / 'trace').read_text().splitlines()]
        self.assertTrue(any('fetch' in call and call[-1] == SHA for call in calls))
        (self.root / 'proof').unlink()
        env['OBSERVED_REVISION'] = 'd' * 40
        result = subprocess.run(['bash', str(loader)], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'proof').exists())
        self.assertEqual(list(self.root.glob('shared-mobile.*')), [])
        env['SHARED_LIBRARY_REVISION'] = 'main'
        result = subprocess.run(['bash', str(loader)], env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    def test_loader_refuses_old_wrong_or_unpinned_python_before_git(self):
        loader = Path(__file__).resolve().parents[1] / 'scripts/apple/run-hook.sh'
        interpreter = self.write('python-fixture', b'''#!/usr/bin/python3
import os,sys
assert sys.argv[1:4] == ['-I','-B','-c']
code=sys.argv[4]
sys.version_info=tuple(int(part) for part in os.environ['OBSERVED_PYTHON'].split('.'))
sys.argv=['-c',sys.argv[5]]
exec(code)
''')
        interpreter.chmod(0o755)
        env = {'PATH': '/usr/bin:/bin', 'TMPDIR': str(self.root), 'SHARED_PYTHON': str(interpreter),
               'SHARED_PYTHON_VERSION': '3.12.3', 'OBSERVED_PYTHON': '3.9.6'}
        # No Cloud/Git inputs are supplied: a version failure must happen first.
        for observed, expected in [('3.9.6', '3.11 or newer'), ('3.12.2', 'differs from')]:
            with self.subTest(observed=observed):
                result = subprocess.run(['bash', str(loader)], env={**env, 'OBSERVED_PYTHON': observed},
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertEqual(list(self.root.glob('shared-mobile.*')), [])
        for changed in ({'SHARED_PYTHON': 'python3'}, {'SHARED_PYTHON_VERSION': '3.12'},
                        {'SHARED_PYTHON': str(self.root / 'absent')}):
            with self.subTest(changed=changed):
                result = subprocess.run(['bash', str(loader)], env={**env, **changed},
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('CI_PRIMARY_REPOSITORY_PATH', result.stderr)

    def test_failed_cli_removes_previous_passing_receipt(self):
        self.write('config.json', b'{"operation":"unknown"}')
        self.write('receipt.json', b'{"schema":1,"status":"passed"}')
        result = subprocess.run([sys.executable, '-m', 'shared_ci.mobile', '--root', str(self.root),
                                 '--config', 'config.json', '--output', 'receipt.json'],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'receipt.json').exists())
        self.assertIn('Unknown mobile operation', result.stderr)

    def test_unknown_config_fields_and_traversal_fail(self):
        with self.assertRaises(PipelineError): execute({'operation': 'unknown'}, self.root)
        config = self.nextbuild(); config['secret'] = 'do-not-allow'
        with self.assertRaises(PipelineError): execute(config, self.root)
        config.pop('secret'); config['ledger'] = '../outside'
        with self.assertRaises(PipelineError): execute(config, self.root)


if __name__ == '__main__': unittest.main()
