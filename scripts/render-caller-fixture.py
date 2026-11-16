#!/usr/bin/env python3
"""Render the credential-free cross-repository contract caller at an exact library SHA."""
import argparse
import re
import shutil
from pathlib import Path
import yaml


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}',args.revision):raise SystemExit('An exact reviewed library commit is required')
    root=Path(__file__).resolve().parent.parent
    output=Path(args.output)
    if output.exists():raise SystemExit('Fixture output must be a fresh directory')
    shutil.copytree(root/'fixtures/caller',output)
    prefix='jumahamdan/github-actions/.github/workflows/'
    workflow={'name':'Shared library cross-repository contract','on':{'push':{'branches':['main']},'workflow_dispatch':None},
              'permissions':{'contents':'read','actions':'read'},'jobs':{
      'build':{'uses':prefix+'v2-checks.yml@'+args.revision,'with':{
        'profile':'shell','config':'.ci/quality.json','artifact-name':'synthetic-build','candidate-config':'.ci/candidate.json',
        'candidate-manifest':'build/candidate.json','candidate-retention-days':7}},
      'verify':{'needs':['build'],'uses':prefix+'v2-evidence.yml@'+args.revision,'with':{
        'config':'build/candidate.json','operation':'verify','artifact-name':'synthetic-verification',
        'artifact-run-id':'${{ github.run_id }}','artifact-run-attempt':'${{ github.run_attempt }}',
        'artifact-source-sha':'${{ needs.build.outputs.source-sha }}','artifact-identity':'${{ needs.build.outputs.candidate-identity }}',
        'artifact-workflow':'.github/workflows/contract.yml','artifact-input-name':'${{ needs.build.outputs.candidate-artifact-name }}',
        'artifact-producer-job':'build / run','artifact-allowed-branches':'["main"]'}}}}
    target=output/'.github/workflows/contract.yml';target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(workflow,sort_keys=False,width=110))
    (output/'README.md').write_text('# Shared workflow contract fixture\n\nSynthetic, credential-free cross-repository build and artifact-transfer test. No product code or provider deployments.\n')
    (output/'.gitignore').write_text('build/\n__pycache__/\n')


if __name__=='__main__':main()
