#!/usr/bin/env python3
"""Render reviewed product caller recipes without account credentials or live calls."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'templates/callers'
SPEC = importlib.util.spec_from_file_location('shared_workflow_source', ROOT / 'scripts/generate-workflows.py')
ENGINE = importlib.util.module_from_spec(SPEC)
_previous_bytecode = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(ENGINE)
finally:
    sys.dont_write_bytecode = _previous_bytecode
Error = ENGINE.GenerationError
SHA = re.compile(r'[0-9a-f]{40}')
FILE = re.compile(r'[a-z][a-z0-9-]*\.yml')
PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*')
TOKEN = re.compile(r'@@([A-Z_]+)@@')
REFERENCE = re.compile(r'jumahamdan/github-actions/\.github/workflows/(v2-[a-z0-9-]+\.yml)@([0-9a-f]{40})')
PROFILE = {'node', 'python', 'deno', 'flutter', 'swift', 'php', 'shell', 'powershell'}
PROVIDER = {'aws', 'firebase', 'cloudflare', 'vercel', 'netlify'}
RECIPES = {'ci', 'promotion-checks', 'candidate-build', 'web-delivery', 'container-delivery',
           'android-staging', 'apple-staging', 'mobile-release', 'wordpress-package', 'container-build'}
COMMON = {'recipe', 'filename', 'binary_manifest', 'npm_tools', 'python_tools', 'tool_bundles'}
OPTIONS = {
    'ci': {'profile', 'quality_config', 'products_config', 'security', 'docs', 'mock_ai',
           'security_config', 'docs_config', 'mock_ai_config'},
    'promotion-checks': {'profile', 'quality_config', 'products_config', 'ci_workflow', 'security_config',
                         'quality_artifact_prefix', 'quality_producer_job', 'quality_branches'},
    'candidate-build': {'profile', 'quality_config', 'candidate_config'},
    'container-build': {'container_build_config', 'candidate_config'},
    'web-delivery': {'provider', 'branch', 'environment', 'delivery_config', 'producer_workflow'},
    'container-delivery': {'branch', 'environment', 'delivery_config', 'producer_workflow'},
    'android-staging': {'profile', 'quality_config', 'build_candidate_config', 'candidate_config',
                        'android_credentials_config', 'android_verify_config', 'store_config'},
    'apple-staging': {'apple_import_config', 'apple_credentials_config', 'store_config'},
    'mobile-release': {'platform', 'store_config', 'apple_credentials_config', 'producer_workflow'},
    'wordpress-package': {'wordpress_config', 'candidate_config'},
}
DEFAULTS = {
    'ci': {'filename': 'ci.yml', 'profile': 'node', 'quality_config': '.ci/quality.json',
           'products_config': '.ci/products.json', 'security': True, 'docs': True, 'mock_ai': False,
           'security_config': '.ci/security.json', 'docs_config': '.ci/docs.json', 'mock_ai_config': '.ci/ai-mock.json'},
    'promotion-checks': {'filename': 'promotion.yml', 'profile': 'node', 'quality_config': '.ci/quality.json',
                         'products_config': '.ci/products.json', 'ci_workflow': '.github/workflows/ci.yml',
                         'security_config': '.ci/security.json', 'quality_artifact_prefix': 'ci-quality',
                         'quality_producer_job': 'quality / run', 'quality_branches': ['develop']},
    'candidate-build': {'filename': 'build-candidate.yml', 'profile': 'node', 'quality_config': '.ci/build.json',
                        'candidate_config': '.ci/candidate.json'},
    'container-build': {'filename': 'build-candidate.yml', 'container_build_config': '.ci/container-build.json',
                        'candidate_config': '.ci/candidate-container.json'},
    'web-delivery': {'filename': 'staging.yml', 'provider': 'aws', 'branch': 'staging',
                     'environment': 'staging', 'delivery_config': '.ci/deploy-staging.json',
                     'producer_workflow': '.github/workflows/build-candidate.yml'},
    'container-delivery': {'filename': 'staging.yml', 'branch': 'staging', 'environment': 'staging',
                           'delivery_config': '.ci/container-deploy.json',
                           'producer_workflow': '.github/workflows/build-candidate.yml'},
    'android-staging': {'filename': 'staging.yml', 'profile': 'flutter', 'quality_config': '.ci/android-build.json',
                        'build_candidate_config': '.ci/candidate-build.json', 'candidate_config': '.ci/candidate-android.json',
                        'android_credentials_config': '.ci/credentials-android.json',
                        'android_verify_config': '.ci/android-verify.json', 'store_config': '.ci/play-internal.json'},
    'apple-staging': {'filename': 'staging.yml', 'apple_import_config': '.ci/apple-import.json',
                      'apple_credentials_config': '.ci/credentials-apple.json', 'store_config': '.ci/testflight.json'},
    'mobile-release': {'filename': 'release.yml', 'platform': 'apple', 'store_config': '.ci/store-release.json',
                       'apple_credentials_config': '.ci/credentials-apple.json', 'producer_workflow': '.github/workflows/staging.yml'},
    'wordpress-package': {'filename': 'staging.yml', 'wordpress_config': '.ci/wordpress.json',
                          'candidate_config': '.ci/candidate-wordpress.json'},
}


def _strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise Error('Duplicate configuration key: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs)
    except ValueError as exc:
        raise Error('Invalid caller configuration JSON: ' + str(exc)) from exc


def _path(value, name, *, empty=False):
    if empty and value == '':
        return value
    if (not isinstance(value, str) or len(value) > 240 or not PATH.fullmatch(value)
            or any(part in {'.', '..', '.git'} for part in value.split('/'))):
        raise Error('Expected a confined literal relative path for ' + name)
    return value


def options(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('recipe'), str) or raw['recipe'] not in RECIPES:
        raise Error('Select an explicit supported recipe')
    recipe = raw['recipe']
    unknown = set(raw) - COMMON - OPTIONS[recipe]
    if unknown:
        raise Error('Unknown ' + recipe + ' options: ' + ', '.join(sorted(unknown)))
    value = {**DEFAULTS[recipe], 'recipe': recipe, 'binary_manifest': '', 'npm_tools': '',
             'python_tools': '', 'tool_bundles': '', **raw}
    if not isinstance(value['filename'], str) or not FILE.fullmatch(value['filename']):
        raise Error('Caller filename must be a plain lower-case .yml basename')
    for name, item in value.items():
        if name.endswith('_config') or name in {'binary_manifest', 'npm_tools', 'python_tools'}:
            _path(item, name, empty=name in {'binary_manifest', 'npm_tools', 'python_tools'})
        elif name.endswith('_workflow'):
            if not isinstance(item, str) or not re.fullmatch(r'\.github/workflows/[a-z][a-z0-9-]*\.yml', item):
                raise Error('Producer workflow must be a literal .github/workflows/*.yml path')
    bundles = value['tool_bundles']
    if not isinstance(bundles, str):
        raise Error('tool_bundles must be a comma-separated built-in bundle list')
    selected = bundles.split(',') if bundles else []
    if len(selected) != len(set(selected)) or set(selected) - {'bundletool', 'android-build-tools', 'google-sdk', 'sonar'}:
        raise Error('Unknown or duplicate built-in tool bundle')
    if 'profile' in value and (not isinstance(value['profile'], str) or value['profile'] not in PROFILE):
        raise Error('Unknown language profile')
    for key in ('security', 'docs', 'mock_ai'):
        if key in value and type(value[key]) is not bool:
            raise Error(key + ' must be an explicit boolean')
    if 'provider' in value and (not isinstance(value['provider'], str) or value['provider'] not in PROVIDER):
        raise Error('Unknown web provider')
    if 'platform' in value and (not isinstance(value['platform'], str) or value['platform'] not in {'apple', 'android'}):
        raise Error('Store platform must be apple or android')
    if recipe == 'promotion-checks':
        if not isinstance(value['quality_artifact_prefix'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', value['quality_artifact_prefix']):
            raise Error('Quality artifact prefix must be an explicit product namespace')
        producer = value['quality_producer_job']
        if not isinstance(producer, str) or not producer.strip() or len(producer) > 200 or any(ord(c) < 32 for c in producer):
            raise Error('Quality producer job must be an explicit job name')
        branches = value['quality_branches']
        if (not isinstance(branches, list) or not branches or len(branches) > 100
                or any(not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', branch)
                       or branch.startswith(('refs/', '/')) or branch.endswith('/') or '..' in branch for branch in branches)
                or len(set(branches)) != len(branches)):
            raise Error('Quality branches must be unique explicit integration branches')
    if recipe in {'ci', 'promotion-checks'}:
        return value
    if recipe == 'mobile-release':
        value.update(branch='main', environment='production')
    elif recipe not in {'web-delivery', 'container-delivery'}:
        value.update(branch='staging', environment='staging')
    if not isinstance(value['branch'], str) or value['branch'] not in {'staging', 'main'}:
        raise Error('Delivery branch must be staging or main')
    if value['environment'] != {'staging': 'staging', 'main': 'production'}[value['branch']]:
        raise Error('Starter branch/environment pair must be staging/staging or main/production')
    return value


def substitute(node, values, *, references_only=False):
    if isinstance(node, list):
        return [substitute(item, values, references_only=references_only) for item in node]
    if isinstance(node, dict):
        if references_only:
            return {key: substitute(item, values) if key == '$ref' else
                    substitute(item, values, references_only=True) for key, item in node.items()}
        return {key: substitute(item, values) for key, item in node.items()}
    if not isinstance(node, str):
        return node
    if references_only:
        return node
    exact = TOKEN.fullmatch(node)
    if exact:
        if exact[1] not in values:
            raise Error('Unknown caller parameter: ' + exact[1])
        return copy.deepcopy(values[exact[1]])
    def replacement(match):
        value = values.get(match[1])
        if not isinstance(value, str):
            raise Error('Unknown or non-string embedded caller parameter: ' + match[1])
        return value
    result = TOKEN.sub(replacement, node)
    if '@@' in result:
        raise Error('Unresolved caller marker')
    return result


def _permissions_allow(actual, required):
    level = {'none': 0, 'read': 1, 'write': 2}
    return isinstance(actual, dict) and all(level.get(actual.get(k, 'none'), -1) >= level[v] for k, v in required.items())


def validate(workflow, revision, *, library=ROOT):
    """Validate callable contracts, permissions, job references and safe starter events."""
    if not isinstance(workflow, dict) or not isinstance(workflow.get('jobs'), dict) or not workflow['jobs']:
        raise Error('Caller must have jobs')
    events = workflow.get('on')
    if not isinstance(events, dict) or len(events) != 1 or not set(events) <= {'pull_request', 'workflow_dispatch'}:
        raise Error('Starters permit only an explicit PR route or manual delivery trigger')
    if 'pull_request' in events and events['pull_request'].get('branches') not in (['develop'], ['staging', 'main']):
        raise Error('Unexpected PR branch route')
    if 'workflow_dispatch' in events and len((events['workflow_dispatch'] or {}).get('inputs', {})) > 10:
        raise Error('Too many workflow_dispatch inputs')
    jobs = workflow['jobs']
    if 'required' not in jobs or jobs['required'].get('if') != '${{ always() }}':
        raise Error('The required gate must run even after failure')
    gate_needs = jobs['required'].get('needs')
    if set(gate_needs or []) != set(jobs) - {'required'}:
        raise Error('The required gate must cover every preceding job')
    edges = {}
    for name, job in jobs.items():
        match = REFERENCE.fullmatch(job.get('uses', ''))
        if not match or match[2] != revision:
            raise Error('Every caller job must use the selected immutable shared-library revision')
        path = Path(library) / '.github/workflows' / match[1]
        if not path.is_file():
            raise Error('Unknown reusable workflow: ' + match[1])
        target = ENGINE.load_yaml(path)
        contract = target.get('on', {}).get('workflow_call')
        if not isinstance(contract, dict):
            raise Error('Selected workflow is not callable')
        supplied = job.get('with', {})
        declared = contract.get('inputs', {})
        if not isinstance(supplied, dict) or set(supplied) - set(declared):
            raise Error('Unknown reusable input for ' + name)
        for key, spec in declared.items():
            if spec.get('required') and key not in supplied and 'default' not in spec:
                raise Error('Missing reusable input: ' + name + '.' + key)
            if key in supplied:
                value = supplied[key]
                if isinstance(value, str) and '${{' in value:
                    continue
                typ = spec.get('type')
                valid = (isinstance(value, str) if typ == 'string' else type(value) is bool if typ == 'boolean'
                         else type(value) in (int, float) if typ == 'number' else False)
                if not valid:
                    raise Error('Wrong reusable input type: ' + name + '.' + key)
        secret_map = job.get('secrets', {})
        secrets = contract.get('secrets', {})
        if not isinstance(secret_map, dict) or set(secret_map) - set(secrets):
            raise Error('Only explicit declared secret mappings are allowed')
        for key, spec in secrets.items():
            if spec.get('required') and key not in secret_map:
                raise Error('Missing required named secret: ' + key)
        for value in secret_map.values():
            if not isinstance(value, str) or not re.fullmatch(r'\$\{\{ secrets\.[A-Z][A-Z0-9_]* \}\}', value):
                raise Error('Secrets must reference existing named GitHub secrets')
        if not _permissions_allow(job.get('permissions', workflow.get('permissions')), target.get('permissions', {})):
            raise Error('Caller permissions cannot satisfy ' + name)
        for target_job in target['jobs'].values():
            if target_job.get('permissions') and not _permissions_allow(job.get('permissions', workflow.get('permissions')), target_job['permissions']):
                raise Error('Caller permissions cannot satisfy nested permissions')
        needs = job.get('needs', [])
        if not isinstance(needs, list) or len(needs) != len(set(needs)) or set(needs) - set(jobs) or name in needs:
            raise Error('Invalid job dependency for ' + name)
        edges[name] = needs
        encoded = json.dumps(job)
        for producer, output in re.findall(r'needs\.([a-z][a-z0-9-]*)\.outputs\.([a-z][a-z0-9-]*)', encoded):
            if producer not in needs:
                raise Error('Output producer must be a direct dependency: ' + producer)
            producer_path = REFERENCE.fullmatch(jobs[producer]['uses'])[1]
            outputs = ENGINE.load_yaml(Path(library) / '.github/workflows' / producer_path)['on']['workflow_call'].get('outputs', {})
            if output not in outputs:
                raise Error('Unknown reusable output: ' + producer + '.' + output)
    def visit(name, stack):
        if name in stack:
            raise Error('Caller job dependency cycle')
        for need in edges[name]:
            visit(need, stack + [name])
    for name in jobs:
        visit(name, [])
    return workflow


def render(config, revision, *, source=SOURCE, library=ROOT):
    if not isinstance(revision, str) or not SHA.fullmatch(revision) or revision == '0' * 40:
        raise Error('An exact nonzero 40-character library SHA is required')
    if not isinstance(config, dict) or set(config) != {'schema', 'repository', 'workflows'} or type(config['schema']) is not int or config['schema'] != 1:
        raise Error('Caller configuration requires schema: 1, repository and workflows')
    if not isinstance(config['repository'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['repository']):
        raise Error('Select the actual product repository as owner/name')
    if not isinstance(config['workflows'], list) or not config['workflows'] or len(config['workflows']) > 20:
        raise Error('Select between one and twenty explicit caller workflows')
    fragments = ENGINE.load_fragments(Path(source) / 'fragments')
    result = {}
    for raw in config['workflows']:
        settings = options(raw)
        recipe, filename = settings['recipe'], settings['filename']
        target = '.github/workflows/' + filename
        if target in result:
            raise Error('Two recipes select the same output workflow: ' + filename)
        policy_path = '.ci/callers/' + filename[:-4] + '-required.json'
        reuse_path = '.ci/callers/' + filename[:-4] + '-reuse.json'
        values = {key.upper(): value for key, value in settings.items()}
        values.update(LIBRARY_SHA=revision, GATE_CONFIG=policy_path, REUSE_CONFIG=reuse_path,
                      GATE_NEEDS=[], VARIABLE_PREFIX='STAGING' if settings.get('branch') == 'staging' else 'PRODUCTION',
                      STAGE_LABEL='Staging' if settings.get('branch') == 'staging' else 'Release',
                      PLATFORM_LABEL='Apple' if settings.get('platform') == 'apple' else 'Android')
        provider = settings.get('provider')
        values['PROVIDER_PERMISSIONS'] = {'contents': 'read', 'actions': 'read', **({'id-token': 'write'} if provider in {'aws', 'firebase'} else {})}
        original = ENGINE.load_yaml(Path(source) / 'recipes' / (recipe + '.yml'))
        # Reference names may select an enumerated provider/platform. Arbitrary paths/code cannot be substituted.
        original = substitute(original, values, references_only=True)
        expanded = ENGINE.expand(original, fragments)
        if recipe == 'ci':
            for option, job in [('security', 'security'), ('docs', 'docs'), ('mock_ai', 'mock-ai')]:
                if not settings[option]:
                    del expanded['jobs'][job]
        if recipe == 'mobile-release' and settings['platform'] == 'apple':
            expanded['jobs']['distribute']['secrets'] = {'ASC_PRIVATE_KEY_BASE64': '${{ secrets.ASC_PRIVATE_KEY_BASE64 }}'}
        workflow = substitute(expanded, values)
        if not settings['tool_bundles']:
            # Preserve provider-specific reusable defaults instead of overriding them with empty text.
            for job in workflow['jobs'].values():
                job.get('with', {}).pop('tool-bundles', None)
        workflow['jobs']['required']['needs'] = [name for name in workflow['jobs'] if name != 'required']
        validate(workflow, revision, library=library)
        header = ('# Generated from shared-library templates/callers/recipes/' + recipe + '.yml.\n'
                  '# Review product configuration, environments and credential mappings before adoption.\n')
        result[target] = header + yaml.safe_dump(workflow, sort_keys=False, allow_unicode=True, width=110)
        policy = {'schema': 1, 'checks': {name: True for name in workflow['jobs'] if name != 'required'}}
        if recipe == 'android-staging':
            # Play acknowledges submission without proving availability. Keep this run usable
            # as a candidate producer while production continues to require passed evidence.
            policy['staging_submissions'] = ['distribute']
        result[policy_path] = json.dumps(policy, indent=2) + '\n'
        if recipe == 'android-staging':
            result['.ci/callers/read-android-build-number.py'] = (Path(source) / 'helpers/read-android-build-number.py').read_text()
        if recipe == 'promotion-checks':
            result[reuse_path] = json.dumps({'schema': 1, 'quality_config': settings['quality_config'], 'max_age_seconds': 86400,
                'source': {'repository': config['repository'], 'workflow': settings['ci_workflow'],
                    'artifact_prefix': settings['quality_artifact_prefix'], 'producer_job': settings['quality_producer_job'],
                    'allowed_branches': settings['quality_branches'], 'allowed_events': ['pull_request']}}, indent=2) + '\n'
    return result


def check_sources(*, actionlint=False):
    """Render every recipe and provider/platform variant and check real callable contracts."""
    cases = [{'recipe': name} for name in sorted(RECIPES)]
    cases += [{'recipe': 'web-delivery', 'provider': provider, 'filename': 'release.yml', 'branch': 'main',
               'environment': 'production'} for provider in sorted(PROVIDER)]
    cases += [{'recipe': 'mobile-release', 'platform': 'android'},
              {'recipe': 'container-delivery', 'branch': 'main', 'environment': 'production'},
              {'recipe': 'ci', 'security': False, 'docs': False, 'mock_ai': True, 'profile': 'shell'}]
    for path in sorted(Path(SOURCE).glob('*.json')):
        render(_strict_json(path.read_text()), 'a' * 40)
    with tempfile.TemporaryDirectory(prefix='shared-ci-callers-') as directory:
        workflows = []
        for index, case in enumerate(cases):
            output = render({'schema': 1, 'repository': 'example/product', 'workflows': [case]}, 'a' * 40)
            for path, contents in output.items():
                if path.startswith('.github/workflows/'):
                    target = Path(directory) / (str(index) + '-' + Path(path).name)
                    target.write_text(contents)
                    workflows.append(str(target))
        if actionlint:
            subprocess.run(['actionlint', *workflows], cwd=ROOT, check=True)
    return {'recipes': len(RECIPES), 'rendered_variants': len(cases), 'contracts': 'passed', 'actionlint': actionlint}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Explicit JSON selection of recipes and product configuration paths')
    parser.add_argument('--library-sha', help='Reviewed immutable library commit to call')
    parser.add_argument('--output', help='Fresh output directory; existing directories are never overwritten')
    parser.add_argument('--check', action='store_true', help='Validate every shipped recipe/variant without provider calls')
    parser.add_argument('--actionlint', action='store_true', help='Also run installed actionlint on rendered variants')
    args = parser.parse_args(argv)
    try:
        if args.check:
            if args.config or args.library_sha or args.output:
                raise Error('--check validates shipped sources; do not combine it with render arguments')
            print(json.dumps(check_sources(actionlint=args.actionlint)))
            return 0
        if not args.config or not args.library_sha or not args.output or args.actionlint:
            raise Error('Rendering requires --config, --library-sha and --output; use --check --actionlint for lint')
        output = Path(args.output)
        if output.exists() or output.is_symlink():
            raise Error('Output must be a fresh directory; copy reviewed files into the product explicitly')
        files = render(_strict_json(Path(args.config).read_text()), args.library_sha)
        output.mkdir(parents=True)
        for relative, content in files.items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        print(json.dumps({'output': str(output.resolve()), 'files': len(files), 'library_sha': args.library_sha}))
        return 0
    except (Error, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('caller templates: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
