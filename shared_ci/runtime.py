"""Trusted action inputs that bind retained evidence to one provider invocation."""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
from pathlib import Path

from .artifacts import no_symlink, safe_name, verify_manifest
from .common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file, strict_keys
from .evidence import fetch_receipt, security_refresh_context, verify_checks
from .security import authorize_refresh
from .stores import env_secret

STATEFUL_STORES = {"play-upload", "play-promote", "apple-submit", "firebase-distribution"}
READ_ONLY_DEPLOY = {"snapshot", "validate", "emulate"}
SELECTION_ENV = ("SHARED_CANDIDATE_MANIFEST", "SHARED_CANDIDATE_IDENTITY", "SHARED_CANDIDATE_SOURCE_SHA")


def json_input(name, default=None):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    if len(raw.encode()) > 65536:
        raise PipelineError("Runtime selection exceeds its size limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PipelineError("Runtime selection contains duplicate fields")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except PipelineError:
        raise
    except (ValueError, TypeError):
        raise PipelineError("Runtime selection must be valid JSON") from None
    if not isinstance(value, dict):
        raise PipelineError("Runtime selection must be a JSON object")
    return value


def selected_candidate(root, library_revision):
    values = [os.environ.get(name, "") for name in SELECTION_ENV]
    if not any(values):
        return None, None
    if not all(values):
        raise PipelineError("Candidate manifest, identity and source must be selected together")
    selection = dict(zip(("candidate", "expected_identity", "expected_source_sha"), values))
    candidate = check_candidate(root, selection, library_revision)
    return selection, candidate


def check_candidate(root, selection, library_revision):
    if not re.fullmatch(r"[0-9a-f]{40}", library_revision):
        raise PipelineError("Selected candidate requires an immutable executing helper revision")
    path = no_symlink(root, safe_name(selection["candidate"]))
    candidate = load_json(path)
    verify_manifest(candidate, root, selection["expected_identity"], selection["expected_source_sha"])
    if candidate.get("library_sha") != library_revision:
        raise PipelineError("Selected candidate library revision differs from the executing helper")
    if run(["git", "rev-parse", "HEAD^{tree}"], cwd=root).stdout.strip() != candidate["source_tree"]:
        raise PipelineError("Selected candidate differs from the checked-out source tree")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
        raise PipelineError("Selected candidate requires clean tracked source")
    return candidate


def authenticated_receipt(selector, *, allow_pull_requests=False):
    required = {"repository", "run_id", "run_attempt", "artifact_name", "run_head_sha", "expected_workflow", "allowed_branches", "allowed_events"}
    strict_keys(selector, required | {"producer_job", "token_env"}, required)
    if selector["repository"] != os.environ.get("GITHUB_REPOSITORY"):
        raise PipelineError("Runtime receipt must come from the current repository")
    token_env = selector.get("token_env") or ("GH_EVIDENCE_TOKEN" if os.environ.get("GH_EVIDENCE_TOKEN") else "GH_TOKEN")
    if token_env not in {"GH_EVIDENCE_TOKEN", "GH_TOKEN", "GH_ARTIFACT_TOKEN"}:
        raise PipelineError("Runtime receipt requires the dedicated GitHub evidence token")
    events = ("push", "workflow_dispatch", "pull_request") if allow_pull_requests else ("push", "workflow_dispatch")
    if not isinstance(selector["allowed_events"], list) or not selector["allowed_events"] or any(event not in events for event in selector["allowed_events"]):
        raise PipelineError("Provider receipt must come from a trusted push/manual event")
    proof = fetch_receipt(selector["repository"], selector["run_id"], selector["artifact_name"], selector["run_head_sha"],
                          selector["expected_workflow"], env_secret(token_env),
                          run_attempt=selector["run_attempt"], allowed_branches=selector["allowed_branches"],
                          allowed_events=selector["allowed_events"], producer_job=selector.get("producer_job"))
    # Never accept an ordinary caller JSON object in place of the transport's typed proof.
    from .evidence import AuthenticatedReceipt
    if not isinstance(proof, AuthenticatedReceipt):
        raise PipelineError("Provider evidence did not return an authenticated receipt")
    proof.validate_integrity()
    return proof


def materialize_checks(config, root, selections):
    declared = {entry.get("path") for entry in config.get("checks", []) if isinstance(entry, dict)}
    if set(selections) - declared:
        raise PipelineError("Authenticated receipt destination must be a declared candidate check")
    prepared, digests = [], {}
    for relative, selector in selections.items():
        if not isinstance(selector, dict):
            raise PipelineError("Authenticated check selection must be an object")
        path = no_symlink(root, safe_name(relative))
        if selector.get("repository") != os.environ.get("GITHUB_REPOSITORY"):
            raise PipelineError("Candidate checks must come from the current repository")
        if run(["git", "ls-files", "--error-unmatch", "--", relative], cwd=root, check=False).returncode == 0:
            raise PipelineError("Authenticated receipt cannot overwrite tracked source")
        proof = authenticated_receipt(selector, allow_pull_requests=True)
        retained = {**proof["receipt"], "authenticated_origin": proof["provenance"]}
        content = (json.dumps(retained, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        if path.exists() and (not path.is_file() or path.read_bytes() != content):
            raise PipelineError("Authenticated receipt conflicts with existing local bytes")
        prepared.append((relative, path, content))
    # Validate every selected proof/conflict before writing any destination.
    created = []
    try:
        for relative, path, content in prepared:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    created.append(path)
                    stream.write(content)
                path.chmod(0o600)
            digests[relative] = sha256_file(path)
    except OSError:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise PipelineError("Could not retain authenticated checks without replacing local files") from None
    return digests


class Invocation:
    def __init__(self, name, config, root, library_revision):
        self.name, self.root, self.library_revision = name, Path(root).resolve(), library_revision
        self.config = copy.deepcopy(config)
        self.selection, self.candidate = selected_candidate(self.root, library_revision)
        self.kwargs, self.refreshes = {}, {}
        self.check_digests = {}
        self.restore = json_input("SHARED_STATE_RESTORE")
        prior = json_input("SHARED_PRIOR_DEPLOYMENT")
        refresh = json_input("SHARED_SECURITY_REFRESH", {})
        bridge = json_input("SHARED_APPLE_BRIDGE_SELECTION")
        current = os.environ.get("SHARED_CURRENT_RELEASE", "")
        operation = os.environ.get("SHARED_OPERATION", "") if name in {"evidence", "release"} else config.get("operation")
        self.operation = operation
        container = json_input("SHARED_CONTAINER_SELECTION")
        store = json_input("SHARED_STORE_SELECTION")
        check_receipts = json_input("SHARED_CHECK_RECEIPTS", {})
        if check_receipts:
            if name != "evidence" or os.environ.get("SHARED_OPERATION") != "create":
                raise PipelineError("Imported check receipts are only supported during candidate creation")
            self.check_digests = materialize_checks(self.config, self.root, check_receipts)

        if name in {"stores", "monitoring"}:
            inventory = name == "stores" and operation in {"play-inventory", "apple-inventory"}
            if not inventory and self.selection is None:
                raise PipelineError("Store/symbol operation requires an explicitly selected retained candidate")
            if not inventory:
                self.config.update(self.selection)
        if store is not None:
            if name != "stores" or self.selection is None:
                raise PipelineError("Store selection requires a selected store candidate")
            key = "play" if str(operation).startswith("play-") else "apple" if str(operation).startswith("apple-") or operation == "testflight" else None
            if key is None:
                raise PipelineError("This store operation has no runtime provider selection")
            strict_keys(store, {key}, {key})
            allowed = {"expected_track_digest"} if key == "play" else {"build_id", "app_store_version_id", "expected_selected_build", "submission_id"}
            strict_keys(store[key], allowed)
            if not store[key]:
                raise PipelineError("Store runtime selection cannot be empty")
            for field, value in store[key].items():
                if field == "expected_track_digest":
                    if not re.fullmatch(r"[0-9a-f]{64}", str(value)):
                        raise PipelineError("Selected Play track digest must be exact SHA256")
                elif field != "expected_selected_build" or value is not None:
                    from .stores import identifier
                    identifier(value, "selected Apple resource")
            self.config[key] = {**self.config[key], **store[key]}
        if name == "deploy":
            mutating = operation not in READ_ONLY_DEPLOY
            if mutating and self.selection is None:
                raise PipelineError("Mutating deployment requires an explicitly selected retained candidate")
            self.kwargs["require_candidate"] = mutating
            if self.selection:
                self.kwargs.update(self.selection)
            if prior is not None:
                if operation != "promote" or not self.selection:
                    raise PipelineError("Prior deployment selection requires a candidate promotion")
                self.kwargs["promotion_receipt"] = authenticated_receipt(prior)
        elif prior is not None:
            raise PipelineError("Prior deployment evidence is only supported by deployment promotion")
        if name == "security" and self.candidate is not None:
            self.kwargs["candidate"] = self.candidate
        if name == "release" and operation == "publish":
            if self.selection is None:
                raise PipelineError("Release publication requires an explicitly selected retained candidate")
            self.config["candidate"] = self.selection["candidate"]
            declared_library = self.config.get("expected_library_sha")
            if declared_library not in ("workflow", library_revision):
                raise PipelineError("Release expected library differs from the executing helper")
            self.config["expected_library_sha"] = library_revision
            if self.config.get("expected_commit") == "workflow":
                self.config["expected_commit"] = os.environ.get("GITHUB_SHA", "")
        if name == "release" and operation == "draft" and self.config.get("candidate_sha") == "workflow":
            self.config["candidate_sha"] = self.selection["expected_source_sha"] if self.selection else os.environ.get("GITHUB_SHA", "")
        if name == "evidence" and operation == "verify" and self.selection:
            if config != self.candidate:
                raise PipelineError("Evidence verification configuration differs from the selected candidate")

        if name == "containers":
            if operation in {"publish", "promote", "manifest", "deploy"} and self.selection is None:
                raise PipelineError("Container delivery requires an explicitly selected retained candidate")
            if operation == "publish":
                record = self.retained_json(self.config["candidate"])
                self.retained_file(record.get("archive", {}).get("path"))
                if record.get("source_sha") != self.candidate["source_sha"]:
                    raise PipelineError("Container build belongs to another candidate source")
            if operation == "deploy":
                record = self.retained_json(self.config["manifest"])
                if record.get("source_sha") != self.candidate["source_sha"]:
                    raise PipelineError("Container manifest belongs to another candidate source")
                for relative in self.config.get("compose_files", []) + list(self.config.get("support_files", {}).values()):
                    self.retained_file(relative)
            if operation == "manifest":
                self.config["source_sha"] = self.candidate["source_sha"]
                for component in self.config.get("components", {}).values():
                    if isinstance(component, dict):
                        publication = self.retained_json(component.get("publication"))
                        if publication.get("source_sha") != self.candidate["source_sha"]:
                            raise PipelineError("Container component publication belongs to another candidate source")
            if operation == "promote":
                if container is None:
                    raise PipelineError("Container promotion requires retained publication selection")
                strict_keys(container, {"publication", "tag", "expected_current_digest"}, {"publication", "tag", "expected_current_digest"})
                publication = self.retained_json(container["publication"])
                if publication.get("status") != "passed" or publication.get("schema") != "shared-ci.container-publication/v1" or publication.get("source_sha") != self.candidate["source_sha"]:
                    raise PipelineError("Container publication belongs to another candidate or did not pass")
                image = publication.get("images", {}).get(container["tag"])
                if not isinstance(image, str):
                    raise PipelineError("Selected tag is missing from retained container publication")
                self.config.update(source=image, expected_current_digest=container["expected_current_digest"])
            elif operation == "rollback" and container is not None:
                strict_keys(container, {"expected_target_release", "rollback_target"}, {"expected_target_release"})
                if not re.fullmatch(r"[0-9a-f]{24}", str(container["expected_target_release"])):
                    raise PipelineError("Rollback requires an exact previous release identity")
                self.config.update(container)
            elif container is not None:
                raise PipelineError("Container selection only supports promotion or rollback")
        elif container is not None:
            raise PipelineError("Container selection cannot be applied to another module")

        if current:
            if name != "containers" or operation not in {"deploy", "rollback"} or not re.fullmatch(r"absent|[0-9a-f]{24}", current):
                raise PipelineError("Current release selection requires a container deploy/rollback and exact release identity")
            self.config["expected_current_release"] = current

        if name == "apple_bridge":
            if operation != "apple-import" or self.selection is not None or bridge is None:
                raise PipelineError("GitHub Apple bridge requires apple-import and an explicit remote selection")
            required = {"expected_identity", "expected_source_sha", "cloud", "version_id"}
            strict_keys(bridge, required, required)
            strict_keys(bridge["cloud"], {"workflow_id", "product_id", "build_run_id", "build_number"}, {"workflow_id", "product_id", "build_run_id", "build_number"})
            for field in ("workflow_id", "product_id"):
                if bridge["cloud"][field] != self.config.get("cloud", {}).get(field):
                    raise PipelineError("Apple bridge runtime selection changed the approved workflow/product")
            self.config.update({key: bridge[key] for key in ("expected_identity", "expected_source_sha", "cloud")})
            self.config["s3"]["version_id"] = bridge["version_id"]
        elif bridge is not None:
            raise PipelineError("Apple bridge selection is only supported by apple-import")

        # The root credential context already minted this alias from the existing P8.
        if os.environ.get("ASC_PRIVATE_KEY_PATH") and os.environ.get("ASC_BEARER_TOKEN"):
            if name == "apple_bridge":
                self.config["asc_token_env"] = "ASC_BEARER_TOKEN"
            elif name == "stores" and (str(operation).startswith("apple-") or operation == "testflight"):
                self.config["token_env"] = "ASC_BEARER_TOKEN"

        if refresh:
            consumes = name in {"stores", "monitoring", "deploy", "containers"} or (name == "release" and operation == "publish") or (name == "evidence" and operation == "verify")
            if self.candidate is None or not consumes:
                raise PipelineError("Security refresh requires a selected candidate consumer")
            for check_path, item in refresh.items():
                if self.candidate.get("check_requirements", {}).get(check_path, {}).get("kind") != "security":
                    raise PipelineError("Refresh does not select a candidate security requirement")
                strict_keys(item, {"selector", "policy", "policy_sha256", "source_sha"}, {"selector", "policy", "policy_sha256", "source_sha"})
                policy = no_symlink(self.root, safe_name(item["policy"]))
                proof = authorize_refresh(authenticated_receipt(item["selector"]), policy, item["policy_sha256"], item["source_sha"])
                proof.validate(self.candidate, self.root)
                self.refreshes[check_path] = proof

        if self.restore is not None and name != "stores":
            raise PipelineError("Store state selection cannot be applied to another module")
        if self.restore is not None and self.restore.get("token_env") != "GH_STATE_TOKEN":
            raise PipelineError("State restore requires its dedicated GitHub state token")
        if name == "stores" and operation in STATEFUL_STORES:
            attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            if not attempt.isdigit() or int(attempt) < 1:
                raise PipelineError("Invalid current store run attempt")
            if int(attempt) > 1 and self.restore is None:
                raise PipelineError("Stateful store rerun requires prior state selection; reconcile a missing artifact before retry")

    def retained_file(self, relative):
        path = no_symlink(self.root, safe_name(relative))
        records = {**self.candidate["inputs"], **self.candidate["artifacts"]}
        record = records.get(relative)
        if not record or not path.is_file() or path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise PipelineError("Container delivery input is absent from the selected candidate")
        return path

    def retained_json(self, relative):
        return load_json(self.retained_file(relative))

    def recheck(self):
        if self.selection:
            check_candidate(self.root, self.selection, self.library_revision)

    def receipt_path(self, relative, configuration):
        path = no_symlink(self.root, safe_name(relative))
        if path == configuration:
            raise PipelineError("Receipt cannot overwrite configuration")
        if run(["git", "ls-files", "--error-unmatch", "--", relative], cwd=self.root, check=False).returncode == 0:
            raise PipelineError("Receipt cannot overwrite tracked source")
        if self.candidate is not None:
            reserved = {self.selection["candidate"]} | set(self.candidate["inputs"]) | set(self.candidate["artifacts"]) | set(self.candidate["checks"])
            if relative in reserved:
                raise PipelineError("Receipt cannot overwrite retained candidate evidence")
        return path

    def imported_apple(self, result):
        if self.name != "apple_bridge":
            return
        if result.get("status") != "passed" or result.get("asc_run_verified") is not True:
            raise PipelineError("Apple import did not authenticate its successful Cloud run")
        selection = {"candidate": result.get("manifest"), "expected_identity": self.config["expected_identity"], "expected_source_sha": self.config["expected_source_sha"]}
        if result.get("candidate_identity") != selection["expected_identity"] or result.get("source_sha") != selection["expected_source_sha"]:
            raise PipelineError("Apple import returned a different candidate")
        self.candidate = check_candidate(self.root, selection, self.library_revision)
        self.selection = selection


@contextlib.contextmanager
def invocation(name, config, root, library_revision, output_directory):
    active = Invocation(name, config, root, library_revision)
    context = security_refresh_context(active.refreshes) if active.refreshes else contextlib.nullcontext()
    with context:
        if active.restore is not None:
            from .state import restore
            selector = dict(active.restore)
            if "run_head_sha" not in selector and str(selector.get("run_id")) == os.environ.get("GITHUB_RUN_ID"):
                selector["run_head_sha"] = os.environ.get("GITHUB_SHA", "")
            restore(selector, root, store_config=active.config)
        try:
            if active.candidate is not None and (name in {"stores", "monitoring"} or name == "deploy" and config.get("operation") not in READ_ONLY_DEPLOY
                                                  or name == "containers" and config.get("operation") in {"publish", "promote", "manifest", "deploy", "rollback"}):
                verify_checks(active.candidate, root)
            yield active
        finally:
            if name == "stores" and active.config.get("state_file"):
                path = no_symlink(root, active.config["state_file"])
                if path.is_file():
                    from .state import snapshot
                    envelope = snapshot(active.config, root)
                    target = Path(output_directory) / "operation-state.json"
                    atomic_json(target, envelope)
                    if os.environ.get("GITHUB_OUTPUT"):
                        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                            stream.write(f"operation-state={target}\noperation-state-sha256={sha256_file(target)}\n")
