import copy
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci import evidence, runtime, state, stores
from shared_ci.common import PipelineError, atomic_json, fingerprint, load_json, sha256_file
from shared_ci.evidence import IDENTITY_FIELDS
from shared_ci.security import _candidate_binding
from test_stores import fixture, PlayAPI, TRUST
from test_wrappers import wrapper


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.config, self.candidate, self.inspected = fixture(self.root)
        self.config_path = self.root / 'config.json'
        # Product configuration does not commit a moving candidate SHA.
        self.config.update(candidate='selected-at-runtime.json', expected_identity='workflow', expected_source_sha='workflow')
        atomic_json(self.config_path, self.config)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', 'config.json', 'input.txt')
        self.git('commit', '-qm', 'fixture')
        self.outputs = Path(self.temp.name) / 'outputs'
        self.env = {**TRUST, 'GITHUB_WORKSPACE': str(self.root), 'GITHUB_OUTPUT': str(self.outputs), 'RUNNER_TEMP': self.temp.name,
                    'GITHUB_REPOSITORY': 'example/app', 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
                    'SHARED_MODULE': 'stores', 'SHARED_CONFIG': 'config.json', 'SHARED_ROOT': '.', 'SHARED_ACTION_REF': 'c' * 40,
                    'SHARED_CANDIDATE_MANIFEST': 'candidate.json', 'SHARED_CANDIDATE_IDENTITY': self.candidate['identity'],
                    'SHARED_CANDIDATE_SOURCE_SHA': self.candidate['source_sha'], 'GH_TOKEN': 'synthetic-evidence-token', 'GH_STATE_TOKEN': 'synthetic-state-token'}
        self.provider = PlayAPI(self.candidate['artifacts']['app.aab']['sha256'])
        self.bind_tree()
        self.selector = {'repository': 'example/app', 'run_id': '41', 'run_attempt': 1, 'artifact_name': 'prior-receipt',
                         'run_head_sha': 'a' * 40, 'expected_workflow': '.github/workflows/release.yml',
                         'allowed_branches': ['staging'], 'allowed_events': ['push'], 'token_env': 'GH_TOKEN'}

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def write_config(self, config):
        self.config = config
        atomic_json(self.config_path, config)
        self.git('add', 'config.json')
        self.git('commit', '--allow-empty', '-qm', 'configure fixture')
        self.bind_tree()

    def bind_tree(self):
        self.candidate['source_tree'] = self.git('rev-parse', 'HEAD^{tree}')
        for relative in self.candidate['checks']:
            receipt = load_json(self.root / relative)
            receipt['assurance']['source_tree'] = self.candidate['source_tree']
            atomic_json(self.root / relative, receipt)
            self.candidate['checks'][relative] = sha256_file(self.root / relative)
        self.rehash()

    def without_candidate(self):
        for key in runtime.SELECTION_ENV:
            self.env.pop(key, None)

    def read_outputs(self):
        return dict(line.split('=', 1) for line in self.outputs.read_text().splitlines())

    def execute(self):
        with patch.dict(os.environ, self.env, clear=True), patch('shared_ci.stores.API', return_value=self.provider), patch('shared_ci.stores.verify_aab', return_value=(self.root / 'app.aab', self.provider.digest)):
            wrapper.main()
        return load_json(self.read_outputs()['receipt'])

    def rehash(self):
        self.candidate['identity'] = fingerprint({key: self.candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.root / 'candidate.json', self.candidate)
        if 'SHARED_CANDIDATE_IDENTITY' in self.env:
            self.env['SHARED_CANDIDATE_IDENTITY'] = self.candidate['identity']

    def receipt_request(self, receipt, selector=None):
        selector = selector or self.selector
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w') as archive:
            archive.writestr('receipt.json', json.dumps(receipt))
        payload = blob.getvalue()
        metadata = {'id': int(selector['run_id']), 'run_attempt': selector['run_attempt'], 'status': 'completed', 'conclusion': 'success',
                    'head_sha': selector['run_head_sha'], 'head_branch': 'staging', 'head_repository': {'full_name': 'example/app'},
                    'path': selector['expected_workflow'], 'event': 'push', 'run_started_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T01:00:00Z'}
        artifact = {'id': 99, 'name': selector['artifact_name'], 'expired': False, 'created_at': '2026-01-01T00:30:00Z',
                    'workflow_run': {'id': int(selector['run_id']), 'head_sha': selector['run_head_sha']},
                    'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(), 'size_in_bytes': len(payload)}
        def request(path, token, binary=False):
            if binary:
                return payload
            if '/artifacts?' in path:
                return {'artifacts': [artifact]}
            return metadata
        return request

    def fetched(self, receipt, selector=None):
        request = self.receipt_request(receipt, selector)
        return patch('shared_ci.runtime.fetch_receipt', side_effect=lambda *args, **kwargs: evidence.fetch_receipt(*args, request=request, **kwargs))

    def test_runtime_candidate_overrides_uncommitted_moving_selection(self):
        result = self.execute()
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['candidate_identity'], self.candidate['identity'])
        self.assertEqual(load_json(self.config_path)['expected_identity'], 'workflow')
        envelope = load_json(Path(self.read_outputs()['bundle']) / 'operation-state.json')
        self.assertEqual(envelope['kind'], 'store-operation-state')
        effective = {**self.config, 'candidate': 'candidate.json', 'expected_identity': self.candidate['identity'], 'expected_source_sha': 'a' * 40}
        self.assertEqual(envelope['request_fingerprint'], fingerprint(effective))

    def test_partial_candidate_selection_blocks_provider(self):
        self.env.pop('SHARED_CANDIDATE_SOURCE_SHA')
        with self.assertRaisesRegex(PipelineError, 'together'):
            self.execute()
        self.assertFalse(self.provider.calls)

    def test_mismatched_selected_library_blocks_provider(self):
        self.env['SHARED_ACTION_REF'] = 'f' * 40
        with self.assertRaisesRegex(PipelineError, 'executing helper'):
            self.execute()
        self.assertFalse(self.provider.calls)

    def test_store_without_selected_candidate_is_refused(self):
        self.without_candidate()
        with self.assertRaisesRegex(PipelineError, 'explicitly selected'):
            self.execute()
        self.assertFalse(self.provider.calls)

    def test_stateful_rerun_without_restore_refuses_provider(self):
        self.env['GITHUB_RUN_ATTEMPT'] = '2'
        with self.assertRaisesRegex(PipelineError, 'prior state selection'):
            self.execute()
        self.assertFalse(self.provider.calls)

    def test_uncertain_commit_survives_fresh_runner_and_cannot_republish(self):
        self.env['SHARED_STORE_SELECTION'] = json.dumps({'play': {'expected_track_digest': fingerprint({'track': 'internal', 'releases': []})}})
        self.config['play']['expected_track_digest'] = 'workflow'
        self.write_config(self.config)
        self.provider.commit_errors = [PipelineError('lost provider response with synthetic-secret')]
        with self.assertRaises(PipelineError):
            self.execute()
        saved = Path(self.read_outputs()['bundle'])
        self.assertEqual(load_json(saved / 'operation-state.json')['state']['phase'], 'commit_started')
        self.assertNotIn('synthetic-secret', (saved / 'receipt.json').read_text())
        archive = Path(self.temp.name) / 'saved-state.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            for path in saved.iterdir():
                zipped.write(path, path.name)
        (self.root / 'state.json').unlink()
        self.env['GITHUB_RUN_ATTEMPT'] = '2'
        selection = {'schema': 1, 'operation': 'restore', 'repository': 'example/app', 'run_id': '42', 'run_attempt': 1,
                     'artifact_name': 'operation-state-42-1', 'expected_workflow': '.github/workflows/store.yml', 'producer_job': 'Store / Execute',
                     'allowed_branches': ['staging'], 'allowed_events': ['workflow_dispatch'], 'allowed_conclusions': ['failure'], 'token_env': 'GH_STATE_TOKEN'}
        self.env['SHARED_STATE_RESTORE'] = json.dumps(selection)
        metadata = {'id': 42, 'run_attempt': 1, 'status': 'completed', 'conclusion': 'failure', 'head_sha': 'a' * 40, 'head_branch': 'staging',
                    'path': '.github/workflows/store.yml', 'head_repository': {'full_name': 'example/app'}, 'event': 'workflow_dispatch',
                    'run_started_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T01:00:00Z'}
        job = dict(metadata, id=123, run_id=42, name='Store / Execute', started_at='2026-01-01T00:01:00Z', completed_at='2026-01-01T00:59:00Z')
        item = {'id': 99, 'name': selection['artifact_name'], 'expired': False, 'created_at': '2026-01-01T00:30:00Z',
                'workflow_run': {'id': 42, 'head_sha': 'a' * 40}, 'size_in_bytes': archive.stat().st_size, 'digest': 'sha256:' + sha256_file(archive)}
        def request(path, token):
            if '/jobs?' in path: return {'jobs': [job]}
            if '/artifacts?' in path: return {'artifacts': [item]}
            return metadata
        restore = state.restore
        def restore_saved(selected, root, *, store_config):
            self.assertEqual(selected['run_head_sha'], 'a' * 40)
            return restore(selected, root, store_config=store_config, request=request,
                           downloader=lambda path, token, destination, limit: shutil.copyfile(archive, destination))
        self.provider.calls.clear()
        with patch('shared_ci.state.restore', side_effect=restore_saved), self.assertRaisesRegex(PipelineError, 'uncertain'):
            self.execute()
        self.assertFalse(self.provider.calls)
        self.assertEqual(load_json(Path(self.read_outputs()['bundle']) / 'operation-state.json')['state']['phase'], 'commit_started')

    def test_readonly_deploy_receives_no_empty_candidate_overrides(self):
        self.without_candidate()
        self.env['SHARED_MODULE'] = 'deploy'
        self.write_config({'operation': 'validate'})
        with patch('shared_ci.deploy.execute', return_value={'status': 'passed'}) as execute:
            self.execute()
        self.assertEqual(execute.call_args.kwargs, {'require_candidate': False})

    def test_mutating_deploy_requires_candidate_before_provider_call(self):
        self.without_candidate()
        self.env['SHARED_MODULE'] = 'deploy'
        self.write_config({'operation': 'deploy'})
        with patch('shared_ci.deploy.execute') as execute, self.assertRaisesRegex(PipelineError, 'Mutating deployment'):
            self.execute()
        execute.assert_not_called()

    def test_deploy_receives_typed_authenticated_prior_receipt(self):
        self.env.update(SHARED_MODULE='deploy', SHARED_PRIOR_DEPLOYMENT=json.dumps(self.selector))
        self.write_config({'operation': 'promote'})
        with self.fetched({'schema': 1, 'status': 'passed', 'kind': 'deployment'}), patch('shared_ci.deploy.execute', return_value={'status': 'passed'}) as execute:
            self.execute()
        kwargs = execute.call_args.kwargs
        self.assertTrue(kwargs['require_candidate'])
        self.assertEqual(kwargs['candidate'], 'candidate.json')
        self.assertEqual(kwargs['expected_identity'], self.candidate['identity'])
        self.assertIsInstance(kwargs['promotion_receipt'], evidence.AuthenticatedReceipt)
        kwargs['promotion_receipt'].validate_integrity()

    def test_security_scan_receives_selected_candidate_object(self):
        self.env['SHARED_MODULE'] = 'security'
        self.write_config({'schema': 1})
        with patch('shared_ci.security.scan', return_value={'status': 'passed'}) as scan:
            self.execute()
        self.assertEqual(scan.call_args.kwargs['candidate']['identity'], self.candidate['identity'])

    def test_security_refresh_is_authenticated_candidate_bound_and_scoped(self):
        policy = {'threshold': 'high', 'max_age_hours': 24, 'exceptions': []}
        atomic_json(self.root / 'policy.json', policy)
        created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
        assurance = {'kind': 'security', 'source_sha': 'a' * 40, 'source_tree': self.candidate['source_tree'], 'config_sha256': sha256_file(self.root / 'policy.json')}
        original = {'schema': 1, 'status': 'passed', 'max_age_hours': 24, 'policy_identity': fingerprint(policy),
                    'checked_at': (created - dt.timedelta(hours=1)).isoformat(), 'assurance': assurance}
        atomic_json(self.root / 'security.json', original)
        self.candidate['inputs']['policy.json'] = {'sha256': sha256_file(self.root / 'policy.json'), 'bytes': (self.root / 'policy.json').stat().st_size}
        self.candidate['checks']['security.json'] = sha256_file(self.root / 'security.json')
        self.candidate['check_requirements']['security.json'] = {'kind': 'security', 'config': 'policy.json'}
        self.candidate['created_at'] = created.isoformat()
        self.rehash()
        fresh = {**original, 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'scanners': [{'id': 'fixture'}], 'counts': {'blocking': 0},
                 'candidate_binding': _candidate_binding(self.candidate, self.root),
                 'provenance': {'repository': 'example/app', 'run_id': '41', 'sha': 'a' * 40}}
        self.env['SHARED_SECURITY_REFRESH'] = json.dumps({'security.json': {'selector': self.selector, 'policy': 'policy.json',
                                                                          'policy_sha256': sha256_file(self.root / 'policy.json'), 'source_sha': 'a' * 40}})
        with self.fetched(fresh):
            result = self.execute()
        self.assertEqual(result['status'], 'pending')
        with self.assertRaisesRegex(PipelineError, 'stale'):
            evidence.verify_checks(self.candidate, self.root)
        self.env.update(SHARED_MODULE='evidence', SHARED_OPERATION='verify', SHARED_CONFIG='candidate.json')
        with self.fetched(fresh):
            self.assertEqual(self.execute()['status'], 'passed')
        with self.assertRaisesRegex(PipelineError, 'stale'):
            evidence.verify_checks(self.candidate, self.root)

    def test_current_container_release_overrides_only_deploy_contract(self):
        self.without_candidate()
        self.env.update(SHARED_MODULE='containers', SHARED_CURRENT_RELEASE='absent')
        self.write_config({'operation': 'rollback', 'expected_current_release': 'workflow'})
        with patch('shared_ci.containers.execute', return_value={'status': 'passed'}) as execute:
            self.execute()
        self.assertEqual(execute.call_args.args[0]['expected_current_release'], 'absent')
        self.env['SHARED_CURRENT_RELEASE'] = '$(unsafe)'
        with self.assertRaisesRegex(PipelineError, 'Current release'):
            self.execute()

    def candidate_config(self, destination='quality.json'):
        self.without_candidate()
        self.env.update(SHARED_MODULE='evidence', SHARED_OPERATION='create')
        self.write_config({'schema': 1, 'product': 'fixture', 'version': '1', 'build': '1', 'library_sha': 'workflow',
                           'inputs': ['input.txt'], 'artifacts': ['app.aab'], 'toolchain': {'fixture': '1'},
                           'checks': [{'path': destination, 'kind': 'quality', 'config': 'input.txt'}]})
        self.selector['run_head_sha'] = self.git('rev-parse', 'HEAD')
        self.env['SHARED_CHECK_RECEIPTS'] = json.dumps({destination: self.selector})
        return {'schema': 1, 'status': 'passed', 'assurance': {'kind': 'quality', 'source_sha': self.selector['run_head_sha'],
                'source_tree': self.git('rev-parse', 'HEAD^{tree}'), 'config_sha256': sha256_file(self.root / 'input.txt')}}

    def test_candidate_creation_materializes_authenticated_other_job_check(self):
        receipt = self.candidate_config()
        with self.fetched(receipt):
            result = self.execute()
        self.assertEqual(result['checks']['quality.json'], sha256_file(self.root / 'quality.json'))
        self.assertEqual(result['library_sha'], 'c' * 40)
        self.assertEqual(result['source_sha'], self.selector['run_head_sha'])
        self.assertEqual(load_json(self.root / 'quality.json')['authenticated_origin']['run_id'], '41')

    def test_imported_check_conflict_is_never_overwritten(self):
        receipt = self.candidate_config()
        (self.root / 'quality.json').write_text('existing unrelated data')
        with self.fetched(receipt), self.assertRaisesRegex(PipelineError, 'conflicts'):
            self.execute()
        self.assertEqual((self.root / 'quality.json').read_text(), 'existing unrelated data')

    def test_imported_check_cannot_overwrite_tracked_source(self):
        receipt = self.candidate_config('input.txt')
        with self.fetched(receipt), self.assertRaisesRegex(PipelineError, 'tracked source'):
            self.execute()

    def test_authenticated_check_wrong_source_cannot_create_candidate(self):
        receipt = self.candidate_config()
        receipt['assurance']['source_sha'] = 'f' * 40
        with self.fetched(receipt), self.assertRaisesRegex(PipelineError, 'not bound'):
            self.execute()

    def test_apple_import_runtime_selection_pins_target_and_emits_manifest(self):
        self.without_candidate()
        cloud = {'workflow_id': 'workflow1', 'product_id': 'product1', 'build_run_id': 'run1', 'build_number': '123'}
        self.write_config({'operation': 'apple-import', 'cloud': cloud, 's3': {}, 'asc_token_env': 'ASC_JWT'})
        self.env.update(SHARED_MODULE='apple_bridge', ASC_PRIVATE_KEY_PATH='/existing/fixture.p8', ASC_BEARER_TOKEN='synthetic-jwt',
                        SHARED_APPLE_BRIDGE_SELECTION=json.dumps({'cloud': cloud, 'expected_identity': self.candidate['identity'],
                                                                  'expected_source_sha': 'a' * 40, 'version_id': 'immutable-version'}))
        result = {'schema': 1, 'status': 'passed', 'asc_run_verified': True, 'manifest': 'candidate.json',
                  'candidate_identity': self.candidate['identity'], 'source_sha': 'a' * 40}
        with patch('shared_ci.apple_bridge.execute', return_value=result) as execute:
            self.execute()
        effective = execute.call_args.args[0]
        self.assertEqual(effective['asc_token_env'], 'ASC_BEARER_TOKEN')
        self.assertEqual(effective['s3']['version_id'], 'immutable-version')
        self.assertEqual(self.read_outputs()['manifest'], 'candidate.json')
        changed = json.loads(self.env['SHARED_APPLE_BRIDGE_SELECTION'])
        changed['cloud']['workflow_id'] = 'unapproved'
        self.env['SHARED_APPLE_BRIDGE_SELECTION'] = json.dumps(changed)
        with self.assertRaisesRegex(PipelineError, 'approved workflow'):
            self.execute()

    def test_receipt_path_cannot_destroy_retained_artifact(self):
        self.env['SHARED_RECEIPT_PATH'] = 'app.aab'
        with self.assertRaisesRegex(PipelineError, 'retained candidate'):
            self.execute()
        self.assertFalse(self.provider.calls)

    def test_duplicate_runtime_json_field_is_refused(self):
        self.env['SHARED_PRIOR_DEPLOYMENT'] = '{"run_id":"1","run_id":"2"}'
        with self.assertRaisesRegex(PipelineError, 'duplicate'):
            self.execute()

    def test_different_or_dirty_source_blocks_provider(self):
        self.git('commit', '--allow-empty', '-qm', 'same tree is allowed')
        (self.root / 'unrelated.txt').write_text('different tree')
        self.git('add', 'unrelated.txt')
        self.git('commit', '-qm', 'different source')
        with self.assertRaisesRegex(PipelineError, 'source tree'):
            self.execute()
        self.bind_tree()
        self.config_path.write_text('{}')
        with self.assertRaisesRegex(PipelineError, 'clean tracked'):
            self.execute()
        self.assertFalse(self.provider.calls)

    def test_changed_retained_bytes_after_operation_do_not_publish_success(self):
        def changed(*args, **kwargs):
            (self.root / 'app.aab').write_bytes(b'changed while executing')
            return {'status': 'passed'}
        with patch('shared_ci.stores.execute', side_effect=changed), self.assertRaises(PipelineError):
            self.execute()
        self.assertEqual(load_json(self.read_outputs()['receipt'])['status'], 'failed')
        self.assertEqual(self.read_outputs()['status'], 'failed')

    def test_provider_token_cannot_be_used_for_github_receipt_fetch(self):
        self.selector['token_env'] = 'STORE_TOKEN'
        self.env.update(SHARED_MODULE='deploy', SHARED_PRIOR_DEPLOYMENT=json.dumps(self.selector))
        self.write_config({'operation': 'promote'})
        with patch('shared_ci.runtime.fetch_receipt') as fetch, self.assertRaisesRegex(PipelineError, 'dedicated GitHub'):
            self.execute()
        fetch.assert_not_called()

    def test_foreign_repository_receipt_is_rejected_before_fetch(self):
        self.selector['repository'] = 'foreign/app'
        self.env.update(SHARED_MODULE='deploy', SHARED_PRIOR_DEPLOYMENT=json.dumps(self.selector))
        self.write_config({'operation': 'promote'})
        with patch('shared_ci.runtime.fetch_receipt') as fetch, self.assertRaisesRegex(PipelineError, 'current repository'):
            self.execute()
        fetch.assert_not_called()

    def retain(self, relative, value):
        atomic_json(self.root / relative, value)
        self.candidate['artifacts'][relative] = {'sha256': sha256_file(self.root / relative), 'bytes': (self.root / relative).stat().st_size}
        self.rehash()

    def test_container_promotion_uses_exact_retained_publication(self):
        tag = 'registry.example/team/app:tested'
        source = 'registry.example/team/app@sha256:' + 'd' * 64
        self.retain('publication.json', {'status': 'passed', 'schema': 'shared-ci.container-publication/v1',
                                       'source_sha': self.candidate['source_sha'], 'images': {tag: source}})
        self.env.update(SHARED_MODULE='containers', SHARED_CONTAINER_SELECTION=json.dumps({
            'publication': 'publication.json', 'tag': tag, 'expected_current_digest': 'absent'}))
        self.write_config({'operation': 'promote', 'source': 'workflow', 'expected_current_digest': 'workflow'})
        with patch('shared_ci.containers.execute', return_value={'status': 'passed'}) as execute:
            self.execute()
        self.assertEqual(execute.call_args.args[0]['source'], source)
        self.assertEqual(execute.call_args.args[0]['expected_current_digest'], 'absent')
        del self.candidate['artifacts']['publication.json']
        self.rehash()
        with patch('shared_ci.containers.execute') as execute, self.assertRaisesRegex(PipelineError, 'absent from'):
            self.execute()
        execute.assert_not_called()

    def test_container_deploy_requires_retained_compose_and_support_files(self):
        self.retain('components.json', {'source_sha': self.candidate['source_sha']})
        (self.root / 'compose.yaml').write_text('services: {}')
        self.env['SHARED_MODULE'] = 'containers'
        self.write_config({'operation': 'deploy', 'manifest': 'components.json', 'compose_files': ['compose.yaml']})
        with patch('shared_ci.containers.execute') as execute, self.assertRaisesRegex(PipelineError, 'absent from'):
            self.execute()
        execute.assert_not_called()

    def test_container_manifest_derives_source_and_checks_component_publication(self):
        self.retain('publication.json', {'source_sha': self.candidate['source_sha']})
        self.env['SHARED_MODULE'] = 'containers'
        self.write_config({'operation': 'manifest', 'source_sha': 'workflow',
                           'components': {'app': {'publication': 'publication.json', 'tag': 'registry.example/app:tested'}}})
        with patch('shared_ci.containers.execute', return_value={'status': 'passed'}) as execute:
            self.execute()
        self.assertEqual(execute.call_args.args[0]['source_sha'], self.candidate['source_sha'])
        self.retain('publication.json', {'source_sha': 'f' * 40})
        with patch('shared_ci.containers.execute') as execute, self.assertRaisesRegex(PipelineError, 'another candidate source'):
            self.execute()
        execute.assert_not_called()

    def test_release_publication_binds_selected_manifest_and_executing_revision(self):
        self.env.update(SHARED_MODULE='release', SHARED_OPERATION='publish')
        self.write_config({'candidate': 'workflow', 'expected_library_sha': 'workflow', 'expected_commit': 'workflow'})
        with patch('shared_ci.release.publish', return_value={'status': 'passed'}) as publish:
            self.execute()
        effective = publish.call_args.args[0]
        self.assertEqual(effective['candidate'], 'candidate.json')
        self.assertEqual(effective['expected_library_sha'], 'c' * 40)
        self.assertEqual(effective['expected_commit'], self.env['GITHUB_SHA'])

    def test_readonly_operation_cannot_accept_irrelevant_refresh(self):
        self.env.update(SHARED_MODULE='release', SHARED_OPERATION='render', SHARED_SECURITY_REFRESH='{"ignored":{}}')
        with self.assertRaisesRegex(PipelineError, 'selected candidate consumer'):
            self.execute()

    def test_store_selection_changes_only_moving_provider_state(self):
        self.env['SHARED_STORE_SELECTION'] = json.dumps({'play': {'expected_track_digest': 'd' * 64}})
        with patch('shared_ci.stores.execute', return_value={'status': 'pending'}) as execute:
            self.execute()
        self.assertEqual(execute.call_args.args[0]['play']['expected_track_digest'], 'd' * 64)
        self.assertEqual(execute.call_args.args[0]['play']['package'], self.config['play']['package'])
        self.env['SHARED_STORE_SELECTION'] = json.dumps({'play': {'package': 'another.app'}})
        with patch('shared_ci.stores.execute') as execute, self.assertRaisesRegex(PipelineError, 'unknown'):
            self.execute()
        execute.assert_not_called()

    def test_apple_selection_keeps_app_groups_and_review_authority(self):
        self.write_config({'operation': 'testflight', 'apple': {'app_id': '100', 'build_id': 'workflow', 'group_ids': ['approved'], 'submit_beta_review': False}})
        self.env['SHARED_STORE_SELECTION'] = json.dumps({'apple': {'build_id': '200', 'expected_selected_build': None}})
        with patch('shared_ci.stores.execute', return_value={'status': 'pending'}) as execute:
            self.execute()
        effective = execute.call_args.args[0]['apple']
        self.assertEqual(effective['build_id'], '200')
        self.assertEqual(effective['app_id'], '100')
        self.assertEqual(effective['group_ids'], ['approved'])
        self.assertFalse(effective['submit_beta_review'])
        self.env['SHARED_STORE_SELECTION'] = json.dumps({'apple': {'submit_beta_review': True}})
        with patch('shared_ci.stores.execute') as execute, self.assertRaisesRegex(PipelineError, 'unknown'):
            self.execute()
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
