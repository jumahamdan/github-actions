#!/usr/bin/env python3
"""Dispatch a pinned shared module in the current Apple-hosted build."""
from __future__ import annotations

import importlib
import json
import os
import platform
import re
import sys
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LIBRARY))

from shared_ci.assurance import stamp
from shared_ci.common import PipelineError, atomic_json, cli_error, confined_path, load_json, run, sha256_file


def library_revision():
    expected = os.environ.get('SHARED_LIBRARY_REVISION', '')
    if not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise PipelineError('Apple hook requires an immutable library revision')
    actual = run(['git', 'rev-parse', 'HEAD'], cwd=LIBRARY).stdout.strip()
    if actual != expected:
        raise PipelineError('Executing Apple library differs from the selected revision')
    if run(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=LIBRARY).stdout.strip():
        raise PipelineError('Executing Apple library contains uncommitted changes')
    return actual


def trusted_check_digests():
    try:
        expected = json.loads(os.environ.get('SHARED_APPLE_CHECK_DIGESTS', '{}'))
    except json.JSONDecodeError as exc:
        raise PipelineError('Apple check digests must be a JSON object') from exc
    if (not isinstance(expected, dict) or not expected
            or any(not isinstance(path, str) or not path or not isinstance(digest, str)
                   or not re.fullmatch(r'[0-9a-f]{64}', digest) for path, digest in expected.items())):
        raise PipelineError('Apple candidate creation requires trusted check digests from prior hook outputs')
    return expected


def current_cloud_build(config):
    """Resolve only an explicitly selected Cloud build marker, never static pins."""
    if config.get('build') != 'workflow':
        return config
    build = os.environ.get('CI_BUILD_NUMBER', '')
    if not re.fullmatch(r'[1-9][0-9]*', build):
        raise PipelineError('Workflow build selection requires a positive integer CI_BUILD_NUMBER')
    return {**config, 'build': build}


def execute_hook(*, emit_summary=False):
    if platform.system() != 'Darwin' or os.environ.get('CI_XCODE_CLOUD', '').lower() != 'true':
        raise PipelineError('Apple hosted hook requires actual Xcode Cloud on a Mac')
    if os.environ.get('GITHUB_ACTIONS', '').lower() == 'true': raise PipelineError('Apple hook cannot run on a GitHub runner')
    revision = library_revision()
    source = Path(os.environ.get('CI_PRIMARY_REPOSITORY_PATH', '')).resolve()
    if run(['git', 'rev-parse', 'HEAD'], cwd=source).stdout.strip() != os.environ.get('CI_COMMIT'):
        raise PipelineError('Apple source does not match the current Cloud commit')
    name = os.environ.get('SHARED_APPLE_MODULE', 'mobile')
    if name not in {'mobile', 'monitoring', 'evidence', 'artifacts', 'apple_bridge'}: raise PipelineError('Unsupported Apple hook module')
    selection = os.environ.get('SHARED_APPLE_ROOT', 'source')
    if selection == 'source': root = source
    elif selection == 'archive':
        if os.environ.get('CI_XCODEBUILD_ACTION') != 'archive' or os.environ.get('CI_XCODEBUILD_EXIT_CODE') != '0':
            raise PipelineError('Archive root requires a successful current archive')
        root = Path(os.environ.get('CI_ARCHIVE_PATH', '')).resolve()
        if not root.is_dir() or root.suffix != '.xcarchive': raise PipelineError('Current archive root is absent')
    else: raise PipelineError('Apple hook root must be source or archive')
    config_path = confined_path(source, os.environ.get('SHARED_APPLE_CONFIG') or os.environ.get('SHARED_MOBILE_CONFIG', ''))
    output = confined_path(root, os.environ.get('SHARED_APPLE_OUTPUT') or os.environ.get('SHARED_MOBILE_OUTPUT', ''), must_exist=False)
    if output == config_path: raise PipelineError('Apple receipt cannot overwrite its configuration')
    if selection == 'archive' and output.relative_to(root).parts[0] != '.shared-ci-evidence':
        raise PipelineError('Archive evidence belongs in .shared-ci-evidence')
    if selection == 'source' and run(['git', 'ls-files', '--error-unmatch', '--', output.relative_to(source).as_posix()], cwd=source, check=False).returncode == 0:
        raise PipelineError('Apple receipt cannot overwrite tracked source')
    output.unlink(missing_ok=True)
    config = load_json(config_path)
    module = importlib.import_module('shared_ci.' + name)
    if name == 'mobile':
        if selection != 'source': raise PipelineError('Mobile inspection needs the source checkout root')
        if config.get('operation') == 'apple-verify':
            config = current_cloud_build(config)
        result = module.execute(config, root)
    elif name == 'monitoring': result = module.execute(config, root)
    elif name == 'apple_bridge':
        if selection != 'archive' or config.get('operation') != 'apple-export':
            raise PipelineError('Native Apple bridge supports current-archive apple-export only')
        result = module.execute(config, root)
    elif name == 'artifacts':
        if config.get('operation') != 'package': raise PipelineError('Apple hosted artifact operation supports package only')
        result = module.package(config, root)
    else:
        operation = os.environ.get('SHARED_APPLE_OPERATION', 'create')
        if operation == 'create':
            if config.get('library_sha') not in ('workflow', revision):
                raise PipelineError('Candidate library revision differs from the executing Apple helper')
            config = {**current_cloud_build(config), 'library_sha': revision}
            result = module.create(config, root, source_root=source, expected_checks=trusted_check_digests())
        elif operation == 'verify':
            result = module.verify(config, root, expected_library_sha=revision, source_root=source)
        else: raise PipelineError('Unsupported Apple candidate operation')
    # Product commands cannot silently modify the helper revision while running.
    if library_revision() != revision:
        raise PipelineError('Executing Apple library changed during the hook')
    # Always bind a check to the real checked-out source; an xcarchive is not a Git repository.
    result = stamp(result, name, config_path, source)
    atomic_json(output, result)
    if result.get('status') != 'passed': raise PipelineError('Apple operation did not produce passing evidence')
    if emit_summary:
        print(json.dumps({'status': 'passed', 'receipt_path': output.relative_to(root).as_posix(),
                          'receipt_sha256': sha256_file(output), 'root': selection,
                          'library_sha': revision, 'source_sha': os.environ['CI_COMMIT']}))
    return result


if __name__ == '__main__': cli_error(lambda: execute_hook(emit_summary=True))
