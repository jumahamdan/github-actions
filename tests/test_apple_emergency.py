import copy
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError, atomic_json, run as real_run, sha256_file
from shared_ci.mobile import execute, validate_emergency_archive
from tests import test_mobile as mobile_fixture
from tests.test_mobile import completed


class EmergencyAppleTests(unittest.TestCase):
    def setUp(self):
        self.fixture=mobile_fixture.MobileTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root;self.verify=self.fixture.apple(signed=True)
        self.fixture.write('.gitignore',b'Candidate.xcarchive/\n.ci-artifacts/\n.ci-results/\n')
        self.fixture.write('Example.xcodeproj/project.pbxproj',b'synthetic project')
        self.fixture.write('Package.resolved',b'{"pins":[]}')
        for command in [['git','init','-q','-b','staging'],['git','add','.'],['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture']]:
            subprocess.run(command,cwd=self.root,check=True,capture_output=True)
        sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root,text=True).strip()
        for name in list(os.environ):
            if name.startswith('CI_') or name.startswith('GITHUB_'):del os.environ[name]
        os.environ.update(SHARED_APPLE_MANUAL_EVENT='emergency-release',SHARED_APPLE_OPERATOR='fixture')
        self.execution={'mode':'emergency-mac','operator':'fixture','reason':'Fixture emergency recovery','source_sha':sha,'ref':'refs/heads/staging','build_id':'manual-fixture-42'}
        self.config={'operation':'apple-emergency-archive','execution':self.execution,'kind':'native','xcode':{'version':'16.2','build':'16C5032a'},'locks':{'Package.resolved':sha256_file(self.root/'Package.resolved')},'project':'Example.xcodeproj','scheme':'Example','archive':'.ci-artifacts/Manual.xcarchive','configuration':'Release','signing':'signed','version':'1.2.0','build':'42','allowed_refs':['refs/heads/staging']}
        self.platform=patch('shared_ci.mobile.platform.system',return_value='Darwin');self.platform.start();self.addCleanup(self.platform.stop)
    def runner(self,argv,**kwargs):
        if argv[0]=='git':return real_run(argv,**kwargs)
        if argv[:2]==['xcodebuild','-version']:return completed(argv,'Xcode 16.2\nBuild version 16C5032a\n')
        if argv[:2]==['xcodebuild','archive']:
            target=Path(argv[argv.index('-archivePath')+1]);shutil.copytree(self.root/'Candidate.xcarchive',target)
            return completed(argv)
        return self.fixture.apple_run(argv,**kwargs)
    def build(self):
        with patch('shared_ci.mobile.run',side_effect=self.runner):result=execute(self.config,self.root)
        path=self.root/'.ci-results/archive.json';atomic_json(path,result)
        self.verify.update(archive=self.config['archive'],execution={**self.execution,'archive_receipt':'.ci-results/archive.json','archive_receipt_sha256':sha256_file(path)})
        return result
    def test_real_git_manual_build_then_signed_identity_inspection(self):
        built=self.build();self.assertFalse(built['distribution_ready']);self.assertEqual(built['execution_context']['mode'],'emergency-mac')
        with patch('shared_ci.mobile.run',side_effect=self.runner):inspected=execute(self.verify,self.root)
        self.assertTrue(inspected['distribution_ready']);self.assertEqual(inspected['source_sha'],self.execution['source_sha'])
        self.assertEqual(inspected['archive_sha256'],built['archive_sha256'])
        self.assertEqual(inspected['build_id'],'manual-fixture-42')
    def test_emergency_refuses_fake_cloud_github_linux_missing_operator_and_dirty_source(self):
        for env in [{'CI_XCODE_CLOUD':'true'},{'GITHUB_ACTIONS':'true'},{'SHARED_APPLE_OPERATOR':'someone-else'},{'SHARED_APPLE_MANUAL_EVENT':''}]:
            with self.subTest(env=env),patch.dict(os.environ,env),patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaises(PipelineError):execute(self.config,self.root)
        with patch('shared_ci.mobile.platform.system',return_value='Linux'),self.assertRaises(PipelineError):execute(self.config,self.root)
        self.fixture.write('untracked-source.swift',b'let changed = true')
        with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaisesRegex(PipelineError,'clean'):execute(self.config,self.root)
    def test_wrong_source_ref_stale_archive_and_failed_build_block(self):
        for field,value in [('source_sha','f'*40),('ref','refs/heads/main')]:
            config=copy.deepcopy(self.config);config['execution'][field]=value
            with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaises(PipelineError):execute(config,self.root)
        config=copy.deepcopy(self.config);config['archive']='Candidate.xcarchive'
        with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaisesRegex(PipelineError,'new xcarchive'):execute(config,self.root)
        def failing(argv,**kwargs):
            if argv[:2]==['xcodebuild','archive']:raise PipelineError('xcodebuild failed')
            return self.runner(argv,**kwargs)
        with patch('shared_ci.mobile.run',side_effect=failing),self.assertRaises(PipelineError):execute(self.config,self.root)
    def test_modified_payload_and_changed_build_proof_fail(self):
        self.build();archive=self.root/self.config['archive'];binary=archive/'Products/Applications/Example.app/Example';binary.write_bytes(b'changed')
        with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaisesRegex(PipelineError,'build proof'):execute(self.verify,self.root)
        binary.write_bytes(b'fixture');self.verify['execution']['archive_receipt_sha256']='0'*64
        with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaisesRegex(PipelineError,'proof'):execute(self.verify,self.root)
    def test_reserved_evidence_does_not_change_payload_or_allow_symbol_substitution(self):
        built=self.build();archive=self.root/self.config['archive'];directory=archive/'.shared-ci-evidence';directory.mkdir();(directory/'candidate.json').write_text('{"fixture":true}')
        with patch('shared_ci.mobile.run',side_effect=self.runner):context=validate_emergency_archive(self.verify['execution'],self.root,archive,build='42')
        self.assertEqual(context['archive_sha256'],built['archive_sha256'])
        self.verify['targets'][0]['dsym']='.shared-ci-evidence/symbols'
        with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaisesRegex(PipelineError,'reserved'):execute(self.verify,self.root)
    def test_payload_symlink_cannot_bypass_hash_through_evidence(self):
        self.build();archive=self.root/self.config['archive'];evidence=archive/'.shared-ci-evidence';evidence.mkdir();target=evidence/'hidden-payload';target.write_text('unbound content')
        (archive/'Products/Applications/Example.app/alias').symlink_to(target)
        with patch('shared_ci.mobile.run',side_effect=self.runner),self.assertRaisesRegex(PipelineError,'excluded evidence'):execute(self.verify,self.root)
    def test_unsigned_manual_candidate_remains_not_distributable(self):
        self.config['signing']='unsigned';self.build();self.verify['signing']='unsigned'
        with patch('shared_ci.mobile.run',side_effect=self.runner):receipt=execute(self.verify,self.root)
        self.assertFalse(receipt['distribution_ready'])


if __name__=='__main__':unittest.main()
