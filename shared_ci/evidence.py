"""Hash-bound candidates and authenticated retrieval of successful GitHub receipts."""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import datetime as dt
import hashlib
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
IDENTITY_FIELDS = ("product", "version", "build", "source_sha", "source_tree", "inputs", "artifacts", "checks", "check_requirements", "library_sha", "toolchain", "created_at")
_FETCH_PROOF = object()
_SECURITY_REFRESH = contextvars.ContextVar("verified_security_refresh", default=None)


class AuthenticatedReceipt(dict):
    """An API-verified fetch result; serialized JSON cannot assert this marker."""

    def __init__(self, value, *, _proof=None):
        if _proof is not _FETCH_PROOF:
            raise PipelineError("Receipt origin must be authenticated by fetch_receipt")
        super().__init__(value)
        self._authenticated_fingerprint = self._fingerprint()

    def _fingerprint(self):
        return fingerprint({"receipt": self["receipt"], "provenance": self["provenance"],
                            "coverage": {path: hashlib.sha256(data).hexdigest()
                                         for path, data in self["coverage_files"].items()}})

    def validate_integrity(self):
        try:
            valid = self._fingerprint() == self._authenticated_fingerprint
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PipelineError("Authenticated receipt was modified after retrieval") from exc
        if not valid:
            raise PipelineError("Authenticated receipt was modified after retrieval")
        return True


@contextlib.contextmanager
def security_refresh_context(refreshes):
    """Apply only typed, origin-verified promotion receipts to nested provider checks."""
    from .security import SecurityRefresh
    if (not isinstance(refreshes, dict) or not refreshes
            or any(not isinstance(path, str) or not path or not isinstance(proof, SecurityRefresh)
                   for path, proof in refreshes.items())):
        raise PipelineError("Security refresh context requires authenticated typed receipts")
    token = _SECURITY_REFRESH.set(dict(refreshes))
    try:
        yield
    finally:
        _SECURITY_REFRESH.reset(token)


