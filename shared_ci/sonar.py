"""Run Sonar analysis using coverage from a separately verified quality run."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file, strict_keys, trusted_context
from .quality import argv, input_digest, snapshot


def validate_coverage_receipt(receipt, root, coverage, *, repository, run_id, source_sha, max_age_seconds=86400):
    if not all(isinstance(v, str) and v for v in (repository, run_id, source_sha)):
        raise PipelineError("verified quality-run repository, run id and source SHA are required")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha) or not run_id.isdigit():
        raise PipelineError("invalid verified quality-run identity")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("kind") != "quality" or receipt.get("status") != "passed":
        raise PipelineError("not a successful quality receipt")
    if receipt.get("provenance") != {"repository": repository, "run_id": run_id, "sha": source_sha}:
        raise PipelineError("quality receipt does not match the externally verified run")
    try:
        created = datetime.fromisoformat(receipt["created_at"])
        if created.tzinfo is None:
            raise ValueError("missing timezone")
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if not -300 <= age <= max_age_seconds:
            raise ValueError("stale or future receipt")
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("quality receipt is stale or has an invalid timestamp") from exc
    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict) or not inputs or receipt.get("input_digest") != input_digest(inputs):
        raise PipelineError("quality input fingerprint is invalid")
    if snapshot(root, receipt.get("input_paths", [])) != inputs:
        raise PipelineError("current source inputs differ from tested inputs")
    summaries = [check.get("test_summary", {}) for check in receipt.get("checks", []) if check.get("kind") == "test" and check.get("status") == "passed"]
    if not summaries or not all(type(s.get("executed")) is int and s["executed"] > 0 and s.get("failed") == 0 for s in summaries):
        raise PipelineError("quality receipt does not prove executed successful tests")
    hashes = receipt.get("coverage", {})
    if not isinstance(hashes, dict):
        raise PipelineError("quality coverage metadata is malformed")
    selected = {}
    for item in coverage:
        relative = item["path"]
        actual = sha256_file(confined_path(root, relative))
        if hashes.get(relative) != actual:
            raise PipelineError("coverage is absent from the tested run or its bytes changed")
        selected[relative] = actual
    return selected


def _text(value, name):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise PipelineError(f"invalid {name}")
    return value


def execute(config, root, *, repository, run_id, source_sha):
    strict_keys(config, {"version", "mode", "project_key", "organization", "host_url", "source_paths", "quality_receipt",
                         "coverage", "branch", "pull_request", "scanner", "allowed_refs", "timeout", "max_receipt_age_seconds", "receipt"},
                required=("version", "mode", "project_key", "host_url", "source_paths", "quality_receipt", "coverage", "scanner", "allowed_refs"))
    if type(config["version"]) is not int or config["version"] != 1 or config["mode"] not in {"pull_request", "branch"}:
        raise PipelineError("unsupported Sonar configuration")
    trusted_context(config["allowed_refs"], allow_pull_requests=config["mode"] == "pull_request")
    if not os.environ.get("SONAR_TOKEN", "").strip():
        raise PipelineError("required SONAR_TOKEN is absent; analysis is unavailable, not successful")
    root = Path(root).resolve()
    host = urlsplit(config["host_url"])
    if host.scheme != "https" or not host.hostname or host.username or host.password or host.query or host.fragment:
        raise PipelineError("Sonar host must be an HTTPS URL without credentials/query/fragment")
    timeout = config.get("timeout", 900)
    max_age = config.get("max_receipt_age_seconds", 86400)
    if type(timeout) is not int or not 1 <= timeout <= 3600 or type(max_age) is not int or not 1 <= max_age <= 604800:
        raise PipelineError("invalid Sonar timeout or receipt freshness limit")
    sources = config["source_paths"]
    if not isinstance(sources, list) or not sources:
        raise PipelineError("source_paths must not be empty")
    for path in sources:
        _text(path, "source path")
        if "," in path:
            raise PipelineError("comma in Sonar source path")
        confined_path(root, path)
    coverage = config["coverage"]
    if not isinstance(coverage, list) or not coverage:
        raise PipelineError("Sonar requires explicit coverage reports")
    properties = {}
    for item in coverage:
        strict_keys(item, {"path", "property"}, required=("path", "property"))
        _text(item["path"], "coverage path")
        if "," in item["path"] or not re.fullmatch(r"sonar\.[A-Za-z][A-Za-z0-9.]*[Rr]eport[Pp]aths?", item["property"]):
            raise PipelineError("invalid Sonar coverage property/path")
        properties.setdefault(item["property"], []).append(item["path"])
    quality_path = confined_path(root, config["quality_receipt"])
    quality = load_json(quality_path)
    hashes = validate_coverage_receipt(quality, root, coverage, repository=repository, run_id=run_id, source_sha=source_sha, max_age_seconds=max_age)
    scanner = config["scanner"]
    strict_keys(scanner, {"command", "version"}, required=("command", "version"))
    command = argv(scanner["command"])
    if len(command) != 1 or Path(command[0]).name not in {"sonar-scanner", "sonar-scanner.bat"}:
        raise PipelineError("scanner command must point to the pinned SonarScanner CLI")
    if not isinstance(scanner["version"], str) or not re.fullmatch(r"\d+(?:\.\d+){1,4}", scanner["version"]):
        raise PipelineError("SonarScanner version must be pinned")
    version = run(command + ["--version"], cwd=root, timeout=60)
    if not re.search(r"(?<![\w.])" + re.escape(scanner["version"]) + r"(?![\w.])", version.stdout + version.stderr):
        raise PipelineError("installed SonarScanner version differs from configuration")
    arguments = command + [f"-Dsonar.projectKey={_text(config['project_key'], 'project key')}",
                           f"-Dsonar.host.url={config['host_url']}", "-Dsonar.sources=" + ",".join(sources),
                           "-Dsonar.qualitygate.wait=true", f"-Dsonar.scm.revision={source_sha}"]
    if config.get("organization"):
        arguments.append(f"-Dsonar.organization={_text(config['organization'], 'organization')}")
    if config["mode"] == "pull_request":
        if "branch" in config:
            raise PipelineError("PR analysis cannot also configure branch analysis")
        pr = config.get("pull_request")
        strict_keys(pr, {"key", "branch", "base"}, required=("key", "branch", "base"))
        for key in ("key", "branch", "base"):
            arguments.append(f"-Dsonar.pullrequest.{key}={_text(pr[key], 'pull request ' + key)}")
    else:
        if "pull_request" in config:
            raise PipelineError("branch analysis cannot contain PR parameters")
        arguments.append(f"-Dsonar.branch.name={_text(config.get('branch'), 'branch')}")
    arguments += [f"-D{key}=" + ",".join(values) for key, values in sorted(properties.items())]
    run(arguments, cwd=root, env_names=("SONAR_TOKEN",), timeout=timeout)
    result = {"schema_version": 1, "kind": "sonar", "status": "passed", "mode": config["mode"],
              "project_key": config["project_key"], "quality_receipt_sha256": sha256_file(quality_path),
              "provenance": {"repository": repository, "run_id": run_id, "sha": source_sha}, "coverage": hashes,
              "scanner_version": scanner["version"], "quality_gate_waited": True}
    if config.get("receipt"):
        atomic_json(confined_path(root, config["receipt"], must_exist=False), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--verified-repository", required=True)
    parser.add_argument("--verified-run-id", required=True)
    parser.add_argument("--verified-source-sha", required=True)
    args = parser.parse_args()
    try:
        result = execute(load_json(confined_path(Path(args.root).resolve(), args.config)), args.root,
                         repository=args.verified_repository, run_id=args.verified_run_id, source_sha=args.verified_source_sha)
        print(json.dumps({"status": result["status"], "mode": result["mode"], "quality_gate_waited": True}))
    except (PipelineError, OSError, ValueError) as exc:
        print(f"sonar: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
