#!/usr/bin/env python3
"""Construct transport requests from explicit workflow inputs, never shell templates."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

LIBRARY=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(LIBRARY))
from shared_ci.artifacts import execute
from shared_ci.common import PipelineError, confined_path, load_json


def main():
    workspace=Path(os.environ['GITHUB_WORKSPACE']).resolve()
    root=confined_path(workspace,os.environ.get('TRANSPORT_ROOT','.'))
    operation=os.environ.get('TRANSPORT_OPERATION','')
    if operation=='fetch':
        try:
            branches=json.loads(os.environ.get('TRANSPORT_ALLOWED_BRANCHES','[]'))
        except json.JSONDecodeError as exc:
            raise PipelineError('Artifact producer branches must be a JSON array') from exc
        if (not isinstance(branches,list) or not branches
                or any(not isinstance(branch,str) or not re.fullmatch(r'[A-Za-z0-9_./-]+',branch)
                       or branch.startswith(('refs/','/')) or '..' in branch or branch.endswith('/') for branch in branches)
                or len(branches)!=len(set(branches))):
            raise PipelineError('Artifact fetch requires explicit unique producer branch names')
        try:
            events=json.loads(os.environ.get('TRANSPORT_ALLOWED_EVENTS','["push","workflow_dispatch"]'))
        except json.JSONDecodeError as exc:
            raise PipelineError('Artifact producer events must be a JSON array') from exc
        if not isinstance(events,list) or not events or any(event not in ('push','workflow_dispatch','pull_request') for event in events):
            raise PipelineError('Artifact producer events must be explicitly allowed')
        config={'schema':1,'operation':'fetch','repository':os.environ['GITHUB_REPOSITORY'],
                'run_id':os.environ['TRANSPORT_RUN_ID'],'run_attempt':int(os.environ['TRANSPORT_RUN_ATTEMPT']),
                'artifact_name':os.environ['TRANSPORT_NAME'],'expected_workflow':os.environ['TRANSPORT_WORKFLOW'],
                'expected_source_sha':os.environ['TRANSPORT_SOURCE'],'expected_identity':os.environ['TRANSPORT_IDENTITY'],
                'token_env':'GH_ARTIFACT_TOKEN','allowed_events':events,
                'allowed_branches':branches}
        if os.environ.get('TRANSPORT_RUN_HEAD_SHA'):
            config['run_head_sha']=os.environ['TRANSPORT_RUN_HEAD_SHA']
        if os.environ.get('TRANSPORT_PRODUCER_JOB'):
            config['producer_job']=os.environ['TRANSPORT_PRODUCER_JOB']
    elif operation=='package':
        manifest=os.environ['TRANSPORT_MANIFEST']
        candidate=load_json(confined_path(root,manifest))
        config={'schema':1,'operation':'package','manifest':manifest,'expected_identity':candidate['identity'],
                'expected_source_sha':candidate['source_sha'],'output_dir_env':'CI_BUNDLE_DIR'}
        os.environ['CI_BUNDLE_DIR']=str(Path(tempfile.mkdtemp(prefix='shared-ci-candidate-',dir=os.environ['RUNNER_TEMP']))/'bundle')
    else:
        raise PipelineError('Artifact operation must be fetch or package')
    result=execute(config,root)
    if not isinstance(result,dict) or result.get('status')!='passed':
        raise PipelineError('Artifact transport did not verify a retained candidate')
    if operation=='package':
        result['manifest']=config['manifest']
    for key in ('manifest','candidate_identity','source_sha'):
        if not isinstance(result.get(key),str) or not result[key] or any(c in result[key] for c in '\r\n\x00'):
            raise PipelineError('Artifact transport returned an invalid selection output')
    with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
        for key in ('directory','manifest','candidate_identity','source_sha'):
            if key in result:stream.write(key.replace('_','-')+'='+str(result[key])+'\n')
    print(json.dumps({'status':result['status'],'operation':operation,'candidate_identity':result['candidate_identity']}))


if __name__=='__main__':
    try:main()
    except (PipelineError,ValueError,KeyError,OSError) as exc:
        print('Artifact transport failed: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