def _candidate_time(candidate):
    try:
        created = dt.datetime.fromisoformat(candidate["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or created > dt.datetime.now(dt.timezone.utc):
            raise ValueError("missing timezone or future candidate")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PipelineError("Candidate creation timestamp is missing, invalid or future") from exc
    return created


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def verify_checks(candidate, root, *, historical_security=False, security_refresh=None):
    if type(historical_security) is not bool:
        raise PipelineError("Historical security verification must be explicit boolean")
    created = _candidate_time(candidate)
    requirements = candidate.get("check_requirements", {})
    if not requirements or set(requirements) != set(candidate.get("checks", {})):
        raise PipelineError("Candidate requires an explicit contract for every check")
    refreshes = security_refresh if security_refresh is not None else _SECURITY_REFRESH.get()
    if historical_security:
        if security_refresh is not None:
            raise PipelineError("Transport cannot authorize promotion with a security refresh")
        if (any(key not in candidate for key in IDENTITY_FIELDS)
                or candidate.get("identity") != fingerprint({key: candidate[key] for key in IDENTITY_FIELDS})):
            raise PipelineError("Historical security requires an intact candidate identity and creation time")
        refreshes = None
    if refreshes is not None:
        from .security import SecurityRefresh
        security_paths = {path for path, requirement in requirements.items()
                          if isinstance(requirement, dict) and requirement.get("kind") == "security"}
        if (not isinstance(refreshes, dict) or set(refreshes) - security_paths
                or any(not isinstance(value, SecurityRefresh) for value in refreshes.values())):
            raise PipelineError("Security refresh does not match the candidate's security requirements")
    for relative, requirement in requirements.items():
        strict_keys(requirement, {"kind", "config"}, {"kind", "config"})
        path = confined_path(root, relative)
        receipt = load_json(path)
        if sha256_file(path) != candidate["checks"][relative] or receipt.get("status") != "passed":
            raise PipelineError("Candidate check evidence changed or did not pass")
        if requirement["config"] not in candidate["inputs"]:
            raise PipelineError("Check configuration must be a recorded candidate input")
        config_path = confined_path(root, requirement["config"])
        assurance = receipt.get("assurance", {})
        expected = {"kind": requirement["kind"], "source_sha": candidate["source_sha"],
                    "source_tree": candidate["source_tree"], "config_sha256": sha256_file(config_path)}
        if assurance != expected:
            raise PipelineError("Check is not bound to the candidate source and configuration")
        if requirement["kind"] == "security":
            from .security import require_policy_fresh
            policy = load_json(config_path)
            # The preserved build scan must genuinely have passed while fresh when
            # this immutable candidate was created, even when a new scan is supplied.
            require_policy_fresh(receipt, policy, at=created)
            if not historical_security:
                if refreshes and relative in refreshes:
                    refreshes[relative].validate(candidate, root)
                else:
                    require_policy_fresh(receipt, policy)


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


def create(config, root, *, source_root=None, expected_checks=None):
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
    checks, requirements = {}, {}
    for entry in config["checks"]:
        strict_keys(entry, {"path", "kind", "config"}, {"path", "kind", "config"})
        relative = entry["path"]
        if relative in checks or not isinstance(entry["kind"], str) or not entry["kind"]:
            raise PipelineError("Duplicate or untyped candidate check")
        receipt = load_json(confined_path(root, relative))
        if receipt.get("status") != "passed":
            raise PipelineError("Candidate contains a missing or unsuccessful check")
        checks[relative] = sha256_file(confined_path(root, relative))
        requirements[relative] = {k: entry[k] for k in ("kind", "config")}
    if expected_checks is not None and (not isinstance(expected_checks, dict) or expected_checks != checks):
        raise PipelineError("Candidate check hashes do not match the trusted workflow step outputs")
    source_root = Path(source_root or root).resolve()
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source_root).stdout.strip():
        raise PipelineError("Candidate source has uncommitted tracked changes")
    source = run(["git", "rev-parse", "HEAD"], cwd=source_root).stdout.strip()
    tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=source_root).stdout.strip()
    inputs = file_records(root, config["inputs"])
    artifacts = file_records(root, config["artifacts"])
    if any(v["bytes"] == 0 for v in artifacts.values()):
        raise PipelineError("Required release artifact is empty")
    result = {"schema": 1, "status": "passed", "product": config["product"], "version": config["version"],
              "build": config["build"], "source_sha": source, "source_tree": tree,
              "library_sha": config["library_sha"], "toolchain": config["toolchain"],
              "inputs": inputs, "artifacts": artifacts, "checks": checks, "check_requirements": requirements, "created_at": now(),
              "run": {k: os.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}}
    verify_checks(result, root)
    result["identity"] = fingerprint({k: result[k] for k in IDENTITY_FIELDS})
    return result


def verify(candidate, root, *, expected_library_sha=None, expected_toolchain=None, require_tree=True,
           historical_security=False, security_refresh=None, source_root=None):
    if candidate.get("schema") != 1 or candidate.get("status") != "passed":
        raise PipelineError("Invalid candidate schema/status")
    for group in ("inputs", "artifacts"):
        if file_records(root, list(candidate.get(group, {}))) != candidate[group]:
            raise PipelineError(f"Candidate {group} changed or missing")
    if not isinstance(candidate.get("checks"), dict) or not candidate["checks"]:
        raise PipelineError("Candidate check evidence is missing")
    verify_checks(candidate, root, historical_security=historical_security, security_refresh=security_refresh)
    identity = fingerprint({k: candidate[k] for k in IDENTITY_FIELDS})
    if identity != candidate.get("identity"):
        raise PipelineError("Candidate identity is corrupt")
    if expected_library_sha is not None and candidate["library_sha"] != expected_library_sha:
        raise PipelineError("Library revision invalidates candidate evidence")
    if expected_toolchain is not None and candidate["toolchain"] != expected_toolchain:
        raise PipelineError("Toolchain invalidates candidate evidence")
    verify_source_tree = require_tree or source_root is not None
    source_root = Path(source_root or root).resolve()
    if verify_source_tree and run(["git", "rev-parse", "HEAD^{tree}"], cwd=source_root).stdout.strip() != candidate.get("source_tree"):
        raise PipelineError("Full merged source tree differs from the candidate")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source_root).stdout.strip():
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


