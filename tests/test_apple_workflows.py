import copy
import json
import tempfile
import unittest
from pathlib import Path
from shared_ci.apple_workflows import execute, normalize
from shared_ci.common import PipelineError


def fixture():
    return {'data':[{'id':'workflow-1','type':'ciWorkflows','attributes':{'name':'Staging archive','isEnabled':True,'clean':True,'containerFilePath':'Example.xcodeproj','actions':[{'name':'Archive','actionType':'ARCHIVE','scheme':'Example','platform':'IOS','isRequiredToPass':True,'destination':'ANY_IOS_DEVICE'}],'branchStartCondition':{'source':{'patterns':[{'pattern':'staging','isPrefix':False}]},'autoCancel':False}},'relationships':{k:{'data':{'type':v,'id':i}} for k,v,i in [('product','ciProducts','product-1'),('repository','scmRepositories','repository-1'),('xcodeVersion','ciXcodeVersions','xcode-1'),('macOsVersion','ciMacOsVersions','macos-1')]}}]}


class AppleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.desired=fixture();self.actual=copy.deepcopy(self.desired)
    def execute(self):
        for name,value in [('desired',self.desired),('actual',self.actual)]: (self.root/(name+'.json')).write_text(json.dumps(value))
        return execute({'operation':'apple-workflow-drift','product_id':'product-1','desired':'desired.json','actual':'actual.json'},self.root)
    def test_server_metadata_and_resource_list_order_ignored(self):
        self.actual['data'][0]['attributes'].update(lastModifiedDate='2026-11-15',isLockedForEditing=True)
        self.actual['data'][0]['links']={'self':'https://example.invalid/server-fields'}
        self.actual['data'][0]['relationships']['buildRuns']={'links':{'related':'https://example.invalid/builds'}}
        result=self.execute();self.assertEqual(result['status'],'passed');self.assertFalse(result['provider_state_fetched'])
    def test_trigger_toolchain_enabled_and_required_action_changes_report_drift(self):
        for change in ('trigger','xcode','enabled','required'):
            self.actual=copy.deepcopy(self.desired);workflow=self.actual['data'][0]
            if change=='trigger':workflow['attributes']['branchStartCondition']['source']['patterns'][0]['pattern']='main'
            elif change=='xcode':workflow['relationships']['xcodeVersion']['data']['id']='other-xcode'
            elif change=='enabled':workflow['attributes']['isEnabled']=False
            else:workflow['attributes']['actions'][0]['isRequiredToPass']=False
            with self.subTest(change=change):
                result=self.execute();self.assertEqual(result['status'],'failed');self.assertTrue(result['drift']);self.assertNotIn('main',json.dumps(result['drift']))
    def test_missing_extra_and_renamed_workflows_are_visible(self):
        self.actual['data'][0]['id']='workflow-2';result=self.execute();self.assertEqual(result['status'],'failed');self.assertEqual(len(result['drift']),2)
    def test_partial_pagination_wrong_product_duplicate_ids_unknown_fields_fail(self):
        cases=[]
        value=fixture();value['links']={'next':'https://example.invalid/page2'};cases.append(value)
        value=fixture();value['data'][0]['relationships']['product']['data']['id']='wrong';cases.append(value)
        value=fixture();value['data'].append(copy.deepcopy(value['data'][0]));cases.append(value)
        value=fixture();value['data'][0]['attributes']['unknownFlag']=True;cases.append(value)
        value=fixture();del value['data'][0]['relationships']['repository']['data'];cases.append(value)
        for value in cases:
            with self.subTest(value=value),self.assertRaises(PipelineError):normalize(value,'product-1')


if __name__=='__main__':unittest.main()
