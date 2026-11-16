"""Optional Firebase APK distribution; called by the stores adapter."""
from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.parse
from pathlib import Path

from .common import PipelineError, confined_path, load_json, run, sha256_file, strict_keys
from .stores import env_secret, load_candidate, save, state_file


def required_checks(settings, candidate, root, digest):
    device_path = settings["device_check"]
    if device_path not in candidate["checks"] or candidate["check_requirements"][device_path]["kind"] != "mobile":
        raise PipelineError("App Distribution requires a candidate-bound mobile device check")
    device = load_json(confined_path(root, device_path))
    if (device.get("operation") != "device-test" or device.get("app_sha256") != digest
            or device.get("source_revision") != candidate["source_sha"] or device.get("status") != "passed"):
        raise PipelineError("Device evidence does not cover this exact APK")
    symbol_checks = settings["symbol_checks"]
    if not isinstance(symbol_checks, dict) or not symbol_checks:
        raise PipelineError("App Distribution requires explicit completed symbol uploads")
    for relative, expected_files in symbol_checks.items():
        if (relative not in candidate["checks"] or candidate["check_requirements"][relative]["kind"] != "monitoring"
                or not isinstance(expected_files, list) or not expected_files):
            raise PipelineError("Required symbol upload is not candidate-bound")
        receipt = load_json(confined_path(root, relative))
        if (receipt.get("status") != "passed" or receipt.get("source_sha") != candidate["source_sha"]
                or not set(expected_files) <= set(receipt.get("uploaded_artifacts", []))
                or not set(expected_files) <= set(candidate["artifacts"])
                or any(receipt.get("artifact_records", {}).get(name) != candidate["artifacts"].get(name) for name in expected_files)):
            raise PipelineError("Required symbols were not uploaded for the selected source/artifacts")


def selected_releases(api, parent, version, build):
    found, page_token = [], None
    for _ in range(20):
        query = {"pageSize": "100"}
        if page_token:
            query["pageToken"] = page_token
        response = api("GET", "/v1/" + parent + "/releases?" + urllib.parse.urlencode(query))
        releases = response.get("releases", [])
        if not isinstance(releases, list):
            raise PipelineError("Malformed Firebase release inventory")
        for release in releases:
            if not isinstance(release, dict):
                raise PipelineError("Malformed Firebase release resource")
            if release.get("displayVersion") == version and str(release.get("buildVersion")) == build:
                if not re.fullmatch(re.escape(parent) + r"/releases/[A-Za-z0-9_-]+", str(release.get("name", ""))):
                    raise PipelineError("Firebase release belongs to another app")
                found.append(release)
        page_token = response.get("nextPageToken")
        if not page_token:
            return found
        if not isinstance(page_token, str):
            raise PipelineError("Malformed Firebase pagination token")
    raise PipelineError("Firebase release inventory exceeds bounded pagination")


