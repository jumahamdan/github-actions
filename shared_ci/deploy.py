"""Explicit, scoped cloud deployment contracts; provider rebuilds are labelled."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from .common import PipelineError, atomic_json, confined_path, fingerprint, load_json, register_secret, run, sha256_file, strict_keys, trusted_context
from .quality import argv, input_digest, parse_test_report, snapshot
from .health import _NoRedirect

PROVIDERS = {"aws-cdk", "aws-static", "firebase", "cloudflare", "vercel", "netlify"}
PROTECTED = {"AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::Cognito::UserPool", "AWS::KMS::Key", "AWS::RDS::DBInstance", "AWS::RDS::DBCluster"}
AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def _string(value, name, pattern=None):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) or (pattern and not re.fullmatch(pattern, value)):
        raise PipelineError(f"invalid {name}")
    return value


def _json(text):
    try:
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError) as exc:
        raise PipelineError("provider returned malformed JSON") from exc
    return value


def _jsonc(path):
    """Read provider JSON/JSONC or TOML without executing configuration code."""
    if Path(path).suffix == '.toml':
        return tomllib.loads(Path(path).read_text())
    text = Path(path).read_text(); output = []; index = 0; quoted = False; escape = False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escape: escape = False
            elif char == '\\': escape = True
            elif char == '"': quoted = False
            index += 1; continue
        if char == '"': quoted = True; output.append(char); index += 1; continue
        if text[index:index + 2] == '//':
            end = text.find('\n', index); index = len(text) if end < 0 else end; continue
        if text[index:index + 2] == '/*':
            end = text.find('*/', index + 2)
            if end < 0: raise PipelineError("unterminated provider configuration comment")
            output.append(' '); index = end + 2; continue
        # Permit trailing commas only outside quoted strings.
        if char == ',':
            look = index + 1
            while look < len(text) and text[look].isspace(): look += 1
            if look < len(text) and text[look] in '}]': index += 1; continue
        output.append(char); index += 1
    clean = ''.join(output)
    clean = re.sub(r'("(?:\\.|[^"\\])*")|,(?=\s*[}\]])', lambda match: match.group(1) or '', clean)
    return _json(clean)


def _require_env(names):
    for name in names:
        if not os.environ.get(name, '').strip():
            raise PipelineError(f"required credential variable is absent: {name}")


def _get_json(url, token, *, timeout=30):
    # URLs are constructed by provider code, never accepted as arbitrary credential destinations.
    register_secret(token)
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}), timeout=timeout) as response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024: raise PipelineError("provider response is too large")
            return _json(body.decode())
    except (HTTPError, URLError, OSError, UnicodeError) as exc:
        raise PipelineError("provider verification request failed") from exc


class Context:
    def __init__(self, config, root, scratch):
        strict_keys(config, {"schema", "provider", "operation", "environment", "allowed_refs", "working_directory", "inputs",
                             "expected_input_digest", "tools", "settings", "receipt", "timeout"},
                    required=("schema", "provider", "operation", "environment", "inputs", "expected_input_digest", "tools", "settings"))
        if type(config['schema']) is not int or config['schema'] != 1 or config['provider'] not in PROVIDERS:
            raise PipelineError("unsupported deployment schema/provider")
        self.config = config; self.root = Path(root).resolve(); self.scratch = Path(scratch)
        self.cwd = confined_path(self.root, config.get('working_directory', '.'))
        self.timeout = config.get('timeout', 900)
        if type(self.timeout) is not int or not 1 <= self.timeout <= 21600:
            raise PipelineError("deployment timeout must be between 1 and 21600 seconds")
        _string(config['environment'], 'environment', r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
        if config['operation'] != 'emulate':
            self.trust = trusted_context(config.get('allowed_refs'))
            if os.environ.get('DEPLOYMENT_ENVIRONMENT') != config['environment']:
                raise PipelineError("workflow environment differs from the deployment target")
        elif config['provider'] != 'firebase' or config['environment'] != 'test':
            raise PipelineError("emulator execution is only available for Firebase test environment")
        self.inputs = snapshot(self.root, config['inputs'])
        if not isinstance(config['expected_input_digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', config['expected_input_digest']) or input_digest(self.inputs) != config['expected_input_digest']:
            raise PipelineError("deployment inputs do not match the selected candidate")
        self.env_names = (); self.extra = {"HOME": str(self.scratch), "XDG_CONFIG_HOME": str(self.scratch / 'config'), "XDG_DATA_HOME": str(self.scratch / 'data')}
        self.tools = {}; self.tool_versions = {}
        if not isinstance(config['tools'], dict): raise PipelineError("tools must be an object")
        if config.get('receipt'):
            receipt = confined_path(self.root, config['receipt'], must_exist=False)
            if receipt.relative_to(self.root).as_posix() in self.inputs: raise PipelineError('deployment receipt cannot overwrite candidate inputs')
        self.settings = config['settings']
        if not isinstance(self.settings, dict): raise PipelineError("settings must be an object")

    def bound(self, path):
        path = Path(path)
        files = snapshot(self.root, [path.relative_to(self.root).as_posix()])
        if any(self.inputs.get(name) != digest for name, digest in files.items()):
            raise PipelineError('deployment file is outside the selected candidate inputs')
        return path

    def tool(self, name):
        if name not in self.tools:
            spec = self.config['tools'].get(name)
            strict_keys(spec, {"command", "version", "version_arguments"}, required=("command", "version"))
            command = argv(spec['command'])
            version = _string(spec['version'], 'tool version', r'\d+(?:\.\d+){1,4}(?:[-+][A-Za-z0-9.-]+)?')
            version_args = spec.get('version_arguments', ['--version'])
            if not isinstance(version_args, list) or not version_args or not all(isinstance(v, str) and v for v in version_args):
                raise PipelineError("invalid tool version arguments")
            result = run(command + version_args, cwd=self.cwd, extra_env=self.extra, timeout=60)
            if not re.search(r'(?<![\w.])' + re.escape(version) + r'(?![\w.])', result.stdout + result.stderr):
                raise PipelineError(f"installed {name} version differs from the pinned contract")
            self.tools[name] = command; self.tool_versions[name] = version
        return self.tools[name]

    def call(self, name, args, *, json_output=False, check=True):
        result = run(self.tool(name) + args, cwd=self.cwd, env_names=self.env_names, extra_env=self.extra, timeout=self.timeout, check=check)
        return _json(result.stdout) if json_output else result

    def aws(self, account, region):
        _string(account, 'AWS account', r'\d{12}'); _string(region, 'AWS region', r'[a-z]{2}(?:-[a-z]+)+-\d')
        _require_env(AWS_ENV); self.env_names = AWS_ENV
        self.extra.update({"AWS_REGION": region, "AWS_DEFAULT_REGION": region, "AWS_EC2_METADATA_DISABLED": "true",
                           "AWS_SHARED_CREDENTIALS_FILE": str(self.scratch / 'no-credentials'), "AWS_CONFIG_FILE": str(self.scratch / 'no-config')})
        identity = self.call('aws', ['sts', 'get-caller-identity', '--output', 'json'], json_output=True)
        if not isinstance(identity, dict) or identity.get('Account') != account or ':assumed-role/' not in identity.get('Arn', ''):
            raise PipelineError("AWS identity is not the configured account's assumed role")
        return identity['Arn']

    def google(self, project):
        _string(project, 'Firebase project', r'[a-z][a-z0-9-]{4,62}')
        _require_env(('GOOGLE_APPLICATION_CREDENTIALS',))
        if not Path(os.environ['GOOGLE_APPLICATION_CREDENTIALS']).is_file():
            raise PipelineError("Google credential file is absent")
        self.env_names = ('GOOGLE_APPLICATION_CREDENTIALS',)
        self.extra['CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE'] = os.environ['GOOGLE_APPLICATION_CREDENTIALS']
        self.extra.update({"GCLOUD_PROJECT": project, "CLOUDSDK_CORE_PROJECT": project})

    def google_token(self):
        token = self.call('gcloud', ['auth', 'application-default', 'print-access-token']).stdout.strip()
        if not token or any(c.isspace() for c in token): raise PipelineError("Google access token generation failed")
        register_secret(token); return token


def inspect_change_set(change_set, *, protected_types=PROTECTED, protected_logical_ids=()):
    if not isinstance(change_set, dict) or change_set.get('Status') != 'CREATE_COMPLETE' or change_set.get('NextToken'):
        raise PipelineError("change set is incomplete or not ready")
    changes = change_set.get('Changes')
    if not isinstance(changes, list): raise PipelineError("change set has no structured resource changes")
    inspected = []
    for change in changes:
        resource = change.get('ResourceChange', {}) if isinstance(change, dict) else {}
        logical = resource.get('LogicalResourceId'); kind = resource.get('ResourceType'); action = resource.get('Action')
        if not all(isinstance(v, str) and v for v in (logical, kind, action)) or action not in {'Add', 'Modify', 'Remove', 'Import', 'Dynamic'}:
            raise PipelineError("change set contains an unknown resource operation")
        if kind == 'AWS::CloudFormation::Stack': raise PipelineError("nested stack changes need explicit nested change-set inspection")
        if logical in protected_logical_ids or action == 'Dynamic':
            raise PipelineError("change set modifies an externally managed or unresolved resource")
        if kind in protected_types and (action == 'Remove' or (action == 'Modify' and resource.get('Replacement') != 'False')):
            raise PipelineError("change set removes or may replace a protected data resource")
        inspected.append({"logical_id": logical, "type": kind, "action": action, "replacement": resource.get('Replacement')})
    return inspected


def _ssm_metadata(ctx, names):
    records = {}
    for name in names:
        _string(name, 'SSM parameter name', r'/[A-Za-z0-9_./-]+')
        response = ctx.call('aws', ['ssm', 'describe-parameters', '--parameter-filters', 'Key=Name,Option=Equals,Values=' + name, '--output', 'json'], json_output=True)
        matches = [p for p in response.get('Parameters', []) if p.get('Name') == name]
        if len(matches) != 1 or type(matches[0].get('Version')) is not int:
            raise PipelineError("externally managed runtime parameter metadata is missing")
        records[name] = matches[0]['Version']
    return records


def _cdk(ctx):
    s = ctx.settings
    strict_keys(s, {"account", "region", "stack", "protected_types", "protected_logical_ids", "externally_managed_ssm_parameters"}, required=("account", "region", "stack", "externally_managed_ssm_parameters"))
    stack = _string(s['stack'], 'CDK stack', r'[A-Za-z][A-Za-z0-9_-]{0,127}')
    ctx.aws(s['account'], s['region'])
    runtime_names = s['externally_managed_ssm_parameters']
    if not isinstance(runtime_names, list): raise PipelineError("runtime parameter names must be an explicit list")
    before_runtime = _ssm_metadata(ctx, runtime_names)
    assembly = ctx.scratch / 'assembly'
    ctx.call('cdk', ['synth', stack, '--output', str(assembly), '--quiet'])
    manifest = load_json(assembly / 'manifest.json')
    artifact = manifest.get('artifacts', {}).get(stack)
    if not isinstance(artifact, dict) or artifact.get('environment') != f"aws://{s['account']}/{s['region']}":
        raise PipelineError("synthesized stack does not match the configured account/region")
    props = artifact.get('properties', {}); physical = props.get('stackName')
    _string(physical, 'CloudFormation stack name', r'[A-Za-z][A-Za-z0-9-]{0,127}')
    template = load_json(confined_path(assembly, props.get('templateFile', '')))
    protected_ids = s.get('protected_logical_ids', [])
    protected_types = s.get('protected_types', [])
    if not isinstance(protected_ids, list) or not isinstance(protected_types, list) or not all(isinstance(v, str) for v in protected_ids + protected_types):
        raise PipelineError("protected-resource selectors must be lists of strings")
    for logical, resource in template.get('Resources', {}).items():
        if resource.get('Type') == 'AWS::SSM::Parameter' and runtime_names:
            name = resource.get('Properties', {}).get('Name')
            if not isinstance(name, str) or name in runtime_names:
                raise PipelineError("synthesized stack would own an externally managed runtime override")
    ctx.call('cdk', ['diff', stack, '--app', str(assembly), '--change-set=false'])
    if ctx.config['operation'] == 'validate':
        return {"stack": physical, "assembly_sha256": fingerprint(snapshot(assembly, ['.'])), "operation": "synth_and_diff", "runtime_parameters_checked": len(runtime_names)}
    if ctx.config['operation'] != 'deploy': raise PipelineError("unsupported CDK operation")
    change_name = 'shared-ci-' + ctx.config['expected_input_digest'][:32]
    ctx.call('cdk', ['deploy', stack, '--app', str(assembly), '--exclusively', '--method=prepare-change-set', '--change-set-name', change_name, '--require-approval', 'never'])
    changes = ctx.call('aws', ['cloudformation', 'describe-change-set', '--stack-name', physical, '--change-set-name', change_name, '--output', 'json'], json_output=True)
    no_change = changes.get('Status') == 'FAILED' and not changes.get('Changes') and any(message in changes.get('StatusReason', '') for message in ("didn't contain changes", 'No updates are to be performed'))
    if no_change:
        inspected = []
    else:
        inspected = inspect_change_set(changes, protected_types=PROTECTED | set(protected_types), protected_logical_ids=protected_ids)
        change_id = _string(changes.get('ChangeSetId'), 'change-set identity')
        ctx.call('cdk', ['deploy', stack, '--app', str(assembly), '--exclusively', '--method=execute-change-set', '--change-set-name', change_name, '--require-approval', 'never'])
    observed = ctx.call('aws', ['cloudformation', 'describe-stacks', '--stack-name', physical, '--output', 'json'], json_output=True)
    stacks = observed.get('Stacks', [])
    if len(stacks) != 1 or stacks[0].get('StackStatus') not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE'}:
        raise PipelineError("CloudFormation stack did not finish successfully")
    if _ssm_metadata(ctx, runtime_names) != before_runtime:
        raise PipelineError("deployment changed an externally managed runtime override")
    return {"stack_id": _string(stacks[0].get('StackId'), 'stack identity'), "stack_status": stacks[0]['StackStatus'], "change_set_id": changes.get('ChangeSetId'),
            "changes": inspected, "no_changes": no_change, "assembly_sha256": fingerprint(snapshot(assembly, ['.'])), "runtime_parameters_preserved": len(runtime_names)}


def _static(ctx):
    s = ctx.settings
    strict_keys(s, {"account", "region", "bucket", "prefix", "directory", "distribution_id", "invalidate_paths", "delete_obsolete"}, required=("account", "region", "bucket", "directory", "distribution_id"))
    if ctx.config['operation'] != 'deploy': raise PipelineError("unsupported AWS static-site operation")
    ctx.aws(s['account'], s['region'])
    bucket = _string(s['bucket'], 'S3 bucket', r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
    distribution = _string(s['distribution_id'], 'CloudFront distribution', r'[A-Z0-9]{5,32}')
    prefix = s.get('prefix', '')
    if not isinstance(prefix, str) or prefix.startswith('/') or '..' in Path(prefix).parts or any(c in prefix for c in '\r\n'):
        raise PipelineError("invalid S3 target prefix")
    directory = confined_path(ctx.root, s['directory'])
    if not directory.is_dir() or directory == ctx.root: raise PipelineError("static-site upload must select a built-output directory")
    ctx.bound(directory)
    files = snapshot(directory, ['.'])
    if any(Path(p).name.startswith('.env') or '.git' in Path(p).parts for p in files):
        raise PipelineError("static output contains source-control or environment material")
    ctx.call('aws', ['s3api', 'head-bucket', '--bucket', bucket, '--expected-bucket-owner', s['account']])
    distribution_info = ctx.call('aws', ['cloudfront', 'get-distribution', '--id', distribution, '--output', 'json'], json_output=True)
    if f":{s['account']}:distribution/{distribution}" not in distribution_info.get('Distribution', {}).get('ARN', ''):
        raise PipelineError("CloudFront distribution does not belong to the configured account")
    command = ['s3', 'sync', str(directory), f"s3://{bucket}/{prefix.strip('/')}", '--only-show-errors', '--no-follow-symlinks']
    if type(s.get('delete_obsolete', False)) is not bool: raise PipelineError("delete_obsolete must be boolean")
    if s.get('delete_obsolete'): command.append('--delete')
    paths = s.get('invalidate_paths', ['/*'])
    if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p.startswith('/') or any(c in p for c in '\r\n') for p in paths):
        raise PipelineError("invalid CloudFront invalidation paths")
    origins = distribution_info.get('Distribution', {}).get('DistributionConfig', {}).get('Origins', {}).get('Items', [])
    if not any(origin.get('DomainName', '').startswith(bucket + '.s3') and origin.get('DomainName', '').endswith('.amazonaws.com') for origin in origins):
        raise PipelineError('CloudFront distribution does not reference the selected S3 bucket')
    ctx.call('aws', command)
    invalidation = ctx.call('aws', ['cloudfront', 'create-invalidation', '--distribution-id', distribution, '--paths', *paths, '--output', 'json'], json_output=True)
    identity = _string(invalidation.get('Invalidation', {}).get('Id'), 'CloudFront invalidation ID')
    ctx.call('aws', ['cloudfront', 'wait', 'invalidation-completed', '--distribution-id', distribution, '--id', identity])
    return {"bucket": bucket, "prefix": prefix, "artifact_sha256": fingerprint(files), "distribution_id": distribution, "invalidation_id": identity, "build_mode": "prebuilt", "publication_mode": "in_place_sync"}


def _firebase(ctx):
    s = ctx.settings
    strict_keys(s, {"project", "config", "components", "allow_provider_rebuild", "functions", "rule_files", "hosting_sites", "indexes_file", "database", "emulators", "test_command", "test_report"}, required=("project", "config"))
    project = _string(s['project'], 'Firebase project', r'[a-z][a-z0-9-]{4,62}')
    config_path = ctx.bound(confined_path(ctx.root, s['config'])); provider_config = _jsonc(config_path)
    if ctx.config['operation'] == 'emulate':
        if not project.startswith('demo-'): raise PipelineError("emulator tests require a demo- project to isolate production")
        emulators = s.get('emulators')
        if not isinstance(emulators, list) or not emulators or any(e not in {'firestore', 'storage', 'auth', 'functions', 'hosting', 'pubsub', 'database'} for e in emulators):
            raise PipelineError("explicit supported emulators are required")
        for name in emulators:
            declaration = provider_config.get('emulators', {}).get(name)
            if not isinstance(declaration, dict) or declaration.get('port') == 3000:
                raise PipelineError("emulator configuration is missing or selects protected port 3000")
        spec = s.get('test_report'); strict_keys(spec, {'path', 'format'}, required=('path', 'format'))
        report = confined_path(ctx.root, spec['path'], must_exist=False)
        if report.relative_to(ctx.root).as_posix() in ctx.inputs: raise PipelineError("emulator report cannot overwrite candidate input")
        report.parent.mkdir(parents=True, exist_ok=True); report.unlink(missing_ok=True)
        command = argv(s.get('test_command'))
        ctx.call('firebase', ['emulators:exec', '--project', project, '--config', str(config_path), '--only', ','.join(emulators), '--non-interactive', shlex.join(command)])
        summary = parse_test_report(report, spec['format'])
        return {"project": project, "build_mode": "emulator_test", "test_summary": summary, "emulators": emulators}
    ctx.google(project)
    if ctx.config['operation'] != 'deploy': raise PipelineError("unsupported Firebase operation")
    components = s.get('components')
    if not isinstance(components, list) or not components or len(components) != len(set(components)) or any(c not in {'functions', 'hosting', 'firestore:rules', 'firestore:indexes', 'storage'} for c in components):
        raise PipelineError("Firebase deployment must select explicit supported components")
    if 'functions' in components and s.get('allow_provider_rebuild') is not True:
        raise PipelineError("Functions deployment requires an explicit provider-rebuild exception")
    if 'functions' in components and not s.get('functions'): raise PipelineError("Functions deployment requires explicit deployed function identities")
    if 'hosting' in components and not s.get('hosting_sites'): raise PipelineError("Hosting deployment requires explicit site identities")
    if any(c in components for c in ('firestore:rules', 'storage')) and not s.get('rule_files'):
        raise PipelineError("rules deployment requires explicit release-to-source mapping")
    if 'firestore:indexes' in components and not s.get('indexes_file'): raise PipelineError("indexes deployment requires the declared indexes file")
    if 'functions' in components:
        configurations = provider_config.get('functions', [])
        if isinstance(configurations, dict): configurations = [configurations]
        if not configurations: raise PipelineError('Functions source configuration is absent')
        for function_config in configurations:
            source = function_config if isinstance(function_config, str) else function_config.get('source')
            ctx.bound(confined_path(config_path.parent, source))
    if 'hosting' in components:
        configurations = provider_config.get('hosting', [])
        if isinstance(configurations, dict): configurations = [configurations]
        if not configurations: raise PipelineError('Hosting output configuration is absent')
        for hosting in configurations:
            if hosting.get('predeploy') or hosting.get('source'):
                raise PipelineError('Hosting deployment requires prebuilt public output without a predeploy build hook')
            ctx.bound(confined_path(config_path.parent, hosting.get('public', '')))
    for component, provider_key, file_key in [('firestore:rules', 'firestore', 'rules'), ('firestore:indexes', 'firestore', 'indexes'), ('storage', 'storage', 'rules')]:
        if component not in components: continue
        declarations = provider_config.get(provider_key, [])
        if isinstance(declarations, dict): declarations = [declarations]
        if not declarations: raise PipelineError('selected Firebase component configuration is absent')
        for declaration in declarations:
            if not isinstance(declaration, dict) or not declaration.get(file_key):
                raise PipelineError('selected Firebase component has no declared file')
            ctx.bound(confined_path(config_path.parent, declaration[file_key]))
    if s.get('rule_files'):
        if not isinstance(s['rule_files'], dict): raise PipelineError('invalid rule release mapping')
        for release, source in s['rule_files'].items():
            _string(release, 'rule release', r'(?:cloud\.firestore(?:/[A-Za-z0-9()_-]+)?|firebase\.storage/[a-z0-9.-]+)')
            ctx.bound(confined_path(ctx.root, source))
    if 'firestore:indexes' in components:
        declared = _jsonc(ctx.bound(confined_path(ctx.root, s['indexes_file'])))
        if not isinstance(declared.get('indexes'), list) or not isinstance(declared.get('fieldOverrides', []), list):
            raise PipelineError('invalid Firestore indexes declaration')
    response = ctx.call('firebase', ['deploy', '--project', project, '--config', str(config_path), '--only', ','.join(components), '--non-interactive', '--json'], json_output=True)
    if not isinstance(response, dict) or response.get('status') != 'success': raise PipelineError("Firebase CLI did not report a successful deployment")
    identities = {}; token = None
    if 'functions' in components:
        for function in s['functions']:
            strict_keys(function, {'name', 'region'}, required=('name', 'region'))
            name = _string(function['name'], 'function name', r'[A-Za-z][A-Za-z0-9_-]{0,62}')
            region = _string(function['region'], 'function region', r'[a-z]+-[a-z0-9]+\d')
            observed = ctx.call('gcloud', ['functions', 'describe', name, '--gen2', '--region', region, '--project', project, '--format=json'], json_output=True)
            revision = observed.get('serviceConfig', {}).get('revision')
            if observed.get('state') != 'ACTIVE' or not isinstance(revision, str) or not revision:
                raise PipelineError("deployed Function has no active revision identity")
            identities['function:' + name] = revision
    if 'hosting' in components or any(c in components for c in ('firestore:rules', 'storage')):
        token = ctx.google_token()
    if 'hosting' in components:
        for site in s['hosting_sites']:
            _string(site, 'Firebase Hosting site', r'[a-z0-9][a-z0-9-]{3,62}')
            current = _get_json(f'https://firebasehosting.googleapis.com/v1beta1/sites/{site}/channels/live/releases?pageSize=1', token)
            releases = current.get('releases', [])
            if not releases or releases[0].get('version', {}).get('status') != 'FINALIZED': raise PipelineError("Hosting has no finalized live release")
            identities['hosting:' + site] = _string(releases[0].get('name'), 'Hosting release identity')
    if any(c in components for c in ('firestore:rules', 'storage')):
        mapping = s['rule_files']
        if not isinstance(mapping, dict) or not mapping: raise PipelineError("invalid rule release mapping")
        covered = set()
        for release, source in mapping.items():
            _string(release, 'rule release', r'(?:cloud\.firestore(?:/[A-Za-z0-9()_-]+)?|firebase\.storage/[a-z0-9.-]+)')
            component = 'firestore:rules' if release.startswith('cloud.firestore') else 'storage'
            if component not in components: raise PipelineError("rule verification selects an undeployed component")
            expected = ctx.bound(confined_path(ctx.root, source)).read_text()
            current = _get_json(f'https://firebaserules.googleapis.com/v1/projects/{project}/releases/{quote(release, safe="/")}', token)
            ruleset = current.get('rulesetName', '')
            if not ruleset.startswith(f'projects/{project}/rulesets/'): raise PipelineError("rules release belongs to another project or is missing")
            details = _get_json('https://firebaserules.googleapis.com/v1/' + ruleset, token)
            files = details.get('source', {}).get('files', [])
            if len(files) != 1 or files[0].get('content') != expected: raise PipelineError("deployed rules do not match the selected source")
            identities['rules:' + release] = ruleset; covered.add(component)
        if set(components) & {'firestore:rules', 'storage'} != covered: raise PipelineError("a deployed rules component has no verification")
    if 'firestore:indexes' in components:
        declared = _jsonc(ctx.bound(confined_path(ctx.root, s['indexes_file'])))
        wanted = declared.get('indexes')
        if not isinstance(wanted, list): raise PipelineError("invalid Firestore index declaration")
        database = _string(s.get('database', '(default)'), 'Firestore database', r'[A-Za-z0-9()_-]+')
        observed = ctx.call('gcloud', ['firestore', 'indexes', 'composite', 'list', '--project', project, '--database', database, '--format=json'], json_output=True)
        if not isinstance(observed, list): raise PipelineError("Firestore index verification returned invalid data")
        ready = []
        for wanted_index in wanted:
            matches = []
            for index in observed:
                name = index.get('name', '')
                fields = [f for f in index.get('fields', []) if f.get('fieldPath') != '__name__' or any(x.get('fieldPath') == '__name__' for x in wanted_index.get('fields', []))]
                if f"/collectionGroups/{wanted_index.get('collectionGroup')}/indexes/" in name and fields == wanted_index.get('fields') and index.get('queryScope') == wanted_index.get('queryScope'):
                    matches.append(index)
            if len(matches) != 1 or matches[0].get('state') != 'READY': raise PipelineError("required Firestore index is missing or not ready")
            ready.append(matches[0]['name'])
        identities['indexes'] = ready
        overrides = declared.get('fieldOverrides', [])
        if not isinstance(overrides, list): raise PipelineError('invalid Firestore field overrides')
        for override in overrides:
            strict_keys(override, {'collectionGroup', 'fieldPath', 'indexes', 'ttl'}, required=('collectionGroup', 'fieldPath'))
            group = _string(override['collectionGroup'], 'collection group', r'[^/\x00-\x1f]+')
            field = _string(override['fieldPath'], 'field path', r'[^/\x00-\x1f]+')
            actual = ctx.call('gcloud', ['firestore', 'indexes', 'fields', 'describe', field, '--collection-group', group, '--database', database, '--project', project, '--format=json'], json_output=True)
            expected_name = f'projects/{project}/databases/{database}/collectionGroups/{group}/fields/{field}'
            if actual.get('name') != expected_name: raise PipelineError('Firestore field identity differs from declaration')
            if 'indexes' in override:
                observed_indexes = actual.get('indexConfig', {}).get('indexes', [])
                normalized = []
                for index in observed_indexes:
                    fields = [item for item in index.get('fields', []) if item.get('fieldPath') == field]
                    if len(fields) != 1 or index.get('state') != 'READY': raise PipelineError('Firestore field index is not ready')
                    normalized.append({'queryScope': index.get('queryScope'), **{k: v for k, v in fields[0].items() if k != 'fieldPath'}})
                if sorted(normalized, key=fingerprint) != sorted(override['indexes'], key=fingerprint):
                    raise PipelineError('Firestore field indexes differ from declaration')
            if 'ttl' in override:
                if type(override['ttl']) is not bool: raise PipelineError('Firestore TTL declaration must be boolean')
                state = actual.get('ttlConfig', {}).get('state')
                if (override['ttl'] and state != 'ACTIVE') or (not override['ttl'] and state not in (None, 'STATE_UNSPECIFIED')):
                    raise PipelineError('Firestore field TTL has not reached the declared state')
            identities['field:' + group + '/' + field] = fingerprint(actual)
    return {"project": project, "components": components, "identities": identities, "provider_response_sha256": fingerprint(response),
            "build_mode": "same_source_provider_rebuild" if 'functions' in components else "configuration_or_prebuilt",
            "observation": "provider identities observed immediately after serialized deployment"}


def _cloudflare(ctx):
    s = ctx.settings
    strict_keys(s, {'account_id', 'worker', 'config', 'wrangler_environment', 'entrypoint', 'artifact_directory', 'version_id'}, required=('account_id', 'worker', 'config', 'wrangler_environment'))
    account = _string(s['account_id'], 'Cloudflare account', r'[0-9a-f]{32}')
    worker = _string(s['worker'], 'Worker name', r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}')
    environment = _string(s['wrangler_environment'], 'Wrangler environment', r'[A-Za-z0-9_-]+')
    _require_env(('CLOUDFLARE_API_TOKEN',)); ctx.env_names = ('CLOUDFLARE_API_TOKEN',)
    ctx.extra.update({'CLOUDFLARE_ACCOUNT_ID': account, 'WRANGLER_SEND_METRICS': 'false', 'CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV': 'false'})
    config_path = ctx.bound(confined_path(ctx.root, s['config'])); provider = _jsonc(config_path)
    selected = provider.get('env', {}).get(environment)
    if not isinstance(selected, dict): raise PipelineError("explicit Wrangler environment is absent from configuration")
    if provider.get('build', {}).get('command') or selected.get('build', {}).get('command'):
        raise PipelineError("prebuilt Worker deployment cannot run a custom build command")
    if selected.get('account_id', provider.get('account_id', account)) != account or selected.get('name', provider.get('name')) != worker:
        raise PipelineError("Wrangler worker/account configuration does not match target")
    flags = ['--config', str(config_path), '--env', environment, '--name', worker]
    if ctx.config['operation'] == 'deploy':
        directory = ctx.bound(confined_path(ctx.root, s.get('artifact_directory', '')))
        if not directory.is_dir() or directory == ctx.root: raise PipelineError('Worker artifact directory must be explicit built output')
        entry = ctx.bound(confined_path(ctx.root, s.get('entrypoint', '')))
        if not entry.is_relative_to(directory): raise PipelineError('Worker entrypoint is outside artifact directory')
        assets = selected.get('assets', provider.get('assets', {}))
        if assets.get('directory'): ctx.bound(confined_path(config_path.parent, assets['directory']))
        if not entry.is_file(): raise PipelineError("prebuilt Worker entrypoint is absent")
        response = ctx.call('wrangler', ['deploy', str(entry), '--no-bundle', *flags])
        match = re.search(r'Current Version ID:\s*([0-9a-f-]{36})', response.stdout + response.stderr)
        if not match: raise PipelineError("Wrangler did not report the deployed version identity")
        version = match.group(1)
    elif ctx.config['operation'] == 'promote':
        version = _string(s.get('version_id'), 'Worker version', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
        ctx.call('wrangler', ['versions', 'deploy', version + '@100%', '--yes', *flags])
    else: raise PipelineError("unsupported Cloudflare operation")
    deployments = ctx.call('wrangler', ['deployments', 'list', '--json', *flags], json_output=True)
    if not isinstance(deployments, list) or not deployments: raise PipelineError("Worker deployment identity is unavailable")
    active = max(deployments, key=lambda item: item.get('created_on', ''))
    versions = active.get('versions', [])
    if len(versions) != 1 or versions[0].get('version_id') != version or versions[0].get('percentage') != 100:
        raise PipelineError("active Worker deployment differs from the selected version")
    return {'worker': worker, 'account_id': account, 'version_id': version, 'deployment_id': _string(active.get('id'), 'Worker deployment ID'), 'build_mode': 'prebuilt'}


def _vercel(ctx):
    s = ctx.settings
    strict_keys(s, {'project_id', 'organization_id', 'target', 'build_mode', 'allow_rebuild', 'deployment_url'}, required=('project_id', 'organization_id', 'target', 'build_mode'))
    project = _string(s['project_id'], 'Vercel project ID', r'prj_[A-Za-z0-9]+')
    org = _string(s['organization_id'], 'Vercel organization ID', r'(?:team|user)_[A-Za-z0-9]+')
    target = _string(s['target'], 'Vercel target', r'[A-Za-z0-9_-]+')
    _require_env(('VERCEL_TOKEN',)); ctx.env_names = ('VERCEL_TOKEN',)
    ctx.extra.update({'VERCEL_ORG_ID': org, 'VERCEL_PROJECT_ID': project, 'VERCEL_TELEMETRY_DISABLED': '1', 'DO_NOT_TRACK': '1'})
    project_file = ctx.bound(ctx.cwd / '.vercel' / 'project.json')
    linked = load_json(project_file)
    if linked.get('projectId') != project or linked.get('orgId') != org:
        raise PipelineError("Vercel linked project differs from the configured target")
    def inspect(url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise PipelineError('invalid Vercel deployment URL')
        query = '?teamId=' + quote(org) if org.startswith('team_') else ''
        return _get_json('https://api.vercel.com/v13/deployments/' + quote(parsed.hostname, safe='') + query, os.environ['VERCEL_TOKEN'])
    if ctx.config['operation'] == 'promote':
        if target != 'production': raise PipelineError('Vercel promote targets production only')
        url = _string(s.get('deployment_url'), 'existing Vercel deployment URL')
        if urlsplit(url).scheme != 'https' or not urlsplit(url).hostname: raise PipelineError("invalid Vercel deployment URL")
        before = inspect(url)
        if before.get('projectId') != project or before.get('readyState') != 'READY': raise PipelineError("Vercel candidate does not belong to the project or is not ready")
        ctx.call('vercel', ['promote', url, '--yes'])
    elif ctx.config['operation'] == 'deploy':
        mode = s['build_mode']
        command = ['deploy', '--yes', '--target', target]
        if mode == 'prebuilt':
            confined_path(ctx.cwd, '.vercel/output/config.json'); ctx.bound(ctx.cwd / '.vercel/output'); command += ['--prebuilt', '--archive=tgz']
        elif mode == 'source':
            if s.get('allow_rebuild') is not True: raise PipelineError("Vercel source deployment requires an explicit rebuild exception")
        else: raise PipelineError("unsupported Vercel build mode")
        response = ctx.call('vercel', command)
        urls = [line.strip() for line in response.stdout.splitlines() if line.strip()]
        if len(urls) != 1 or urlsplit(urls[0]).scheme != 'https' or not urlsplit(urls[0]).hostname:
            raise PipelineError("Vercel did not return one deployment URL")
        url = urls[0]
    else: raise PipelineError("unsupported Vercel operation")
    observed = inspect(url)
    if observed.get('projectId') != project or observed.get('readyState') != 'READY' or not observed.get('id'):
        raise PipelineError("Vercel deployment identity/state does not match target")
    return {'project_id': project, 'deployment_id': observed['id'], 'url': url, 'target': target,
            'build_mode': 'existing_deployment' if ctx.config['operation'] == 'promote' else ('prebuilt' if s['build_mode'] == 'prebuilt' else 'provider_rebuild')}


def _netlify(ctx):
    s = ctx.settings
    strict_keys(s, {'site_id', 'directory', 'context', 'production', 'build_mode', 'allow_rebuild'}, required=('site_id', 'directory', 'context', 'production', 'build_mode'))
    if ctx.config['operation'] != 'deploy': raise PipelineError("unsupported Netlify operation")
    site = _string(s['site_id'], 'Netlify site ID', r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
    context = _string(s['context'], 'Netlify context', r'(?:production|deploy-preview|branch-deploy|dev|branch:[A-Za-z0-9_/-]+)')
    if type(s['production']) is not bool: raise PipelineError("Netlify production selection must be explicit boolean")
    _require_env(('NETLIFY_AUTH_TOKEN',)); ctx.env_names = ('NETLIFY_AUTH_TOKEN',)
    ctx.extra.update({'NETLIFY_SITE_ID': site, 'NETLIFY_TELEMETRY_DISABLED': '1', 'DO_NOT_TRACK': '1'})
    if s['build_mode'] == 'source':
        if s.get('allow_rebuild') is not True: raise PipelineError("Netlify source mode requires an explicit local-rebuild exception")
        ctx.call('netlify', ['build', '--context', context])
    elif s['build_mode'] != 'prebuilt': raise PipelineError("unsupported Netlify build mode")
    directory = confined_path(ctx.root, s['directory'])
    if not directory.is_dir() or directory == ctx.root: raise PipelineError("Netlify upload needs a built-output directory")
    if s['build_mode'] == 'prebuilt': ctx.bound(directory)
    artifact = fingerprint(snapshot(directory, ['.']))
    command = ['deploy', '--site', site, '--dir', str(directory), '--context', context, '--no-build', '--json']
    if s['production']: command.append('--prod')
    response = ctx.call('netlify', command, json_output=True)
    if response.get('site_id') != site or not response.get('deploy_id') or not response.get('deploy_url'):
        raise PipelineError("Netlify returned no deployment identity for the configured site")
    return {'site_id': site, 'deployment_id': response['deploy_id'], 'url': response['deploy_url'], 'artifact_sha256': artifact,
            'build_mode': 'prebuilt' if s['build_mode'] == 'prebuilt' else 'local_rebuild', 'production': s['production']}


def _snapshot(ctx):
    s = ctx.settings
    strict_keys(s, {'kind', 'project', 'account', 'region', 'baseline'}, required=('kind',))
    if ctx.config['provider'] == 'firebase' and s['kind'] == 'remote-config':
        ctx.google(s.get('project'))
        value = ctx.call('firebase', ['remoteconfig:get', '--project', s['project'], '--json'], json_output=True)
    elif ctx.config['provider'] == 'aws-cdk' and s['kind'] in {'ssm-metadata', 'alarms'}:
        ctx.aws(s.get('account'), s.get('region'))
        args = ['ssm', 'describe-parameters'] if s['kind'] == 'ssm-metadata' else ['cloudwatch', 'describe-alarms']
        value = ctx.call('aws', args + ['--output', 'json'], json_output=True)
    else: raise PipelineError("unsupported read-only configuration snapshot")
    # Configuration values can contain sensitive settings. Retain fingerprints only.
    current = {'configuration_sha256': fingerprint(value), 'snapshot_kind': s['kind']}
    if s.get('baseline'):
        baseline = load_json(confined_path(ctx.root, s['baseline']))
        if baseline.get('details', {}).get('configuration_sha256') != current['configuration_sha256']:
            raise PipelineError("provider configuration differs from the declared baseline")
    return current


def execute(config, root):
    with tempfile.TemporaryDirectory(prefix='shared-ci-deploy-') as scratch:
        ctx = Context(config, root, scratch)
        if config['operation'] == 'snapshot': details = _snapshot(ctx)
        elif config['provider'] == 'aws-cdk': details = _cdk(ctx)
        elif config['provider'] == 'aws-static': details = _static(ctx)
        elif config['provider'] == 'firebase': details = _firebase(ctx)
        elif config['provider'] == 'cloudflare': details = _cloudflare(ctx)
        elif config['provider'] == 'vercel': details = _vercel(ctx)
        else: details = _netlify(ctx)
        if snapshot(ctx.root, config['inputs']) != ctx.inputs:
            raise PipelineError("provider operation changed the selected candidate inputs")
        result = {'schema': 1, 'kind': 'deployment', 'status': 'passed', 'provider': config['provider'], 'operation': config['operation'],
                  'environment': config['environment'], 'input_digest': config['expected_input_digest'], 'inputs': ctx.inputs,
                  'tools': ctx.tool_versions, 'details': details}
        if config.get('receipt'):
            output = confined_path(ctx.root, config['receipt'], must_exist=False)
            if output.relative_to(ctx.root).as_posix() in ctx.inputs: raise PipelineError("deployment receipt cannot overwrite candidate inputs")
            atomic_json(output, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True); parser.add_argument('--root', default='.')
    args = parser.parse_args()
    try:
        result = execute(load_json(confined_path(args.root, args.config)), args.root)
        print(json.dumps({k: result[k] for k in ('status', 'provider', 'operation', 'environment')}))
    except (PipelineError, OSError, ValueError) as exc:
        print(f'deployment: {exc}', file=sys.stderr); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
