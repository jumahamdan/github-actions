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
                  "SHARED_MODULE":"stores","SHARED_CONFIG":"config.json","SHARED_ROOT":".","SHARED_ACTION_REF":"a"*40,
                  "SHARED_CREDENTIALS_CONFIG":"","SHARED_RECEIPT_PATH":"","SHARED_OPERATION":""}

    def test_pending_operation_has_stamped_receipt_and_outputs_but_cannot_pass_release_gate(self):
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
        with patch.dict(os.environ,{**self.env,"SHARED_RECEIPT_PATH":"config.json"}),patch("shared_ci.stores.execute",return_value={"status":"passed"}):
            with self.assertRaisesRegex(PipelineError,"overwrite"):wrapper.main()
