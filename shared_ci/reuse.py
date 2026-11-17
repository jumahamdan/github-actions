"""Reuse authenticated quality evidence only after comparing its complete inputs."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .assurance import library_identity, stamp
from .common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file, strict_keys
from .evidence import fetch_receipt
from .quality import input_digest, snapshot

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
LIBRARY = Path(__file__).resolve().parent.parent


def _selection(value):
    """Resolve explicit runtime selections without interpolation or implicit discovery."""
    if isinstance(value, dict):
        strict_keys(value, {"env"}, {"env"})
        if not isinstance(value["env"], str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value["env"]):
            raise PipelineError("Invalid quality-source environment name")
        value = os.environ.get(value["env"], "")
    return value


def _identity(value, name, pattern):
    value = _selection(value)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PipelineError(f"Explicit valid {name} is required")
    return value


def _attempt(value):
    value = _selection(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        try:
            value = int(value)
        except ValueError:
            raise PipelineError("Explicit positive quality run attempt is required") from None
    if type(value) is not int or value < 1:
        raise PipelineError("Explicit positive quality run attempt is required")
    return value


def _allowlist(value, name):
    if isinstance(value, dict):
        value = _selection(value)
        if not isinstance(value, str) or len(value) > 65536:
            raise PipelineError(f"Explicit quality {name} must be a JSON array")
        try:
            value = json.loads(value)
        except ValueError:
            raise PipelineError(f"Explicit quality {name} must be a JSON array") from None
    if (not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))):
        raise PipelineError(f"Explicit unique quality {name} are required")
    if name == "allowed_events":
        valid = all(item in ("push", "pull_request", "workflow_dispatch") for item in value)
    else:
        valid = all(re.fullmatch(r"[A-Za-z0-9_./-]+", item) and not item.startswith(("refs/", "/"))
                    and not item.endswith("/") and ".." not in item for item in value)
    if not valid:
        raise PipelineError(f"Invalid quality {name} selection")
    return value


def _producer(value):
    value = _selection(value)
    if value is None or value == "":
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PipelineError("Explicit quality producer job name is invalid")
    return value


def _fresh(receipt, maximum):
    try:
        created = datetime.fromisoformat(receipt["created_at"])
        if created.tzinfo is None:
            raise ValueError("missing timezone")
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if not 0 <= age <= maximum:
            raise ValueError("stale or future timestamp")
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("Fresh quality evidence is required: receipt is stale or timestamp invalid") from exc


def _checks(receipt, config):
    expected, required = config.get("checks"), config.get("required_checks")
    if not isinstance(expected, list) or not expected or not isinstance(required, list) or not required:
        raise PipelineError("Quality configuration must declare required checks")
    names = [entry.get("name") for entry in expected if isinstance(entry, dict)]
    if (len(names) != len(expected) or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names) or any(not isinstance(name, str) for name in required)
            or len(set(required)) != len(required) or set(required) - set(names)):
        raise PipelineError("Invalid required quality check names")
    actual = receipt.get("checks")
    if not isinstance(actual, list) or len(actual) != len(expected) or not all(isinstance(c, dict) for c in actual):
        raise PipelineError("Fresh quality evidence is required: checks are missing")
    recorded = {entry.get("name"): entry for entry in actual if isinstance(entry.get("name"), str)}
    if set(recorded) != set(names) or len(recorded) != len(actual):
        raise PipelineError("Fresh quality evidence is required: checks differ or are duplicated")
    tests = 0
    for entry in expected:
        check = recorded[entry["name"]]
        if check.get("kind") != entry.get("kind") or check.get("status") != "passed":
            raise PipelineError("Fresh quality evidence is required: a configured check did not pass")
        if entry.get("kind") == "test":
            summary = check.get("test_summary", {})
            if (not isinstance(summary, dict) or type(summary.get("executed")) is not int or summary["executed"] < 1
                    or type(summary.get("failed")) is not int or summary["failed"] != 0
                    or type(summary.get("skipped")) is not int or summary["skipped"] < 0
                    or not isinstance(summary.get("sha256"), str) or not DIGEST.fullmatch(summary["sha256"])):
                raise PipelineError("Fresh quality evidence is required: executed successful tests are missing")
            tests += 1
    if not tests:
        raise PipelineError("Quality reuse requires at least one executed test check")


def execute(config, root, *, request=None, library_root=None):
    strict_keys(config, {"schema", "quality_config", "source", "token_env", "max_age_seconds"},
                {"schema", "quality_config", "source", "max_age_seconds"})
    if type(config["schema"]) is not int or config["schema"] != 1:
        raise PipelineError("Unsupported quality reuse schema")
    maximum = config["max_age_seconds"]
    if type(maximum) is not int or not 1 <= maximum <= 604800:
        raise PipelineError("Quality evidence age must be explicitly bounded to 1–604800 seconds")
    source = config["source"]
    source_fields = {"repository", "workflow", "run_id", "run_attempt", "run_head_sha", "source_sha", "artifact",
                     "allowed_branches", "allowed_events"}
    strict_keys(source, source_fields | {"producer_job"}, source_fields)
    repository = source["repository"]
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PipelineError("Explicit quality repository is required")
    if os.environ.get("GITHUB_REPOSITORY") and os.environ["GITHUB_REPOSITORY"] != repository:
        raise PipelineError("Quality reuse must stay within the current product repository")
    workflow = source["workflow"]
    if not isinstance(workflow, str) or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise PipelineError("Explicit trusted quality workflow is required")
    artifact = _identity(source["artifact"], "quality artifact name", r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    run_id = _identity(source["run_id"], "quality run ID", r"[1-9][0-9]*")
    run_attempt = _attempt(source["run_attempt"])
    run_head = _identity(source["run_head_sha"], "Actions run head SHA", SHA)
    tested_sha = _identity(source["source_sha"], "tested source SHA", SHA)
    branches = _allowlist(source["allowed_branches"], "allowed_branches")
    events = _allowlist(source["allowed_events"], "allowed_events")
    producer = _producer(source.get("producer_job"))
    token_name = config.get("token_env", "GH_TOKEN")
    if not isinstance(token_name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_name):
        raise PipelineError("Invalid GitHub token environment name")
    root = Path(root).resolve()
    quality_path = confined_path(root, config["quality_config"])
    expected_config_hash = sha256_file(quality_path)
    quality = load_json(quality_path)
    if not isinstance(quality, dict) or type(quality.get("version")) is not int or quality["version"] != 1:
        raise PipelineError("Quality reuse requires the current version 1 quality configuration")
    # Quality does not record these values. Matching names cannot prove matching inputs.
    if quality.get("build_credentials") or quality.get("environment_names"):
        raise PipelineError("Quality reuse cannot prove unchanged credential or environment-dependent checks")
    options = {"request": request} if request is not None else {}
    verified = fetch_receipt(repository, run_id, artifact, run_head, workflow,
                             os.environ.get(token_name, ""), run_attempt=run_attempt, producer_job=producer,
                             allowed_branches=branches, allowed_events=events, **options)
    receipt = verified["receipt"]
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("kind") != "quality" or receipt.get("status") != "passed"
            or receipt.get("profile") != quality.get("profile") or "reused_from" in receipt):
        raise PipelineError("Original successful quality evidence is required; reused evidence cannot be chained")
    if receipt.get("provenance") != {"repository": repository, "run_id": run_id, "sha": tested_sha}:
        raise PipelineError("Quality receipt does not belong to the verified run and tested source")
    assurance = receipt.get("assurance", {})
    if (not isinstance(assurance, dict) or set(assurance) != {"kind", "source_sha", "source_tree", "config_sha256"}
            or assurance.get("kind") != "quality" or assurance.get("source_sha") != tested_sha
            or not isinstance(assurance.get("source_tree"), str) or not SHA.fullmatch(assurance["source_tree"])
            or assurance.get("config_sha256") != expected_config_hash
            or receipt.get("config_sha256") != expected_config_hash):
        raise PipelineError("Fresh quality evidence is required: source/configuration assurance differs")
    _fresh(receipt, maximum)
    inputs = snapshot(root, quality.get("inputs", []))
    if (receipt.get("input_paths") != quality.get("inputs") or receipt.get("inputs") != inputs
            or receipt.get("input_digest") != input_digest(inputs)):
        raise PipelineError("Fresh quality evidence is required: tested source selection or file content changed")
    locks = quality.get("lockfiles", [])
    if not isinstance(locks, list) or any(not isinstance(path, str) for path in locks) or len(locks) != len(set(locks)):
        raise PipelineError("Invalid quality lockfile selection")
    lock_hashes = {path: sha256_file(confined_path(root, path)) for path in locks}
    if receipt.get("lockfiles") != lock_hashes:
        raise PipelineError("Fresh quality evidence is required: dependency locks changed")
    immutable = snapshot(root, quality.get("immutable_paths", quality.get("inputs", [])))
    covered = {**inputs, **lock_hashes}
    if any(covered.get(path) != digest for path, digest in immutable.items()):
        raise PipelineError("Fresh quality evidence is required: immutable files are outside recorded inputs/locks")
    if not quality.get("toolchain") or receipt.get("toolchain") != quality["toolchain"]:
        raise PipelineError("Fresh quality evidence is required: toolchain expectations changed")
    library = receipt.get("library", {})
    if not isinstance(library, dict) or library.get("identity") != library_identity(library_root or LIBRARY):
        raise PipelineError("Fresh quality evidence is required: shared library implementation changed")
    _checks(receipt, quality)
    result = copy.deepcopy(receipt)
    result["reused_from"] = {
        **verified["provenance"], "run_head_sha": run_head, "source_sha": tested_sha,
        "source_tree": assurance["source_tree"], "artifact": artifact,
        "assurance": copy.deepcopy(assurance), "library": copy.deepcopy(library),
        "coverage": copy.deepcopy(receipt.get("coverage", {})),
        "verified_at": datetime.now(timezone.utc).isoformat(), "max_age_seconds": maximum,
    }
    # Coverage remains in the original authenticated artifact. This receipt does not
    # claim to carry bytes that the reuse action never downloaded or regenerated.
    result["coverage"] = {}
    return stamp(result, "quality", quality_path, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        root = Path(args.root).resolve()
        path = confined_path(root, args.config)
        config = load_json(path)
        quality_path = confined_path(root, config["quality_config"])
        quality = load_json(quality_path)
        output = confined_path(root, args.output, must_exist=False)
        protected = {path, quality_path} | {confined_path(root, name) for name in
                    set(snapshot(root, quality.get("inputs", []))) | set(quality.get("lockfiles", []))}
        if output in protected or run(["git", "ls-files", "--error-unmatch", "--", str(output.relative_to(root))],
                                 cwd=root, check=False).returncode == 0:
            raise PipelineError("Reuse receipt cannot overwrite tracked source or configuration")
        output.unlink(missing_ok=True)
        result = execute(config, root)
        atomic_json(output, result)
        print('quality reuse: passed')
    except (PipelineError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"quality reuse: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
