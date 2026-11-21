import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from shared_ci.common import PipelineError

LIBRARY = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('transport_wrapper', LIBRARY / 'scripts/transport-artifacts.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class TransportWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.root.mkdir()
        self.output = Path(self.temp.name) / 'outputs'
        self.env = {'GITHUB_WORKSPACE': str(self.root), 'GITHUB_OUTPUT': str(self.output),
                    'GITHUB_REPOSITORY': 'owner/product', 'RUNNER_TEMP': self.temp.name,
                    'TRANSPORT_ROOT': '.', 'TRANSPORT_OPERATION': 'fetch', 'TRANSPORT_RUN_ID': '12',
                    'TRANSPORT_RUN_ATTEMPT': '2', 'TRANSPORT_NAME': 'build-run-12-attempt-2',
                    'TRANSPORT_WORKFLOW': '.github/workflows/product.yml',
                    'TRANSPORT_SOURCE': 'a' * 40, 'TRANSPORT_RUN_HEAD_SHA': 'b' * 40,
                    'TRANSPORT_IDENTITY': 'c' * 64, 'TRANSPORT_ALLOWED_BRANCHES': '["feature/example"]',
                    'TRANSPORT_ALLOWED_EVENTS': '["pull_request"]', 'TRANSPORT_PRODUCER_JOB': 'build / run'}
        self.result = {'status': 'passed', 'manifest': 'build/candidate.json',
                       'candidate_identity': 'c' * 64, 'source_sha': 'a' * 40}

    def test_fetch_passes_separate_run_head_and_candidate_source_then_exports_verified_selection(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(wrapper, 'execute', return_value=self.result) as execute:
            wrapper.main()
        request, root = execute.call_args.args
        self.assertEqual(root, self.root)
        self.assertEqual(request['expected_source_sha'], 'a' * 40)
        self.assertEqual(request['run_head_sha'], 'b' * 40)
        self.assertEqual(request['repository'], 'owner/product')
        self.assertEqual(request['run_attempt'], 2)
        self.assertEqual(request['producer_job'], 'build / run')
        self.assertEqual(request['allowed_branches'], ['feature/example'])
        self.assertEqual(request['allowed_events'], ['pull_request'])
        outputs = dict(line.split('=', 1) for line in self.output.read_text().splitlines())
        self.assertEqual(outputs, {'manifest': 'build/candidate.json', 'candidate-identity': 'c' * 64, 'source-sha': 'a' * 40})

    def test_missing_invalid_or_ambiguous_branches_stop_before_transport(self):
        for branches in ('[]', '{}', '["staging","staging"]', '["refs/heads/main"]', '["../main"]', '["main\\nforged=true"]', 'invalid'):
            with self.subTest(branches=branches), patch.dict(os.environ, {**self.env, 'TRANSPORT_ALLOWED_BRANCHES': branches}, clear=True), \
                    patch.object(wrapper, 'execute') as execute:
                with self.assertRaises(PipelineError):
                    wrapper.main()
                execute.assert_not_called()

    def test_untrusted_events_workspace_escape_and_nonpassing_receipts_cannot_export_outputs(self):
        for updates in ({'TRANSPORT_ALLOWED_EVENTS': '["pull_request_target"]'}, {'TRANSPORT_ROOT': '..'}):
            with patch.dict(os.environ, {**self.env, **updates}, clear=True), patch.object(wrapper, 'execute') as execute:
                with self.assertRaises(PipelineError):
                    wrapper.main()
                execute.assert_not_called()
        for result in ({**self.result, 'status': 'failed'}, {**self.result, 'manifest': 'build/file\nforged=value'}):
            with patch.dict(os.environ, self.env, clear=True), patch.object(wrapper, 'execute', return_value=result):
                with self.assertRaises(PipelineError):
                    wrapper.main()
        self.assertFalse(self.output.exists())

    def test_package_allocates_fresh_destination_outside_caller_and_uses_selected_manifest(self):
        manifest = self.root / 'candidate.json'
        manifest.write_text(json.dumps({'identity': 'c' * 64, 'source_sha': 'a' * 40}))
        def package(request, root):
            directory = Path(os.environ[request['output_dir_env']])
            self.assertFalse(directory.is_relative_to(root))
            self.assertFalse(directory.exists())
            self.assertEqual(request['manifest'], 'candidate.json')
            return {**self.result, 'directory': str(directory)}
        env = {**self.env, 'TRANSPORT_OPERATION': 'package', 'TRANSPORT_MANIFEST': 'candidate.json'}
        with patch.dict(os.environ, env, clear=True), patch.object(wrapper, 'execute', side_effect=package):
            wrapper.main()
        self.assertIn('manifest=candidate.json\n', self.output.read_text())


class RuntimeWorkflowContractTests(unittest.TestCase):
    def workflows(self):
        for path in sorted((LIBRARY / '.github/workflows').glob('v2-*.yml')):
            yield path.name, yaml.safe_load(path.read_text())

    def test_aws_branch_authentication_omits_environment_without_changing_deployment_steps(self):
        workflow = dict(self.workflows())['v2-aws.yml']
        contract = workflow['on']['workflow_call']
        option = contract['inputs']['use-github-environment']
        self.assertEqual(option['type'], 'boolean')
        self.assertIs(option['default'], True)
        self.assertTrue(contract['inputs']['environment']['required'])
        self.assertEqual(set(workflow['jobs']), {'run', 'branch'})
        run, branch = workflow['jobs']['run'], workflow['jobs']['branch']
        self.assertEqual(run['environment'], '${{ inputs.environment }}')
        self.assertNotIn('environment', branch)
        event_guard = "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
        self.assertEqual(run['if'], '${{ inputs.use-github-environment && ' + event_guard + ' }}')
        self.assertEqual(branch['if'], '${{ !inputs.use-github-environment && ' + event_guard + ' }}')
        # Both routes retain the same setup, trusted import, authentication, provider and artifact steps.
        common_run = {key: value for key, value in run.items() if key not in {'environment', 'if'}}
        common_branch = {key: value for key, value in branch.items() if key != 'if'}
        self.assertEqual(common_run, common_branch)
        execute = next(step for step in branch['steps'] if step.get('id') == 'execute')
        self.assertEqual(execute['env']['DEPLOYMENT_ENVIRONMENT'], '${{ inputs.environment }}')
        self.assertFalse(branch['concurrency']['cancel-in-progress'])
        self.assertEqual(set(contract['outputs']), set(run['outputs']))
        for name, output in contract['outputs'].items():
            self.assertEqual(output['value'], '${{ jobs.run.outputs.' + name + ' || jobs.branch.outputs.' + name + ' }}')

    def test_candidate_composition_authenticates_cross_job_checks_with_step_scoped_read_token(self):
        count = 0
        for name, workflow in self.workflows():
            for job in workflow['jobs'].values():
                for step in job.get('steps', []):
                    inputs = step.get('with', {})
                    if inputs.get('module') == 'evidence' and inputs.get('operation') == 'create':
                        count += 1
                        self.assertEqual(inputs['check-receipts'], '${{ inputs.check-receipts }}', name)
                        self.assertEqual(step['env']['GH_TOKEN'], '${{ secrets.ARTIFACT_READ_TOKEN || github.token }}', name)
                        self.assertEqual(workflow['permissions']['actions'], 'read', name)
                        self.assertEqual(inputs['candidate-build'], '${{ inputs.candidate-build }}', name)
                        self.assertEqual(workflow['on']['workflow_call']['inputs']['candidate-build']['default'], '', name)
        self.assertGreater(count, 10)

    def test_credentialed_imports_cannot_opt_into_pr_producers_and_authentication_follows_setup(self):
        protected = {'v2-ai-live.yml', 'v2-android-build.yml', 'v2-aws.yml', 'v2-container-delivery.yml',
                     'v2-device-tests.yml', 'v2-firebase.yml', 'v2-mobile-stores.yml', 'v2-mobile-verify.yml',
                     'v2-monitoring.yml', 'v2-release-publish.yml', 'v2-web-deploy.yml', 'v2-apple-import.yml'}
        for name, workflow in self.workflows():
            if name not in protected:
                continue
            with self.subTest(workflow=name):
                contract = workflow['on']['workflow_call']
                self.assertNotIn('artifact-allowed-events', contract['inputs'])
                job = workflow['jobs']['run']
                self.assertIn("github.event_name == 'push'", job['if'])
                self.assertIn("github.event_name == 'workflow_dispatch'", job['if'])
                steps = job['steps']
                setup = next(i for i, s in enumerate(steps) if s.get('uses') == '$/actions/v2-setup')
                for i, step in enumerate(steps):
                    uses = step.get('uses', '')
                    if uses.startswith(('aws-actions/configure-aws-credentials@', 'google-github-actions/auth@')):
                        self.assertGreater(i, setup)
                    if step.get('id') == 'import-candidate':
                        self.assertNotIn('allowed-events', step['with'])

    def test_provider_execution_uses_authenticated_import_outputs_and_retry_safe_artifact_names(self):
        providers = {'v2-aws.yml', 'v2-firebase.yml', 'v2-web-deploy.yml', 'v2-mobile-stores.yml', 'v2-monitoring.yml'}
        for name, workflow in self.workflows():
            if name not in providers:
                continue
            job = workflow['jobs']['run']
            steps = {step.get('id'): step for step in job['steps'] if step.get('id')}
            for input_name, output_name in [('selected-manifest', 'manifest'), ('selected-identity', 'candidate-identity'),
                                            ('selected-source-sha', 'source-sha')]:
                self.assertEqual(steps['execute']['with'][input_name], '${{ steps.import-candidate.outputs.' + output_name + ' }}', name)
            for retain in ('retain-evidence', 'retain-candidate'):
                value = steps[retain]['with']['name']
                self.assertIn('github.run_id', value, name)
                self.assertIn('github.run_attempt', value, name)
            self.assertIn('receipt-artifact-name', workflow['on']['workflow_call']['outputs'])
            self.assertIn('candidate-artifact-name', workflow['on']['workflow_call']['outputs'])
        stores = yaml.safe_load((LIBRARY / '.github/workflows/v2-mobile-stores.yml').read_text())
        run = stores['jobs']['run']
        execute = next(step for step in run['steps'] if step.get('id') == 'execute')
        self.assertEqual(execute['with']['state-restore'], '${{ inputs.state-restore }}')
        self.assertEqual(execute['with']['store-selection'], '${{ inputs.store-selection }}')
        action = yaml.safe_load((LIBRARY / 'actions/v2-run/action.yml').read_text())
        self.assertEqual(action['runs']['steps'][0]['env']['SHARED_STORE_SELECTION'], '${{ inputs.store-selection }}')
        self.assertEqual(action['outputs']['build-number']['value'], '${{ steps.execute.outputs.build-number }}')
        self.assertIn('GH_STATE_TOKEN', execute['env'])
        self.assertFalse(run['concurrency']['cancel-in-progress'])
        evidence = yaml.safe_load((LIBRARY / '.github/workflows/v2-evidence.yml').read_text())
        creation = next(step for step in evidence['jobs']['run']['steps'] if step.get('id') == 'execute')
        self.assertEqual(creation['with']['candidate-build'], '${{ inputs.candidate-build }}')
        monitoring = yaml.safe_load((LIBRARY / '.github/workflows/v2-monitoring.yml').read_text())
        execute = next(step for step in monitoring['jobs']['run']['steps'] if step.get('id') == 'execute')
        self.assertEqual(execute['with']['prior-deployment'], '${{ inputs.prior-deployment }}')
        self.assertEqual(execute['env']['GH_EVIDENCE_TOKEN'], '${{ secrets.ARTIFACT_READ_TOKEN || github.token }}')
