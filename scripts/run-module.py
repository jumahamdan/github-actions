#!/usr/bin/env python3
"""Composite-action entry point. Values arrive via environment, never shell source."""

import importlib
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

LIBRARY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LIBRARY))

from shared_ci.common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file
from shared_ci.assurance import stamp, library_identity
from shared_ci.runtime import invocation, json_input
from shared_ci.quality import QualityFailure


def dispatch(name, module, config, root, path, output, operation, active):
    if name == "quality":
        result = module.execute(config, root, config_hash=sha256_file(path))
    elif name == "credentials":
        result = {"status": "passed", **module.preflight(config)}
    elif name == "security":
        result = module.scan(config, root, **active.kwargs)
    elif name == "ai":
        result = module.execute(config, root, output, source_revision=os.environ.get("GITHUB_SHA"))
    elif name == "evidence":
        if operation not in ("create", "verify"):
            raise PipelineError("Evidence operation must be create or verify")
        library_revision=os.environ.get("SHARED_ACTION_REF", "")
        if not re.fullmatch(r"[0-9a-f]{40}",library_revision):
            raise PipelineError("Candidate evidence requires an immutable executing action revision")
        if operation == "create":
            if config["library_sha"] not in ("workflow",library_revision):
                raise PipelineError("Candidate library revision differs from the executing helper")
            config={**config,"library_sha":library_revision}
            selected_build = os.environ.get("SHARED_CANDIDATE_BUILD", "")
            if config.get("build") == "workflow":
                if not re.fullmatch(r"[1-9][0-9]{0,17}", selected_build):
                    raise PipelineError("Candidate build marker requires an explicitly allocated positive integer")
                config["build"] = selected_build
            elif selected_build:
                raise PipelineError("Runtime build number cannot override a static candidate build")
            expected=json_input("SHARED_CHECK_DIGESTS", {})
            for retained, digest in active.check_digests.items():
                if retained in expected and expected[retained] != digest:
                    raise PipelineError("Imported check differs from the trusted digest selection")
                expected[retained] = digest
            current_path=os.environ.get("SHARED_CURRENT_RECEIPT_PATH","")
            current_digest=os.environ.get("SHARED_CURRENT_RECEIPT_SHA","")
            if bool(current_path) != bool(current_digest):
                raise PipelineError("Current check path and digest must be selected together")
            if current_path and current_digest:
                if current_path in expected and expected[current_path]!=current_digest:
                    raise PipelineError("Conflicting trusted check selection")
                expected[current_path]=current_digest
            if not expected:
                raise PipelineError("Candidate creation requires trusted check digests from workflow outputs")
            result=module.create(config,root,expected_checks=expected)
        else:
            result=module.verify(config,root,expected_library_sha=library_revision)
    elif name == "release":
        if operation == "publish":
            result = module.publish(config, root)
        elif operation == "render":
            notes = module.render(config, os.environ.get("SHARED_AUDIENCE", "public"))
            (output.parent / "notes.md").write_text(notes)
            result = {"schema": 1, "status": "passed", "state": "draft", "notes_sha256": sha256_file(output.parent / "notes.md")}
        elif operation == "draft":
            result = module.draft(config, root)
            result["status"] = "passed"
            result["state"] = "draft"
        else:
            raise PipelineError("Unknown release operation")
    elif name == "gate":
        result = module.evaluate(config, json.loads(os.environ.get("SHARED_JOB_RESULTS", "{}")))
    elif name == "sonar":
        from shared_ci.evidence import fetch_receipt
        from shared_ci.common import trusted_context
        trusted_context(config["allowed_refs"], allow_pull_requests=config.get("mode") == "pull_request")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("QUALITY_RUN_ID", "")
        source = os.environ.get("QUALITY_SOURCE_SHA", "")
        attempt = os.environ.get("QUALITY_RUN_ATTEMPT", "")
        if not re.fullmatch(r"[1-9][0-9]*", attempt):
            raise PipelineError("Sonar requires an exact positive quality run attempt")
        try:
            branches = json.loads(os.environ.get("QUALITY_ALLOWED_BRANCHES", ""))
            events = json.loads(os.environ.get("QUALITY_ALLOWED_EVENTS", ""))
        except json.JSONDecodeError as exc:
            raise PipelineError("Sonar requires explicit JSON quality branch and event allowlists") from exc
        if not isinstance(branches, list) or not branches or not isinstance(events, list) or not events:
            raise PipelineError("Sonar requires nonempty quality branch and event allowlists")
        verified = fetch_receipt(repository, run_id, os.environ.get("QUALITY_ARTIFACT", ""), os.environ.get("QUALITY_RUN_HEAD_SHA", source),
                                 os.environ.get("QUALITY_WORKFLOW", ""), os.environ.get("GH_TOKEN", ""),
                                 coverage_paths=[entry["path"] for entry in config["coverage"]],
                                 producer_job=os.environ.get("QUALITY_PRODUCER_JOB") or None,
                                 run_attempt=int(attempt), allowed_branches=branches, allowed_events=events)
        receipt_path = confined_path(root, config["quality_receipt"], False)
        for relative, data in verified["coverage_files"].items():
            target = confined_path(root, relative, False)
            if target in (path, receipt_path) or relative in verified["receipt"].get("inputs", {}):
                raise PipelineError("Coverage destination would overwrite source/configuration")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        atomic_json(receipt_path, verified["receipt"])
        result = module.execute(config, root, repository=repository, run_id=run_id, source_sha=source)
    else:
        result = module.execute(config, root, **active.kwargs)
    active.imported_apple(result)
    return result


