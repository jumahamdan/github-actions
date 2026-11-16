#!/usr/bin/env python3
"""Generate the caller's exact inputs, secret names and outputs from workflow metadata."""
import argparse
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def render():
    lines = ['# Reusable workflow contracts', '',
             'Generated from the callable workflows. Supply public account IDs, role ARNs, model IDs, environment names and paths through `with:`; map existing credentials explicitly through `secrets:`. Never put credential values in a configuration file or commit. Optional secrets are required when the selected operation uses that provider. See [credential ownership and rotation](credentials.md) and [integration](integration.md).', '',
             'Every workflow below also requires the caller permissions declared in its YAML; a called workflow cannot raise the caller’s token permissions. Environment protections and provider access must be configured in the consuming repository/account.', '']
    for path in sorted((ROOT / '.github/workflows').glob('v2-*.yml')):
        data = yaml.safe_load(path.read_text())
        contract = data.get('on', data.get(True, {})).get('workflow_call', {})
        lines += ['## ' + path.stem, '', '[Workflow source](../.github/workflows/' + path.name + ')', '',
                  '| Input | Required / default | Meaning |', '| --- | --- | --- |']
        for name, spec in contract.get('inputs', {}).items():
            default = 'required' if spec.get('required') else repr(spec.get('default', ''))
            lines.append('| `' + name + '` | ' + cell(default) + ' | ' + cell(spec.get('description', '')) + ' |')
        lines += ['', '**Named secrets:** ' + (', '.join('`' + name + '`' + (' (required)' if spec.get('required') else '')
                    for name, spec in contract.get('secrets', {}).items()) or 'none') + '.', '',
                  '**Outputs:** ' + (', '.join('`' + name + '`' for name in contract.get('outputs', {})) or 'none') + '.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = ROOT / 'docs/workflow-contracts.md'
    content = render()
    if args.check:
        if not target.is_file() or target.read_text() != content:
            raise SystemExit('Stale workflow contracts: run python3 scripts/workflow-contracts.py')
    else:
        target.write_text(content)


if __name__ == '__main__':
    main()
