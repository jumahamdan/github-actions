import importlib.util
import json
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.gate import evaluate
from shared_ci.common import atomic_json, fingerprint, load_json, sha256_file
from shared_ci.evidence import IDENTITY_FIELDS
from shared_ci.quality import QualityFailure
from test_stores import fixture

LIBRARY=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location("shared_wrapper",LIBRARY/"scripts/run-module.py")
wrapper=importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"source";self.root.mkdir()
        self.outputs=Path(self.temp.name)/"outputs"
        self.config=self.root/"config.json";self.config.write_text('{}')
        def git(*args):subprocess.run(["git",*args],cwd=self.root,check=True,capture_output=True)
        git("init","-q");git("config","user.email","fixture@example.invalid");git("config","user.name","Fixture")
        git("add","config.json");git("commit","-qm","fixture")
        self.env={"GITHUB_WORKSPACE":str(self.root),"GITHUB_OUTPUT":str(self.outputs),"RUNNER_TEMP":self.temp.name,
                  "SHARED_MODULE":"docs","SHARED_CONFIG":"config.json","SHARED_ROOT":".","SHARED_ACTION_REF":"a"*40,
                  "SHARED_CREDENTIALS_CONFIG":"","SHARED_RECEIPT_PATH":"","SHARED_OPERATION":""}

    def selected_store(self):
        config,candidate,_=fixture(self.root)
        candidate['library_sha']='a'*40
        candidate['identity']=fingerprint({key:candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.root/'candidate.json',candidate)
        config['expected_identity']=candidate['identity']
        atomic_json(self.config,config)
        subprocess.run(['git','add','config.json'],cwd=self.root,check=True,capture_output=True)
        subprocess.run(['git','commit','-qm','selected store configuration'],cwd=self.root,check=True,capture_output=True)
        candidate['source_tree']=subprocess.run(['git','rev-parse','HEAD^{tree}'],cwd=self.root,check=True,capture_output=True,text=True).stdout.strip()
        inspected=load_json(self.root/'inspection.json')
        inspected['assurance']['source_tree']=candidate['source_tree']
        atomic_json(self.root/'inspection.json',inspected)
        candidate['checks']['inspection.json']=sha256_file(self.root/'inspection.json')
        candidate['identity']=fingerprint({key:candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.root/'candidate.json',candidate)
        self.env.update(SHARED_MODULE='stores',SHARED_CANDIDATE_MANIFEST='candidate.json',SHARED_CANDIDATE_IDENTITY=candidate['identity'],SHARED_CANDIDATE_SOURCE_SHA=candidate['source_sha'])

    def test_pending_operation_has_stamped_receipt_and_outputs_but_cannot_pass_release_gate(self):
        self.selected_store()
        with patch.dict(os.environ,self.env),patch("shared_ci.stores.execute",return_value={"status":"pending","state":"submitted_for_review"}):
            wrapper.main()
        outputs=dict(line.split("=",1) for line in self.outputs.read_text().splitlines())
        self.assertEqual(outputs["status"],"pending")
        receipt=json.loads(Path(outputs["receipt"]).read_text())
        self.assertEqual(receipt["assurance"]["kind"],"stores")
        self.assertEqual(outputs["state"],"submitted_for_review")
        with self.assertRaises(PipelineError):
            evaluate({"schema":1,"checks":{"distribution":True}}, {"distribution":{"result":"success","outputs":{"status":"pending"}}})

    def test_materialized_credentials_and_environment_are_cleaned_after_failure(self):
        contract={"schema":1,"files":[{"name":"TEST_SECRET_BASE64","required":True,"encoding":"base64","format":"binary","output_env":"TEST_SECRET_FILE"}]}
        (self.root/"credentials.json").write_text(json.dumps(contract)); observed=[]
        def fail(*args,**kwargs):
            observed.append(Path(os.environ["TEST_SECRET_FILE"]))
            self.assertTrue(observed[0].is_file())
            raise PipelineError("deliberate fixture failure")
        env={**self.env,"SHARED_MODULE":"docs","SHARED_CREDENTIALS_CONFIG":"credentials.json","TEST_SECRET_BASE64":"c3ludGhldGlj"}
        with patch.dict(os.environ,env),patch("shared_ci.docs.execute",side_effect=fail):
            with self.assertRaises(SystemExit):runpy.run_path(str(LIBRARY/"scripts/run-module.py"),run_name="__main__")
            self.assertNotIn("TEST_SECRET_FILE",os.environ)
        self.assertFalse(observed[0].exists())

    def test_candidate_rejects_false_executing_library_revision(self):
        self.config.write_text(json.dumps({"library_sha":"b"*40}))
        with patch.dict(os.environ,{**self.env,"SHARED_MODULE":"evidence","SHARED_OPERATION":"create"}):
            with self.assertRaisesRegex(PipelineError,"executing helper"):wrapper.main()

    def test_receipt_cannot_overwrite_tracked_source(self):
        with patch.dict(os.environ,{**self.env,"SHARED_RECEIPT_PATH":"config.json"}),patch("shared_ci.docs.execute",return_value={"status":"passed"}):
            with self.assertRaisesRegex(PipelineError,"overwrite"):wrapper.main()

    def test_failed_operation_retains_only_sanitized_failure_receipt(self):
        with patch.dict(os.environ,self.env),patch('shared_ci.docs.execute',side_effect=PipelineError('provider body with synthetic-secret')):
            with self.assertRaises(PipelineError):wrapper.main()
        outputs=dict(line.split('=',1) for line in self.outputs.read_text().splitlines())
        self.assertEqual(outputs['status'],'failed')
        self.assertEqual(json.loads(Path(outputs['receipt']).read_text()),{'schema':1,'status':'failed','module':'docs'})
        self.assertNotIn('synthetic-secret',Path(outputs['receipt']).read_text())

    def test_quality_failure_details_survive_in_existing_evidence_bundle(self):
        details = {"check": "unit", "phase": "test", "elapsed_seconds": 1.2, "exit_code": 7, "timed_out": False,
                   "summary": "command exited unsuccessfully", "test_diagnostics": {"report": "junit", "failed_cases": [{"case": 1, "type": "failure", "comparison": "1 != 2"}], "omitted_failures": 0}}
        with patch.dict(os.environ, self.env | {"SHARED_MODULE": "quality"}), patch('shared_ci.quality.execute', side_effect=QualityFailure(details)):
            with self.assertRaises(QualityFailure):
                wrapper.main()
        outputs = dict(line.split('=', 1) for line in self.outputs.read_text().splitlines())
        receipt = json.loads(Path(outputs['receipt']).read_text())
        self.assertEqual(receipt['failure'], details)
        self.assertEqual(outputs['status'], 'failed')
        self.assertEqual(list(Path(outputs['bundle']).iterdir()), [Path(outputs['receipt'])])

    def test_failure_after_receipt_creation_replaces_passing_evidence(self):
        self.env['SHARED_MODULE']='quality'
        (self.root/'coverage.xml').write_text('actual coverage')
        result={'status':'passed','coverage':{'coverage.xml':'0'*64}}
        with patch.dict(os.environ,self.env),patch('shared_ci.quality.execute',return_value=result):
            with self.assertRaisesRegex(PipelineError,'Coverage changed'):wrapper.main()
        outputs=dict(line.split('=',1) for line in self.outputs.read_text().splitlines())
        self.assertEqual(outputs['status'],'failed')
        self.assertEqual(load_json(outputs['receipt'])['status'],'failed')
