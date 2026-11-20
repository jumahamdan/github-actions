#!/usr/bin/env python3
"""Keep the known-scope capability index tied to real examples and test methods."""
import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def render():
    data = json.loads((ROOT / 'docs/capabilities.json').read_text())
    ids = set()
    for row in data['services']:
        if row['id'] in ids:
            raise ValueError('Duplicate capability ID')
        ids.add(row['id'])
        for module in row['modules']:
            if not (ROOT / 'shared_ci' / (module + '.py')).is_file():
                raise ValueError('Missing implementation: ' + module)
        json.loads((ROOT / row['example']).read_text())
        for suite in row['tests']:
            tree = ast.parse((ROOT / suite['file']).read_text())
            suite['cases'] = sorted(node.name for node in ast.walk(tree)
                                    if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'))
            if not suite['cases']:
                raise ValueError('Empty contract test suite')
    intro = f'''# Known capability coverage

All {len(ids)} scoped service sections map to executable shared modules, representative configuration and named test cases in [the machine-readable ledger](capabilities.json). The ledger records contract/fixture proof; no live product account, paid device/model run or deployment is certified by these tests. Final library integration and independent review are tracked in the pull request.

Runtime-service rows use actual product-command execution, infrastructure change analysis, redacted configuration fingerprints and HTTP/DNS/TLS verification. Their application behavior and live account settings remain product-owned; these rows do not claim a service-specific adapter where the contract uses product commands.

Regenerate this index with `python3 scripts/capabilities.py`; self-CI checks that its paths and named tests remain current.

| ID | Service | Implementation / contract tests | Example | Adoption evidence |
| --- | --- | --- | --- | --- |
'''
    lines = []
    for row in data['services']:
        modules = ', '.join(f'[{m}](../shared_ci/{m}.py)' for m in row['modules'])
        tests = ', '.join(f'[tests](../{s["file"]})' for s in row['tests'])
        lines.append(f'| {row["id"]} | {row["service"]} | {modules}; {tests} | [config](../{row["example"]}) | {row["adoption"]} |')
    return {'docs/capabilities.json': json.dumps(data, indent=2) + '\n',
            'docs/capabilities.md': intro + '\n'.join(lines) + '\n'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for name, content in render().items():
        path = ROOT / name
        if args.check:
            if path.read_text() != content:
                raise SystemExit('Stale capability index: run python3 scripts/capabilities.py')
        else:
            path.write_text(content)


if __name__ == '__main__':
    main()
