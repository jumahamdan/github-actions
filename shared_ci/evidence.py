"""Hash-bound candidates and authenticated retrieval of successful GitHub receipts."""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .common import PipelineError, atomic_json, cli_error, confined_path, fingerprint, load_json, run, sha256_file, strict_keys

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_records(root, paths):
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise PipelineError("Evidence requires a nonempty unique list of paths")
    records = {}
    for relative in paths:
        path = confined_path(root, relative)
        if path.is_symlink() or not path.is_file():
            raise PipelineError("Evidence inputs must be regular files")
        if any(part.startswith('.env') for part in Path(relative).parts) or path.suffix in (".p8", ".p12", ".keystore", ".jks"):
            raise PipelineError("Secret/signing material cannot be an evidence artifact")
        records[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    return records


def create(config, root):
    strict_keys(config, {"schema", "product", "version", "build", "library_sha", "inputs", "artifacts", "toolchain", "checks"},
                {"schema", "product", "version", "build", "library_sha", "inputs", "artifacts", "toolchain", "checks"})
    if config["schema"] != 1 or not SHA.fullmatch(config["library_sha"]):
        raise PipelineError("Candidate requires schema 1 and an immutable library SHA")
    for key in ("product", "version", "build"):
        if not isinstance(config[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", config[key]):
            raise PipelineError(f"Invalid candidate {key}")
    if not isinstance(config["toolchain"], dict) or not config["toolchain"]:
        raise PipelineError("Candidate requires a toolchain record")
    if not isinstance(config["checks"], list) or not config["checks"]:
        raise PipelineError("Candidate requires successful check receipt paths")
    checks = {}
    for relative in config["checks"]:
        receipt = load_json(confined_path(root, relative))
        if receipt.get("status") != "passed":
            raise PipelineError("Candidate contains a missing or unsuccessful check")
        checks[relative] = sha256_file(confined_path(root, relative))
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
        raise PipelineError("Candidate source has uncommitted tracked changes")
    source = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=root).stdout.strip()
    inputs = file_records(root, config["inputs"])
    artifacts = file_records(root, config["artifacts"])
    if any(v["bytes"] == 0 for v in artifacts.values()):
        raise PipelineError("Required release artifact is empty")
    result = {"schema": 1, "status": "passed", "product": config["product"], "version": config["version"],
              "build": config["build"], "source_sha": source, "source_tree": tree,
              "library_sha": config["library_sha"], "toolchain": config["toolchain"],
              "inputs": inputs, "artifacts": artifacts, "checks": checks, "created_at": now(),
              "run": {k: os.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}}
    result["identity"] = fingerprint({k: result[k] for k in ("product", "version", "build", "inputs", "artifacts", "checks", "library_sha", "toolchain")})
    return result


def verify(candidate, root, *, expected_library_sha=None, expected_toolchain=None, require_tree=True):
    if candidate.get("schema") != 1 or candidate.get("status") != "passed":
        raise PipelineError("Invalid candidate schema/status")
    for group in ("inputs", "artifacts"):
        if file_records(root, list(candidate.get(group, {}))) != candidate[group]:
            raise PipelineError(f"Candidate {group} changed or missing")
    if not isinstance(candidate.get("checks"), dict) or not candidate["checks"]:
        raise PipelineError("Candidate check evidence is missing")
    for relative, digest in candidate["checks"].items():
        path = confined_path(root, relative)
        if sha256_file(path) != digest or load_json(path).get("status") != "passed":
            raise PipelineError("Candidate check evidence changed or did not pass")
    identity = fingerprint({k: candidate[k] for k in ("product", "version", "build", "inputs", "artifacts", "checks", "library_sha", "toolchain")})
    if identity != candidate.get("identity"):
        raise PipelineError("Candidate identity is corrupt")
    if expected_library_sha is not None and candidate["library_sha"] != expected_library_sha:
        raise PipelineError("Library revision invalidates candidate evidence")
    if expected_toolchain is not None and candidate["toolchain"] != expected_toolchain:
        raise PipelineError("Toolchain invalidates candidate evidence")
    if require_tree and run(["git", "rev-parse", "HEAD^{tree}"], cwd=root).stdout.strip() != candidate.get("source_tree"):
        raise PipelineError("Full merged source tree differs from the candidate")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
        raise PipelineError("Working source differs from its recorded tree")
    return {"schema": 1, "status": "passed", "identity": identity, "verified_at": now()}


def _api(path, token, binary=False):
    if not path.startswith("/repos/"):
        raise PipelineError("Invalid GitHub API path")
    request = urllib.request.Request("https://api.github.com" + path,
                                    headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                             "X-GitHub-Api-Version": "2022-11-28"})
    try:
        # urllib strips no Authorization header on redirects: handle artifact redirects explicitly.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            if not binary or exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers.get("Location", "")
            parsed = urllib.parse.urlparse(location)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                raise PipelineError("Unsafe artifact download redirect")
            response = urllib.request.urlopen(urllib.request.Request(location), timeout=60)
        with response:
            data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise PipelineError("Receipt download exceeds limit")
        return data if binary else json.loads(data)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise PipelineError("GitHub evidence retrieval failed") from exc


def fetch_receipt(repository, run_id, artifact_name, expected_sha, expected_workflow, token, *, request=_api):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not str(run_id).isdigit():
        raise PipelineError("Invalid repository/run identity")
    if not SHA.fullmatch(expected_sha) or not expected_workflow.startswith(".github/workflows/"):
        raise PipelineError("Explicit source SHA and trusted workflow path are required")
    if not token:
        raise PipelineError("GitHub evidence token is missing")
    prefix = f"/repos/{repository}/actions/runs/{run_id}"
    metadata = request(prefix, token)
    if (metadata.get("status") != "completed" or metadata.get("conclusion") != "success"
            or metadata.get("head_sha") != expected_sha
            or metadata.get("path", "").split("@")[0] != expected_workflow
            or metadata.get("head_repository", {}).get("full_name") != repository
            or metadata.get("event") not in ("push", "pull_request", "workflow_dispatch")):
        raise PipelineError("Run is not successful trusted evidence for the expected source/workflow")
    artifacts = []
    page = 1
    while True:
        batch = request(prefix + f"/artifacts?per_page=100&page={page}", token).get("artifacts", [])
        artifacts.extend(a for a in batch if a.get("name") == artifact_name and not a.get("expired"))
        if len(batch) < 100:
            break
        page += 1
        if page > 20:
            raise PipelineError("Artifact lookup exceeds bounded pagination")
    if len(artifacts) != 1:
        raise PipelineError("Exactly one unexpired evidence artifact is required")
    item = artifacts[0]
    data = request(f"/repos/{repository}/actions/artifacts/{int(item['id'])}/zip", token, True)
    import hashlib
    if item.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest():
        raise PipelineError("Artifact digest is absent or does not match GitHub metadata")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != "receipt.json" or entries[0].file_size > 1024 * 1024:
                raise PipelineError("Evidence artifact must contain only a bounded receipt.json")
            receipt = json.loads(archive.read("receipt.json"))
    except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise PipelineError("Malformed evidence artifact") from exc
    if receipt.get("status") != "passed":
        raise PipelineError("Receipt did not pass")
    return {"receipt": receipt, "provenance": {"repository": repository, "run_id": str(run_id),
            "source_sha": expected_sha, "workflow": expected_workflow, "artifact_digest": item["digest"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = create(load_json(args.config), args.root) if args.operation == "create" else verify(load_json(args.config), args.root)
    atomic_json(args.output, result)


if __name__ == "__main__":
    cli_error(main)
