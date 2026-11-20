"""Product callers must compose real contracts and retain their trust boundaries."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('caller_templates', ROOT / 'scripts/render-product-workflows.py')
CALLERS = importlib.util.module_from_spec(SPEC)
_previous = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(CALLERS)
finally:
    sys.dont_write_bytecode = _previous
SHA = 'a' * 40


def configuration(*workflows):
    return {'schema': 1, 'repository': 'example/product', 'workflows': list(workflows)}


def rendered(recipe, **values):
    files = CALLERS.render(configuration({'recipe': recipe, **values}), SHA)
    name = next(path for path in files if path.startswith('.github/workflows/'))
    return CALLERS.yaml.load(files[name], Loader=CALLERS.ENGINE.StrictLoader), files


class CallerTemplateTests(unittest.TestCase):
    def test_all_recipes_and_provider_variants_match_reusable_contracts(self):
        report = CALLERS.check_sources()
        self.assertEqual(report['recipes'], 10)
        self.assertGreaterEqual(report['rendered_variants'], 18)

    def test_ci_checks_only_feature_prs_and_has_no_provider_credentials(self):
        workflow, files = rendered('ci')
        self.assertEqual(workflow['on'], {'pull_request': {'branches': ['develop']}})
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertIn('v2-plan.yml@', workflow['jobs']['route']['uses'])
        self.assertIn('v2-checks.yml@', workflow['jobs']['quality']['uses'])
        self.assertNotIn('mock-ai', workflow['jobs'])
        self.assertTrue(all(not job.get('secrets') for job in workflow['jobs'].values()))
        self.assertNotIn('workflow_dispatch', workflow['on'])
        self.assertNotIn('push', workflow['on'])
        self.assertTrue(workflow['concurrency']['cancel-in-progress'])

    def test_enabled_checks_have_an_always_reported_complete_required_policy(self):
        for options in ({}, {'security': False, 'docs': False, 'mock_ai': True}):
            workflow, files = rendered('ci', **options)
            gate = workflow['jobs']['required']
            expected = set(workflow['jobs']) - {'required'}
            self.assertEqual(gate['if'], '${{ always() }}')
            self.assertEqual(set(gate['needs']), expected)
            policy = json.loads(files[gate['with']['config']])
            self.assertEqual(policy, {'schema': 1, 'checks': {key: True for key in expected}})
            self.assertNotIn('continue-on-error', json.dumps(workflow))

    def test_promotion_uses_authenticated_original_quality_without_reexecuting_tests(self):
        workflow, files = rendered('promotion-checks')
        self.assertEqual(workflow['on'], {'pull_request': {'branches': ['staging', 'main']}})
        self.assertEqual(set(workflow['jobs']), {'route', 'reuse', 'required'})
        reuse = workflow['jobs']['reuse']
        self.assertIn('v2-reuse.yml@', reuse['uses'])
        self.assertNotIn('if', reuse)
        self.assertEqual(reuse['with']['quality-producer-job'], 'quality / run')
        self.assertEqual(reuse['with']['quality-allowed-events'], '["pull_request"]')
        for key in ('quality-run-id', 'quality-run-attempt', 'quality-run-head-sha', 'quality-tested-sha', 'quality-artifact'):
            self.assertIn('fromJSON(vars.PROMOTION_QUALITY)', reuse['with'][key])
        config = json.loads(files[reuse['with']['config']])
        self.assertEqual(config['source']['repository'], 'example/product')
        self.assertEqual(config['source']['workflow'], '.github/workflows/ci.yml')
        self.assertEqual(config['source']['run_attempt'], {'env': 'QUALITY_RUN_ATTEMPT'})
        self.assertEqual(config['source']['allowed_branches'], {'env': 'QUALITY_ALLOWED_BRANCHES'})
        self.assertGreater(config['max_age_seconds'], 0)

    def test_provider_delivery_is_manual_branch_bound_and_authenticates_retained_candidate(self):
        for provider in CALLERS.PROVIDER:
            for branch, environment in [('staging', 'staging'), ('main', 'production')]:
                workflow, _ = rendered('web-delivery', provider=provider, branch=branch, environment=environment)
                self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
                job = workflow['jobs']['delivery']
                self.assertIn('refs/heads/' + branch, job['if'])
                self.assertEqual(job['with']['environment'], environment)
                self.assertEqual(job['with']['artifact-allowed-branches'], '["staging"]')
                self.assertEqual(job['with']['artifact-workflow'], '.github/workflows/build-candidate.yml')
                for selector in ('artifact-run-id', 'artifact-run-attempt', 'artifact-run-head-sha', 'artifact-source-sha', 'artifact-identity', 'artifact-input-name'):
                    self.assertIn('fromJSON(inputs.candidate)', job['with'][selector])
                self.assertFalse(workflow['concurrency']['cancel-in-progress'])

    def test_only_selected_hosting_provider_secret_is_exposed(self):
        expected = {'aws': set(), 'firebase': set(), 'cloudflare': {'CLOUDFLARE_API_TOKEN'},
                    'vercel': {'VERCEL_TOKEN'}, 'netlify': {'NETLIFY_AUTH_TOKEN'}}
        for provider, names in expected.items():
            workflow, _ = rendered('web-delivery', provider=provider)
            self.assertEqual(set(workflow['jobs']['delivery'].get('secrets', {})), names)
            if provider in {'aws', 'firebase'}:
                self.assertEqual(workflow['permissions']['id-token'], 'write')
            else:
                self.assertNotIn('id-token', workflow['permissions'])

    def test_reusable_tool_defaults_are_preserved_and_bundles_accept_explicit_lists(self):
        for recipe in ('android-staging', 'apple-staging', 'web-delivery'):
            values = {'provider': 'firebase'} if recipe == 'web-delivery' else {}
            workflow, _ = rendered(recipe, **values)
            self.assertTrue(all('tool-bundles' not in job.get('with', {}) for job in workflow['jobs'].values()))
        workflow, _ = rendered('android-staging', tool_bundles='bundletool,android-build-tools')
        self.assertEqual(workflow['jobs']['verify']['with']['tool-bundles'], 'bundletool,android-build-tools')
        for invalid in ('latest', 'bundletool,bundletool', '.ci/tools.json'):
            with self.assertRaises(CALLERS.Error):
                rendered('android-staging', tool_bundles=invalid)

    def test_android_inspection_and_store_jobs_consume_actual_producer_outputs(self):
        workflow, _ = rendered('android-staging')
        jobs = workflow['jobs']
        self.assertEqual(jobs['verify']['needs'], ['build'])
        self.assertEqual(jobs['distribute']['needs'], ['verify'])
        for consumer, producer in [('verify', 'build'), ('distribute', 'verify')]:
            with_ = jobs[consumer]['with']
            self.assertEqual(with_['artifact-run-attempt'], '${{ needs.' + producer + '.outputs.run-attempt }}')
            self.assertEqual(with_['artifact-input-name'], '${{ needs.' + producer + '.outputs.candidate-artifact-name }}')
            self.assertEqual(with_['artifact-identity'], '${{ needs.' + producer + '.outputs.candidate-identity }}')
            self.assertEqual(with_['artifact-producer-job'], producer + ' / run')
        self.assertIn('needs.build.outputs.receipt-artifact-name', jobs['verify']['with']['check-receipts'])
        self.assertEqual(jobs['verify']['with']['receipt-path'], 'checks/android.json')
        self.assertIn('already selected by product build configuration', workflow['on']['workflow_dispatch']['inputs']['build_number']['description'])

    def test_android_helper_passes_the_dispatch_number_to_product_builds_and_rejects_invalid_context(self):
        _, files = rendered('android-staging')
        path = '.ci/callers/read-android-build-number.py'
        self.assertIn(path, files)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = root / 'number.py'; helper.write_text(files[path])
            event = root / 'event.json'; event.write_text(json.dumps({'inputs': {'build_number': '42'}}))
            env = {**os.environ, 'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_REF': 'refs/heads/staging', 'GITHUB_EVENT_PATH': str(event)}
            command = [sys.executable, '-B', str(helper)]
            success = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(success.returncode, 0)
            self.assertEqual(success.stdout, '42\n')
            for value in ('0', '-1', '2;echo token', '2000000001', 42, None):
                event.write_text(json.dumps({'inputs': {'build_number': value}}))
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
            event.write_text(json.dumps({'inputs': {'build_number': '42'}}))
            result = subprocess.run(command, env={**env, 'GITHUB_REF': 'refs/heads/main'}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_store_retries_use_previous_state_and_gate_pending_outcomes(self):
        for recipe in ('android-staging', 'apple-staging', 'mobile-release'):
            workflow, files = rendered(recipe)
            state = workflow['jobs']['distribute']['with']['state-restore']
            self.assertIn('"run_attempt":"previous"', state)
            self.assertIn('store-run-{run_id}-attempt-{run_attempt}', state)
            self.assertIn('"producer_job":"distribute / run"', state)
            gate = workflow['jobs']['required']
            self.assertTrue(json.loads(files[gate['with']['config']])['checks']['distribute'])

    def test_apple_import_keeps_compilation_outside_github_and_requires_named_existing_key(self):
        workflow, _ = rendered('apple-staging')
        jobs = workflow['jobs']
        self.assertIn('v2-apple-import.yml@', jobs['import']['uses'])
        self.assertEqual(jobs['import']['secrets'], {'ASC_PRIVATE_KEY_BASE64': '${{ secrets.ASC_PRIVATE_KEY_BASE64 }}'})
        self.assertEqual(jobs['distribute']['with']['artifact-producer-job'], 'import / run')
        self.assertEqual(jobs['distribute']['with']['artifact-source-sha'], '${{ needs.import.outputs.source-sha }}')
        self.assertNotIn('macos', json.dumps(workflow).lower())
        self.assertTrue(all('runs-on' not in job for job in jobs.values()))

    def test_production_store_recipe_requires_main_and_selected_platform_identity(self):
        for platform in ('apple', 'android'):
            workflow, _ = rendered('mobile-release', platform=platform)
            job = workflow['jobs']['distribute']
            self.assertIn('refs/heads/main', job['if'])
            self.assertEqual(job['with']['environment'], 'production')
            self.assertEqual(job['with']['artifact-workflow'], '.github/workflows/staging.yml')
            if platform == 'apple':
                self.assertIn('ASC_PRIVATE_KEY_BASE64', job['secrets'])
                self.assertNotIn('google-service-account', job['with'])
            else:
                self.assertIn('google-service-account', job['with'])
                self.assertNotIn('secrets', job)

    def test_container_build_uses_native_adapter_and_retains_exact_candidate(self):
        workflow, _ = rendered('container-build')
        build = workflow['jobs']['build']
        self.assertIn('v2-container-build.yml@', build['uses'])
        self.assertEqual(build['with']['config'], '.ci/container-build.json')
        self.assertEqual(build['with']['receipt-path'], 'checks/container-build.json')
        self.assertEqual(build['with']['candidate-config'], '.ci/candidate-container.json')
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertNotIn('secrets', build)

    def test_wordpress_packages_without_implicit_dashboard_publication(self):
        workflow, _ = rendered('wordpress-package')
        self.assertEqual(set(workflow['jobs']), {'package', 'required'})
        self.assertIn('v2-wordpress-package.yml@', workflow['jobs']['package']['uses'])
        self.assertFalse(any(job.get('secrets') for job in workflow['jobs'].values()))
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})

    def test_untrusted_ref_invalid_paths_and_unknown_or_typed_options_fail(self):
        for revision in ('main', 'v2.0.0', '0' * 40, 'x' * 40):
            with self.assertRaises(CALLERS.Error):
                CALLERS.render(configuration({'recipe': 'ci'}), revision)
        for change in ({'quality_config': '../secret.json'}, {'quality_config': '${{ secrets.KEY }}'},
                       {'filename': '../ci.yml'}, {'filename': 'CI.yml'}, {'secrets': 'inherit'},
                       {'security': 'false'}, {'profile': 'arbitrary'}, {'profile': []}, {'unknown': True}):
            with self.subTest(change=change), self.assertRaises(CALLERS.Error):
                rendered('ci', **change)
        with self.assertRaises(CALLERS.Error):
            rendered('web-delivery', branch='develop')
        with self.assertRaises(CALLERS.Error):
            rendered('web-delivery', branch='main', environment='staging')
        with self.assertRaises(CALLERS.Error):
            rendered('web-delivery', producer_workflow='.github/workflows/../../evil.yml')

    def test_missing_repository_duplicate_output_and_duplicate_json_keys_fail(self):
        with self.assertRaises(CALLERS.Error):
            CALLERS.render({'schema': 1, 'workflows': [{'recipe': 'ci'}]}, SHA)
        with self.assertRaises(CALLERS.Error):
            CALLERS.render(configuration({'recipe': []}), SHA)
        with self.assertRaises(CALLERS.Error):
            CALLERS.render(configuration({'recipe': 'web-delivery'}, {'recipe': 'android-staging'}), SHA)
        with self.assertRaises(CALLERS.Error):
            CALLERS._strict_json('{"schema":1,"schema":2}')

    def test_validation_refuses_weakened_or_invalid_caller_contracts(self):
        workflow, _ = rendered('ci')
        mutations = [lambda d: d['jobs']['quality']['with'].update(unknown='bad'),
                     lambda d: d['jobs']['quality']['with'].pop('profile'),
                     lambda d: d.update(permissions={'contents': 'read'}),
                     lambda d: d['jobs']['quality'].update(secrets='inherit'),
                     lambda d: d['jobs']['required'].update(needs=['route']),
                     lambda d: d['jobs']['required'].pop('if'),
                     lambda d: d['jobs']['quality'].update(uses=d['jobs']['quality']['uses'].replace(SHA, 'main')),
                     lambda d: d.update(on={'pull_request_target': {'branches': ['develop']}})]
        for mutate in mutations:
            value = copy.deepcopy(workflow); mutate(value)
            with self.assertRaises(CALLERS.Error):
                CALLERS.validate(value, SHA)

    def test_validation_refuses_unknown_producer_outputs_and_dependency_cycles(self):
        workflow, _ = rendered('android-staging')
        workflow['jobs']['verify']['with']['artifact-run-id'] = '${{ needs.build.outputs.not-a-real-output }}'
        with self.assertRaises(CALLERS.Error):
            CALLERS.validate(workflow, SHA)
        workflow, _ = rendered('ci')
        workflow['jobs']['route']['needs'] = ['quality']
        with self.assertRaises(CALLERS.Error):
            CALLERS.validate(workflow, SHA)

    def test_cli_writes_deterministic_files_and_never_overwrites_existing_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'selection.json'; source.write_text(json.dumps(configuration({'recipe': 'ci', 'profile': 'shell', 'security': False, 'docs': False})))
            output = root / 'rendered'
            command = [sys.executable, '-B', str(ROOT / 'scripts/render-product-workflows.py'), '--config', str(source), '--library-sha', SHA, '--output', str(output)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            original = (output / '.github/workflows/ci.yml').read_text()
            self.assertEqual(original, CALLERS.render(json.loads(source.read_text()), SHA)['.github/workflows/ci.yml'])
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((output / '.github/workflows/ci.yml').read_text(), original)
            self.assertFalse(any(root.rglob('__pycache__')))
