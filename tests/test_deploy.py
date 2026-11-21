import copy
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci import artifacts, assurance, deploy, evidence, quality
from shared_ci.common import PipelineError, atomic_json, fingerprint, run, sha256_file
from shared_ci.quality import input_digest, snapshot

ACCOUNT = '123456789012'
VERSION = '11111111-2222-3333-4444-555555555555'
SITE = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.write('source.txt', 'synthetic source'); self.write('dist/index.html', '<p>fixture</p>')
        self.write('functions/index.js', 'exports.fixture = function() {};')
        self.write('dist/worker.js', 'export default {};')
        self.write('firebase.json', json.dumps({'emulators': {'firestore': {'port': 8180}}, 'functions': {'source': 'functions'}, 'hosting': {'public': 'dist'}, 'firestore': {'rules': 'rules.txt', 'indexes': 'indexes.json'}}))
        self.write('wrangler.jsonc', json.dumps({'env': {'staging': {'name': 'fixture-worker'}}}))
        self.write('.vercel/project.json', json.dumps({'projectId': 'prj_fixture', 'orgId': 'team_fixture'}))
        self.write('.vercel/output/config.json', '{"version":3}')
        self.write('rules.txt', 'rules_version = "2";\n')
        self.write('indexes.json', '{"indexes": [], "fieldOverrides": []}')
        self.write('google.json', '{"type":"external_account"}')
        self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/staging', 'GITHUB_REPOSITORY': 'example/fixture', 'GITHUB_SHA': 'a'*40, 'DEPLOYMENT_ENVIRONMENT': 'staging', 'AWS_ACCESS_KEY_ID': 'fixture-id', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret', 'AWS_SESSION_TOKEN': 'fixture-session', 'GOOGLE_APPLICATION_CREDENTIALS': str(self.root/'google.json'), 'CLOUDFLARE_API_TOKEN': 'fixture-token', 'VERCEL_TOKEN': 'fixture-token', 'NETLIFY_AUTH_TOKEN': 'fixture-token'}
        self.patcher = patch.dict(os.environ, self.env, clear=True); self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.calls = []; self.change = {'Status': 'CREATE_COMPLETE', 'ChangeSetId': 'arn:aws:cloudformation:us-east-1:'+ACCOUNT+':changeSet/fixture/1', 'Changes': [{'ResourceChange': {'LogicalResourceId': 'Web', 'ResourceType': 'AWS::Lambda::Function', 'Action': 'Modify', 'Replacement': 'False'}}]}
        self.runtime_version = 1; self.stack_status = 'UPDATE_COMPLETE'; self.template_resources = {}
        self.api_values = {}; self.active_worker = VERSION
        self.stack_properties = {'stackName': 'Fixture', 'templateFile': 'Fixture.template.json'}
    def write(self, path, content):
        file = self.root / path; file.parent.mkdir(parents=True, exist_ok=True); file.write_text(content); return file
    def config(self, provider, settings, operation='deploy'):
        inputs = ['functions', 'source.txt', 'dist', 'firebase.json', 'wrangler.jsonc', '.vercel', 'rules.txt', 'indexes.json']
        return {'schema': 1, 'provider': provider, 'operation': operation, 'environment': 'staging', 'allowed_refs': ['refs/heads/staging'], 'inputs': inputs, 'expected_input_digest': input_digest(snapshot(self.root, inputs)), 'tools': {name: {'command': [name], 'version': '1.2.3'} for name in ('aws','cdk','firebase','gcloud','wrangler','vercel','netlify')}, 'settings': settings, 'receipt': 'receipt.json'}
    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs)); name, args = command[0], command[1:]
        value = None; text = ''
        if args == ['--version']: text = name + ' 1.2.3'
        elif name == 'aws':
            if args[:2] == ['sts', 'get-caller-identity']: value = {'Account': ACCOUNT, 'Arn': 'arn:aws:sts::'+ACCOUNT+':assumed-role/fixture/gha'}
            elif args[:2] == ['ssm', 'describe-parameters']: value = {'Parameters': [{'Name': '/runtime/model', 'Version': self.runtime_version}]}
            elif args[:2] == ['cloudformation', 'describe-change-set']: value = self.change
            elif args[:2] == ['cloudformation', 'describe-stacks']: value = {'Stacks': [{'StackId': 'arn:aws:cloudformation:us-east-1:'+ACCOUNT+':stack/Fixture/1', 'StackStatus': self.stack_status}]}
            elif args[:2] == ['cloudfront', 'get-distribution']: value = {'Distribution': {'ARN': 'arn:aws:cloudfront::'+ACCOUNT+':distribution/EXAMPLE', 'DistributionConfig': {'Origins': {'Items': [{'DomainName': 'synthetic-site.s3.us-east-1.amazonaws.com'}]}}}}
            elif args[:2] == ['cloudfront', 'create-invalidation']: value = {'Invalidation': {'Id': 'INV123'}}
            elif args[:2] in (['s3api', 'head-bucket'], ['s3', 'sync'], ['cloudfront', 'wait']): pass
            else: self.fail('unexpected AWS command: ' + str(args))
        elif name == 'cdk':
            if args[0] == 'synth':
                directory = Path(args[args.index('--output') + 1]); directory.mkdir()
                (directory / 'manifest.json').write_text(json.dumps({'artifacts': {'Fixture': {'environment': 'aws://'+ACCOUNT+'/us-east-1', 'properties': self.stack_properties}}}))
                (directory / 'Fixture.template.json').write_text(json.dumps({'Resources': self.template_resources}))
            elif args[0] not in ('diff','deploy'): self.fail('unexpected CDK command')
        elif name == 'firebase':
            if args[0] == 'deploy': value = {'status': 'success', 'result': {}}
            elif args[0] == 'emulators:exec': self.write('test.xml', '<testsuite><testcase name="rules deny unauthorized write"/></testsuite>')
            elif args[0] == 'remoteconfig:get': value = {'configuration': 'sensitive fixture'}
            else: self.fail('unexpected Firebase command')
        elif name == 'gcloud':
            if args[:3] == ['auth', 'application-default', 'print-access-token']: text = 'synthetic-derived-token'
            elif args[:2] == ['functions', 'describe']: value = {'state': 'ACTIVE', 'serviceConfig': {'revision': 'fixture-00001-ab'}}
            elif args[:4] == ['firestore', 'indexes', 'composite', 'list']: value = self.api_values.get('indexes', [])
            elif args[:4] == ['firestore', 'indexes', 'fields', 'describe']: value = self.api_values['field']
            else: self.fail('unexpected gcloud command: ' + str(args))
        elif name == 'wrangler':
            if args[0] == 'deploy': text = 'Current Version ID: ' + VERSION
            elif args[:2] == ['versions', 'deploy']: pass
            elif args[:2] == ['deployments', 'list']: value = [{'id': 'deployment-fixture', 'created_on': '2026-01-01', 'versions': [{'version_id': self.active_worker, 'percentage': 100}]}]
            else: self.fail('unexpected Wrangler command')
        elif name == 'vercel':
            if args[0] == 'deploy': text = 'https://fixture.vercel.app\n'
            elif args[0] != 'promote': self.fail('unexpected Vercel command')
        elif name == 'netlify':
            if args[0] == 'deploy': value = {'site_id': SITE, 'deploy_id': 'fixture-deploy', 'deploy_url': 'https://fixture.netlify.app'}
            elif args[0] != 'build': self.fail('unexpected Netlify command')
        else: self.fail('unexpected executable')
        return subprocess.CompletedProcess(command, 0, json.dumps(value) if value is not None else text, '')
    def run_config(self, config):
        with patch('shared_ci.deploy.run', side_effect=self.fake_run): return deploy.execute(config, self.root)
    def cdk(self):
        return self.config('aws-cdk', {'account': ACCOUNT, 'region': 'us-east-1', 'stack': 'Fixture', 'externally_managed_ssm_parameters': ['/runtime/model']})
    def test_cdk_inspects_real_change_set_before_execution_and_preserves_runtime(self):
        result = self.run_config(self.cdk()); self.assertEqual(result['status'], 'passed'); self.assertEqual(result['details']['runtime_parameters_preserved'], 1)
        commands = [c for c, _ in self.calls]
        inspect = next(i for i,c in enumerate(commands) if c[1:3] == ['cloudformation','describe-change-set'])
        execute = next(i for i,c in enumerate(commands) if '--method=execute-change-set' in c)
        self.assertLess(inspect, execute)
        for command, kwargs in self.calls:
            if command[0] == 'aws' and command[1] != '--version': self.assertEqual(kwargs['env_names'], deploy.AWS_ENV)
            self.assertIn('HOME', kwargs['extra_env'])
    def test_cdk_omitted_physical_stack_name_defaults_to_artifact_id(self):
        del self.stack_properties['stackName']
        result = self.run_config(self.cdk())
        self.assertEqual(result['status'], 'passed')
        stack_calls = [c for c, _ in self.calls if c[0] == 'aws' and '--stack-name' in c]
        self.assertTrue(stack_calls)
        self.assertTrue(all(c[c.index('--stack-name') + 1] == 'Fixture' for c in stack_calls))

    def test_cdk_explicit_physical_stack_name_is_preserved(self):
        self.stack_properties['stackName'] = 'ProductionBackend'
        config = self.cdk(); config['operation'] = 'validate'
        self.assertEqual(self.run_config(config)['details']['stack'], 'ProductionBackend')
        self.calls.clear()
        self.run_config(self.cdk())
        stack_calls = [c for c, _ in self.calls if c[0] == 'aws' and '--stack-name' in c]
        self.assertTrue(stack_calls)
        self.assertTrue(all(c[c.index('--stack-name') + 1] == 'ProductionBackend' for c in stack_calls))

    def test_cdk_invalid_explicit_stack_name_does_not_fall_back(self):
        for name in (None, '', 'invalid/name'):
            self.stack_properties['stackName'] = name; self.calls.clear()
            with self.subTest(name=name), self.assertRaisesRegex(PipelineError, 'CloudFormation stack name'):
                self.run_config(self.cdk())
            self.assertFalse(any('--method=prepare-change-set' in c for c, _ in self.calls))

    def test_protected_replacement_nested_and_unknown_changes_fail_before_execution(self):
        for kind, action, replacement in [('AWS::DynamoDB::Table','Modify','Conditional'), ('AWS::S3::Bucket','Remove','False'), ('AWS::CloudFormation::Stack','Modify','False'), ('AWS::Lambda::Function','Dynamic','False')]:
            self.change['Changes'][0]['ResourceChange'].update(ResourceType=kind, Action=action, Replacement=replacement); self.calls.clear()
            with self.subTest(kind=kind), self.assertRaises(PipelineError): self.run_config(self.cdk())
            self.assertFalse(any('--method=execute-change-set' in command for command,_ in self.calls))
    def test_runtime_parameter_cannot_become_stack_owned(self):
        self.template_resources = {'Model': {'Type': 'AWS::SSM::Parameter', 'Properties': {'Name': '/runtime/model'}}}
        with self.assertRaisesRegex(PipelineError, 'runtime override'): self.run_config(self.cdk())
        self.assertFalse(any('--method=prepare-change-set' in c for c,_ in self.calls))
    def test_cdk_validate_noop_rollback_and_paginated_failures(self):
        config = self.cdk(); config['operation'] = 'validate'; self.assertEqual(self.run_config(config)['details']['operation'], 'synth_and_diff')
        self.change = {'Status': 'FAILED', 'StatusReason': "The submitted information didn't contain changes.", 'Changes': []}
        self.assertTrue(self.run_config(self.cdk())['details']['no_changes'])
        self.stack_status = 'UPDATE_ROLLBACK_COMPLETE'
        with self.assertRaises(PipelineError): self.run_config(self.cdk())
        with self.assertRaises(PipelineError): deploy.inspect_change_set({'Status':'CREATE_COMPLETE','NextToken':'more','Changes':[]})
    def test_untrusted_environment_hash_credentials_and_version_fail(self):
        for variable,value in [('GITHUB_EVENT_NAME','pull_request'), ('DEPLOYMENT_ENVIRONMENT','production'), ('AWS_SESSION_TOKEN','')]:
            with patch.dict(os.environ, {variable:value}), self.subTest(variable=variable), self.assertRaises(PipelineError): self.run_config(self.cdk())
        config = self.cdk(); config['expected_input_digest'] = '0'*64
        with self.assertRaisesRegex(PipelineError,'candidate'): self.run_config(config)
        config = self.cdk(); config['tools']['aws']['version'] = '9.9.9'
        with self.assertRaisesRegex(PipelineError,'pinned'): self.run_config(config)
        config = self.cdk(); config['settings']['account'] = '999999999999'
        with self.assertRaisesRegex(PipelineError,'identity'): self.run_config(config)
    def test_static_prebuilt_sync_and_invalidation_wait(self):
        config = self.config('aws-static', {'account':ACCOUNT,'region':'us-east-1','bucket':'synthetic-site','directory':'dist','distribution_id':'EXAMPLE'})
        result = self.run_config(config)
        self.assertEqual(result['details']['build_mode'],'prebuilt')
        sync = next(c for c,_ in self.calls if c[1:3] == ['s3','sync']); self.assertNotIn('--delete',sync); self.assertIn('--no-follow-symlinks',sync)
        self.assertTrue(any(c[1:4] == ['cloudfront','wait','invalidation-completed'] for c,_ in self.calls))
        config['inputs'].remove('dist'); config['expected_input_digest']=input_digest(snapshot(self.root,config['inputs']))
        with self.assertRaisesRegex(PipelineError,'outside'): self.run_config(config)
    def test_emulator_exec_requires_fresh_executed_tests_and_demo_project(self):
        config=self.config('firebase',{'project':'demo-fixture','config':'firebase.json','emulators':['firestore'],'test_command':['node','rules.test.js'],'test_report':{'path':'test.xml','format':'junit'}}, 'emulate');config['environment']='test'
        result=self.run_config(config);self.assertEqual(result['details']['test_summary']['executed'],1)
        emulator=next(c for c,_ in self.calls if 'emulators:exec' in c);self.assertEqual(emulator[-1],'node rules.test.js')
        config['settings']['project']='real-project'
        with self.assertRaisesRegex(PipelineError,'demo-'): self.run_config(config)
    def test_function_rebuild_exception_and_revision(self):
        config=self.config('firebase',{'project':'fixture-project','config':'firebase.json','components':['functions'],'functions':[{'name':'fixture','region':'us-central1'}]})
        with self.assertRaisesRegex(PipelineError,'rebuild'):self.run_config(config)
        config['settings']['allow_provider_rebuild']=True
        result=self.run_config(config);self.assertEqual(result['details']['identities']['function:fixture'],'fixture-00001-ab');self.assertEqual(result['details']['build_mode'],'same_source_provider_rebuild')
    def test_hosting_uses_live_channel_and_rules_match_actual_bytes(self):
        config=self.config('firebase',{'project':'fixture-project','config':'firebase.json','components':['hosting','firestore:rules'],'hosting_sites':['fixture-site'],'rule_files':{'cloud.firestore':'rules.txt'}})
        urls=[]
        def get(url,token):
            urls.append(url)
            if 'firebasehosting' in url:return {'releases':[{'name':'sites/fixture-site/releases/abc','version':{'status':'FINALIZED'}}]}
            if '/releases/' in url:return {'rulesetName':'projects/fixture-project/rulesets/abc'}
            return {'source':{'files':[{'content':(self.root/'rules.txt').read_text()}]}}
        with patch('shared_ci.deploy._get_json',side_effect=get): result=self.run_config(config)
        self.assertIn('/channels/live/releases?',urls[0]);self.assertIn('rules:cloud.firestore',result['details']['identities'])
        with patch('shared_ci.deploy._get_json',return_value={'releases':[]} ),self.assertRaises(PipelineError):self.run_config(config)
    def test_firestore_indexes_and_field_override_verify_readiness(self):
        self.write('indexes.json',json.dumps({'indexes':[{'collectionGroup':'Events','queryScope':'COLLECTION','fields':[{'fieldPath':'time','order':'ASCENDING'}]}],'fieldOverrides':[{'collectionGroup':'Events','fieldPath':'private','indexes':[]}]}))
        name='projects/fixture-project/databases/(default)/collectionGroups/Events/'
        self.api_values['indexes']=[{'name':name+'indexes/abc','queryScope':'COLLECTION','fields':[{'fieldPath':'time','order':'ASCENDING'},{'fieldPath':'__name__','order':'ASCENDING'}],'state':'READY'}]
        self.api_values['field']={'name':name+'fields/private','indexConfig':{'indexes':[]}}
        config=self.config('firebase',{'project':'fixture-project','config':'firebase.json','components':['firestore:indexes'],'indexes_file':'indexes.json'})
        self.assertTrue(self.run_config(config)['details']['identities']['indexes'])
        self.api_values['indexes'][0]['state']='CREATING'
        with self.assertRaisesRegex(PipelineError,'not ready'):self.run_config(config)
    def test_worker_prebuilt_identity_and_promotion(self):
        config=self.config('cloudflare',{'account_id':'a'*32,'worker':'fixture-worker','config':'wrangler.jsonc','wrangler_environment':'staging','entrypoint':'dist/worker.js','artifact_directory':'dist'})
        result=self.run_config(config);self.assertEqual(result['details']['version_id'],VERSION)
        self.assertTrue(any('--no-bundle' in c for c,_ in self.calls))
        config['operation']='promote';config['settings']['version_id']=VERSION
        self.run_config(config);self.assertTrue(any(VERSION+'@100%' in c for c,_ in self.calls))
        self.active_worker='f'*36
        with self.assertRaisesRegex(PipelineError,'differs'):self.run_config(config)
    def test_vercel_prebuilt_and_source_and_rest_identity(self):
        config=self.config('vercel',{'project_id':'prj_fixture','organization_id':'team_fixture','target':'preview','build_mode':'prebuilt'})
        observed={'projectId':'prj_fixture','readyState':'READY','id':'dpl_fixture'}
        with patch('shared_ci.deploy._get_json',return_value=observed) as get: result=self.run_config(config)
        self.assertEqual(result['details']['deployment_id'],'dpl_fixture');self.assertIn('/v13/deployments/fixture.vercel.app?teamId=team_fixture',get.call_args.args[0])
        self.assertTrue(any('--prebuilt' in c and '--archive=tgz' in c for c,_ in self.calls))
        config['settings']['build_mode']='source'
        with self.assertRaisesRegex(PipelineError,'rebuild'):self.run_config(config)
        config['settings']['allow_rebuild']=True
        with patch('shared_ci.deploy._get_json',return_value={**observed,'projectId':'prj_wrong'}),self.assertRaises(PipelineError):self.run_config(config)
    def test_netlify_prebuilt_no_build_and_explicit_source_build(self):
        config=self.config('netlify',{'site_id':SITE,'directory':'dist','context':'deploy-preview','production':False,'build_mode':'prebuilt'})
        self.assertEqual(self.run_config(config)['details']['build_mode'],'prebuilt')
        self.assertTrue(any('--no-build' in c for c,_ in self.calls));self.assertFalse(any(c[1]=='build' for c,_ in self.calls))
        config['settings'].update(build_mode='source',allow_rebuild=True)
        self.assertEqual(self.run_config(config)['details']['build_mode'],'local_rebuild')
        self.assertTrue(any(c[1]=='build' for c,_ in self.calls))
    def test_snapshot_retains_hash_only_and_checks_declared_baseline(self):
        config=self.config('firebase',{'project':'fixture-project','kind':'remote-config'},'snapshot')
        result=self.run_config(config);self.assertNotIn('sensitive fixture',json.dumps(result))
        self.write('baseline.json',json.dumps(result));config['settings']['baseline']='baseline.json';self.run_config(config)
        self.write('baseline.json','{"details":{"configuration_sha256":"wrong"}}')
        with self.assertRaisesRegex(PipelineError,'baseline'):self.run_config(config)
    def test_cli_failure_malformed_response_and_input_mutation_fail(self):
        config=self.cdk()
        with patch('shared_ci.deploy.run',side_effect=PipelineError('provider exited nonzero')),self.assertRaises(PipelineError):deploy.execute(config,self.root)
        with self.assertRaises(PipelineError):deploy._json('not json')
        def mutate(command, **kwargs):
            result=self.fake_run(command,**kwargs)
            if '--method=execute-change-set' in command:self.write('source.txt','changed during deployment')
            return result
        with patch('shared_ci.deploy.run',side_effect=mutate),self.assertRaisesRegex(PipelineError,'changed the selected'):deploy.execute(config,self.root)
    def test_runtime_override_change_after_execute_blocks_release(self):
        def mutate(command, **kwargs):
            result=self.fake_run(command,**kwargs)
            if '--method=execute-change-set' in command:self.runtime_version=2
            return result
        with patch('shared_ci.deploy.run',side_effect=mutate),self.assertRaisesRegex(PipelineError,'runtime override'):deploy.execute(self.cdk(),self.root)
    def test_old_emulator_report_cannot_pass_without_new_execution(self):
        self.write('test.xml','<testsuite><testcase name="old"/></testsuite>')
        config=self.config('firebase',{'project':'demo-fixture','config':'firebase.json','emulators':['firestore'],'test_command':['node','rules.test.js'],'test_report':{'path':'test.xml','format':'junit'}},'emulate');config['environment']='test'
        def no_report(command,**kwargs):
            if 'emulators:exec' in command:return subprocess.CompletedProcess(command,0,'','')
            return self.fake_run(command,**kwargs)
        with patch('shared_ci.deploy.run',side_effect=no_report),self.assertRaisesRegex(PipelineError,'absent or empty'):deploy.execute(config,self.root)
    def test_jsonc_and_unknown_contract_rejected(self):
        path=self.write('test.jsonc','{/* comment */ "url":"https://example.invalid/a", "a": [1,],}')
        self.assertEqual(deploy._jsonc(path)['a'],[1])
        config=self.cdk();config['typo']=True
        with self.assertRaises(PipelineError):self.run_config(config)


