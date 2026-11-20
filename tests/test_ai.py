import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shared_ci.ai import (PipelineError, execute, parse_results, provider_environment,
                          runtime_config, validate, revision_probe)

FAKE = r'''
import json,os,sys,time
from pathlib import Path
trace=Path(__file__).with_name('trace.jsonl')
with trace.open('a') as f: f.write(json.dumps({'argv':sys.argv[1:],'env':dict(os.environ)})+'\n')
if '--version' in sys.argv:
 print('0.125.1')
 sys.exit(0)
scenario=os.environ.get('AI_VAR_SCENARIO','pass')
if scenario=='sleep': time.sleep(10)
if scenario=='missing': sys.exit(0)
if scenario=='tool-error': sys.exit(2)
path=Path(os.environ['AI_RESULTS_PATH'])
if scenario=='malformed':
 path.write_text('{not json')
 sys.exit(0)
rows=[{'success':True,'response':{'output':'private-prompt-contents sensitive-synthetic-value','cost':0.01}}, {'success':True,'response':{'output':'safe','cost':0.02}}]
for row in rows: row['gradingResult']={'pass':True,'componentResults':[{'pass':True,'assertion':{'type':'equals'}}]}
stats={'successes':2,'failures':0,'errors':0,'tokenUsage':{'total':14}}
if scenario in ('failure','provider-error'):
 rows[1]['success']=False
 rows[1]['failureReason']=2 if scenario=='provider-error' else 1
 stats['successes']=1
 stats['failures']=1
 if scenario=='provider-error': stats['errors']=1
if scenario=='empty': rows=[]; stats.update(successes=0)
if scenario=='partial': rows=rows[:1]; stats.update(successes=1)
if scenario=='inconsistent': stats['successes']=500
if scenario=='cached': rows[0]['response']['cached']=True
if scenario=='no-assertions': rows[0].pop('gradingResult')
if scenario=='no-stats': stats=None
if scenario=='no-tokens': del stats['tokenUsage']
if scenario=='overspend': rows[0]['response']['cost']=500
path.write_text(json.dumps({'results':{'results':rows,'stats':stats}}))
if '--config' in sys.argv:
 cfg=Path(sys.argv[sys.argv.index('--config')+1])
 Path(__file__).with_name('runtime.json').write_text(cfg.read_text())
print('do not publish raw output sensitive-synthetic-value')
sys.exit(100 if scenario=='failure' else 0)
'''


def cfg(tool):
    return {
        'schema_version':1,'mode':'mock','engine':'promptfoo',
        'promptfoo':{'command':[sys.executable,str(tool)],'version':'0.125.1'},
        'evaluation':{'config':'promptfoo.json','inputs':['promptfoo.json'],'expected_results':2,'minimum_pass_rate':1},
        'providers':[{'kind':'mock','id':'echo','model':'echo','env':{}}],
        'limits':{'timeout_seconds':10,'call_timeout_seconds':3,'max_concurrency':2,'max_retries':0,'max_results':2,'max_output_tokens':128,'upstream_budget':'not-applicable'},
        'identity':{'candidate':'fixture-1'},
    }


class AIRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.tool=self.root/'fake_promptfoo.py'
        self.tool.write_text(FAKE)
        self.out=self.root/'receipt.json'
        self.prompt=self.root/'promptfoo.json'
        self.prompt.write_text(json.dumps({'providers':['echo'],'prompts':['{{message}}'],'tests':[{'vars':{'message':'hello'},'assert':[{'type':'contains','value':'hello'}]},{'vars':{'message':'world'},'assert':[{'type':'equals','value':'world'}]}]}))
        for args in (['git','init','-q'],['git','add','.'],['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture']):
            subprocess.run(args,cwd=self.root,check=True,stdout=subprocess.DEVNULL)
        self.sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root,text=True).strip()
        self.config=cfg(self.tool)
        self.env=patch.dict(os.environ,{'PATH':os.environ.get('PATH',''),'HOME':str(self.root/'ambient-home'),'OPENAI_API_KEY':'ambient-should-not-pass','ANTHROPIC_API_KEY':'ambient-anthropic','AWS_PROFILE':'ambient-profile','GITHUB_TOKEN':'ambient-gh'},clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def traces(self):
        return [json.loads(line) for line in (self.root/'trace.jsonl').read_text().splitlines()]

    def live(self,mode='live'):
        self.config.update(mode=mode,live_enabled=True,trusted_refs=['refs/heads/staging'])
        self.config['limits']['upstream_budget']='product-enforced'
        self.config['providers']=[{'kind':'anthropic','id':'anthropic:messages:claude-fixture','model':'claude-fixture','env':{'ANTHROPIC_API_KEY':'EVAL_ANTHROPIC_SECRET'}}]
        data=json.loads(self.prompt.read_text()); data['providers']=['anthropic:messages:claude-fixture']; self.prompt.write_text(json.dumps(data))
        os.environ.update(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/staging',GITHUB_REPOSITORY='example/repo',EVAL_ANTHROPIC_SECRET='sensitive-synthetic-value')

    def test_mock_executes_real_process_and_exports_only_aggregate(self):
        receipt=execute(self.config,self.root,self.out)
        self.assertEqual(receipt['status'],'passed')
        self.assertEqual(receipt['source_revision'],self.sha)
        self.assertEqual(receipt['summary']['total'],2)
        text=self.out.read_text()
        self.assertNotIn('private-prompt',text)
        self.assertNotIn('sensitive-synthetic-value',text)
        traces=self.traces()
        for call in traces:
            env=call['env']
            self.assertNotIn('OPENAI_API_KEY',env)
            self.assertNotIn('ANTHROPIC_API_KEY',env)
            self.assertNotIn('AWS_PROFILE',env)
            self.assertNotIn('GITHUB_TOKEN',env)
            self.assertNotEqual(env['HOME'],os.environ['HOME'])
            self.assertFalse(Path(env['HOME']).exists())
        self.assertIn('--no-cache',traces[1]['argv'])
        self.assertIn('--no-write',traces[1]['argv'])
        self.assertEqual(traces[1]['env']['PROMPTFOO_DISABLE_REMOTE_GENERATION'],'true')
        runtime=json.loads((self.root/'runtime.json').read_text())
        self.assertFalse(runtime['sharing'])
        self.assertFalse(runtime['writeLatestResults'])
        self.assertEqual(runtime['providers'][0]['config']['maxRetries'],0)

    def test_live_only_selected_secret_and_tool_validation_has_no_secret(self):
        self.live()
        receipt=execute(self.config,self.root,self.out)
        calls=self.traces()
        self.assertNotIn('ANTHROPIC_API_KEY',calls[0]['env'])
        self.assertEqual(calls[1]['env']['ANTHROPIC_API_KEY'],'sensitive-synthetic-value')
        self.assertNotIn('OPENAI_API_KEY',calls[1]['env'])
        self.assertNotIn('EVAL_ANTHROPIC_SECRET',calls[1]['env'])
        self.assertNotIn('sensitive-synthetic-value',json.dumps(receipt))
        runtime=json.loads((self.root/'runtime.json').read_text())
        self.assertEqual(runtime['providers'][0]['config']['max_tokens'],128)

    def test_no_live_optin_fails_before_tool(self):
        self.live(); self.config['live_enabled']=False
        with self.assertRaisesRegex(PipelineError,'explicitly enabled'): execute(self.config,self.root,self.out)
        self.assertFalse((self.root/'trace.jsonl').exists())

    def test_untrusted_context_fails_before_tool(self):
        self.live(); os.environ['GITHUB_EVENT_NAME']='pull_request_target'
        with self.assertRaisesRegex(PipelineError,'Untrusted event'): execute(self.config,self.root,self.out)
        self.assertFalse((self.root/'trace.jsonl').exists())

    def test_missing_secret_reports_name_never_value(self):
        self.live(); del os.environ['EVAL_ANTHROPIC_SECRET']
        with self.assertRaisesRegex(PipelineError,'EVAL_ANTHROPIC_SECRET'): execute(self.config,self.root,self.out)

    def test_version_mismatch_prevents_eval(self):
        self.config['promptfoo']['version']='0.125.2'
        with self.assertRaisesRegex(PipelineError,'pinned version'): execute(self.config,self.root,self.out)
        self.assertEqual(len(self.traces()),1)

    def test_nonexact_version_rejected(self):
        for version in ['latest','^0.125.1','0.121','*']:
            with self.subTest(version=version):
                self.config['promptfoo']['version']=version
                with self.assertRaises(PipelineError): validate(self.config)

    def test_incomplete_or_malformed_outputs_never_pass(self):
        for scenario in ['missing','malformed','empty','partial','inconsistent','no-stats','cached','provider-error','tool-error','no-assertions']:
            with self.subTest(scenario=scenario):
                self.config['variables']={'AI_VAR_SCENARIO':scenario}
                with self.assertRaises(PipelineError): execute(self.config,self.root,self.out)
                self.assertFalse(self.out.exists())

    def test_failed_threshold_writes_failed_aggregate_then_blocks(self):
        self.config['variables']={'AI_VAR_SCENARIO':'failure'}
        with self.assertRaisesRegex(PipelineError,'threshold'): execute(self.config,self.root,self.out)
        self.assertEqual(json.loads(self.out.read_text())['status'],'failed')

    def test_product_threshold_can_accept_assertion_fail_exit(self):
        self.config['variables']={'AI_VAR_SCENARIO':'failure'}
        self.config['evaluation']['minimum_pass_rate']=0.5
        self.assertEqual(execute(self.config,self.root,self.out)['status'],'passed')

    def test_review_is_advisory_not_release_evidence(self):
        self.live('review'); self.config['variables']={'AI_VAR_SCENARIO':'failure'}
        receipt=execute(self.config,self.root,self.out)
        self.assertEqual(receipt['status'],'advisory')
        self.assertFalse(receipt['threshold_passed'])
        self.config['release']=True
        with self.assertRaisesRegex(PipelineError,'Only live'): validate(self.config)

    def test_product_gate_is_real_and_blocks(self):
        gate=self.root/'gate.py'
        gate.write_text('import os,json,sys\nassert json.load(open(os.environ["AI_RESULTS_PATH"]))["results"]["stats"]["successes"]==2\nsys.exit(3)\n')
        self.config['evaluation']['gate']=[sys.executable,str(gate)]
        with self.assertRaisesRegex(PipelineError,'Product-owned AI gate failed'): execute(self.config,self.root,self.out)
        self.assertFalse(self.out.exists())
        gate.write_text('import os,json\nassert json.load(open(os.environ["AI_RESULTS_PATH"]))["results"]["stats"]["successes"]==2\n')
        self.assertTrue(execute(self.config,self.root,self.out)['product_gate_passed'])

    def test_product_runner_contract(self):
        self.config['engine']='product'
        self.config['evaluation']['command']=[sys.executable,str(self.tool)]
        receipt=execute(self.config,self.root,self.out)
        self.assertEqual(receipt['status'],'passed')
        self.assertEqual(self.traces()[1]['env']['AI_EXPECTED_REVISION'],self.sha)
        self.assertTrue(self.traces()[1]['env']['EVALS_ENV'].endswith('empty.env'))

    def test_postrun_usage_gate_is_not_treated_as_spend_cap(self):
        self.config['limits']['max_reported_tokens']=10
        with self.assertRaisesRegex(PipelineError,'post-run gate'): execute(self.config,self.root,self.out)
        self.config['limits']['max_reported_tokens']=20
        receipt=execute(self.config,self.root,self.out)
        self.assertIn('not a hard',receipt['bounds']['usage_limits'])
        self.config['variables']={'AI_VAR_SCENARIO':'no-tokens'}
        with self.assertRaisesRegex(PipelineError,'accounting is absent'): execute(self.config,self.root,self.out)

    def test_postrun_cost_gate(self):
        self.config['limits']['max_reported_cost_usd']=1
        self.config['variables']={'AI_VAR_SCENARIO':'overspend'}
        with self.assertRaisesRegex(PipelineError,'post-run gate'): execute(self.config,self.root,self.out)

    def test_deadline_cleans_private_files(self):
        self.config['limits'].update(timeout_seconds=1,call_timeout_seconds=1)
        self.config['variables']={'AI_VAR_SCENARIO':'sleep'}
        with self.assertRaisesRegex(PipelineError,'deadline'): execute(self.config,self.root,self.out)
        self.assertFalse(Path(self.traces()[1]['env']['HOME']).exists())

    def test_wrong_source_revision_blocks(self):
        with self.assertRaisesRegex(PipelineError,'source differs'): execute(self.config,self.root,self.out,'0'*40)

    def test_changed_inputs_change_receipt_identity(self):
        first=execute(self.config,self.root,self.out)['input_hashes']
        data=json.loads(self.prompt.read_text()); data['description']='changed'; self.prompt.write_text(json.dumps(data))
        second=execute(self.config,self.root,self.out)['input_hashes']
        self.assertNotEqual(first,second)

    def test_unknown_fields_bad_types_paths_and_counts_rejected(self):
        variants=[]
        c=copy.deepcopy(self.config); c['unknown']=1; variants.append(c)
        c=copy.deepcopy(self.config); c['limits']['max_concurrency']=True; variants.append(c)
        c=copy.deepcopy(self.config); c['evaluation']['expected_results']=3; variants.append(c)
        c=copy.deepcopy(self.config); c['evaluation']['config']='../outside'; variants.append(c)
        for c in variants:
            with self.assertRaises(PipelineError): execute(c,self.root,self.out)

    def test_mock_remote_or_executable_assertions_rejected(self):
        for assertion in ['llm-rubric','javascript','similar','webhook']:
            with self.subTest(assertion=assertion):
                data={'providers':['echo'],'prompts':['hello'],'tests':[{'assert':[{'type':assertion,'value':'test'}]}]}
                self.prompt.write_text(json.dumps(data))
                with self.assertRaisesRegex(PipelineError,'deterministic'): execute(self.config,self.root,self.out)

    def test_native_config_output_and_env_override_rejected(self):
        for key in ['env','outputPath','commandLineOptions','tracing']:
            with self.subTest(key=key):
                self.prompt.write_text(json.dumps({'providers':['echo'],'prompts':['hello'],key:{}}))
                with self.assertRaisesRegex(PipelineError,'override'): execute(self.config,self.root,self.out)

    def test_release_http_requires_revision_proof(self):
        self.live()
        self.config.update(release=True,providers=[{'kind':'http','id':'http','model':'service-v1','env':{}}])
        with self.assertRaisesRegex(PipelineError,'revision probe'): execute(self.config,self.root,self.out)

    def test_provider_contracts_and_output_caps(self):
        cases=[('openai','openai:responses:model-fixture','model-fixture',{'OPENAI_API_KEY':'FIXTURE_KEY'},'max_output_tokens'),('openai','openai:chat:model-fixture','model-fixture',{'OPENAI_API_KEY':'FIXTURE_KEY'},'max_completion_tokens'),('gemini','google:model-fixture','model-fixture',{'GOOGLE_API_KEY':'FIXTURE_KEY'},'maxOutputTokens'),('bedrock','bedrock:converse:model-fixture','model-fixture',{x:x for x in ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_REGION']},'maxTokens')]
        for kind,ident,model,env,field in cases:
            with self.subTest(kind=ident):
                c=copy.deepcopy(self.config); c.update(mode='live'); c['limits']['upstream_budget']='product-enforced'; c['providers']=[{'kind':kind,'id':ident,'model':model,'env':env}]
                validate(c)
                self.prompt.write_text(json.dumps({'providers':[ident],'prompts':['test']}))
                with tempfile.TemporaryDirectory() as private:
                    path=runtime_config(c,self.prompt,self.root,Path(private))
                    self.assertEqual(json.loads(path.read_text())['providers'][0]['config'][field],128)

    def test_provider_env_crossovers_and_missing_models_rejected(self):
        self.live()
        self.config['providers'][0]['env']['OPENAI_API_KEY']='OTHER_SECRET'
        with self.assertRaisesRegex(PipelineError,'outside the selected'): validate(self.config)
        self.config['providers'][0]['env'].pop('OPENAI_API_KEY')
        self.config['providers'][0]['model']=''
        with self.assertRaisesRegex(PipelineError,'explicit'): validate(self.config)

    def test_live_implicit_grader_is_rejected(self):
        self.live()
        data=json.loads(self.prompt.read_text())
        data['tests'][0]['assert']=[{'type':'llm-rubric','value':'good'}]
        self.prompt.write_text(json.dumps(data))
        with self.assertRaisesRegex(PipelineError,'explicit product evaluation adapter'): execute(self.config,self.root,self.out)

    def test_old_receipt_is_removed_before_a_failed_execution(self):
        execute(self.config,self.root,self.out)
        self.config['variables']={'AI_VAR_SCENARIO':'missing'}
        with self.assertRaises(PipelineError): execute(self.config,self.root,self.out)
        self.assertFalse(self.out.exists())

    def test_revision_probe_mismatch_and_redirect_safe(self):
        class Response:
            headers={'X-Revision':'wrong'}
            def __enter__(self): return self
            def __exit__(self,*args): pass
        class Opener:
            def open(self,*args,**kw): return Response()
        with patch('shared_ci.ai.urllib.request.build_opener',return_value=Opener()):
            with self.assertRaisesRegex(PipelineError,'differs'): revision_probe({'url_env':'AI_TARGET_URL','header':'X-Revision'},{'AI_TARGET_URL':'https://example.invalid/health'},self.sha,1)
        with self.assertRaisesRegex(PipelineError,'HTTPS'): revision_probe({'url_env':'AI_TARGET_URL','header':'X-Revision'},{'AI_TARGET_URL':'http://example.invalid'},self.sha,1)


if __name__=='__main__': unittest.main()
