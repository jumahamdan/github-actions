import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from shared_ci.common import PipelineError

spec=importlib.util.spec_from_file_location('apple_hook',Path(__file__).resolve().parents[1]/'scripts/apple/run-hook.py')
hook=importlib.util.module_from_spec(spec);spec.loader.exec_module(hook)


class AppleHookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir();self.archive=self.root/'Archive.xcarchive';self.archive.mkdir()
        (self.source/'config.json').write_text('{}')
        env={'CI_XCODE_CLOUD':'true','CI_COMMIT':'a'*40,'CI_PRIMARY_REPOSITORY_PATH':str(self.source),'CI_ARCHIVE_PATH':str(self.archive),'CI_XCODEBUILD_ACTION':'archive','CI_XCODEBUILD_EXIT_CODE':'0','SHARED_APPLE_CONFIG':'config.json','SHARED_APPLE_OUTPUT':'.shared-ci-evidence/receipt.json'}
        p=patch.dict(os.environ,env,clear=True);p.start();self.addCleanup(p.stop)
        p=patch.object(hook.platform,'system',return_value='Darwin');p.start();self.addCleanup(p.stop)
        p=patch.object(hook,'stamp',side_effect=lambda value,*args:value);p.start();self.addCleanup(p.stop)
        p=patch.object(hook,'run',side_effect=lambda command,**kwargs:subprocess.CompletedProcess(command,1 if 'ls-files' in command else 0,'a'*40,''));p.start();self.addCleanup(p.stop)
    def test_archive_monitoring_uses_checkout_config_and_current_archive_root(self):
        os.environ.update(SHARED_APPLE_MODULE='monitoring',SHARED_APPLE_ROOT='archive')
        seen=[];module=types.SimpleNamespace(execute=lambda config,root:seen.append(root) or {'status':'passed'})
        with patch.object(hook.importlib,'import_module',return_value=module):hook.execute_hook()
        self.assertEqual(seen,[self.archive]);self.assertTrue((self.archive/'.shared-ci-evidence/receipt.json').is_file())
    def test_archive_candidate_uses_verified_git_source_root(self):
        os.environ.update(SHARED_APPLE_MODULE='evidence',SHARED_APPLE_ROOT='archive')
        seen=[]
        def create(config,root,**kwargs):seen.append((root,kwargs['source_root']));return {'status':'passed'}
        with patch.object(hook.importlib,'import_module',return_value=types.SimpleNamespace(create=create)):hook.execute_hook()
        self.assertEqual(seen,[(self.archive,self.source)])
    def test_bridge_export_is_native_archive_only(self):
        os.environ.update(SHARED_APPLE_MODULE='apple_bridge',SHARED_APPLE_ROOT='archive')
        (self.source/'config.json').write_text('{"operation":"apple-export"}')
        module=types.SimpleNamespace(execute=lambda config,root:{'status':'passed','operation':config['operation']})
        with patch.object(hook.importlib,'import_module',return_value=module):self.assertEqual(hook.execute_hook()['operation'],'apple-export')
        (self.source/'config.json').write_text('{"operation":"apple-import"}')
        with patch.object(hook.importlib,'import_module',return_value=module),self.assertRaises(PipelineError):hook.execute_hook()
    def test_failed_result_has_receipt_and_fails(self):
        os.environ.update(SHARED_APPLE_MODULE='monitoring',SHARED_APPLE_ROOT='archive')
        with patch.object(hook.importlib,'import_module',return_value=types.SimpleNamespace(execute=lambda *args:{'status':'pending'})),self.assertRaises(PipelineError):hook.execute_hook()
        self.assertEqual(json.loads((self.archive/'.shared-ci-evidence/receipt.json').read_text())['status'],'pending')
    def test_wrong_host_context_root_module_and_payload_output_refused(self):
        for values in [{'GITHUB_ACTIONS':'true'},{'CI_XCODE_CLOUD':'false'},{'SHARED_APPLE_MODULE':'os'},{'SHARED_APPLE_ROOT':'anywhere'},{'SHARED_APPLE_ROOT':'archive','CI_XCODEBUILD_EXIT_CODE':'1'},{'SHARED_APPLE_ROOT':'archive','SHARED_APPLE_OUTPUT':'Products/payload.json'}]:
            with self.subTest(values=values),patch.dict(os.environ,values),self.assertRaises(PipelineError):hook.execute_hook()
        with patch.object(hook.platform,'system',return_value='Linux'),self.assertRaises(PipelineError):hook.execute_hook()


if __name__=='__main__':unittest.main()
