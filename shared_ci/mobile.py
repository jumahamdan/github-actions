"""Pinned mobile preparation, candidate verification, and explicit device tests."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import plistlib
import platform
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .common import (PipelineError, atomic_json, cli_error, confined_path, fingerprint,
                     load_json, run, sha256_file, strict_keys, trusted_context)

TEST_LAB_RETIREMENT = dt.date(2027, 9, 30)
GCLOUD_VERSION = "582.0.0"  # Includes the inspected Preview command/result contracts.
GCLOUD_BETA_VERSION = "2026.08.21"


def _text(value, label, pattern=r"[A-Za-z0-9_.:/+-]+"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PipelineError(f"Invalid {label}")
    return value


def _integer(value, label, minimum=1, maximum=2100000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PipelineError(f"Invalid {label}")
    return value


def _json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PipelineError("Tool returned invalid JSON") from exc


def _mapping(value):
    if not isinstance(value, dict):
        raise PipelineError("Tool result field must be an object")
    return value


def _file(root, relative):
    path = confined_path(root, relative)
    if not path.is_file() or not path.stat().st_size:
        raise PipelineError("Required artifact is absent or empty")
    return path


def _cloud_context(root, *, archive=False):
    if platform.system() != "Darwin" or os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise PipelineError("Apple Cloud operations require the Apple-hosted Mac context")
    if os.environ.get("CI_XCODE_CLOUD", "").lower() != "true":
        raise PipelineError("Apple operation requires Xcode Cloud")
    commit = _text(os.environ.get("CI_COMMIT"), "Xcode Cloud commit", r"[0-9a-f]{40}")
    if Path(os.environ.get("CI_PRIMARY_REPOSITORY_PATH", "")).resolve() != Path(root).resolve():
        raise PipelineError("Xcode Cloud repository path mismatch")
    context = {"source_revision": commit, "build_id": _text(os.environ.get("CI_BUILD_ID"), "build id"),
               "build_number": _text(os.environ.get("CI_BUILD_NUMBER"), "build number", r"[1-9][0-9]*")}
    if archive and (os.environ.get("CI_XCODEBUILD_ACTION") != "archive" or
                    os.environ.get("CI_XCODEBUILD_EXIT_CODE") != "0"):
        raise PipelineError("A successful Xcode Cloud archive is required")
    return context


def validate_emergency_context(execution, source_root, *, build):
    """Validate an explicitly invoked operator Mac; never impersonate Xcode Cloud."""
    strict_keys(execution, {'mode', 'operator', 'reason', 'source_sha', 'ref', 'build_id',
                            'archive_receipt', 'archive_receipt_sha256'},
                {'mode', 'operator', 'reason', 'source_sha', 'ref', 'build_id'})
    if execution['mode'] != 'emergency-mac' or platform.system() != 'Darwin':
        raise PipelineError('Emergency Apple operation requires an actual Mac')
    if os.environ.get('GITHUB_ACTIONS', '').lower() == 'true' or os.environ.get('CI_XCODE_CLOUD', '').lower() == 'true':
        raise PipelineError('Emergency Mac context cannot run as GitHub Actions or Xcode Cloud')
    operator = _text(execution['operator'], 'manual operator', r'[A-Za-z0-9_.@+-]{1,100}')
    if os.environ.get('SHARED_APPLE_MANUAL_EVENT') != 'emergency-release' or os.environ.get('SHARED_APPLE_OPERATOR') != operator:
        raise PipelineError('Emergency Apple operation needs explicit matching manual event/operator')
    reason = execution['reason']
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or any(ord(c) < 32 for c in reason):
        raise PipelineError('Emergency release needs a bounded one-line reason')
    source = _text(execution['source_sha'], 'expected source commit', r'[0-9a-f]{40}')
    ref = _text(execution['ref'], 'manual source ref', r'refs/heads/[A-Za-z0-9_./-]+')
    build_id = _text(execution['build_id'], 'manual build ID', r'[A-Za-z0-9_.-]{1,100}')
    _text(build, 'manual build number', r'[1-9][0-9]*')
    if run(['git', 'rev-parse', 'HEAD'], cwd=source_root).stdout.strip() != source:
        raise PipelineError('Emergency Mac source differs from the selected commit')
    if run(['git', 'symbolic-ref', 'HEAD'], cwd=source_root).stdout.strip() != ref:
        raise PipelineError('Emergency Mac branch differs from the selected ref')
    if run(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=source_root).stdout.strip():
        raise PipelineError('Emergency Mac source must be clean; outputs must be ignored')
    tree = _text(run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source_root).stdout.strip(), 'source tree', r'[0-9a-f]{40}')
    return {'source_revision': source, 'source_sha': source, 'source_tree': tree, 'build_id': build_id,
            'build_number': build, 'execution_context': {'mode': 'emergency-mac', 'operator': operator, 'reason': reason, 'ref': ref}}


def _apple_context(config, root, *, archive=False):
    execution = config.get('execution')
    if execution is None:
        return {**_cloud_context(root, archive=archive), 'execution_context': {'mode': 'xcode-cloud', 'ref': os.environ.get('CI_GIT_REF', '')}}
    return validate_emergency_context(execution, root, build=config.get('build'))


def _archive_hash(archive):
    # Evidence is retained beside the payload without changing its immutable identity.
    return _tree_hash(archive, exclude=('.shared-ci-evidence',))


def validate_emergency_archive(execution, source_root, archive, *, build):
    context = validate_emergency_context(execution, source_root, build=build)
    proof_path = confined_path(source_root, execution.get('archive_receipt', ''))
    expected = _text(execution.get('archive_receipt_sha256'), 'archive proof checksum', r'[0-9a-f]{64}')
    if not proof_path.is_file() or sha256_file(proof_path) != expected:
        raise PipelineError('Emergency archive build proof is missing or changed')
    proof = load_json(proof_path)
    required = {'schema': 1, 'status': 'passed', 'operation': 'apple-emergency-archive',
                'source_sha': context['source_sha'], 'source_tree': context['source_tree'], 'build_id': context['build_id'],
                'build_number': build, 'execution_context': context['execution_context']}
    if any(proof.get(key) != value for key, value in required.items()):
        raise PipelineError('Emergency archive proof belongs to a different source/build/operator')
    if confined_path(source_root, proof.get('archive', '')) != Path(archive).resolve() or proof.get('archive_sha256') != _archive_hash(Path(archive)):
        raise PipelineError('Emergency archive does not match the successful build proof')
    return {**context, 'archive_sha256': proof['archive_sha256']}


def apple_emergency_archive(config, root):
    allowed = {'operation', 'execution', 'kind', 'xcode', 'locks', 'project', 'scheme', 'flutter', 'pods',
               'archive', 'configuration', 'signing', 'version', 'build', 'allowed_refs'}
    strict_keys(config, allowed, {'execution', 'kind', 'xcode', 'locks', 'project', 'scheme', 'archive',
                                 'configuration', 'signing', 'version', 'build', 'allowed_refs'})
    context = validate_emergency_context(config['execution'], root, build=config['build'])
    refs = config['allowed_refs']
    if not isinstance(refs, list) or context['execution_context']['ref'] not in refs:
        raise PipelineError('Manual archive ref is not authorized')
    archive = confined_path(root, config['archive'], must_exist=False)
    if archive.suffix != '.xcarchive' or archive.exists() or archive.is_symlink():
        raise PipelineError('Manual archive must use a new xcarchive output path')
    if config['signing'] not in ('unsigned', 'signed'): raise PipelineError('Invalid manual archive signing mode')
    _text(config['configuration'], 'build configuration', r'[A-Za-z0-9_. -]+')
    _text(config['version'], 'marketing version', r'[0-9]+(?:\.[0-9]+){1,2}')
    bootstrap = {key: value for key, value in config.items() if key in {'execution', 'kind', 'xcode', 'locks', 'project', 'scheme', 'flutter', 'pods', 'build'}}
    bootstrap['operation'] = 'apple-bootstrap'
    apple_bootstrap(bootstrap, root)
    project = confined_path(root, config['project']); flag = '-workspace' if project.suffix == '.xcworkspace' else '-project'
    with tempfile.TemporaryDirectory(prefix='shared-apple-derived-') as derived:
        command = ['xcodebuild', 'archive', flag, str(project), '-scheme', config['scheme'], '-configuration', config['configuration'],
                   '-archivePath', str(archive), '-derivedDataPath', derived, '-destination', 'generic/platform=iOS',
                   '-onlyUsePackageVersionsFromResolvedFile', '-disableAutomaticPackageResolution',
                   'CURRENT_PROJECT_VERSION=' + config['build'], 'MARKETING_VERSION=' + config['version'],
                   'CODE_SIGNING_ALLOWED=' + ('YES' if config['signing'] == 'signed' else 'NO')]
        run(command, cwd=root, env_names=('DEVELOPER_DIR',), timeout=21600)
    if not archive.is_dir(): raise PipelineError('Xcode did not produce the manual archive')
    after = validate_emergency_context(config['execution'], root, build=config['build'])
    if after != context: raise PipelineError('Source changed during manual archive')
    _locks(root, config['locks'])
    return {**context, 'kind': 'apple-emergency-archive', 'archive': config['archive'], 'archive_sha256': _archive_hash(archive),
            'signing_requested': config['signing'], 'distribution_ready': False, 'toolchain': config['xcode'],
            'command_sha256': fingerprint([arg if arg != derived else '<temporary-derived-data>' for arg in command]),
            'dependency_locks': config['locks'], 'built_at': dt.datetime.now(dt.timezone.utc).isoformat()}


def _locks(root, values):
    if not isinstance(values, dict) or not values:
        raise PipelineError("Explicit dependency locks are required")
    result = {}
    for name, digest in values.items():
        _text(digest, "lock SHA256", r"[0-9a-f]{64}")
        actual = sha256_file(_file(root, name))
        if actual != digest:
            raise PipelineError(f"Dependency lock changed: {name}")
        result[name] = actual
    return result


def apple_bootstrap(config, root):
    strict_keys(config, {"operation", "kind", "xcode", "locks", "project", "scheme", "flutter", "pods", "execution", "build"},
                {"kind", "xcode", "locks", "project", "scheme"})
    context = _apple_context(config, root)
    if config["kind"] not in ("native", "flutter"):
        raise PipelineError("Apple kind must be native or flutter")
    strict_keys(config["xcode"], {"version", "build"}, {"version", "build"})
    expected = f"Xcode {config['xcode']['version']}\nBuild version {config['xcode']['build']}"
    if run(["xcodebuild", "-version"], cwd=root).stdout.strip() != expected:
        raise PipelineError("Xcode version/build mismatch")
    locks = _locks(root, config["locks"])
    project = confined_path(root, config["project"])
    scheme = _text(config["scheme"], "scheme", r"[A-Za-z0-9_. -]+")
    # Credentials are absent; Xcode's proxy variables are required by its hosted network.
    inherited = ("DEVELOPER_DIR", "HTTP_PROXY", "HTTPS_PROXY")
    if config["kind"] == "flutter":
        flutter = strict_keys(config.get("flutter"), {"version", "revision", "directory"},
                              {"version", "revision", "directory"})
        observed = _json(run(["flutter", "--version", "--machine"], cwd=root,
                             env_names=inherited).stdout)
        observed = _mapping(observed)
        if (observed.get("frameworkVersion") != flutter["version"] or
                observed.get("frameworkRevision") != flutter["revision"]):
            raise PipelineError("Flutter version/revision mismatch")
        if not any(Path(name).name == "pubspec.lock" for name in locks):
            raise PipelineError("Flutter requires a pinned pubspec.lock")
        directory = confined_path(root, flutter["directory"])
        for args in (["precache", "--ios"], ["pub", "get", "--enforce-lockfile"],
                     ["build", "ios", "--release", "--no-codesign", "--config-only", "--no-pub"]):
            run(["flutter", *args], cwd=directory, env_names=inherited, timeout=900)
    elif "flutter" in config:
        raise PipelineError("Native bootstrap cannot use Flutter settings")
    if "pods" in config:
        pods = strict_keys(config["pods"], {"version", "directory"}, {"version", "directory"})
        if not {"Podfile.lock", "Gemfile.lock"} <= {Path(name).name for name in locks}:
            raise PipelineError("CocoaPods requires Podfile.lock and Gemfile.lock")
        directory = confined_path(root, pods["directory"])
        if run(["bundle", "exec", "pod", "--version"], cwd=directory).stdout.strip() != pods["version"]:
            raise PipelineError("CocoaPods version mismatch")
        run(["bundle", "exec", "pod", "install", "--deployment"], cwd=directory,
            env_names=inherited, extra_env={"BUNDLE_FROZEN": "true"}, timeout=900)
    flag = "-workspace" if project.suffix == ".xcworkspace" else "-project"
    if project.suffix not in (".xcworkspace", ".xcodeproj"):
        raise PipelineError("Expected an Xcode project or workspace")
    run(["xcodebuild", "-resolvePackageDependencies", flag, str(project), "-scheme", scheme,
         "-onlyUsePackageVersionsFromResolvedFile", "-disableAutomaticPackageResolution"],
        cwd=root, env_names=inherited, timeout=900)
    _locks(root, locks)
    return {**context, "dependency_locks": locks, "toolchain": config["xcode"], "kind": config["kind"]}


def _tree_hash(directory, *, exclude=()):
    entries = {}
    for path in sorted(directory.rglob("*")):
        if path.relative_to(directory).parts[0] in exclude: continue
        if path.is_symlink():
            # Apple frameworks may have internal symlinks; hash their target, forbid escape.
            if not path.resolve().is_relative_to(directory.resolve()):
                raise PipelineError("Artifact symlink escapes candidate directory")
            if path.resolve().relative_to(directory.resolve()).parts and path.resolve().relative_to(directory.resolve()).parts[0] in exclude:
                raise PipelineError("Payload symlink cannot target excluded evidence")
            entries[path.relative_to(directory).as_posix()] = {"symlink": os.readlink(path)}
        elif path.is_file():
            entries[path.relative_to(directory).as_posix()] = sha256_file(path)
    if not entries:
        raise PipelineError("Candidate directory is empty")
    return fingerprint(entries)


def _plist(path):
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise PipelineError("Required plist is invalid") from exc
    if not isinstance(value, dict):
        raise PipelineError("Expected plist dictionary")
    return value


def apple_verify(config, root):
    strict_keys(config, {"operation", "archive", "signing", "targets", "version", "build", "allowed_refs", "execution"},
                {"archive", "signing", "targets", "version", "build"})
    context = _apple_context(config, root, archive=True)
    if config["build"] != context["build_number"]:
        raise PipelineError("Manifest build differs from Xcode Cloud build")
    if config["signing"] not in ("unsigned", "signed"):
        raise PipelineError("Signing must be unsigned or signed")
    if config["signing"] == "signed":
        refs = config.get("allowed_refs", [])
        if (not isinstance(refs, list) or not refs or
                any(not isinstance(ref, str) or not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", ref) for ref in refs) or
                (context["execution_context"]["mode"] == "xcode-cloud" and os.environ.get("CI_START_CONDITION") not in ("push", "manual", "manual_rebuild")) or
                context["execution_context"]["ref"] not in refs):
            raise PipelineError("Signed candidates require an authorized Xcode Cloud branch")
    # Archive paths are outside the source checkout on Xcode Cloud; trust only the Cloud-owned path.
    if context['execution_context']['mode'] == 'xcode-cloud':
        archive_env = Path(os.environ.get("CI_ARCHIVE_PATH", "")).resolve()
        archive = archive_env if config["archive"] == "$CI_ARCHIVE_PATH" else confined_path(root, config["archive"])
        if archive != archive_env: raise PipelineError("Candidate must be the current Xcode Cloud archive")
    else:
        archive = confined_path(root, config['archive'])
        context = validate_emergency_archive(config['execution'], root, archive, build=config['build'])
    if not archive.is_dir() or archive.suffix != '.xcarchive': raise PipelineError('Apple candidate must be an xcarchive directory')
    targets = config["targets"]
    if not isinstance(targets, list) or not targets:
        raise PipelineError("At least one target is required")
    expected_paths, ids, records, symbols, artifacts = set(), set(), [], [], {}
    for target in targets:
        strict_keys(target, {"path", "bundle_id", "executable", "plist", "entitlements", "dsym"},
                    {"path", "bundle_id", "executable", "plist", "entitlements", "dsym"})
        if any(Path(target[key]).parts[0] == ".shared-ci-evidence" for key in ("path", "dsym")):
            raise PipelineError("Apple payload cannot use the reserved evidence directory")
        path = confined_path(archive, target["path"])
        if path.suffix not in (".app", ".appex") or target["path"] in expected_paths or target["bundle_id"] in ids:
            raise PipelineError("Duplicate or invalid embedded target")
        expected_paths.add(target["path"]); ids.add(target["bundle_id"])
        info = _plist(path / "Info.plist")
        expected = {"CFBundleIdentifier": target["bundle_id"], "CFBundleVersion": config["build"],
                    "CFBundleShortVersionString": config["version"], "CFBundleExecutable": target["executable"]}
        if not isinstance(target["plist"], dict) or not isinstance(target["entitlements"], dict):
            raise PipelineError("Expected target plist/entitlement dictionaries")
        if set(expected) & set(target["plist"]):
            raise PipelineError("Target cannot override identity/version fields")
        expected.update(target["plist"])
        if any(info.get(key) != value for key, value in expected.items()):
            raise PipelineError("Embedded target plist identity/version/extension mismatch")
        executable = _file(path, target["executable"])
        if not executable.stat().st_mode & 0o111:
            raise PipelineError("Embedded target executable is not executable")
        dsym = confined_path(archive, target["dsym"])
        dwarf = _file(dsym, "Contents/Resources/DWARF/" + target["executable"])
        uuid_args = ["dwarfdump", "--uuid"]
        binary_uuids = run([*uuid_args, str(executable)], cwd=root).stdout
        symbol_uuids = run([*uuid_args, str(dwarf)], cwd=root).stdout
        parse_uuids = lambda text: set(re.findall(r"UUID: ([0-9A-Fa-f-]{36}) \(([^)]+)\)", text))
        if not parse_uuids(binary_uuids) or parse_uuids(binary_uuids) != parse_uuids(symbol_uuids):
            raise PipelineError("dSYM UUIDs do not match target executable")
        if config["signing"] == "signed":
            run(["codesign", "--verify", "--strict", str(path)], cwd=root)
            result = run(["codesign", "-d", "--entitlements", ":-", str(path)], cwd=root)
            text = result.stdout + result.stderr
            start, end = text.find("<?xml"), text.find("</plist>")
            try:
                entitlements = plistlib.loads(text[start:end + len("</plist>")].encode())
            except Exception as exc:
                raise PipelineError("Cannot read signed entitlements") from exc
            if (not isinstance(entitlements, dict) or entitlements != target["entitlements"] or
                    entitlements.get("get-task-allow") is True):
                raise PipelineError("Signed entitlements differ from the product manifest")
        for uuid, arch in sorted(parse_uuids(binary_uuids)):
            symbols.append({"binary_path": executable.relative_to(archive).as_posix(), "symbol_path": dwarf.relative_to(archive).as_posix(),
                            "binary_sha256": sha256_file(executable), "symbol_sha256": sha256_file(dwarf),
                            "uuid": uuid.upper(), "arch": arch})
        for artifact in (executable, dwarf):
            artifacts[artifact.relative_to(archive).as_posix()] = {"sha256": sha256_file(artifact), "bytes": artifact.stat().st_size}
        records.append({"bundle_id": target["bundle_id"], "path": target["path"],
                        "executable_sha256": sha256_file(executable), "dsym_sha256": _tree_hash(dsym)})
    actual_paths = {p.relative_to(archive).as_posix() for p in (archive / "Products").rglob("*")
                    if p.is_dir() and p.suffix in (".app", ".appex")}
    if actual_paths != expected_paths:
        raise PipelineError("Undeclared or missing app/extension in archive")
    host_targets = [t for t in targets if Path(t["path"]).parent.as_posix() == "Products/Applications"]
    if len(host_targets) != 1:
        raise PipelineError("Archive must contain exactly one host application")
    return {**context, "source_sha": context["source_revision"], "kind": "apple",
            "application": {"id": host_targets[0]["bundle_id"], "version": config["version"], "build": config["build"]},
            "artifacts": artifacts, "artifact_root": "$CI_ARCHIVE_PATH" if context["execution_context"]["mode"] == "xcode-cloud" else config["archive"], "debug_symbols": symbols,
            "archive_sha256": _archive_hash(archive), "targets": records,
            "manifest_sha256": fingerprint(config), "signing": config["signing"],
            "distribution_ready": config["signing"] == "signed", "version": config["version"]}


def next_build(config, root):
    strict_keys(config, {"operation", "product", "remote_highest", "ledger", "serialization_key"},
                {"product", "remote_highest", "ledger", "serialization_key"})
    product = _text(config["product"], "product")
    key = _text(config["serialization_key"], "serialization key")
    if os.environ.get("MOBILE_BUILD_SERIALIZATION_KEY") != key:
        raise PipelineError("Caller must hold its product-wide allocation lock")
    remote = load_json(_file(root, config["remote_highest"]))
    strict_keys(remote, {"product", "highest", "checked_at"}, {"product", "highest", "checked_at"})
    if remote["product"] != product:
        raise PipelineError("Remote build-number evidence belongs to a different product")
    highest = _integer(remote["highest"], "remote highest", minimum=0)
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(remote["checked_at"])).total_seconds()
    except (ValueError, TypeError) as exc:
        raise PipelineError("Remote build-number check needs a timezone timestamp") from exc
    if not 0 <= age <= 300:
        raise PipelineError("Remote build-number evidence must be at most five minutes old")
    ledger = confined_path(root, config["ledger"], must_exist=False)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock = ledger.with_suffix(ledger.suffix + ".lock")
    if lock.is_symlink():
        raise PipelineError("Build lock cannot be a symlink")
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        previous = load_json(ledger) if ledger.exists() else {"product": product, "highest": 0}
        strict_keys(previous, {"product", "highest"}, {"product", "highest"})
        if previous["product"] != product:
            raise PipelineError("Build ledger belongs to another product")
        value = max(highest, _integer(previous["highest"], "ledger highest", minimum=0)) + 1
        _integer(value, "next build")
        atomic_json(ledger, {"product": product, "highest": value})
    return {"product": product, "build_number": value, "remote_highest": highest,
            "allocation": "caller-serialized-local-ledger", "serialization_key": key}


def _secret(name):
    _text(name, "secret environment name", r"[A-Z][A-Z0-9_]*")
    value = os.environ.get(name, "")
    if not value or "\n" in value or "\r" in value:
        raise PipelineError("Required secret is absent or malformed")
    return value


def android_derive(config, root):
    strict_keys(config, {"operation", "aab", "output_apk", "bundletool", "apksigner", "signing", "allowed_refs"},
                {"aab", "output_apk", "bundletool", "apksigner", "signing", "allowed_refs"})
    context = trusted_context(config["allowed_refs"])
    _text(context["sha"], "source commit", r"[0-9a-f]{40}")
    bundle = strict_keys(config["bundletool"], {"jar", "jar_env", "sha256", "version"}, {"sha256", "version"})
    if ("jar" in bundle) == ("jar_env" in bundle):
        raise PipelineError("Choose one relative bundletool jar or installed jar environment path")
    _text(bundle["sha256"], "bundletool SHA256", r"[0-9a-f]{64}")
    if "jar_env" in bundle:
        name = _text(bundle["jar_env"], "bundletool path environment name", r"[A-Z][A-Z0-9_]*")
        value = os.environ.get(name, "")
        if not value or any(ord(c) < 32 for c in value):
            raise PipelineError("Installed bundletool path environment is missing or invalid")
        jar = Path(value)
        if (not jar.is_absolute() or not jar.is_file() or not jar.stat().st_size
                or any(path.is_symlink() for path in (jar, *jar.parents))):
            raise PipelineError("Installed bundletool must be an absolute regular file without symlinks")
        jar = jar.resolve()
    else:
        jar = _file(root, bundle["jar"])
    if sha256_file(jar) != bundle["sha256"]:
        raise PipelineError("bundletool integrity mismatch")
    bundle_cmd = ["java", "-jar", str(jar)]
    if run([*bundle_cmd, "version"], cwd=root).stdout.strip() != bundle["version"]:
        raise PipelineError("bundletool version mismatch")
    signer = strict_keys(config["apksigner"], {"command", "version"}, {"command", "version"})
    command = _text(signer["command"], "apksigner path")
    if run([command, "version"], cwd=root).stdout.strip() != signer["version"]:
        raise PipelineError("apksigner version mismatch")
    signing = strict_keys(config["signing"], {"keystore_env", "store_password_env", "key_password_env", "alias", "sha256"},
                          {"keystore_env", "store_password_env", "key_password_env", "alias", "sha256"})
    keystore = Path(_secret(signing["keystore_env"]))
    if not keystore.is_file():
        raise PipelineError("Keystore file is missing")
    alias = _text(signing["alias"], "key alias")
    expected = _text(signing["sha256"], "signer SHA256", r"[0-9A-Fa-f]{64}").lower()
    aab = _file(root, config["aab"])
    if aab.suffix != ".aab":
        raise PipelineError("APK must be derived from an AAB")
    before = sha256_file(aab)
    manifest_text = run([*bundle_cmd, "dump", "manifest", "--bundle=" + str(aab), "--module=base"], cwd=root).stdout
    if "<!DOCTYPE" in manifest_text or "<!ENTITY" in manifest_text:
        raise PipelineError("Unsafe AAB manifest XML")
    try:
        manifest = ET.fromstring(manifest_text)
        application = {"id": manifest.attrib["package"],
                       "version": manifest.attrib["{http://schemas.android.com/apk/res/android}versionName"],
                       "build": manifest.attrib["{http://schemas.android.com/apk/res/android}versionCode"]}
        _text(application["id"], "Android package", r"[A-Za-z][A-Za-z0-9_.]+")
        _text(application["build"], "Android versionCode", r"[1-9][0-9]*")
    except (ET.ParseError, KeyError) as exc:
        raise PipelineError("AAB manifest identity/version is missing") from exc
    output = confined_path(root, config["output_apk"], must_exist=False)
    if output.exists() or output.suffix != ".apk":
        raise PipelineError("APK output must be a new .apk path")
    with tempfile.TemporaryDirectory(prefix="mobile-sign-") as private:
        private = Path(private)
        store_pass, key_pass = private / "store-pass", private / "key-pass"
        for path, name in ((store_pass, signing["store_password_env"]), (key_pass, signing["key_password_env"])):
            path.write_text(_secret(name)); path.chmod(0o600)
        apks = private / "candidate.apks"
        run([*bundle_cmd, "build-apks", "--bundle=" + str(aab), "--output=" + str(apks), "--mode=universal",
             "--ks=" + str(keystore), "--ks-key-alias=" + alias, "--ks-pass=file:" + str(store_pass),
             "--key-pass=file:" + str(key_pass)], cwd=root, extra_env={"HOME": str(private)}, timeout=900)
        try:
            with zipfile.ZipFile(apks) as archive:
                entries = [x for x in archive.infolist() if x.filename == "universal.apk"]
                if len(entries) != 1 or not 0 < entries[0].file_size <= 2 * 1024 ** 3:
                    raise PipelineError("bundletool did not produce exactly one universal APK")
                apk = private / "universal.apk"
                with archive.open(entries[0]) as source, apk.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PipelineError("bundletool APK archive is invalid") from exc
        result = run([command, "verify", "--print-certs", str(apk)], cwd=root)
        certs = re.findall(r"^Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$", result.stdout, re.M)
        if len(certs) != 1 or certs[0].lower() != expected:
            raise PipelineError("APK upload signer differs from expected certificate")
        if sha256_file(aab) != before:
            raise PipelineError("AAB changed while deriving APK")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(apk, output)
    return {"source_revision": context["sha"], "source_sha": context["sha"], "kind": "android",
            "application": application,
            "artifacts": {p.relative_to(root).as_posix(): {"sha256": sha256_file(p), "bytes": p.stat().st_size} for p in (aab, output)},
            "aab_sha256": before, "apk_sha256": sha256_file(output),
            "upload_signer_sha256": expected, "bundletool_version": bundle["version"],
            "apksigner_version": signer["version"], "play_app_signing_verified": False}


def _testlab_results(value, devices):
    expected = {"-".join(device[k] for k in ("model", "version", "locale", "orientation")) for device in devices}
    if not isinstance(value, list) or len(value) != len(expected):
        raise PipelineError("Test Lab returned missing/extra device results")
    observed = []
    for row in value:
        if not isinstance(row, dict) or row.get("outcome") != "Passed" or not isinstance(row.get("axis_value"), str):
            raise PipelineError("Test Lab did not pass every device")
        observed.append(row["axis_value"])
    if len(set(observed)) != len(observed) or set(observed) != expected:
        raise PipelineError("Test Lab device matrix mismatch")
    return len(observed)


def _ddp_results(value, name, devices):
    if not isinstance(value, dict) or value.get("name") != name:
        raise PipelineError("DDP session identity mismatch")
    def passed(report):
        if (not isinstance(report, dict) or _mapping(report.get("status", {})).get("statusType") != "DONE" or
                _mapping(report.get("result", {})).get("resultType") != "PASSED"):
            raise PipelineError("DDP session/job/execution did not finish and pass")
    report = value.get("sessionReport", {})
    passed(report)
    jobs = report.get("jobReports")
    configs = _mapping(value.get("sessionConfig", {})).get("jobConfigs")
    if not isinstance(jobs, list) or len(jobs) != len(devices) or not isinstance(configs, list) or len(configs) != len(devices):
        raise PipelineError("DDP returned missing/extra device jobs")
    actual_devices = []
    for item in configs:
        allocated = _mapping(_mapping(item).get("allocationConfig", {})).get("deviceConfigs", [])
        if not isinstance(allocated, list) or len(allocated) != 1 or not isinstance(allocated[0], dict):
            raise PipelineError("DDP device allocation differs from submitted matrix")
        actual_devices.append(_mapping(allocated[0].get("requirement", {})).get("deviceId"))
    if sorted(actual_devices, key=str) != sorted(devices):
        raise PipelineError("DDP device allocation differs from submitted matrix")
    # One job per device; every execution must pass, including any infrastructure retry.
    executions = 0
    ids = set()
    for job in jobs:
        passed(job)
        if not job.get("id") or job["id"] in ids:
            raise PipelineError("DDP missing/duplicate job identity")
        ids.add(job["id"])
        attempts = job.get("executionReports")
        if not isinstance(attempts, list) or not attempts:
            raise PipelineError("DDP has no execution results")
        for attempt in attempts:
            passed(attempt)
            executions += 1
    return executions


def device_test(config, root):
    strict_keys(config, {"operation", "provider", "app", "test", "devices", "project", "bucket", "location",
                        "timeout_seconds", "deadline_seconds", "allowed_refs", "access_token_env", "live_enabled"},
                {"provider", "app", "devices", "project", "bucket", "timeout_seconds", "deadline_seconds",
                 "allowed_refs", "access_token_env", "live_enabled"})
    if config["live_enabled"] is not True:
        raise PipelineError("Device testing requires explicit live opt-in")
    context = trusted_context(config["allowed_refs"])
    _text(context["sha"], "source commit", r"[0-9a-f]{40}")
    provider = config["provider"]
    if provider not in ("testlab-robo", "ddp-instrumentation"):
        raise PipelineError("Device provider must be testlab-robo or ddp-instrumentation")
    if provider == "testlab-robo" and dt.datetime.now(dt.timezone.utc).date() >= TEST_LAB_RETIREMENT:
        raise PipelineError("Test Lab retired; migrate explicitly to a supported DDP test type")
    devices = config["devices"]
    if not isinstance(devices, list) or not 1 <= len(devices) <= 20 or len({fingerprint(d) for d in devices}) != len(devices):
        raise PipelineError("Device matrix must contain 1..20 unique entries")
    duration = _integer(config["timeout_seconds"], "test timeout", maximum=2700)
    deadline = _integer(config["deadline_seconds"], "job deadline", maximum=21600)
    if deadline < duration + 120:
        raise PipelineError("Job deadline must allow test timeout plus setup")
    app = _file(root, config["app"])
    project = _text(config["project"], "Google project", r"[a-z][a-z0-9-]{4,61}[a-z0-9]")
    bucket = _text(config["bucket"], "results bucket", r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
    input_hashes = {"app_sha256": sha256_file(app)}
    token = _secret(config["access_token_env"])
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mobile-google-") as private:
        private = Path(private)
        tokenfile = private / "access-token"
        tokenfile.write_text(token); tokenfile.chmod(0o600)
        env = {"HOME": str(private), "CLOUDSDK_CONFIG": str(private / "gcloud"),
               "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true", "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK": "true"}
        def command(args, timeout=60):
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                raise PipelineError("Device job deadline exceeded; remote job may still require cancellation")
            return run(["gcloud", *args, "--project=" + project, "--access-token-file=" + str(tokenfile),
                        "--quiet", "--format=json"], cwd=root, extra_env=env, timeout=min(timeout, remaining))
        sdk = _mapping(_json(command(["version"]).stdout))
        if sdk.get("Google Cloud SDK") != GCLOUD_VERSION:
            raise PipelineError("Google Cloud SDK version mismatch")
        if provider == "ddp-instrumentation" and sdk.get("beta") != GCLOUD_BETA_VERSION:
            raise PipelineError("Pinned gcloud beta component must be installed before credentialed execution")
        if provider == "testlab-robo":
            if "test" in config or "location" in config:
                raise PipelineError("Robo does not accept instrumentation or DDP location settings")
            args = ["firebase", "test", "android", "run", "--type=robo", "--app=" + str(app),
                    "--timeout=" + str(duration) + "s", "--num-flaky-test-attempts=0", "--results-bucket=" + bucket,
                    "--results-dir=shared-ci/" + input_hashes["app_sha256"][:16] + "/" + private.name]
            for device in devices:
                strict_keys(device, {"model", "version", "locale", "orientation"}, {"model", "version", "locale", "orientation"})
                for key, value in device.items():
                    _text(value, "device " + key, r"[A-Za-z0-9_-]+")
                if device["orientation"] not in ("portrait", "landscape"):
                    raise PipelineError("Invalid device orientation")
                args.append("--device=" + ",".join(k + "=" + device[k] for k in ("model", "version", "locale", "orientation")))
            count = _testlab_results(_json(command(args, deadline).stdout), devices)
            result = {"executions": count, "retirement_date": TEST_LAB_RETIREMENT.isoformat()}
        else:
            location = _text(config.get("location"), "DDP location", r"[a-z0-9-]+")
            test = _file(root, config.get("test"))
            if "," in str(app) or "," in str(test):
                raise PipelineError("DDP artifact paths cannot contain CSV separators")
            if test.suffix != ".apk":
                raise PipelineError("DDP instrumentation requires a test APK; no Robo fallback")
            if duration < 60:
                raise PipelineError("DDP instrumentation timeout must be at least 60 seconds")
            input_hashes["test_sha256"] = sha256_file(test)
            for device in devices:
                _text(device, "DDP device", r"[A-Za-z0-9_.-]+")
            args = ["beta", "device-run", "sessions", "submit", "instrumentation", "--async",
                    "--apps=" + str(app), "--test=" + str(test), "--device=" + ",".join(devices),
                    "--bucket-name=" + bucket, "--location=" + location,
                    "--instrumentation-timeout=" + str(duration) + "s", "--flaky-test-attempts=1",
                    "--no-flaky-test-parallel-retry"]
            submitted = command(args, min(900, deadline))
            # SDK 582 async submit has no JSON return value. Its documented status log is the ID source.
            names = re.findall(r"Creating session \[([A-Za-z0-9_-]+)\] in location \[" + re.escape(location) + r"\]", submitted.stderr)
            if len(names) != 1:
                raise PipelineError("Cannot identify submitted DDP session; inspect provider jobs before retrying")
            name = f"projects/{project}/locations/{location}/sessions/{names[0]}"
            try:
                command(["beta", "device-run", "sessions", "wait", name], deadline)
                result = _json(command(["beta", "device-run", "sessions", "describe", name, "--full"]).stdout)
                count = _ddp_results(result, name, devices)
            except PipelineError as exc:
                # Best-effort bounded cleanup; a cancelled/failed job is never a passing receipt.
                try:
                    run(["gcloud", "beta", "device-run", "sessions", "cancel", name,
                         "--project=" + project, "--access-token-file=" + str(tokenfile), "--quiet"],
                        cwd=root, extra_env=env, timeout=30, check=False)
                except PipelineError:
                    pass
                raise PipelineError(f"DDP session {name} failed; cancellation attempted, verify provider state") from exc
            result = {"executions": count, "session": name, "preview": True}
        if provider == "ddp-instrumentation" and sha256_file(test) != input_hashes["test_sha256"]:
            raise PipelineError("Instrumentation artifact changed during device testing")
        if sha256_file(app) != input_hashes["app_sha256"]:
            raise PipelineError("App artifact changed during device testing")
    return {**result, **input_hashes, "source_revision": context["sha"], "provider": provider,
            "device_matrix_sha256": fingerprint(devices), "config_sha256": fingerprint(config),
            "gcloud_version": GCLOUD_VERSION, "results_bucket": bucket}


def execute(config, root):
    if not isinstance(config, dict):
        raise PipelineError("Mobile configuration must be an object")
    from .apple_workflows import execute as workflow_drift
    operations = {"apple-workflow-drift": workflow_drift, "apple-bootstrap": apple_bootstrap, "apple-verify": apple_verify, "apple-emergency-archive": apple_emergency_archive,
                  "next-build": next_build, "android-derive": android_derive, "device-test": device_test}
    operation = config.get("operation")
    if operation not in operations:
        raise PipelineError("Unknown mobile operation")
    return {"schema": 1, "status": "passed", "operation": operation, **operations[operation](config, Path(root).resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    output = confined_path(root, args.output, must_exist=False)
    config_path = confined_path(root, args.config)
    if output == config_path:
        raise PipelineError("Receipt output cannot overwrite its configuration")
    if run(["git", "ls-files", "--error-unmatch", "--", output.relative_to(root).as_posix()], cwd=root, check=False).returncode == 0:
        raise PipelineError("Mobile receipt cannot overwrite tracked source")
    output.unlink(missing_ok=True)  # A failed rerun cannot retain a passing receipt.
    from .assurance import stamp
    result = stamp(execute(load_json(config_path), root), "mobile", config_path, root)
    atomic_json(output, result)
    if result.get("status") != "passed": raise PipelineError("Mobile operation found drift or missing required proof")


if __name__ == "__main__":
    cli_error(main)