def fetch_receipt(repository, run_id, artifact_name, expected_sha, expected_workflow, token, *, request=_api, coverage_paths=(),
                  producer_job=None, run_attempt=None, allowed_branches=None, allowed_events=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not str(run_id).isdigit():
        raise PipelineError("Invalid repository/run identity")
    if not SHA.fullmatch(expected_sha) or not expected_workflow.startswith(".github/workflows/"):
        raise PipelineError("Explicit source SHA and trusted workflow path are required")
    if not token:
        raise PipelineError("GitHub evidence token is missing")
    prefix = f"/repos/{repository}/actions/runs/{run_id}"
    metadata = request(prefix, token)
    if any(value is not None for value in (producer_job, run_attempt, allowed_branches, allowed_events)):
        # A lazy import avoids artifacts -> evidence -> artifacts at module loading.
        from .artifacts import authenticated_artifact
        config = {"repository": repository, "run_id": str(run_id), "run_attempt": run_attempt,
                  "expected_source_sha": expected_sha, "expected_workflow": expected_workflow,
                  "artifact_name": artifact_name, "allowed_branches": allowed_branches,
                  "allowed_events": allowed_events if allowed_events is not None else ["push", "pull_request", "workflow_dispatch"]}
        if producer_job is not None:
            config["producer_job"] = producer_job
        def cached_request(path, credential, binary=False):
            return metadata if path == prefix and not binary else request(path, credential, binary)
        item = authenticated_artifact(config, token, cached_request)
    else:
        # Existing callers remain completed-run-only. New wrappers supply exact
        # attempt/branch policy, also allowing a completed producer in this run.
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
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or "receipt.json" not in names or archive.getinfo("receipt.json").file_size > 1024 * 1024:
                raise PipelineError("Evidence artifact must contain one bounded receipt.json")
            receipt = json.loads(archive.read("receipt.json"))
            hashes = receipt.get("coverage", {})
            if coverage_paths and (not isinstance(hashes, dict) or set(coverage_paths) - set(hashes)):
                raise PipelineError("Expected coverage is missing from the authenticated receipt")
            expected_names = {"receipt.json"}
            if hashes:
                if not isinstance(hashes, dict) or len(hashes) > 50 or any(not DIGEST.fullmatch(str(digest)) for digest in hashes.values()):
                    raise PipelineError("Invalid coverage digest manifest")
                expected_names |= {"coverage/" + digest for digest in hashes.values()}
            # upload-artifact can retain explicit directory entries, but never extra files.
            actual_files = {entry.filename for entry in entries if not entry.is_dir()}
            if actual_files != expected_names or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
                raise PipelineError("Unexpected or oversized files in evidence artifact")
            coverage_files = {}
            for relative in coverage_paths:
                content = archive.read("coverage/" + hashes[relative])
                if hashlib.sha256(content).hexdigest() != hashes[relative]:
                    raise PipelineError("Coverage bytes do not match the authenticated receipt")
                coverage_files[relative] = content
    except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise PipelineError("Malformed evidence artifact") from exc
    if receipt.get("status") != "passed":
        raise PipelineError("Receipt did not pass")
    return AuthenticatedReceipt({"receipt": receipt, "coverage_files": coverage_files,
                                 "provenance": {"repository": repository, "run_id": str(run_id),
                                                "source_sha": expected_sha, "workflow": expected_workflow,
                                                "artifact_digest": item["digest"], "event": metadata.get("event"),
                                                "head_branch": metadata.get("head_branch"),
                                                "run_attempt": metadata.get("run_attempt")}}, _proof=_FETCH_PROOF)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--source-root", help="Separate clean source checkout for Apple archive evidence")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.operation == "verify" and args.source_root:
        raise PipelineError("Source-root override is supported only for candidate creation")
    result = create(load_json(args.config), args.root, source_root=args.source_root) if args.operation == "create" else verify(load_json(args.config), args.root)
    atomic_json(args.output, result)


if __name__ == "__main__":
    cli_error(main)
