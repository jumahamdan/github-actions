"""Install checksum-locked files and runtime trees without executing installers."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import stat
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .common import PipelineError, atomic_json, cli_error, fingerprint, load_json, sha256_file, strict_keys

DEFAULT_BOUNDS = {'max_download_bytes': 128 * 1024 * 1024, 'max_expanded_bytes': 1024 * 1024 * 1024, 'max_files': 50000}
MAX_BOUNDS = {'max_download_bytes': 512 * 1024 * 1024, 'max_expanded_bytes': 2 * 1024 * 1024 * 1024, 'max_files': 100000}
SOURCE_KEYS = {'url', 'sha256', 'format', 'strip_prefix'}


def _name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', value) or value in ('.', '..'):
        raise PipelineError('Invalid ' + label)
    return value


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        raise PipelineError('Invalid archive path')
    path = PurePosixPath(value)
    if value in ('.', './') or path.is_absolute() or '..' in path.parts or value.startswith('./') or '//' in value or str(path) != value.rstrip('/'):
        raise PipelineError('Archive path traversal or non-canonical name')
    return path.as_posix()


def _bounds(value):
    if not isinstance(value, dict): raise PipelineError('Tool bounds must be an object')
    strict_keys(value, DEFAULT_BOUNDS)
    result = {**DEFAULT_BOUNDS, **value}
    if any(type(n) is not int or not 1 <= n <= MAX_BOUNDS[key] for key, n in result.items()):
        raise PipelineError('Tool archive bound is outside the supported range')
    return result


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https': raise PipelineError('Tool download redirect must remain HTTPS')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(source, bounds, fetch):
    url = urllib.parse.urlsplit(source['url'])
    if url.scheme != 'https' or url.username or url.password or not url.hostname or url.fragment or url.query or re.search(r'(^|/)latest(/|$)', url.path):
        raise PipelineError('Tool URL must be pinned HTTPS without credentials/query')
    if not isinstance(source['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', source['sha256']):
        raise PipelineError('Tool checksum is required')
    if fetch:
        data = fetch(source['url'])
    else:
        chunks = []; size = 0; deadline = time.monotonic() + 120
        with urllib.request.build_opener(_HttpsRedirect()).open(source['url'], timeout=30) as response:
            while True:
                block = response.read(min(1024 * 1024, bounds['max_download_bytes'] + 1 - size))
                if not block: break
                chunks.append(block); size += len(block)
                if size > bounds['max_download_bytes'] or time.monotonic() > deadline:
                    raise PipelineError('Tool download exceeds size/time bounds')
        data = b''.join(chunks)
    if not isinstance(data, bytes) or len(data) > bounds['max_download_bytes'] or hashlib.sha256(data).hexdigest() != source['sha256']:
        raise PipelineError('Tool archive checksum mismatch or oversized download')
    return data


def _format(source):
    value = source.get('format')
    if value is None: value = 'zip' if urllib.parse.urlsplit(source['url']).path.endswith('.zip') else 'tar'
    if value not in {'tar', 'zip', 'raw'}: raise PipelineError('Unsupported tool archive format')
    return value


def _archive(data, source, bounds):
    """Yield verified metadata/readers; never call extract or extractall."""
    archive_type = _format(source); prefix = source.get('strip_prefix', '')
    if prefix: prefix = _relative(prefix).rstrip('/') + '/'
    try:
        archive = zipfile.ZipFile(io.BytesIO(data)) if archive_type == 'zip' else tarfile.open(fileobj=io.BytesIO(data), mode='r:*')
        with archive:
            members = archive.infolist() if archive_type == 'zip' else archive
            seen = set(); entries = []; size = 0
            for count, member in enumerate(members, 1):
                if count > bounds['max_files']: raise PipelineError('Archive has too many entries')
                full_name = _relative(member.filename if archive_type == 'zip' else member.name)
                if full_name in seen: raise PipelineError('Archive contains duplicate paths')
                seen.add(full_name)
                if archive_type == 'zip':
                    mode = member.external_attr >> 16; kind = stat.S_IFMT(mode)
                    if member.flag_bits & 1: raise PipelineError('Encrypted tool archive is unsupported')
                    directory = member.is_dir(); linked = kind == stat.S_IFLNK
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK) or (kind == stat.S_IFDIR and not directory) or (directory and kind not in (0, stat.S_IFDIR)):
                        raise PipelineError('Archive contains a special device/file')
                    member_size = member.file_size
                    reader = lambda m=member: archive.open(m)
                    link_target = None
                else:
                    mode = member.mode; directory = member.isdir(); linked = member.issym() or member.islnk()
                    if not (directory or member.isfile() or linked) or member.issparse():
                        raise PipelineError('Archive contains a special device/file')
                    member_size = member.size; reader = lambda m=member: archive.extractfile(m)
                    link_target = member.linkname if linked else None
                if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                    raise PipelineError('Archive contains privileged permission bits')
                if member_size < 0: raise PipelineError('Archive has invalid entry size')
                size += member_size
                if size > bounds['max_expanded_bytes']: raise PipelineError('Archive expansion exceeds bounds')
                if prefix:
                    if full_name == prefix.rstrip('/') and directory: continue
                    if not full_name.startswith(prefix): raise PipelineError('Archive entry is outside strip_prefix')
                    name = full_name[len(prefix):]
                else: name = full_name
                _relative(name)
                if linked and archive_type == 'zip':
                    if member_size > 4096: raise PipelineError('Archive link target is too large')
                    with reader() as stream: link_target = stream.read(4097).decode('utf-8')
                if linked:
                    if not isinstance(link_target, str) or not link_target or '\\' in link_target or ':' in link_target or any(ord(c) < 32 for c in link_target):
                        raise PipelineError('Invalid archive link target')
                    if PurePosixPath(link_target).is_absolute(): raise PipelineError('Absolute archive link target')
                    # Tar hardlinks are archive-root relative; symlinks are parent relative.
                    parts = [] if archive_type == 'tar' and member.islnk() else list(PurePosixPath(name).parent.parts)
                    raw_target = link_target
                    if archive_type == 'tar' and member.islnk() and prefix:
                        if not raw_target.startswith(prefix): raise PipelineError('Hardlink escapes selected archive prefix')
                        raw_target = raw_target[len(prefix):]
                    for part in PurePosixPath(raw_target).parts:
                        if part == '..':
                            if not parts: raise PipelineError('Archive link escapes runtime root')
                            parts.pop()
                        elif part != '.': parts.append(part)
                    link_target = _relative('/'.join(parts))
                entries.append((name, 'directory' if directory else 'link' if linked else 'file', member_size, bool(mode & 0o111), link_target, reader))
            # All paths/types validated before returning any content for installation.
            for entry in entries: yield entry
    except (tarfile.TarError, zipfile.BadZipFile, KeyError, UnicodeError, OSError) as exc:
        raise PipelineError('Invalid or unsupported tool archive') from exc


def _write_file(path, data, executable):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data or bool(path.stat().st_mode & 0o111) != executable:
            raise PipelineError('Tool destination already contains different content')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755 if executable else 0o644)
    with os.fdopen(fd, 'wb') as output: output.write(data)
    path.chmod(0o755 if executable else 0o644)


def _tree(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink(): raise PipelineError('Installed runtime contains a filesystem link')
        if not path.is_file() and not path.is_dir(): raise PipelineError('Installed runtime contains a special file')
        if path.is_file() and path != root / '.shared-ci-install.json':
            if stat.S_IMODE(path.stat().st_mode) not in (0o644, 0o755): raise PipelineError('Installed runtime permissions were changed')
            result[path.relative_to(root).as_posix()] = {'sha256': sha256_file(path), 'executable': bool(path.stat().st_mode & 0o111)}
    return result


def _install_tree(item, destination, bounds, fetch):
    entrypoints = item.get('entrypoints')
    if not isinstance(entrypoints, dict) or not entrypoints: raise PipelineError('Runtime needs explicit entrypoints')
    for name, target in entrypoints.items(): _name(name, 'entrypoint name'); _relative(target)
    env = item.get('environment', {})
    if not isinstance(env, dict) or any(not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key) or not isinstance(value, str) or any(ord(c) < 32 for c in value) or re.search(r'TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY', key) for key, value in env.items()):
        raise PipelineError('Invalid non-secret tool launcher environment')
    exclusions = item.get('exclude_paths', [])
    if not isinstance(exclusions, list) or len(exclusions) != len(set(exclusions)): raise PipelineError('Runtime exclusions must be unique paths')
    exclusions = [_relative(value) for value in exclusions]
    allow_links = item.get('materialize_internal_links', False)
    if type(allow_links) is not bool: raise PipelineError('Link materialization must be explicit boolean')
    sources = [{key: item[key] for key in SOURCE_KEYS if key in item}, *item.get('overlays', [])]
    for source in sources: strict_keys(source, SOURCE_KEYS, {'url', 'sha256'})
    identity = fingerprint(item)
    versions = destination / '.shared-tools'; versions.mkdir(exist_ok=True)
    if versions.is_symlink(): raise PipelineError('Tool version directory cannot be a link')
    final = versions / (item['name'] + '-' + item['version'] + '-' + identity[:16])
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=versions) as temporary:
        stage = Path(temporary); entries = {}; expanded = 0
        for source in sources:
            if _format(source) == 'raw': raise PipelineError('Runtime overlays must be tar or zip archives')
            data = _download(source, bounds, fetch)
            for name, kind, size, executable, target, reader in _archive(data, source, bounds):
                if any(name == excluded or name.startswith(excluded + '/') for excluded in exclusions): continue
                if name == '.shared-ci-install.json': raise PipelineError('Archive uses reserved installation metadata path')
                if name in entries: raise PipelineError('Runtime archives contain duplicate paths')
                if len(entries) >= bounds['max_files']: raise PipelineError('Runtime has too many entries')
                entries[name] = {'kind': kind, 'executable': executable, 'target': target}
                path = stage / name
                if any(entries.get(str(parent), {}).get('kind') in {'file', 'link'} for parent in PurePosixPath(name).parents if str(parent) != '.'):
                    raise PipelineError('Archive file is used as a parent directory')
                if kind == 'directory': path.mkdir(parents=True, exist_ok=True)
                elif kind == 'link':
                    if not allow_links: raise PipelineError('Archive links require reviewed materialization opt-in')
                else:
                    expanded += size
                    if expanded > bounds['max_expanded_bytes']: raise PipelineError('Runtime expansion exceeds bounds')
                    with reader() as stream: binary = stream.read(size + 1)
                    if len(binary) != size: raise PipelineError('Archive file size mismatch')
                    _write_file(path, binary, executable)
        def resolve(name, active):
            if name in active or len(active) > 64: raise PipelineError('Archive link cycle/depth exceeded')
            entry = entries.get(name)
            if not entry or entry['kind'] == 'directory': raise PipelineError('Archive link target is absent or not a regular file')
            if entry['kind'] == 'link': return resolve(entry['target'], active | {name})
            return name
        for name, entry in entries.items():
            if entry['kind'] != 'link': continue
            target = resolve(name, set()); origin = stage / target
            expanded += origin.stat().st_size
            if expanded > bounds['max_expanded_bytes']: raise PipelineError('Materialized runtime expansion exceeds bounds')
            _write_file(stage / name, origin.read_bytes(), entries[target]['executable'])
        metadata = item.get('metadata_files', [])
        if not isinstance(metadata, list): raise PipelineError('Runtime metadata files must be a list')
        for declaration in metadata:
            strict_keys(declaration, {'path', 'content', 'sha256'}, {'path', 'content', 'sha256'})
            name = _relative(declaration['path']); content = declaration['content']
            if name == '.shared-ci-install.json' or name in entries or not isinstance(content, str):
                raise PipelineError('Runtime metadata cannot replace an archive entry')
            binary = content.encode()
            if len(binary) > 1024 * 1024 or hashlib.sha256(binary).hexdigest() != declaration['sha256']:
                raise PipelineError('Runtime metadata checksum mismatch or oversized content')
            expanded += len(binary)
            if expanded > bounds['max_expanded_bytes'] or len(entries) >= bounds['max_files']:
                raise PipelineError('Runtime metadata exceeds archive bounds')
            _write_file(stage / name, binary, False); entries[name] = {'kind': 'file', 'executable': False}
        for target in entrypoints.values():
            path = stage / target
            if not path.is_file(): raise PipelineError('Runtime entrypoint is absent')
            path.chmod(0o755)
        record = {'schema': 1, 'manifest_sha256': identity, 'files': _tree(stage)}
        atomic_json(stage / '.shared-ci-install.json', record)
        if final.exists():
            if final.is_symlink() or load_json(final / '.shared-ci-install.json') != record or _tree(final) != record['files']:
                raise PipelineError('Previously installed runtime was changed')
        else:
            # Rename a completed tree, so launchers never expose partial extraction.
            os.rename(stage, final)
        for name, target in entrypoints.items():
            exports = ''.join('export ' + key + '=' + shlex.quote(value) + '\n' for key, value in sorted(env.items()))
            launcher = '#!/bin/sh\nset -eu\n' + exports + 'exec ' + shlex.quote(str(final / target)) + ' "$@"\n'
            _write_file(destination / name, launcher.encode(), True)
    return {'name': item['name'], 'version': item['version'], 'manifest_sha256': identity, 'files': len(record['files']), 'entrypoints': sorted(entrypoints)}


def install(manifest, destination, *, fetch=None):
    strict_keys(manifest, {'schema', 'tools', 'bounds'}, {'schema', 'tools'})
    if type(manifest['schema']) is not int or manifest['schema'] != 1 or not isinstance(manifest['tools'], list) or not manifest['tools']:
        raise PipelineError('Invalid tool manifest')
    bounds = _bounds(manifest.get('bounds', {}))
    destination = Path(destination).absolute()
    if any(path.is_symlink() for path in [destination, *destination.parents]): raise PipelineError('Tool destination cannot traverse filesystem links')
    destination.mkdir(parents=True, exist_ok=True)
    names = set(); outputs = set(); results = []
    for item in manifest['tools']:
        strict_keys(item, SOURCE_KEYS | {'name', 'version', 'member', 'executable', 'entrypoints', 'overlays', 'environment', 'materialize_internal_links', 'exclude_paths', 'metadata_files'}, {'name', 'version', 'url', 'sha256'})
        name = _name(item['name'], 'tool name'); _name(item['version'], 'tool version')
        if not re.fullmatch(r'\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?', item['version']): raise PipelineError('Tool version must be an exact numeric release')
        if name in names: raise PipelineError('Duplicate tool name')
        names.add(name)
        item_outputs = list(item.get('entrypoints', {name: ''}))
        if any(key in outputs for key in item_outputs): raise PipelineError('Duplicate tool entrypoint destination')
        outputs.update(item_outputs)
        if 'entrypoints' in item:
            if 'member' in item or 'executable' in item: raise PipelineError('Runtime layout cannot also select one member')
            results.append(_install_tree(item, destination, bounds, fetch)); continue
        if any(key in item for key in ('overlays', 'environment', 'materialize_internal_links', 'exclude_paths', 'metadata_files')):
            raise PipelineError('Runtime options require entrypoints')
        executable = item.get('executable', True)
        if type(executable) is not bool: raise PipelineError('Tool executable must be boolean')
        data = _download(item, bounds, fetch)
        if _format(item) == 'raw':
            if 'member' in item or 'strip_prefix' in item: raise PipelineError('Raw files cannot have archive members')
            binary = data
        else:
            wanted = _relative(item.get('member')); binary = None
            for member, kind, size, _, _, reader in _archive(data, item, bounds):
                if kind == 'link': raise PipelineError('Single-file tool archives cannot contain links')
                if member == wanted:
                    if kind != 'file': raise PipelineError('Tool member is not a regular file')
                    with reader() as stream: binary = stream.read(size + 1)
                    if len(binary) != size: raise PipelineError('Archive file size mismatch')
            if binary is None: raise PipelineError('Tool binary absent from archive')
        _write_file(destination / name, binary, executable)
        results.append({'name': name, 'version': item['version'], 'sha256': hashlib.sha256(binary).hexdigest(), 'executable': executable})
    return {'schema': 1, 'status': 'passed', 'tools': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True); parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(json.dumps(install(load_json(args.manifest), args.destination)))


if __name__ == '__main__': cli_error(main)