def main():
    started=time.monotonic()
    name = os.environ.get("SHARED_MODULE", "")
    if name not in {"quality", "docs", "credentials", "security", "evidence", "release", "ai", "containers",
                    "sonar", "deploy", "health", "wordpress", "mobile", "stores", "monitoring", "gate", "plan", "reuse", "apple_bridge"}:
        raise PipelineError("Unknown shared workflow module")
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
    root = confined_path(workspace, os.environ.get("SHARED_ROOT", "."))
    path = confined_path(root, os.environ.get("SHARED_CONFIG", ""))
    config = load_json(path)
    if not isinstance(config, dict):
        raise PipelineError("Shared module configuration must be a JSON object")
    # A fresh artifact directory cannot contain caller-planted files or provider credentials.
    output = Path(tempfile.mkdtemp(prefix=f"shared-ci-{name}-", dir=os.environ.get("RUNNER_TEMP", "/tmp"))) / "receipt.json"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"receipt={output}\nbundle={output.parent}\n")
    active = None
    try:
        module = importlib.import_module("shared_ci." + name)
        operation = os.environ.get("SHARED_OPERATION", "")
        if name == "mobile" and operation and config.get("operation") != operation:
            raise PipelineError("Mobile configuration does not match this workflow's supported operation")
        with invocation(name, config, root, os.environ.get("SHARED_ACTION_REF", ""), output.parent) as active:
            config = active.config
            receipt_relative = os.environ.get("SHARED_RECEIPT_PATH", "") or config.get("receipt", "")
            if receipt_relative:
                active.receipt_path(receipt_relative, path)
            result = dispatch(name, module, config, root, path, output, operation, active)
            active.recheck()
            if receipt_relative:
                active.receipt_path(receipt_relative, path)
        accepted = ("passed", "advisory", "pending") if name == "stores" else ("passed", "advisory")
        if not isinstance(result, dict) or result.get("status") not in accepted:
            if isinstance(result, dict):
                atomic_json(output, result)
            raise PipelineError("Shared module did not produce a passing result")
        result["library"] = {"module_sha256": sha256_file(Path(module.__file__)),
                             "reference": os.environ.get("SHARED_ACTION_REF", ""), "identity": library_identity(LIBRARY)}
        result["elapsed_seconds"]=round(time.monotonic()-started,3)
        if name != "reuse":
            stamp(result, name, path, root)
        atomic_json(output, result)
        if receipt_relative:
            receipt_target = confined_path(root, receipt_relative, False)
            if receipt_target == path:
                raise PipelineError("Receipt cannot overwrite configuration")
            if run(["git", "ls-files", "--error-unmatch", "--", str(receipt_target.relative_to(root))], cwd=root, check=False).returncode == 0:
                raise PipelineError("Receipt cannot overwrite tracked source")
            atomic_json(receipt_target, result)
        else:
            receipt_relative=""
        # Only the small allowlisted receipt/coverage bundle is uploaded; never a workspace glob.
        coverage = result.get("coverage", {}) if name == "quality" else {}
        if coverage:
            directory = output.parent / "coverage"
            directory.mkdir(exist_ok=True)
            for relative, digest in coverage.items():
                source_path = confined_path(root, relative)
                if sha256_file(source_path) != digest:
                    raise PipelineError("Coverage changed before artifact preservation")
                (directory / digest).write_bytes(source_path.read_bytes())
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(f"receipt={output}\nbundle={output.parent}\nstatus={result['status']}\n")
                stream.write(f"receipt-sha256={sha256_file(output)}\nretained-path={receipt_relative}\n")
                state=result.get("state","")
                if state and isinstance(state,str) and re.fullmatch(r"[A-Za-z0-9_-]+",state):
                    stream.write(f"state={state}\n")
                if name == "evidence" and operation == "create":
                    stream.write(f"candidate-identity={result['identity']}\nsource-sha={result['source_sha']}\n")
                elif name == "apple_bridge":
                    stream.write(f"manifest={result['manifest']}\ncandidate-identity={result['candidate_identity']}\nsource-sha={result['source_sha']}\n")
                if name == "mobile" and config.get("operation") == "next-build":
                    build_number = result.get("build_number")
                    if type(build_number) is not int or build_number < 1:
                        raise PipelineError("Build allocation did not return a positive integer")
                    stream.write(f"build-number={build_number}\n")
                if name == "plan":
                    stream.write("plan="+json.dumps(result,separators=(",",":"))+"\n")
        print(json.dumps({"module": name, "status": result["status"]}))
    except BaseException as error:
        if not output.exists() or load_json(output).get("status") != "failed":
            failed = {"schema": 1, "status": "failed", "module": name}
            if name == "quality" and isinstance(error, QualityFailure):
                failed["failure"] = error.details
            operation_name = config.get("operation")
            if isinstance(operation_name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", operation_name):
                failed["operation"] = operation_name
            identity = (active.selection["expected_identity"] if active and active.selection else
                        os.environ.get("SHARED_CANDIDATE_IDENTITY", ""))
            if re.fullmatch(r"[0-9a-f]{64}", identity):
                failed["candidate_identity"] = identity
            atomic_json(output, failed)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("status=failed\n")
        raise



if __name__ == "__main__":
    try:
        from shared_ci.credentials import materialize
        from shared_ci.auth import existing_apple_credentials
        contract = os.environ.get("SHARED_CREDENTIALS_CONFIG", "")
        workspace = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
        root = confined_path(workspace, os.environ.get("SHARED_ROOT", "."))
        credential_context = materialize(load_json(confined_path(root, contract))) if contract else contextlib.nullcontext({})
        previous = dict(os.environ)
        try:
            with credential_context as credentials:
                os.environ.update(credentials)
                token = existing_apple_credentials()
                if token:
                    os.environ["ASC_BEARER_TOKEN"] = token
                main()
        finally:
            os.environ.clear()
            os.environ.update(previous)
    except (PipelineError, OSError, ValueError, KeyError, TypeError) as exc:
        # Configuration errors are safe; never show a raw provider/tool traceback.
        print(f"Shared workflow failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