def distribute(config, root, candidate, inspected, api):
    from .monitoring import firebase_app, pinned_tool
    settings = config["firebase"]
    required = {"app_id", "app_config", "artifact", "credentials_env", "tool", "group_aliases", "release_notes", "device_check", "symbol_checks"}
    strict_keys(settings, required, required)
    root = Path(root).resolve()
    artifact = settings["artifact"]
    apk = confined_path(root, artifact)
    if apk.suffix != ".apk" or artifact not in inspected["artifacts"]:
        raise PipelineError("Firebase distribution requires the inspected, device-tested retained APK")
    digest = candidate["artifacts"][artifact]["sha256"]
    if inspected.get("apk_sha256") != digest:
        raise PipelineError("Selected APK does not match Android derivation/signature inspection")
    firebase_app(settings, inspected, root, candidate)
    required_checks(settings, candidate, root, digest)
    groups = settings["group_aliases"]
    if (not isinstance(groups, list) or not 1 <= len(groups) <= 999 or len(groups) != len(set(groups))
            or any(not isinstance(group, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", group) for group in groups)):
        raise PipelineError("Explicit unique Firebase tester group aliases are required")
    notes_path = confined_path(root, settings["release_notes"])
    if settings["release_notes"] not in candidate["inputs"] and settings["release_notes"] not in candidate["artifacts"]:
        raise PipelineError("Firebase release notes must be candidate-bound")
    try:
        notes = notes_path.read_text()
    except UnicodeError:
        raise PipelineError("Firebase release notes must be UTF-8 text") from None
    if not notes.strip() or len(notes) > 10000:
        raise PipelineError("Firebase release notes are empty or oversized")
    parent = "projects/" + settings["app_id"].split(":")[1] + "/apps/" + settings["app_id"]
    path, state = state_file(config, root)
    if state["phase"] == "done":
        return state["receipt"]
    if state["phase"] in ("upload_started", "distribution_started"):
        raise PipelineError("Firebase operation outcome is uncertain; reconcile upload/distribution before retry")
    if state["phase"] == "new":
        if selected_releases(api, parent, candidate["version"], candidate["build"]):
            raise PipelineError("Firebase already has this app version/build; absent a prior successful receipt, reconcile instead of replacing it")
        credential = Path(env_secret(settings["credentials_env"])).resolve()
        if not credential.is_file() or credential.is_relative_to(root):
            raise PipelineError("Firebase ADC must be an ephemeral file outside retained source/artifacts")
        with tempfile.TemporaryDirectory(prefix="shared-appdistribution-") as temporary:
            clean = {"HOME": temporary, "XDG_CONFIG_HOME": temporary, "FIREBASE_CLI_DISABLE_UPDATE_CHECK": "true"}
            prefix = pinned_tool(settings["tool"], temporary, clean)
            extra = dict(clean, GOOGLE_APPLICATION_CREDENTIALS=str(credential))
            save(path, state, "upload_started", artifact_sha256=digest)
            # No groups/testers on upload. Verify provider release metadata before distributing.
            response = run(prefix + ["appdistribution:distribute", str(apk), "--app", settings["app_id"],
                                    "--release-notes-file", str(notes_path), "--json", "--non-interactive"],
                           cwd=temporary, extra_env=extra, timeout=config.get("timeout", 900))
            try:
                output = json.loads(response.stdout)
            except json.JSONDecodeError:
                raise PipelineError("Firebase CLI returned malformed JSON; upload outcome requires reconciliation") from None
            if not isinstance(output, dict) or output.get("status") != "success":
                raise PipelineError("Firebase CLI did not return a successful upload result")
            # CLI's distribute command does not currently return the Release in its JSON result.
            # Its supported REST inventory supplies app/version/build identity instead.
            save(path, state, "uploaded")
    deadline = time.monotonic() + config.get("timeout", 900)
    while True:
        releases = selected_releases(api, parent, candidate["version"], candidate["build"])
        if len(releases) > 1:
            raise PipelineError("Multiple Firebase releases match selected version/build; refusing ambiguous distribution")
        if releases:
            release = releases[0]
            break
        if time.monotonic() >= deadline:
            return {"schema": 1, "status": "pending", "operation": "firebase-distribution", "state": "processing_timeout",
                    "candidate_identity": candidate["identity"], "artifact_sha256": digest, "firebase_app_id": settings["app_id"]}
        time.sleep(min(config.get("poll_interval", 15), max(0, deadline - time.monotonic())))
    if release.get("releaseNotes", {}).get("text") != notes:
        raise PipelineError("Firebase release notes differ from the selected upload")
    load_candidate(config, root, {"android"})
    save(path, state, "distribution_started", release_name=release["name"])
    result = api("POST", "/v1/" + release["name"] + ":distribute", {"groupAliases": groups})
    if result:
        raise PipelineError("Unexpected Firebase distribution response; reconcile before retry")
    receipt = {"schema": 1, "status": "passed", "operation": "firebase-distribution", "state": "distributed_to_groups",
               "candidate_identity": candidate["identity"], "source_sha": candidate["source_sha"], "firebase_app_id": settings["app_id"],
               "release_name": release["name"], "version": candidate["version"], "build": candidate["build"], "artifact": artifact,
               "artifact_sha256": digest, "group_aliases": groups, "provider_binary_sha256_available": False,
               "tester_install_verified": False}
    save(path, state, "done", receipt=receipt)
    return receipt
