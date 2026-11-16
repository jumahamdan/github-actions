import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci import deploy
from shared_ci.common import PipelineError, fingerprint
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
                (directory / 'manifest.json').write_text(json.dumps({'artifacts': {'Fixture': {'environment': 'aws://'+ACCOUNT+'/us-east-1', 'properties': {'stackName': 'Fixture', 'templateFile': 'Fixture.template.json'}}}}))
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


if __name__=='__main__':unittest.main()
