"""Find original quality evidence for this promotion; uncertainty requires fresh checks."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

from . import reuse
from .common import PipelineError, confined_path, run, strict_keys
from .evidence import _api, fetch_receipt

EVIDENCE_ERRORS = (OSError, ValueError, TypeError, AttributeError, KeyError)


def policy(config, root, quality_path):
    strict_keys(config, {"schema", "quality_config", "source", "max_age_seconds", "max_runs", "token_env"},
                {"schema", "quality_config", "source", "max_age_seconds"})
    if type(config["schema"]) is not int or config["schema"] != 1:
        raise PipelineError("Unsupported quality discovery schema")
    if confined_path(root, config["quality_config"]) != Path(quality_path).resolve():
        raise PipelineError("Quality discovery must select this job's quality configuration")
    maximum, limit = config["max_age_seconds"], config.get("max_runs", 20)
    if type(maximum) is not int or not 1 <= maximum <= 604800 or type(limit) is not int or not 1 <= limit <= 100:
        raise PipelineError("Quality discovery needs bounded freshness and 1–100 candidate runs")
    source = config["source"]
    fields = {"repository", "workflow", "artifact_prefix", "producer_job", "allowed_branches", "allowed_events"}
    strict_keys(source, fields, fields)
    repository = source["repository"]
    if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or os.environ.get("GITHUB_REPOSITORY") != repository):
        raise PipelineError("Quality discovery must select the current product repository")
    if not isinstance(source["workflow"], str) or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", source["workflow"]):
        raise PipelineError("Quality discovery needs one trusted producer workflow")
    if not isinstance(source["artifact_prefix"], str):
        raise PipelineError("Quality artifact namespace must be literal")
    reuse._identity(source["artifact_prefix"], "quality artifact namespace", r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
    if not isinstance(source["producer_job"], str) or not reuse._producer(source["producer_job"]):
        raise PipelineError("Quality discovery needs one exact producer job")
    branches = reuse._allowlist(source["allowed_branches"], "allowed_branches")
    events = reuse._allowlist(source["allowed_events"], "allowed_events")
    token_name = config.get("token_env", "GH_TOKEN")
    if not isinstance(token_name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_name):
        raise PipelineError("Invalid quality discovery token environment name")
    return source, branches, events, limit, os.environ.get(token_name, "")


def ancestor(root, sha):
    return (isinstance(sha, str) and reuse.SHA.fullmatch(sha) is not None
            and run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=root, check=False).returncode == 0)


def source_in_candidate(metadata, source, branches, root):
    """Branches constrain trust; an authenticated commit relation selects the candidate."""
    event, head = metadata.get("event"), metadata.get("head_sha")
    if event != "pull_request":
        return metadata.get("head_branch") in branches and ancestor(root, head)
    pulls = metadata.get("pull_requests")
    if not isinstance(pulls, list) or len(pulls) != 1 or not isinstance(pulls[0], dict):
        return False
    number = pulls[0].get("number")
    if type(number) is not int or number < 1:
        return False
    pr = pulls[0]
    base, pr_head = pr.get("base"), pr.get("head")
    if not isinstance(base, dict) or not isinstance(pr_head, dict):
        return False
    return (pr.get("url") == f"https://api.github.com/repos/{source['repository']}/pulls/{number}"
            and base.get("ref") in branches and pr_head.get("sha") == head and ancestor(root, head))


def discover(config, root, quality_path, *, request=None, library_root=None):
    """Return a verified reuse receipt or None. Invalid caller policy still fails."""
    root = Path(root).resolve()
    source, branches, events, limit, token = policy(config, root, quality_path)
    if not token:
        return None
    request = request or _api
    repository, workflow = source["repository"], source["workflow"]
    try:
        workflow_id = quote(workflow.rsplit("/", 1)[-1], safe="")
        listing = request(f"/repos/{repository}/actions/workflows/{workflow_id}/runs?status=success&per_page={limit}&page=1", token)
        runs = listing.get("workflow_runs") if isinstance(listing, dict) else None
        if not isinstance(runs, list) or len(runs) > limit:
            return None
    except EVIDENCE_ERRORS:
        return None
    for item in runs:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
            continue
        run_id = str(item["id"])
        if run_id == os.environ.get("GITHUB_RUN_ID"):
            continue
        cache = {}
        def cached(path, credential, binary=False):
            key = (path, binary)
            if key not in cache:
                cache[key] = request(path, credential, binary)
            return cache[key]
        try:
            metadata = cached(f"/repos/{repository}/actions/runs/{run_id}", token)
            if (not isinstance(metadata, dict) or metadata.get("id") != item["id"]
                    or metadata.get("status") != "completed" or metadata.get("conclusion") != "success"
                    or not isinstance(metadata.get("path"), str) or metadata["path"].split("@")[0] != workflow
                    or metadata.get("event") not in events
                    or not isinstance(metadata.get("head_repository"), dict)
                    or metadata["head_repository"].get("full_name") != repository):
                continue
            head = reuse._identity(metadata.get("head_sha"), "run head SHA", reuse.SHA)
            attempt = reuse._attempt(metadata.get("run_attempt"))
            branch = reuse._allowlist([metadata.get("head_branch")], "allowed_branches")
            if not source_in_candidate(metadata, source, branches, root):
                continue
            artifact = f"{source['artifact_prefix']}-run-{run_id}-attempt-{attempt}"
            verified = fetch_receipt(repository, run_id, artifact, head, workflow, token, request=cached,
                                     run_attempt=attempt, producer_job=source["producer_job"],
                                     allowed_branches=branch, allowed_events=events)
            receipt = verified["receipt"]
            provenance = receipt.get("provenance") if isinstance(receipt, dict) else None
            tested = provenance.get("sha") if isinstance(provenance, dict) else None
            selection = {"schema": 1, "quality_config": config["quality_config"],
                         "max_age_seconds": config["max_age_seconds"], "token_env": config.get("token_env", "GH_TOKEN"),
                         "source": {"repository": repository, "workflow": workflow, "run_id": run_id,
                                    "run_head_sha": head, "source_sha": tested, "run_attempt": attempt,
                                    "artifact": artifact, "producer_job": source["producer_job"],
                                    "allowed_branches": branch, "allowed_events": events}}
            return reuse.execute(selection, root, request=cached, library_root=library_root)
        except EVIDENCE_ERRORS:
            # Neither a lookup error nor rejected evidence is a passing check.
            # The caller executes its normal quality runner after all misses.
            continue
    return None