class RetainedDeploymentTests(unittest.TestCase):
    """Real local build/evidence/transport, with only the external provider mocked."""

    fake_run = DeploymentTests.fake_run
    write = DeploymentTests.write

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        self.calls = []
        self.write('source.txt', 'A synthetic & tested site')
        self.write('build.py', "from pathlib import Path\nimport html\nPath('dist').mkdir(exist_ok=True)\nPath('dist/index.html').write_text('<p>' + html.escape(Path('source.txt').read_text()) + '</p>')\n")
        self.write('test_build.py', "from pathlib import Path\nassert Path('dist/index.html').read_text() == '<p>A synthetic &amp; tested site</p>'\nPath('test.xml').write_text('<testsuite><testcase name=\"render escapes source text\"/></testsuite>')\n")
        self.write('wrangler.jsonc', json.dumps({'env': {'staging': {'name': 'fixture-worker'}}}))
        self.write('.vercel/project.json', json.dumps({'projectId': 'prj_fixture', 'orgId': 'team_fixture'}))
        bash = subprocess.run(['bash', '--version'], check=True, capture_output=True, text=True).stdout
        bash_version = re.search(r'version ([0-9]+(?:\.[0-9]+)+)', bash).group(1)
        self.quality_config = {'version': 1, 'profile': 'shell', 'toolchain': [
            {'command': ['bash', '--version'], 'version': bash_version},
            {'command': [sys.executable, '--version'], 'version': platform.python_version()}],
            'inputs': ['source.txt', 'build.py', 'test_build.py', 'quality.json'],
            'checks': [{'name': 'build', 'kind': 'build', 'command': [sys.executable, 'build.py']},
                       {'name': 'test', 'kind': 'test', 'command': [sys.executable, 'test_build.py'],
                        'test_report': {'path': 'test.xml', 'format': 'junit'}}],
            'required_checks': ['build', 'test']}
        self.config = {'schema': 1, 'provider': 'aws-static', 'operation': 'deploy', 'environment': 'staging',
                       'allowed_refs': ['refs/heads/staging'], 'inputs': ['dist'],
                       'tools': {'aws': {'command': ['aws'], 'version': '1.2.3'}},
                       'settings': {'account': ACCOUNT, 'region': 'us-east-1', 'bucket': 'synthetic-site',
                                    'directory': 'dist', 'distribution_id': 'EXAMPLE'}, 'receipt': 'deployment.json'}
        atomic_json(self.root / 'quality.json', self.quality_config)
        atomic_json(self.root / 'deploy.json', self.config)
        self.git(self.root, 'init', '-q')
        self.git(self.root, 'add', '.')
        self.git(self.root, '-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.sha = self.git(self.root, 'rev-parse', 'HEAD')
        self.tree = self.git(self.root, 'rev-parse', 'HEAD^{tree}')
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/staging',
               'GITHUB_REPOSITORY': 'example/fixture', 'GITHUB_SHA': self.sha, 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
               'DEPLOYMENT_ENVIRONMENT': 'staging', 'AWS_ACCESS_KEY_ID': 'fixture-id', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret',
               'AWS_SESSION_TOKEN': 'fixture-session', 'ARTIFACT_TOKEN': 'synthetic-token',
               'TEST_BUNDLE_OUTPUT': str(self.base / 'bundle')}
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        checked = quality.execute(self.quality_config, self.root)
        atomic_json(self.root / 'quality-receipt.json', assurance.stamp(checked, 'quality', self.root / 'quality.json', self.root))
        self.candidate = evidence.create({'schema': 1, 'product': 'synthetic-site', 'version': '1.0.0', 'build': '42',
                                         'library_sha': 'b' * 40, 'toolchain': {'python': platform.python_version()},
                                         'inputs': ['source.txt', 'build.py', 'test_build.py', 'quality.json', 'deploy.json', 'wrangler.jsonc', '.vercel/project.json'],
                                         'artifacts': ['dist/index.html'],
                                         'checks': [{'path': 'quality-receipt.json', 'kind': 'quality', 'config': 'quality.json'}]}, self.root)
        atomic_json(self.root / 'candidate.json', self.candidate)
        self.runtime = {'candidate': 'candidate.json', 'expected_identity': self.candidate['identity'],
                        'expected_source_sha': self.sha, 'require_candidate': True}

    def git(self, root, *args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

    def command(self, command, **kwargs):
        if command[0] == 'git':
            return run(command, **kwargs)
        return self.fake_run(command, **kwargs)

    def execute(self, **runtime):
        with patch('shared_ci.deploy.run', side_effect=self.command):
            return deploy.execute(self.config, self.root, **(runtime or self.runtime))

    def import_built_candidate(self):
        artifacts.package({'schema': 1, 'operation': 'package', 'manifest': 'candidate.json',
                           'expected_identity': self.candidate['identity'], 'expected_source_sha': self.sha,
                           'output_dir_env': 'TEST_BUNDLE_OUTPUT'}, self.root)
        archive = self.base / 'candidate.zip'
        with zipfile.ZipFile(archive, 'w') as target:
            for path in sorted((self.base / 'bundle').rglob('*')):
                if path.is_file():
                    target.write(path, path.relative_to(self.base / 'bundle').as_posix())
        consumer = self.base / 'consumer'
        self.git(self.base, 'clone', '--quiet', '--no-hardlinks', str(self.root), str(consumer))
        metadata = {'status': 'completed', 'conclusion': 'success', 'head_sha': self.sha,
                    'head_branch': 'staging', 'path': '.github/workflows/build.yml',
                    'head_repository': {'full_name': 'example/fixture'}, 'run_attempt': 1, 'event': 'push',
                    'run_started_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T01:00:00Z'}
        item = {'id': 99, 'name': 'candidate', 'expired': False, 'created_at': '2026-01-01T00:30:00Z',
                'workflow_run': {'id': 42, 'head_sha': self.sha}, 'digest': 'sha256:' + sha256_file(archive),
                'size_in_bytes': archive.stat().st_size}
        def request(path, token):
            self.assertEqual(token, 'synthetic-token')
            return {'artifacts': [item]} if '/artifacts?' in path else metadata
        def download(path, token, destination, max_bytes):
            self.assertEqual(path, '/repos/example/fixture/actions/artifacts/99/zip')
            shutil.copyfile(archive, destination)
        config = {'schema': 1, 'operation': 'fetch', 'repository': 'example/fixture', 'run_id': '42', 'run_attempt': 1,
                  'artifact_name': 'candidate', 'expected_workflow': '.github/workflows/build.yml',
                  'expected_source_sha': self.sha, 'expected_identity': self.candidate['identity'],
                  'token_env': 'ARTIFACT_TOKEN', 'allowed_events': ['push'], 'allowed_branches': ['staging']}
        receipt = artifacts.fetch(config, consumer, request=request, downloader=download)
        self.root = consumer
        return receipt

    def test_build_candidate_authenticated_import_and_deploy_without_committed_digest(self):
        self.assertNotIn('expected_input_digest', self.config)
        self.assertFalse('dist/' in self.git(self.root, 'ls-files'))
        imported = self.import_built_candidate()
        result = self.execute()
        self.assertEqual(result['candidate_identity'], imported['candidate_identity'])
        self.assertEqual(result['manifest'], imported['manifest'])
        self.assertEqual(result['source_sha'], self.sha)
        self.assertEqual(result['input_authentication'], 'retained_candidate')
        self.assertEqual(result['input_digest'], input_digest({'dist/index.html': self.candidate['artifacts']['dist/index.html']['sha256']}))
        self.assertTrue(any(command[1:3] == ['s3', 'sync'] for command, _ in self.calls))

    def test_unretained_deploy_file_fails_before_provider(self):
        self.import_built_candidate()
        self.write('dist/unreviewed.html', 'not in the candidate')
        with self.assertRaisesRegex(PipelineError, 'absent from'):
            self.execute()
        self.assertFalse(self.calls)

    def test_missing_or_changed_retained_file_fails_before_provider(self):
        path = self.root / 'dist/index.html'
        original = path.read_bytes()
        for content in (None, b'changed after inspection'):
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(PipelineError):
                self.execute()
            self.assertFalse(self.calls)
            path.write_bytes(original)

    def test_required_manifest_and_runtime_selection_fail_before_provider(self):
        for change in ({'candidate': None}, {'candidate': 'absent.json'}, {'expected_identity': 'f' * 64},
                       {'expected_source_sha': 'f' * 40}, {'expected_identity': None}):
            with self.subTest(change=change), self.assertRaises(PipelineError):
                self.execute(**dict(self.runtime, **change))
            self.assertFalse(self.calls)

    def test_new_source_tree_or_dirty_tracked_source_refuses(self):
        # Even source files outside the selected upload directory remain tree-bound.
        self.write('extra-source.txt', 'different tree')
        self.git(self.root, 'add', 'extra-source.txt')
        with self.assertRaisesRegex(PipelineError, 'matching clean source tree'):
            self.execute()
        self.git(self.root, '-c', 'user.name=Synthetic Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'different source')
        with self.assertRaisesRegex(PipelineError, 'matching clean source tree'):
            self.execute()
        self.assertFalse(self.calls)

    def test_receipt_cannot_replace_any_candidate_evidence(self):
        for output in ('candidate.json', 'quality-receipt.json', 'source.txt', 'dist/index.html'):
            self.config['receipt'] = output
            with self.subTest(output=output), self.assertRaisesRegex(PipelineError, 'overwrite'):
                self.execute()
        self.assertFalse(self.calls)

    def test_mutation_of_unselected_candidate_file_is_detected(self):
        original = self.command
        def mutate(command, **kwargs):
            value = original(command, **kwargs)
            if command[1:3] == ['s3', 'sync']:
                self.write('source.txt', 'mutated during provider call')
            return value
        with patch('shared_ci.deploy.run', side_effect=mutate), self.assertRaises(PipelineError):
            deploy.execute(self.config, self.root, **self.runtime)

    def test_provider_cannot_mutate_the_retained_manifest(self):
        original = self.command
        def mutate(command, **kwargs):
            value = original(command, **kwargs)
            if command[1:3] == ['s3', 'sync']:
                self.write('candidate.json', '{"status":"passed"}')
            return value
        with patch('shared_ci.deploy.run', side_effect=mutate), self.assertRaisesRegex(PipelineError, 'changed the retained deployment manifest'):
            deploy.execute(self.config, self.root, **self.runtime)

    def test_current_security_gate_invoked_before_any_provider(self):
        with patch('shared_ci.deploy.verify_checks', side_effect=PipelineError('current scan required')), self.assertRaisesRegex(PipelineError, 'current scan'):
            self.execute()
        self.assertFalse(self.calls)

    def test_legacy_declared_digest_remains_explicit_and_cannot_bypass_required_candidate(self):
        self.config['expected_input_digest'] = input_digest(snapshot(self.root, self.config['inputs']))
        result = deploy.execute
        with patch('shared_ci.deploy.run', side_effect=self.command):
            self.assertEqual(result(self.config, self.root)['input_authentication'], 'declared_digest')
            self.calls.clear()
            with self.assertRaisesRegex(PipelineError, 'requires a retained manifest'):
                result(self.config, self.root, require_candidate=True)
            self.assertFalse(self.calls)

    def prior_deployment(self, provider='cloudflare'):
        return {'schema': 1, 'kind': 'deployment', 'status': 'passed', 'provider': provider, 'operation': 'deploy',
                'candidate_identity': self.candidate['identity'], 'source_sha': self.sha, 'source_tree': self.tree,
                'input_authentication': 'retained_candidate', 'environment': 'staging',
                'details': ({'account_id': 'a' * 32, 'worker': 'fixture-worker', 'version_id': VERSION,
                             'deployment_id': 'deployment-fixture', 'build_mode': 'prebuilt'} if provider == 'cloudflare' else
                            {'organization_id': 'team_fixture', 'project_id': 'prj_fixture', 'deployment_id': 'dpl_fixture',
                             'url': 'https://fixture.vercel.app', 'target': 'preview', 'build_mode': 'prebuilt'})}

    def authenticate_receipt(self, receipt, *, event='push', branch='staging'):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('receipt.json', json.dumps(receipt))
        content = content.getvalue()
        def request(path, token, binary=False):
            if binary:
                return content
            if '/artifacts?' in path:
                return {'artifacts': [{'id': 101, 'name': 'deployment', 'expired': False,
                                       'digest': 'sha256:' + hashlib.sha256(content).hexdigest()}]}
            return {'status': 'completed', 'conclusion': 'success', 'head_sha': self.sha, 'head_branch': branch,
                    'path': '.github/workflows/deploy.yml', 'event': event, 'run_attempt': 1,
                    'head_repository': {'full_name': 'example/fixture'}}
        return evidence.fetch_receipt('example/fixture', '42', 'deployment', self.sha,
                                      '.github/workflows/deploy.yml', 'synthetic-token', request=request)

    def select_promotion(self, provider='cloudflare'):
        self.config.update(provider=provider, operation='promote', promotion_source_environment='staging',
                           promotion_source_refs=['refs/heads/staging'], environment='staging', allowed_refs=['refs/heads/staging'])
        os.environ.update(DEPLOYMENT_ENVIRONMENT='staging', GITHUB_REF='refs/heads/staging')
        if provider == 'cloudflare':
            self.config.update(inputs=['wrangler.jsonc'], tools={'wrangler': {'command': ['wrangler'], 'version': '1.2.3'}},
                               settings={'account_id': 'a' * 32, 'worker': 'fixture-worker', 'config': 'wrangler.jsonc', 'wrangler_environment': 'staging'})
            os.environ['CLOUDFLARE_API_TOKEN'] = 'synthetic-token'
            self.active_worker = VERSION
        else:
            self.config.update(environment='production', inputs=['.vercel/project.json'], tools={'vercel': {'command': ['vercel'], 'version': '1.2.3'}},
                               allowed_refs=['refs/heads/main'],
                               settings={'project_id': 'prj_fixture', 'organization_id': 'team_fixture', 'target': 'production', 'build_mode': 'prebuilt'})
            os.environ.update(VERCEL_TOKEN='synthetic-token', DEPLOYMENT_ENVIRONMENT='production', GITHUB_REF='refs/heads/main')

    def test_worker_promotion_uses_authenticated_runtime_version(self):
        self.select_promotion()
        proof = self.authenticate_receipt(self.prior_deployment())
        result = self.execute(**dict(self.runtime, promotion_receipt=proof))
        self.assertEqual(result['details']['version_id'], VERSION)
        self.assertEqual(result['promotion']['provenance']['run_id'], '42')
        self.assertNotIn('version_id', self.config['settings'])
        self.assertTrue(any(VERSION + '@100%' in command for command, _ in self.calls))

    def test_vercel_staging_to_production_uses_authenticated_deployment(self):
        self.select_promotion('vercel')
        proof = self.authenticate_receipt(self.prior_deployment('vercel'))
        observed = {'projectId': 'prj_fixture', 'readyState': 'READY', 'id': 'dpl_fixture'}
        with patch('shared_ci.deploy._get_json', return_value=observed):
            result = self.execute(**dict(self.runtime, promotion_receipt=proof))
        self.assertEqual(result['environment'], 'production')
        self.assertEqual(result['promotion']['source_environment'], 'staging')
        self.assertEqual(result['details']['deployment_id'], 'dpl_fixture')
        self.assertNotIn('deployment_url', self.config['settings'])
        self.assertTrue(any(command[1:3] == ['promote', 'https://fixture.vercel.app'] for command, _ in self.calls))

    def test_promotion_rejects_raw_missing_mutated_wrong_provider_environment_candidate(self):
        self.select_promotion()
        for proof in (None, {'receipt': self.prior_deployment(), 'provenance': {}}):
            with self.subTest(proof=proof), self.assertRaises(PipelineError):
                self.execute(**dict(self.runtime, promotion_receipt=proof))
            self.assertFalse(self.calls)
        for change in ({'provider': 'vercel'}, {'environment': 'production'}, {'candidate_identity': 'f' * 64},
                       {'source_sha': 'f' * 40}, {'source_tree': 'f' * 40}, {'schema': 2},
                       {'input_authentication': 'declared_digest'}):
            proof = self.authenticate_receipt(dict(self.prior_deployment(), **change))
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, 'differs from selected'):
                self.execute(**dict(self.runtime, promotion_receipt=proof))
            self.assertFalse(self.calls)
        proof = self.authenticate_receipt(self.prior_deployment())
        proof['receipt']['details']['version_id'] = 'ffffffff-1111-2222-3333-444444444444'
        with self.assertRaises(PipelineError):
            self.execute(**dict(self.runtime, promotion_receipt=proof))
        self.assertFalse(self.calls)

    def test_wrong_provider_account_or_project_blocks_promotion(self):
        for provider, field, value in [('cloudflare', 'account_id', 'f' * 32), ('cloudflare', 'worker', 'another-worker'),
                                       ('vercel', 'project_id', 'prj_other'), ('vercel', 'organization_id', 'team_other')]:
            self.select_promotion(provider)
            receipt = self.prior_deployment(provider)
            receipt['details'][field] = value
            with self.subTest(provider=provider, field=field), self.assertRaisesRegex(PipelineError, 'belongs to another'):
                self.execute(**dict(self.runtime, promotion_receipt=self.authenticate_receipt(receipt)))
            self.assertFalse(self.calls)

    def test_vercel_changed_remote_identity_is_not_promoted(self):
        self.select_promotion('vercel')
        proof = self.authenticate_receipt(self.prior_deployment('vercel'))
        with patch('shared_ci.deploy._get_json', return_value={'projectId': 'prj_fixture', 'readyState': 'READY', 'id': 'dpl_other'}), self.assertRaisesRegex(PipelineError, 'remote deployment differs'):
            self.execute(**dict(self.runtime, promotion_receipt=proof))
        self.assertFalse(any(command[1:2] == ['promote'] for command, _ in self.calls))

    def test_conflicting_committed_provider_selection_is_not_used(self):
        self.select_promotion()
        self.config['settings']['version_id'] = 'ffffffff-1111-2222-3333-444444444444'
        proof = self.authenticate_receipt(self.prior_deployment())
        with self.assertRaisesRegex(PipelineError, 'committed provider identity differs'):
            self.execute(**dict(self.runtime, promotion_receipt=proof))
        self.assertFalse(self.calls)

    def test_pr_or_unapproved_branch_deployment_receipt_cannot_promote(self):
        self.select_promotion()
        for event, branch in [('pull_request', 'staging'), ('push', 'feature/untrusted'), ('workflow_dispatch', 'feature/untrusted')]:
            proof = self.authenticate_receipt(self.prior_deployment(), event=event, branch=branch)
            with self.subTest(event=event, branch=branch), self.assertRaisesRegex(PipelineError, 'trusted push/manual branch'):
                self.execute(**dict(self.runtime, promotion_receipt=proof))
            self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
