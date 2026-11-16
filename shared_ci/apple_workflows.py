"""Read-only comparison of complete exported Xcode Cloud workflow settings."""
from __future__ import annotations

from .common import PipelineError, confined_path, fingerprint, load_json, sha256_file, strict_keys

CONDITIONS = ('branchStartCondition', 'tagStartCondition', 'pullRequestStartCondition', 'scheduledStartCondition',
              'manualBranchStartCondition', 'manualTagStartCondition', 'manualPullRequestStartCondition')
ATTRIBUTES = {'name', 'description', 'isEnabled', 'clean', 'containerFilePath', 'actions', *CONDITIONS}
SERVER_ATTRIBUTES = {'lastModifiedDate', 'isLockedForEditing'}
RELATIONSHIPS = {'product': 'ciProducts', 'repository': 'scmRepositories', 'xcodeVersion': 'ciXcodeVersions', 'macOsVersion': 'ciMacOsVersions'}
ACTION_FIELDS = {'name', 'actionType', 'destination', 'buildDistributionAudience', 'testConfiguration', 'scheme', 'platform', 'isRequiredToPass'}


def _text(value, label):
    if not isinstance(value, str) or not value or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise PipelineError('Invalid workflow ' + label)
    return value


def normalize(document, product_id):
    if not isinstance(document, dict) or not isinstance(document.get('data'), list) or not document['data']:
        raise PipelineError('Workflow export must contain a nonempty data array')
    if document.get('links', {}).get('next'):
        raise PipelineError('Workflow export pagination is incomplete')
    workflows = {}
    for workflow in document['data']:
        if not isinstance(workflow, dict) or workflow.get('type') != 'ciWorkflows':
            raise PipelineError('Workflow export contains another resource type')
        identity = _text(workflow.get('id'), 'identity')
        if identity in workflows: raise PipelineError('Duplicate workflow ID')
        attributes = workflow.get('attributes')
        strict_keys(attributes, ATTRIBUTES | SERVER_ATTRIBUTES, {'name', 'isEnabled', 'clean', 'containerFilePath', 'actions'})
        for key in ('isEnabled', 'clean'):
            if type(attributes[key]) is not bool: raise PipelineError('Workflow flags must be explicit booleans')
        for key in ('name', 'containerFilePath'): _text(attributes[key], key)
        actions = attributes['actions']
        if not isinstance(actions, list) or not actions: raise PipelineError('Workflow actions cannot be empty')
        normalized_actions = {}
        for action in actions:
            strict_keys(action, ACTION_FIELDS, {'name', 'actionType', 'scheme', 'platform', 'isRequiredToPass'})
            name = _text(action['name'], 'action name')
            if name in normalized_actions: raise PipelineError('Duplicate workflow action name')
            if type(action['isRequiredToPass']) is not bool: raise PipelineError('Action required flag must be boolean')
            for key in ('actionType', 'scheme', 'platform'): _text(action[key], key)
            normalized_actions[name] = {key: action.get(key) for key in ACTION_FIELDS if key != 'name'}
        relations = workflow.get('relationships')
        strict_keys(relations, set(RELATIONSHIPS) | {'buildRuns'}, set(RELATIONSHIPS))
        normalized_relations = {}
        for key, resource_type in RELATIONSHIPS.items():
            relation = relations[key]
            if not isinstance(relation, dict) or not isinstance(relation.get('data'), dict):
                raise PipelineError('Workflow export needs complete relationship IDs')
            data = relation['data']
            if data.get('type') != resource_type: raise PipelineError('Workflow relationship has wrong resource type')
            normalized_relations[key] = _text(data.get('id'), key + ' ID')
        if normalized_relations['product'] != product_id: raise PipelineError('Workflow belongs to another product')
        normalized = {key: attributes.get(key) for key in ATTRIBUTES - {'actions', 'description'}}
        normalized['description'] = attributes.get('description') or ''
        for name in CONDITIONS:
            if normalized[name] is not None and not isinstance(normalized[name], dict):
                raise PipelineError('Workflow start condition must be an object or null')
        normalized['actions'] = normalized_actions
        workflows[identity] = {'attributes': normalized, 'relationships': normalized_relations}
    return workflows


def _differences(expected, actual, prefix=''):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            yield from _differences(expected.get(key), actual.get(key), prefix + '/' + key)
    elif expected != actual:
        yield {'path': prefix, 'expected_sha256': fingerprint(expected), 'actual_sha256': fingerprint(actual)}


def execute(config, root):
    strict_keys(config, {'operation', 'product_id', 'desired', 'actual'}, {'product_id', 'desired', 'actual'})
    product = _text(config['product_id'], 'product ID')
    desired = confined_path(root, config['desired']); actual = confined_path(root, config['actual'])
    expected = normalize(load_json(desired), product); observed = normalize(load_json(actual), product)
    drift = list(_differences(expected, observed))
    return {'schema': 1, 'kind': 'xcode-cloud-workflow-drift', 'status': 'failed' if drift else 'passed',
            'product_id': product, 'workflow_ids': sorted(expected), 'desired_export_sha256': sha256_file(desired),
            'actual_export_sha256': sha256_file(actual), 'normalized_desired_sha256': fingerprint(expected),
            'normalized_actual_sha256': fingerprint(observed), 'drift': drift, 'operation': 'apple-workflow-drift',
            'read_only': True, 'provider_state_fetched': False}
