"""Explicit retained-build bundles and authenticated GitHub artifact import."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .common import PipelineError, atomic_json, cli_error, confined_path, fingerprint, load_json, run, sha256_file, strict_keys
from .evidence import IDENTITY_FIELDS, _api, verify_checks
from .stores import HEX, SHA, bounded, env_secret, verify_files

INDEX = "bundle.json"
MAX_BYTES = 8 * 1024 * 1024 * 1024
SECRET_SUFFIXES = {".p8", ".p12", ".pfx", ".jks", ".keystore", ".pem", ".key"}
SECRET_NAMES = {"id_rsa", "id_ed25519", "credentials", "credentials.json", "service-account.json", "service_account.json", "key.properties", ".npmrc", ".pypirc", "application_default_credentials.json"}
SECRET_DIRS = {".git", ".ssh", ".aws", "vault", "secrets"}
SECRET_CONTENT = re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\"(?:private_key|client_secret)\"\s*:|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|\bsk-proj-[A-Za-z0-9_-]{20,}")


def safe_name(relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative
            or ":" in relative or any(ord(c) < 32 for c in relative) or relative.startswith("/")
            or PurePosixPath(relative).as_posix() != relative or any(p in ("", ".", "..") for p in relative.split("/"))):
        raise PipelineError("Artifact path must be a canonical relative POSIX path")
    path = PurePosixPath(relative)
    if any(part.lower().startswith(".env") or part.lower() in SECRET_DIRS for part in path.parts):
        raise PipelineError("Secret/internal paths cannot enter a retained artifact")
    if path.name.lower() in SECRET_NAMES or path.suffix.lower() in SECRET_SUFFIXES:
        raise PipelineError("Credential/signing file names cannot enter a retained artifact")
    return relative


def scan_file(path):
    # Stream to avoid loading large app/image artifacts; overlap handles signatures at chunk boundaries.
    overlap = b""
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            content = overlap + chunk
            if SECRET_CONTENT.search(content):
                raise PipelineError("Credential-like material detected in a retained artifact")
            overlap = content[-512:]


def no_symlink(root, relative):
    current = Path(root)
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise PipelineError("Artifact paths cannot traverse symbolic links")
    return current


def manifest_files(candidate, manifest):
    safe_name(manifest)
    if manifest == INDEX:
        raise PipelineError("Manifest path conflicts with bundle index")
    records = {}
    for group in ("inputs", "artifacts"):
        entries = candidate.get(group)
        if not isinstance(entries, dict) or not entries:
            raise PipelineError("Candidate file inventory is missing")
        for path, record in entries.items():
            safe_name(path)
            if path in (INDEX, manifest):
                raise PipelineError("Candidate contains a reserved bundle path")
            if path in records and records[path] != record:
                raise PipelineError("Candidate contains conflicting file records")
            records[path] = record
    checks = candidate.get("checks")
    if not isinstance(checks, dict) or not checks:
        raise PipelineError("Candidate check inventory is missing")
    for path, digest in checks.items():
        safe_name(path)
        if path in records or path in (INDEX, manifest) or not HEX.fullmatch(str(digest)):
            raise PipelineError("Candidate check path conflicts with artifact/input evidence")
        records[path] = {"sha256": digest}
    if len({path.casefold() for path in records}) != len(records):
        raise PipelineError("Artifact inventory has case-colliding paths")
    return records


def verify_manifest(candidate, root, expected_identity, expected_source):
    if (candidate.get("schema") != 1 or candidate.get("status") != "passed"
            or not HEX.fullmatch(str(expected_identity)) or not SHA.fullmatch(str(expected_source))
            or candidate.get("identity") != expected_identity or candidate.get("source_sha") != expected_source
            or any(k not in candidate for k in IDENTITY_FIELDS)
            or fingerprint({k: candidate[k] for k in IDENTITY_FIELDS}) != expected_identity):
        raise PipelineError("Retained manifest differs from trusted source/candidate selection")
    verify_files(root, candidate["inputs"], allow_empty=True)
    verify_files(root, candidate["artifacts"])
    verify_checks(candidate, root)


def package(config, root):
    required = {"schema", "operation", "manifest", "expected_identity", "expected_source_sha", "output_dir_env"}
    strict_keys(config, required | {"max_bytes", "max_files", "source_root_env"}, required)
    if config["schema"] != 1 or config["operation"] != "package":
        raise PipelineError("Unsupported artifact package request")
    max_bytes, max_files = limits(config)
    root = Path(root).resolve()
    manifest = safe_name(config["manifest"])
    candidate = load_json(confined_path(root, manifest))
    verify_manifest(candidate, root, config["expected_identity"], config["expected_source_sha"])
    # Packaging is performed on the clean source/build job, before GitHub upload-artifact.
    source_root = Path(env_secret(config["source_root_env"])).resolve() if "source_root_env" in config else root
    if run(["git", "rev-parse", "HEAD^{tree}"], cwd=source_root).stdout.strip() != candidate["source_tree"]:
        raise PipelineError("Packaging source tree differs from the retained candidate")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source_root).stdout.strip():
        raise PipelineError("Packaging source has modified tracked files")
    records = manifest_files(candidate, manifest)
    records[manifest] = {"sha256": sha256_file(root / manifest), "bytes": (root / manifest).stat().st_size}
    if len(records) + 1 > max_files:
        raise PipelineError("Retained artifact file count exceeds limit")
    total = 0
    for relative, record in records.items():
        path = no_symlink(root, relative)
        if not path.is_file():
            raise PipelineError("Retained artifact is not a regular file")
        total += path.stat().st_size
        if total > max_bytes:
            raise PipelineError("Retained artifact bytes exceed limit")
        scan_file(path)
        record["bytes"] = path.stat().st_size
    output_name = config["output_dir_env"]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(output_name)) or not os.environ.get(output_name):
        raise PipelineError("Explicit artifact output directory environment variable is missing")
    destination = Path(os.environ[output_name])
    if not destination.is_absolute() or destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(root):
        raise PipelineError("Package output must be a fresh absolute directory outside the source workspace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".shared-bundle-", dir=destination.parent))
    try:
        for relative in sorted(records):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, target)
            if sha256_file(target) != records[relative]["sha256"]:
                raise PipelineError("Artifact changed while packaging")
            target.chmod(0o600)
        index = {"schema": 1, "manifest": manifest, "candidate_identity": candidate["identity"],
                 "source_sha": candidate["source_sha"], "files": records}
        atomic_json(stage / INDEX, index)
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"schema": 1, "status": "passed", "operation": "package", "candidate_identity": candidate["identity"],
            "source_sha": candidate["source_sha"], "files": len(records), "bytes": total,
            "bundle_index_sha256": sha256_file(destination / INDEX), "directory": str(destination)}


def limits(config):
    size = config.get("max_bytes", 2 * 1024 * 1024 * 1024)
    count = config.get("max_files", 20000)
    if not isinstance(size, int) or not isinstance(count, int):
        raise PipelineError("Artifact limits must be integers")
    bounded(size, 1024, MAX_BYTES, "artifact size limit")
    bounded(count, 1, 100000, "artifact file limit")
    return size, count


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (AttributeError, TypeError, ValueError):
        raise PipelineError("Missing or malformed authenticated run/artifact timestamp") from None


def download(path, token, destination, max_bytes):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect())
    deadline = time.monotonic() + 900
    request = urllib.request.Request("https://api.github.com" + path, headers={"Authorization": "Bearer " + token,
                                    "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        try:
            response = opener.open(request, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers.get("Location", "")
            parsed = urllib.parse.urlsplit(location)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise PipelineError("Unsafe GitHub artifact redirect")
            # Signed storage redirect gets NO GitHub token, and cannot redirect a second time.
            response = opener.open(urllib.request.Request(location), timeout=60)
        with response, Path(destination).open("xb") as stream:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if time.monotonic() > deadline:
                    raise PipelineError("Artifact download exceeded its total deadline")
                if total > max_bytes:
                    raise PipelineError("Artifact download exceeds configured limit")
                stream.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PipelineError("Authenticated artifact download failed") from None


def authenticated_artifact(config, token, request):
    repo = config["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repo)) or not str(config["run_id"]).isdigit():
        raise PipelineError("Invalid GitHub repository/run selection")
    if not isinstance(config["run_attempt"], int) or isinstance(config["run_attempt"], bool) or config["run_attempt"] < 1:
        raise PipelineError("Exact positive GitHub run attempt is required")
    workflow = config["expected_workflow"]
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", str(workflow)):
        raise PipelineError("Exact trusted producer workflow path is required")
    events = config["allowed_events"]
    if not isinstance(events, list) or not events or any(e not in ("push", "workflow_dispatch", "pull_request") for e in events):
        raise PipelineError("Explicit trusted producer event allowlist is required")
    base = f"/repos/{repo}/actions/runs/{config['run_id']}"
    run_info = request(base, token)
    current_running = (run_info.get("status") == "in_progress" and os.environ.get("GITHUB_ACTIONS") == "true"
                       and str(config["run_id"]) == os.environ.get("GITHUB_RUN_ID")
                       and str(config["run_attempt"]) == os.environ.get("GITHUB_RUN_ATTEMPT")
                       and repo == os.environ.get("GITHUB_REPOSITORY")
                       and config["expected_source_sha"] == os.environ.get("GITHUB_SHA"))
    completed = run_info.get("status") == "completed" and run_info.get("conclusion") == "success"
    if ((not completed and not current_running) or run_info.get("head_sha") != config["expected_source_sha"]
            or run_info.get("path", "").split("@")[0] != workflow
            or run_info.get("head_repository", {}).get("full_name") != repo
            or run_info.get("run_attempt") != config["run_attempt"] or run_info.get("event") not in events):
        raise PipelineError("Producer run is not successful trusted evidence for the selected source/attempt/workflow")
    started = timestamp(run_info.get("run_started_at"))
    updated = timestamp(run_info.get("updated_at"))
    if current_running:
        producer_name = config.get("producer_job")
        if not isinstance(producer_name, str) or not producer_name.strip() or len(producer_name) > 200:
            raise PipelineError("Same-run artifact import requires an explicit completed producer job")
        producers = []
        for page in range(1, 21):
            response = request(base + f"/attempts/{config['run_attempt']}/jobs?per_page=100&page={page}", token)
            batch = response.get("jobs")
            if not isinstance(batch, list):
                raise PipelineError("Malformed producer job inventory")
            producers.extend(job for job in batch if job.get("name") == producer_name)
            if len(batch) < 100:
                break
        else:
            raise PipelineError("Producer job inventory exceeds pagination limit")
        if (len(producers) != 1 or producers[0].get("status") != "completed" or producers[0].get("conclusion") != "success"
                or producers[0].get("run_id") != int(config["run_id"]) or not isinstance(producers[0].get("id"), int)
                or producers[0]["id"] <= 0):
            raise PipelineError("Exactly one completed successful producer job is required for same-run import")
        job = producers[0]
        started = max(started, timestamp(job.get("started_at")))
        # run.updated_at can precede a still-running job; the completed producer supplies a bounded upper time.
        updated = timestamp(job.get("completed_at"))
    matches = []
    for page in range(1, 21):
        response = request(base + f"/artifacts?per_page=100&page={page}", token)
        batch = response.get("artifacts")
        if not isinstance(batch, list):
            raise PipelineError("Malformed GitHub artifact inventory")
        matches.extend(item for item in batch if item.get("name") == config["artifact_name"] and not item.get("expired"))
        if len(batch) < 100:
            break
    else:
        raise PipelineError("Artifact inventory exceeds pagination limit")
    if len(matches) != 1:
        raise PipelineError("Exactly one current named artifact is required")
    item = matches[0]
    if (not isinstance(item.get("id"), int) or item["id"] <= 0
            or item.get("workflow_run", {}).get("id") != int(config["run_id"])
            or item.get("workflow_run", {}).get("head_sha") != config["expected_source_sha"]
            or not started <= timestamp(item.get("created_at")) <= updated
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("digest", "")))):
        raise PipelineError("Artifact identity/digest/attempt provenance is absent or stale")
    return item


def extract_bundle(archive_path, stage, config, max_bytes, max_files):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > max_files * 2:
                raise PipelineError("Artifact archive entry count exceeds limit")
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or len({name.casefold() for name in names}) != len(names) or INDEX not in names:
                raise PipelineError("Archive contains duplicate entries or no bundle index")
            files = {}
            total = 0
            for entry in entries:
                name = entry.filename[:-1] if entry.is_dir() else entry.filename
                safe_name(name)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)) or entry.flag_bits & 1:
                    raise PipelineError("Archive links, special files and encrypted entries are forbidden")
                if entry.is_dir():
                    continue
                total += entry.file_size
                if total > max_bytes or entry.file_size < 0:
                    raise PipelineError("Archive expanded bytes exceed limit")
                files[entry.filename] = entry
            if len(files) > max_files or files[INDEX].file_size > 16 * 1024 * 1024:
                raise PipelineError("Archive inventory exceeds limit")
            index_path = stage / INDEX
            index_path.write_bytes(archive.read(INDEX))
            index = load_json(index_path)
            strict_keys(index, {"schema", "manifest", "candidate_identity", "source_sha", "files"}, {"schema", "manifest", "candidate_identity", "source_sha", "files"})
            if index["schema"] != 1 or index["candidate_identity"] != config["expected_identity"] or index["source_sha"] != config["expected_source_sha"]:
                raise PipelineError("Bundle index differs from trusted selection")
            records = index["files"]
            if not isinstance(records, dict) or set(files) != set(records) | {INDEX} or INDEX in records:
                raise PipelineError("Archive files differ from explicit bundle inventory")
            for relative, record in records.items():
                safe_name(relative)
                strict_keys(record, {"sha256", "bytes"}, {"sha256", "bytes"})
                if not HEX.fullmatch(str(record["sha256"])) or record["bytes"] != files[relative].file_size:
                    raise PipelineError("Invalid bundle file identity")
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(files[relative]) as source, target.open("xb") as output:
                    digest, count = hashlib.sha256(), 0
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        if count > record["bytes"]:
                            raise PipelineError("Archive member exceeded declared size")
                        digest.update(chunk)
                        output.write(chunk)
                if count != record["bytes"] or digest.hexdigest() != record["sha256"]:
                    raise PipelineError("Bundle file hash/size mismatch")
                scan_file(target)
                target.chmod(0o600)
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise PipelineError("Malformed retained artifact archive") from None
    manifest = safe_name(index["manifest"])
    if manifest not in records:
        raise PipelineError("Bundle manifest is absent")
    candidate = load_json(stage / manifest)
    expected = manifest_files(candidate, manifest)
    if set(records) != set(expected) | {manifest}:
        raise PipelineError("Bundle contains files not declared by the candidate")
    for relative, record in expected.items():
        if any(records[relative].get(key) != value for key, value in record.items()):
            raise PipelineError("Bundle file differs from candidate inventory")
    verify_manifest(candidate, stage, config["expected_identity"], config["expected_source_sha"])
    return candidate, index


def import_files(stage, root, records):
    """Preflight the entire import; never replace differing existing source/config bytes."""
    root = Path(root).resolve()
    for relative, record in records.items():
        target = no_symlink(root, relative)
        confined_path(root, relative, must_exist=False)
        if target.exists() and (not target.is_file() or sha256_file(target) != record["sha256"]):
            raise PipelineError("Retained artifact conflicts with an existing source/config file")
        parent = target.parent
        while parent != root:
            if parent.exists() and not parent.is_dir():
                raise PipelineError("Retained artifact path conflicts with existing workspace structure")
            parent = parent.parent
    created = []
    try:
        for relative, record in records.items():
            target = root / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation handles races without overwriting another job's output.
            with target.open("xb") as output, (stage / relative).open("rb") as source:
                created.append(target)
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(0o600)
            if sha256_file(target) != record["sha256"]:
                raise PipelineError("Imported file hash mismatch")
    except (OSError, PipelineError) as exc:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError("Artifact import failed without overwriting existing files") from None


def fetch(config, root, *, request=_api, downloader=download):
    required = {"schema", "operation", "repository", "run_id", "run_attempt", "artifact_name", "expected_workflow",
                "expected_source_sha", "expected_identity", "token_env", "allowed_events"}
    strict_keys(config, required | {"max_bytes", "max_files", "producer_job"}, required)
    if config["schema"] != 1 or config["operation"] != "fetch" or not HEX.fullmatch(str(config["expected_identity"])) or not SHA.fullmatch(str(config["expected_source_sha"])):
        raise PipelineError("Invalid artifact fetch selection")
    if not isinstance(config["artifact_name"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", config["artifact_name"]):
        raise PipelineError("Invalid artifact name")
    max_bytes, max_files = limits(config)
    root = Path(root).resolve()
    token = env_secret(config["token_env"])
    item = authenticated_artifact(config, token, request)
    if not isinstance(item.get("size_in_bytes"), int) or not 0 < item["size_in_bytes"] <= max_bytes:
        raise PipelineError("GitHub artifact size is absent or exceeds limit")
    with tempfile.TemporaryDirectory(prefix="shared-artifacts-") as temporary:
        temporary = Path(temporary)
        archive = temporary / "download.zip"
        downloader(f"/repos/{config['repository']}/actions/artifacts/{item['id']}/zip", token, archive, max_bytes)
        if archive.stat().st_size > max_bytes or "sha256:" + sha256_file(archive) != item["digest"]:
            raise PipelineError("Downloaded artifact digest differs from authenticated GitHub metadata")
        stage = temporary / "retained"
        stage.mkdir()
        candidate, index = extract_bundle(archive, stage, config, max_bytes, max_files)
        source_tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=root).stdout.strip()
        if source_tree != candidate["source_tree"]:
            raise PipelineError("Checked-out source tree differs from retained build")
        if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
            raise PipelineError("Cannot import into modified tracked source")
        import_files(stage, root, index["files"])
        verify_manifest(candidate, root, config["expected_identity"], config["expected_source_sha"])
    return {"schema": 1, "status": "passed", "operation": "fetch", "candidate_identity": candidate["identity"],
            "source_sha": candidate["source_sha"], "manifest": index["manifest"], "files": len(index["files"]),
            "provenance": {"repository": config["repository"], "run_id": str(config["run_id"]), "run_attempt": config["run_attempt"],
                           "workflow": config["expected_workflow"], "artifact_id": item["id"], "artifact_digest": item["digest"]}}


def execute(config, root):
    operation = config.get("operation")
    if operation == "package":
        return package(config, root)
    if operation == "fetch":
        return fetch(config, root)
    raise PipelineError("Unknown artifact operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    atomic_json(args.output, execute(load_json(args.config), args.root))


if __name__ == "__main__":
    cli_error(main)
