"""Restore only authenticated, exact-request store state from an earlier workflow attempt."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path

from .artifacts import download, no_symlink, timestamp
from .common import PipelineError, atomic_json, cli_error, confined_path, fingerprint, load_json, sha256_file
from .common import strict_keys, trusted_context
from .evidence import _api
from .stores import HEX, SHA, env_secret, identifier, state_file

STATE_NAME = "operation-state.json"
MAX_FILE = 1024 * 1024
MAX_ARCHIVE = 2 * MAX_FILE
PHASES = {
    "play-inspect": {"edit_created", "done"},
    "play-upload": {"edit_created", "upload_started", "uploaded", "track_updated", "commit_started", "done"},
    "play-promote": {"edit_created", "track_updated", "commit_started", "done"},
    "apple-build": set(), "apple-select": set(), "apple-status": set(), "apple-beta-notes": set(),
    "testflight": {"done"}, "apple-submit": {"creating_submission", "submission_created", "done"},
    "firebase-distribution": {"upload_started", "uploaded", "distribution_started", "done"},
}


def serialized(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def validate_store_state(value, config):
    allowed = {"schema", "request", "phase", "edit_id", "updated_track", "receipt", "submission_id", "artifact_sha256", "release_name"}
    strict_keys(value, allowed, {"schema", "request", "phase"})
    operation = config.get("operation")
    if (operation not in PHASES or value["schema"] != 1 or value["request"] != fingerprint(config)
            or value["phase"] not in PHASES[operation]):
        raise PipelineError("Retained store state does not match the exact requested operation/configuration")
    if not HEX.fullmatch(str(config.get("expected_identity", ""))) or not SHA.fullmatch(str(config.get("expected_source_sha", ""))):
        raise PipelineError("Store state needs an explicitly selected candidate/source")
    if operation.startswith("play-"):
        identifier(value.get("edit_id"), "retained Play edit")
        if value["phase"] in ("track_updated", "commit_started") and not isinstance(value.get("updated_track"), dict):
            raise PipelineError("Retained Play commit state lacks its track update")
    elif "edit_id" in value or "updated_track" in value:
        raise PipelineError("Non-Play state contains Play fields")
    if "submission_id" in value:
        if operation != "apple-submit":
            raise PipelineError("Retained submission belongs to another store operation")
        identifier(value["submission_id"], "Apple review submission")
    if operation == "apple-submit" and value["phase"] == "submission_created" and "submission_id" not in value:
        raise PipelineError("Retained Apple submission ID is missing")
    if operation == "firebase-distribution":
        if not HEX.fullmatch(str(value.get("artifact_sha256", ""))):
            raise PipelineError("Retained Firebase upload digest is missing")
        if value["phase"] in ("distribution_started", "done") and not re.fullmatch(
                r"projects/[0-9]+/apps/1:[0-9]+:android:[0-9a-f]+/releases/[A-Za-z0-9_-]+", str(value.get("release_name", ""))):
            raise PipelineError("Retained Firebase release identity is missing")
    elif "artifact_sha256" in value or "release_name" in value:
        raise PipelineError("Non-Firebase state contains Firebase fields")
    if value["phase"] == "done":
        receipt = value.get("receipt")
        if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("status") not in ("passed", "pending")
                or receipt.get("operation") != operation or receipt.get("candidate_identity") != config["expected_identity"]):
            raise PipelineError("Retained completed state has no matching store receipt")
    elif "receipt" in value:
        raise PipelineError("Unfinished store state cannot contain a completed receipt")
    if len(serialized(value)) > MAX_FILE // 2:
        raise PipelineError("Retained store state exceeds its bounded schema")
    return value


def snapshot(store_config, root):
    """Wrapper uses this only when state_file exists, including on failed/cancelled attempts."""
    raw_path = no_symlink(root, store_config["state_file"])
    path, value = state_file(store_config, root)
    if not raw_path.is_file() or not path.is_file():
        raise PipelineError("No durable store operation state exists to retain")
    validate_store_state(value, store_config)
    state_bytes = serialized(value)
    if path.read_bytes() != state_bytes:
        raise PipelineError("Store state does not use the canonical module serialization")
    return {"schema": 1, "kind": "store-operation-state", "operation": store_config["operation"],
            "candidate_identity": store_config["expected_identity"], "source_sha": store_config["expected_source_sha"],
            "request_fingerprint": fingerprint(store_config), "state_sha256": hashlib.sha256(state_bytes).hexdigest(), "state": value}


def authenticated_state_artifact(selection, config, token, request):
    repository = selection["repository"]
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repository))
            or repository != os.environ.get("GITHUB_REPOSITORY") or not str(selection["run_id"]).isdigit()):
        raise PipelineError("Store state must come from the selected current repository/run")
    run_head = selection["run_head_sha"]
    if not SHA.fullmatch(str(run_head)):
        raise PipelineError("Explicit prior store workflow head SHA is required")
    attempt = selection["run_attempt"]
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise PipelineError("Exact positive prior state run attempt is required")
    if str(selection["run_id"]) == os.environ.get("GITHUB_RUN_ID"):
        current_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        if not current_attempt.isdigit() or attempt >= int(current_attempt):
            raise PipelineError("Cannot restore state from the current or a future attempt")
    workflow, producer = selection["expected_workflow"], selection["producer_job"]
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", str(workflow)) or not isinstance(producer, str) or not producer.strip() or len(producer) > 200:
        raise PipelineError("Exact state producer workflow and job are required")
    branches, events, conclusions = selection["allowed_branches"], selection["allowed_events"], selection["allowed_conclusions"]
    if (not isinstance(branches, list) or not branches
            or any(not isinstance(b, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", b) or b.startswith(("refs/", "/")) or b.endswith("/") or ".." in b for b in branches)
            or len(branches) != len(set(branches))):
        raise PipelineError("Explicit state producer branches are required")
    if not isinstance(events, list) or not events or any(e not in ("push", "workflow_dispatch") for e in events):
        raise PipelineError("Store state producer must be an explicit trusted push/manual event")
    if not isinstance(conclusions, list) or not conclusions or any(c not in ("success", "failure", "cancelled", "timed_out") for c in conclusions):
        raise PipelineError("Explicit completed state producer conclusions are required")
    base = f"/repos/{repository}/actions/runs/{selection['run_id']}"
    # The attempt endpoint preserves old failure/cancellation metadata even after a rerun starts.
    attempt_path = base + f"/attempts/{attempt}"
    metadata = request(attempt_path, token)
    if (metadata.get("id") != int(selection["run_id"]) or metadata.get("run_attempt") != attempt
            or metadata.get("status") != "completed" or metadata.get("conclusion") not in conclusions
            or metadata.get("head_sha") != run_head
            or metadata.get("path", "").split("@")[0] != workflow
            or metadata.get("head_repository", {}).get("full_name") != repository
            or metadata.get("head_branch") not in branches or metadata.get("event") not in events):
        raise PipelineError("Prior store run attempt has untrusted source/workflow/branch/event/outcome")
    jobs = []
    for page in range(1, 21):
        items = request(attempt_path + f"/jobs?per_page=100&page={page}", token).get("jobs")
        if not isinstance(items, list):
            raise PipelineError("Malformed prior state job inventory")
        jobs.extend(job for job in items if job.get("name") == producer)
        if len(items) < 100:
            break
    else:
        raise PipelineError("Prior state job inventory exceeds limit")
    if (len(jobs) != 1 or jobs[0].get("run_id") != int(selection["run_id"])
            or not isinstance(jobs[0].get("id"), int) or jobs[0]["id"] <= 0
            or jobs[0].get("status") != "completed" or jobs[0].get("conclusion") not in conclusions
            or jobs[0].get("head_sha") != run_head or jobs[0].get("head_branch") != metadata.get("head_branch")):
        raise PipelineError("Exactly one completed approved state producer job is required")
    job = jobs[0]
    start = max(timestamp(metadata.get("run_started_at")), timestamp(job.get("started_at")))
    end = min(timestamp(metadata.get("updated_at")), timestamp(job.get("completed_at")))
    matches = []
    for page in range(1, 21):
        items = request(base + f"/artifacts?per_page=100&page={page}", token).get("artifacts")
        if not isinstance(items, list):
            raise PipelineError("Malformed prior state artifact inventory")
        for item in items:
            if item.get("name") == selection["artifact_name"] and not item.get("expired"):
                created = timestamp(item.get("created_at"))
                if start <= created <= end:
                    matches.append(item)
        if len(items) < 100:
            break
    else:
        raise PipelineError("Prior state artifact inventory exceeds limit")
    if len(matches) != 1:
        raise PipelineError("Exactly one state artifact from the selected producer attempt is required")
    artifact = matches[0]
    if (not isinstance(artifact.get("id"), int) or artifact["id"] <= 0
            or artifact.get("workflow_run", {}).get("id") != int(selection["run_id"])
            or artifact.get("workflow_run", {}).get("head_sha") != run_head
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", "")))
            or not isinstance(artifact.get("size_in_bytes"), int) or not 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE):
        raise PipelineError("State artifact source, digest or bounded size is invalid")
    return artifact, job


def read_envelope(archive_path, temporary, config):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            items = archive.infolist()
            names = [item.filename for item in items]
            if len(names) != len(set(names)) or not {STATE_NAME} <= set(names) <= {STATE_NAME, "receipt.json"}:
                raise PipelineError("State artifact must contain only operation-state.json and optional receipt.json")
            for item in items:
                mode = item.external_attr >> 16
                if item.is_dir() or item.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or not 0 < item.file_size <= MAX_FILE:
                    raise PipelineError("State artifact contains a link, special/encrypted or oversized file")
                (temporary / item.filename).write_bytes(archive.read(item))
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise PipelineError("Malformed state artifact archive") from None
    envelope = load_json(temporary / STATE_NAME)
    required = {"schema", "kind", "operation", "candidate_identity", "source_sha", "request_fingerprint", "state_sha256", "state"}
    strict_keys(envelope, required, required)
    if (envelope["schema"] != 1 or envelope["kind"] != "store-operation-state" or envelope["operation"] != config["operation"]
            or envelope["candidate_identity"] != config["expected_identity"] or envelope["source_sha"] != config["expected_source_sha"]
            or envelope["request_fingerprint"] != fingerprint(config)):
        raise PipelineError("State envelope belongs to another operation/candidate/source/configuration")
    value = validate_store_state(envelope["state"], config)
    if hashlib.sha256(serialized(value)).hexdigest() != envelope["state_sha256"]:
        raise PipelineError("State envelope payload digest is corrupt")
    receipt_path = temporary / "receipt.json"
    if receipt_path.exists():
        receipt = load_json(receipt_path)
        if (not isinstance(receipt, dict) or receipt.get("status") not in ("passed", "pending", "failed")
                or receipt.get("operation") != config["operation"] or receipt.get("candidate_identity") != config["expected_identity"]):
            raise PipelineError("Optional store receipt belongs to another operation/candidate")
    return value


def restore(selection, root, *, store_config, request=_api, downloader=download):
    required = {"schema", "operation", "repository", "run_id", "run_attempt", "run_head_sha", "artifact_name", "expected_workflow", "producer_job",
                "allowed_branches", "allowed_events", "allowed_conclusions", "token_env"}
    strict_keys(selection, required, required)
    if selection["schema"] != 1 or selection["operation"] != "restore":
        raise PipelineError("Unsupported state restore operation")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(selection["artifact_name"])):
        raise PipelineError("Invalid state artifact name")
    trusted_context(store_config["allowed_refs"])
    no_symlink(root, store_config["state_file"])
    target, existing = state_file(store_config, root)
    token = env_secret(selection["token_env"])
    artifact, producer = authenticated_state_artifact(selection, store_config, token, request)
    with tempfile.TemporaryDirectory(prefix="shared-store-state-") as temp:
        temporary = Path(temp)
        archive_path = temporary / "download.zip"
        downloader(f"/repos/{selection['repository']}/actions/artifacts/{artifact['id']}/zip", token, archive_path, MAX_ARCHIVE)
        if archive_path.stat().st_size > MAX_ARCHIVE or "sha256:" + sha256_file(archive_path) != artifact["digest"]:
            raise PipelineError("State archive differs from authenticated artifact digest")
        value = read_envelope(archive_path, temporary, store_config)
        expected_bytes = serialized(value)
        if target.exists():
            if not target.is_file() or target.read_bytes() != expected_bytes:
                raise PipelineError("Restored state conflicts with existing local operation state")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Exclusive creation prevents replacement if another process creates state meanwhile.
                with target.open("xb") as stream:
                    stream.write(expected_bytes)
                target.chmod(0o600)
            except OSError:
                raise PipelineError("Could not restore state without overwriting existing data") from None
    return {"schema": 1, "status": "passed", "operation": "restore", "store_operation": store_config["operation"],
            "candidate_identity": store_config["expected_identity"], "request_fingerprint": fingerprint(store_config), "phase": value["phase"],
            "state_sha256": hashlib.sha256(expected_bytes).hexdigest(), "state_file": store_config["state_file"],
            "provenance": {"repository": selection["repository"], "run_id": str(selection["run_id"]), "run_attempt": selection["run_attempt"],
                           "run_head_sha": selection["run_head_sha"],
                           "workflow": selection["expected_workflow"], "producer_job_id": producer["id"], "producer_conclusion": producer["conclusion"],
                           "artifact_id": artifact["id"], "artifact_digest": artifact["digest"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--store-config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    atomic_json(args.output, restore(load_json(args.config), args.root, store_config=load_json(args.store_config)))


if __name__ == "__main__":
    cli_error(main)
