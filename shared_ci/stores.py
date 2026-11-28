"""Candidate-bound Google Play and App Store Connect operations; never rebuild/upload an IPA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .evidence import IDENTITY_FIELDS, verify_checks
from .common import (PipelineError, atomic_json, cli_error, confined_path, fingerprint,
                     load_json, run, sha256_file, strict_keys, trusted_context)

HEX = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
ENV = re.compile(r"^[A-Z][A-Z0-9_]*$")


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise PipelineError(f"Invalid {label}")
    return value


def bounded(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise PipelineError(f"Invalid {label}")
    return value


def env_secret(name):
    if not isinstance(name, str) or not ENV.fullmatch(name) or not os.environ.get(name):
        raise PipelineError("Required named credential is missing")
    value = os.environ[name]
    if any(c in value for c in "\r\n\x00"):
        raise PipelineError("Invalid credential encoding")
    return value


def verify_files(root, records, *, allow_empty=False):
    if not isinstance(records, dict) or not records:
        raise PipelineError("Nonempty file evidence is required")
    for relative, record in records.items():
        strict_keys(record, {"sha256", "bytes"}, {"sha256", "bytes"})
        path = confined_path(root, relative)
        if not path.is_file() or (Path(root) / relative).is_symlink() or not HEX.fullmatch(str(record["sha256"])):
            raise PipelineError("Artifact evidence must describe regular files")
        if not isinstance(record["bytes"], int) or isinstance(record["bytes"], bool) or (record["bytes"] < 0 if allow_empty else record["bytes"] <= 0) or path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise PipelineError("Retained artifact changed, empty, or missing")


def load_candidate(config, root, kinds):
    """Verify retained bytes; caller must authenticate downloaded artifact origin separately."""
    candidate = load_json(confined_path(root, config["candidate"]))
    if candidate.get("schema") != 1 or candidate.get("status") != "passed":
        raise PipelineError("Candidate has not passed")
    fields = IDENTITY_FIELDS
    if any(key not in candidate for key in fields):
        raise PipelineError("Candidate evidence is incomplete")
    if (not HEX.fullmatch(str(config["expected_identity"])) or candidate.get("identity") != config["expected_identity"]
            or fingerprint({key: candidate[key] for key in fields}) != candidate["identity"]
            or not SHA.fullmatch(str(config["expected_source_sha"]))
            or candidate.get("source_sha") != config["expected_source_sha"]):
        raise PipelineError("Candidate does not match trusted caller selection")
    verify_files(root, candidate["inputs"], allow_empty=True)
    verify_files(root, candidate["artifacts"])
    if not isinstance(candidate["checks"], dict) or not candidate["checks"]:
        raise PipelineError("Candidate checks are missing")
    verify_checks(candidate, root)
    if config["inspection"] not in candidate["checks"]:
        raise PipelineError("Inspection is not bound to candidate checks")
    inspected = load_json(confined_path(root, config["inspection"]))
    if inspected.get("source_sha") != candidate["source_sha"] or inspected.get("kind") not in kinds:
        raise PipelineError("Inspection source or platform differs from candidate")
    application = inspected.get("application", {})
    if (not isinstance(application, dict) or not application.get("id")
            or str(application.get("version")) != candidate["version"]
            or str(application.get("build")) != candidate["build"]):
        raise PipelineError("Inspected application identity differs from candidate")
    records = inspected.get("artifacts")
    verify_files(root, records)
    if any(candidate["artifacts"].get(path) != record for path, record in records.items()):
        raise PipelineError("Inspected files are not retained candidate artifacts")
    return candidate, inspected


class APIError(PipelineError):
    def __init__(self, status, reason="request_rejected"):
        self.status, self.reason = status, reason
        super().__init__(f"Store API rejected request (HTTP {status}, {reason})")


class APIUnavailable(PipelineError):
    pass


class DeliveryDeadline(PipelineError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, provider, token, timeout=60):
        self.base = {"play": "https://androidpublisher.googleapis.com", "apple": "https://api.appstoreconnect.apple.com", "firebase": "https://firebaseappdistribution.googleapis.com"}[provider]
        self.token, self.timeout = token, timeout
        # A processing poll can outlive a ten-minute JWT. The caller keeps its
        # existing temporary private-key file available until the operation ends.
        self.apple_key = None
        self.token_expires = 0
        if provider == "apple" and os.environ.get("ASC_PRIVATE_KEY_PATH"):
            self.apple_key = (os.environ["ASC_PRIVATE_KEY_PATH"], os.environ.get("ASC_KEY_ID", ""), os.environ.get("ASC_ISSUER_ID", ""))
        self.opener = urllib.request.build_opener(NoRedirect())

    def authorization(self):
        if self.apple_key:
            now = int(time.time())
            if now >= self.token_expires - 60:
                from .auth import apple_token
                self.token = apple_token(*self.apple_key, now=now)
                self.token_expires = now + 600
        return "Bearer " + self.token

    def __call__(self, method, path, body=None, upload=None):
        if not path.startswith(("/v1/", "/androidpublisher/v3/", "/upload/androidpublisher/v3/")) or "\n" in path:
            raise PipelineError("Invalid store API path")
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = {"Authorization": self.authorization(), "Accept": "application/json"}
        stream = None
        if upload is not None:
            stream = Path(upload).open("rb")
            data = stream
            headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(Path(upload).stat().st_size)})
        elif body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise PipelineError("Store API response exceeds size limit")
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                raise PipelineError("Malformed store API response")
            return value
        except urllib.error.HTTPError as exc:
            # Never log provider bodies: they can contain account information or echoed credentials.
            raw = exc.read(65536).decode("utf-8", "replace")
            reason = "review_must_be_deferred" if "Changes cannot be sent for review automatically" in raw else "request_rejected"
            raise APIError(exc.code, reason) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise APIUnavailable("Store API request failed; inspect saved operation state before retry") from None
        finally:
            if stream:
                stream.close()


def collection(api, path):
    items = []
    for _ in range(20):
        response = api("GET", path)
        data = response.get("data")
        if not isinstance(data, list):
            raise PipelineError("Malformed store collection")
        items.extend(data)
        nxt = response.get("links", {}).get("next")
        if not nxt:
            return items
        parsed = urllib.parse.urlsplit(nxt)
        if parsed.scheme != "https" or parsed.netloc != "api.appstoreconnect.apple.com" or not parsed.path.startswith("/v1/"):
            raise PipelineError("Unsafe store pagination link")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
    raise PipelineError("Store pagination exceeded limit")


def data(api, method, path, body=None):
    response = api(method, path, body)
    value = response.get("data")
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise PipelineError("Malformed store resource")
    return value


def state_file(config, root):
    path = confined_path(root, config["state_file"], must_exist=False)
    candidate = load_json(confined_path(root, config["candidate"]))
    reserved = {config["candidate"], config["inspection"]} | set(candidate.get("inputs", {})) | set(candidate.get("artifacts", {})) | set(candidate.get("checks", {}))
    if config["state_file"] in reserved:
        raise PipelineError("Store resume state cannot overwrite retained evidence")
    identity = fingerprint(config)
    state = load_json(path) if path.exists() else {"schema": 1, "request": identity, "phase": "new"}
    if state.get("schema") != 1 or state.get("request") != identity:
        raise PipelineError("Store resume state belongs to another request")
    return path, state


def save(path, state, phase, **values):
    state.update(values, phase=phase)
    atomic_json(path, state)


def verify_aab(config, root, candidate, inspected):
    path = confined_path(root, config["artifact"])
    if path.suffix != ".aab" or config["artifact"] not in inspected["artifacts"]:
        raise PipelineError("Play upload requires the inspected retained AAB")
    digest = candidate["artifacts"][config["artifact"]]["sha256"]
    signer = config.get("upload_signer_sha256", "").lower().replace(":", "")
    if not HEX.fullmatch(signer) or signer != inspected.get("upload_signer_sha256"):
        raise PipelineError("Expected upload certificate must match Android inspection")
    # Android upload keys are normally self-signed. jarsigner strict bit 4 is PKIX/certificate
    # trust, which is replaced here by an explicit SHA256 pin; unsigned entries (16) fail.
    verified = run(["jarsigner", "-J-Duser.language=en", "-verify", "-strict", str(path)], cwd=root, check=False, timeout=180)
    if verified.returncode not in (0, 4) or "jar verified" not in verified.stdout.lower():
        raise PipelineError("AAB JAR signature verification failed")
    cert = run(["keytool", "-J-Duser.language=en", "-printcert", "-jarfile", str(path)], cwd=root, timeout=180)
    fingerprints = re.findall(r"SHA256:\s*([0-9A-Fa-f:]+)", cert.stdout)
    if len(fingerprints) != 1 or fingerprints[0].replace(":", "").lower() != signer:
        raise PipelineError("AAB upload signer is not the pinned certificate")
    return path, digest


def track_versions(track):
    releases = track.get("releases", [])
    if not isinstance(releases, list):
        raise PipelineError("Malformed Play track")
    return {str(code) for release in releases for code in release.get("versionCodes", [])}


def play(config, root, candidate, inspected, api):
    settings = config["play"]
    allowed = {"package", "artifact", "upload_signer_sha256", "track", "source_track", "expected_track_digest",
               "release_status", "release_name", "release_notes", "user_fraction", "review_policy", "allow_review_defer_fallback"}
    strict_keys(settings, allowed, {"package", "track"})
    package, track = identifier(settings["package"]), identifier(settings["track"])
    if package != inspected["application"]["id"]:
        raise PipelineError("Play package differs from inspected AAB")
    version = str(inspected["application"]["build"])
    if not version.isdigit() or int(version) <= 0:
        raise PipelineError("Play versionCode must be positive")
    path, state = state_file(config, root)
    if state["phase"] == "done":
        return state["receipt"]
    if state["phase"] == "commit_started":
        raise PipelineError("Play commit outcome is uncertain; reconcile in Console/API before a new operation")
    base = f"/androidpublisher/v3/applications/{package}/edits"
    if state["phase"] == "new":
        edit = api("POST", base, {})
        save(path, state, "edit_created", edit_id=identifier(edit.get("id"), "Play edit id"))
    edit_base = base + "/" + identifier(state["edit_id"])
    current = api("GET", edit_base + "/tracks/" + track)
    current_digest = fingerprint(current)
    if config["operation"] == "play-inspect":
        receipt = {"schema": 1, "status": "passed", "operation": "play-inspect", "track_digest": current_digest,
                   "versions": sorted(track_versions(current)), "candidate_identity": candidate["identity"]}
        api("DELETE", edit_base)
        save(path, state, "done", receipt=receipt)
        return receipt
    if not HEX.fullmatch(str(settings.get("expected_track_digest", ""))):
        raise PipelineError("Expected target track digest is required (use play-inspect)")
    if state["phase"] in ("edit_created", "upload_started", "uploaded") and current_digest != settings["expected_track_digest"]:
        raise PipelineError("Play target track changed since approved selection")
    bundles = api("GET", edit_base + "/bundles").get("bundles", [])
    if not isinstance(bundles, list):
        raise PipelineError("Malformed Play bundle inventory")
    selected = [b for b in bundles if str(b.get("versionCode")) == version]
    if len(selected) > 1:
        raise PipelineError("Duplicate Play version identity")
    if config["operation"] == "play-upload":
        if "artifact" not in settings or "upload_signer_sha256" not in settings:
            raise PipelineError("AAB and certificate pin are required")
        artifact, digest = verify_aab(settings, root, candidate, inspected)
        if selected and selected[0].get("sha256", "").lower() != digest:
            raise PipelineError("Play versionCode already belongs to different bytes")
        if not selected:
            if state["phase"] == "uploaded":
                raise PipelineError("Previously uploaded Play bundle disappeared")
            if any(int(b.get("versionCode", 0)) >= int(version) for b in bundles):
                raise PipelineError("Play versionCode must increase for a new upload")
            save(path, state, "upload_started")
            response = api("POST", "/upload" + edit_base + "/bundles?uploadType=media", upload=artifact)
            if str(response.get("versionCode")) != version or response.get("sha256", "").lower() != digest:
                raise PipelineError("Google returned a different uploaded bundle identity")
            save(path, state, "uploaded")
    elif config["operation"] == "play-promote":
        source = identifier(settings.get("source_track"), "source track")
        if source == track or version not in track_versions(api("GET", edit_base + "/tracks/" + source)) or not selected:
            raise PipelineError("Promotion requires the existing selected version on another source track")
        artifact = settings.get("artifact")
        if not artifact or artifact not in inspected["artifacts"] or selected[0].get("sha256", "").lower() != candidate["artifacts"][artifact]["sha256"]:
            raise PipelineError("Existing Play bundle does not match retained candidate")
    status = settings.get("release_status")
    if status not in ("draft", "completed", "inProgress", "halted"):
        raise PipelineError("Invalid Play release status")
    release = {"versionCodes": [version], "status": status}
    if "release_name" in settings:
        if not isinstance(settings["release_name"], str) or len(settings["release_name"]) > 50:
            raise PipelineError("Invalid Play release name")
        release["name"] = settings["release_name"]
    if "release_notes" in settings:
        notes = settings["release_notes"]
        if not isinstance(notes, list) or len(notes) > 100:
            raise PipelineError("Invalid Play release notes")
        languages = set()
        for note in notes:
            strict_keys(note, {"language", "text"}, {"language", "text"})
            identifier(note["language"], "release note language")
            if note["language"] in languages or not isinstance(note["text"], str) or len(note["text"]) > 500:
                raise PipelineError("Duplicate or oversized Play release notes")
            languages.add(note["language"])
        release["releaseNotes"] = notes
    if status in ("inProgress", "halted"):
        release["userFraction"] = bounded(settings.get("user_fraction"), 0.000001, 0.999999, "rollout fraction")
    elif "user_fraction" in settings:
        raise PipelineError("Only staged/paused rollouts accept a fraction")
    desired = {"track": track, "releases": [release]}
    if state["phase"] == "track_updated" and fingerprint(current) != fingerprint(state["updated_track"]):
        raise PipelineError("Play edit track changed while resuming")
    if state["phase"] != "track_updated":
        response = api("PUT", edit_base + "/tracks/" + track, desired)
        returned = response.get("releases", [])
        if track_versions(response) != {version} or len(returned) != 1 or any(returned[0].get(k) != v for k, v in release.items()):
            raise PipelineError("Play did not retain the selected release")
        save(path, state, "track_updated", updated_track=response)
    policy = settings.get("review_policy")
    if policy not in ("submit", "defer"):
        raise PipelineError("Explicit Play review_policy is required")
    deferred = policy == "defer"
    query = "?changesInReviewBehavior=ERROR_IF_IN_REVIEW" + ("&changesNotSentForReview=true" if deferred else "")
    save(path, state, "commit_started")
    try:
        api("POST", edit_base + ":commit" + query)
    except APIError as exc:
        # A definitive HTTP rejection did not commit; retry only the documented review constraint.
        save(path, state, "track_updated")
        if exc.status != 400 or exc.reason != "review_must_be_deferred" or not settings.get("allow_review_defer_fallback") or deferred:
            raise
        deferred = True
        save(path, state, "commit_started")
        api("POST", edit_base + ":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW&changesNotSentForReview=true")
    receipt = {"schema": 1, "status": "pending", "operation": config["operation"], "candidate_identity": candidate["identity"],
               "package": package, "version_code": version, "track": track, "release_status": status,
               "state": "draft" if status == "draft" else ("console_review_required" if deferred else "submitted"),
               "availability_verified": False, "edit_id": state["edit_id"]}
    save(path, state, "done", receipt=receipt)
    return receipt


def apple_build(settings, inspected, api, timeout, interval, *, deadline=None):
    app, build = identifier(settings["app_id"]), identifier(settings["build_id"])
    resource = "/v1/builds/" + build
    deadline = time.monotonic() + timeout if deadline is None else deadline
    while True:
        item = data(api, "GET", resource)
        attributes = item.get("attributes", {})
        if (item["id"] != build or str(attributes.get("version")) != str(inspected["application"]["build"])
                or attributes.get("expired") is not False):
            raise PipelineError("Selected Apple build number differs or build expired")
        if attributes.get("expirationDate") is not None:
            try:
                expiry = datetime.fromisoformat(attributes["expirationDate"].replace("Z", "+00:00"))
                if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                    raise ValueError("expired or timezone absent")
            except (TypeError, AttributeError, ValueError):
                raise PipelineError("Apple build expiration is invalid or past") from None
        app_item = data(api, "GET", resource + "/app")
        pre = data(api, "GET", resource + "/preReleaseVersion").get("attributes", {})
        if (app_item["id"] != app or app_item.get("attributes", {}).get("bundleId") != inspected["application"]["id"]
                or pre.get("version") != inspected["application"]["version"] or pre.get("platform") != settings["platform"]):
            raise PipelineError("Apple build app/version/platform differs from inspected candidate")
        state = attributes.get("processingState")
        if state == "VALID":
            return build
        if state in ("FAILED", "INVALID"):
            raise PipelineError("Apple rejected build processing")
        if state != "PROCESSING":
            raise PipelineError("Unknown Apple processing state")
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def delivery_notes(settings, root, candidate):
    """Use existing candidate-bound note selections, or explicitly named readback locales."""
    locales = settings.get("delivery_locales", [])
    if (not isinstance(locales, list) or len(locales) > 100
            or any(not isinstance(locale, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", locale) for locale in locales)
            or len(locales) != len(set(locales))):
        raise PipelineError("Invalid or duplicate TestFlight delivery locales")
    desired = dict.fromkeys(locales)
    entries = settings.get("localizations", [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise PipelineError("Invalid TestFlight localized note selection")
    seen = set()
    for entry in entries:
        strict_keys(entry, {"locale", "notes", "expected_current_sha256"}, {"locale", "notes"})
        locale = entry["locale"]
        if (not isinstance(locale, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", locale)
                or locale in seen):
            raise PipelineError("Invalid or duplicate TestFlight note locale")
        seen.add(locale)
        if not isinstance(entry["notes"], str) or (entry["notes"] not in candidate["inputs"] and entry["notes"] not in candidate["artifacts"]):
            raise PipelineError("TestFlight approved notes must be candidate-bound")
        try:
            notes = confined_path(root, entry["notes"]).read_text(encoding="utf-8")
        except UnicodeError:
            raise PipelineError("TestFlight approved notes must be UTF-8 text") from None
        if not notes.strip() or len(notes) > 4000:
            raise PipelineError("TestFlight approved notes are empty or exceed 4000 characters")
        desired[locale] = notes
    return desired


def testflight_groups(settings, groups, build, api):
    result = {}
    for group in groups:
        item = data(api, "GET", "/v1/betaGroups/" + group)
        internal = item.get("attributes", {}).get("isInternalGroup")
        if item["id"] != group or not isinstance(internal, bool):
            raise PipelineError("TestFlight group identity or type is missing")
        app = data(api, "GET", "/v1/betaGroups/" + group + "/app")
        if app["id"] != settings["app_id"]:
            raise PipelineError("TestFlight group belongs to another app")
        members = collection(api, "/v1/betaGroups/" + group + "/relationships/builds")
        if any(not isinstance(member, dict) or member.get("type") != "builds" or not isinstance(member.get("id"), str) for member in members):
            raise PipelineError("Malformed TestFlight group membership")
        result[group] = {"internal": internal, "member": any(member["id"] == build for member in members)}
    return result


def testflight(config, root, candidate, inspected, api, path, state, receipt, build, deadline):
    settings = config["apple"]
    groups = settings.get("group_ids")
    if (not isinstance(groups, list) or not groups or len(groups) > 100
            or any(not isinstance(group, str) for group in groups) or len(groups) != len(set(groups))):
        raise PipelineError("TestFlight needs unique explicit groups")
    for group in groups:
        identifier(group)
    desired_notes = delivery_notes(settings, root, candidate)
    historical = state["phase"] == "done"
    group_state = testflight_groups(settings, groups, build, api)
    attempted = state.get("attempted_groups", [])
    if not isinstance(attempted, list) or any(group not in groups for group in attempted):
        raise PipelineError("Invalid saved TestFlight group attempts")
    # Record intent before each write. A delayed or lost response is reconciled by
    # readback on retry; it must never assign or trigger beta review a second time.
    for group in groups:
        if not historical and not group_state[group]["member"] and group not in attempted:
            attempted = attempted + [group]
            save(path, state, "delivery_pending", attempted_groups=attempted)
            api("POST", "/v1/betaGroups/" + group + "/relationships/builds", {"data": [{"type": "builds", "id": build}]})
    beta = data(api, "GET", "/v1/builds/" + build + "/buildBetaDetail").get("attributes", {})
    if (not historical and settings.get("submit_beta_review") and not state.get("beta_review_attempted")
            and beta.get("externalBuildState") == "READY_FOR_BETA_SUBMISSION"):
        existing = collection(api, "/v1/betaAppReviewSubmissions?filter%5Bbuild%5D=" + build)
        if len(existing) > 1:
            raise PipelineError("Multiple beta review submissions require reconciliation")
        if not existing:
            save(path, state, "delivery_pending", beta_review_attempted=True)
            api("POST", "/v1/betaAppReviewSubmissions", {"data": {"type": "betaAppReviewSubmissions", "relationships": {"build": {"data": {"type": "builds", "id": build}}}}})
    while True:
        reasons = []
        if apple_build(settings, inspected, api, 0, 0) is None:
            reasons.append("build_processing")
        current_groups = testflight_groups(settings, groups, build, api)
        beta = data(api, "GET", "/v1/builds/" + build + "/buildBetaDetail").get("attributes", {})
        if beta.get("externalBuildState") == "BETA_REJECTED" or beta.get("internalBuildState") in ("EXPIRED", "PROCESSING_EXCEPTION"):
            raise PipelineError("Apple beta testing rejected/expired")
        if any(not group["member"] for group in current_groups.values()):
            reasons.append("group_membership_not_visible")
        if any(beta.get("internalBuildState" if group["internal"] else "externalBuildState")
               not in ("READY_FOR_BETA_TESTING", "IN_BETA_TESTING") for group in current_groups.values()):
            reasons.append("beta_processing_or_review")
        existing_notes = collection(api, "/v1/builds/" + build + "/betaBuildLocalizations")
        by_locale = {}
        for item in existing_notes:
            if not isinstance(item, dict) or not isinstance(item.get("attributes"), dict):
                raise PipelineError("Malformed Apple beta localization")
            attributes = item.get("attributes", {})
            locale = attributes.get("locale")
            if not isinstance(locale, str) or locale in by_locale:
                raise PipelineError("Apple beta localizations contain absent or duplicate locales")
            by_locale[locale] = attributes.get("whatsNew")
        selected = desired_notes or dict.fromkeys(by_locale)
        notes_ready = bool(selected)
        summaries = []
        for locale, expected in selected.items():
            text = by_locale.get(locale)
            if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                    or (expected is not None and text != expected)):
                notes_ready = False
            else:
                summaries.append({"locale": locale, "sha256": hashlib.sha256(text.encode()).hexdigest()})
        if not notes_ready:
            reasons.append("tester_notes_missing_or_different")
        ready = not reasons
        receipt.update(status="passed" if ready else "pending", state="testing_ready" if ready else "beta_processing_or_review",
                       groups=groups, beta=beta, localizations=summaries, pending_reasons=reasons,
                       readiness_verified=ready, historical_success=historical)
        if ready:
            receipt["verified_at"] = datetime.now(timezone.utc).isoformat()
            save(path, state, "done", receipt=receipt)
            return receipt
        if time.monotonic() >= deadline:
            return receipt
        time.sleep(min(config.get("poll_interval", 15), max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return receipt


def apple(config, root, candidate, inspected, api, *, deadline=None):
    settings = config["apple"]
    strict_keys(settings, {"app_id", "build_id", "platform", "group_ids", "submit_beta_review", "app_store_version_id", "expected_selected_build", "submission_id", "localizations", "delivery_locales"},
                {"app_id", "build_id", "platform"})
    if settings["platform"] not in ("IOS", "MAC_OS", "TV_OS", "VISION_OS"):
        raise PipelineError("Unsupported Apple platform")
    if inspected.get("distribution_ready") is not True:
        raise PipelineError("Apple distribution requires verified signed archive evidence")
    path, state = state_file(config, root)
    if state["phase"] == "done" and config["operation"] != "testflight":
        return state["receipt"]
    if state["phase"] == "creating_submission":
        raise PipelineError("Review submission creation outcome is uncertain; reconcile its ID before retry")
    receipt = {"schema": 1, "status": "pending", "operation": config["operation"],
               "candidate_identity": candidate["identity"], "app_id": settings["app_id"], "build_id": settings["build_id"]}
    if config["operation"] == "testflight" and deadline is None:
        deadline = time.monotonic() + config.get("timeout", 900)
    build = apple_build(settings, inspected, api, config.get("timeout", 900), config.get("poll_interval", 15), deadline=deadline)
    if build is None:
        return dict(receipt, state="processing_timeout")
    if config["operation"] == "apple-build":
        return dict(receipt, status="passed", state="processed", availability_verified=False)
    if config["operation"] == "apple-beta-notes":
        localizations = settings.get("localizations")
        if not isinstance(localizations, list) or not localizations:
            raise PipelineError("Apple beta notes require explicit locale entries")
        desired, locales = [], set()
        for entry in localizations:
            strict_keys(entry, {"locale", "notes", "expected_current_sha256"}, {"locale", "notes", "expected_current_sha256"})
            locale = entry["locale"]
            if not isinstance(locale, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", locale) or locale in locales:
                raise PipelineError("Invalid or duplicate Apple beta-note locale")
            locales.add(locale)
            notes_path = confined_path(root, entry["notes"])
            if entry["notes"] not in candidate["inputs"] and entry["notes"] not in candidate["artifacts"]:
                raise PipelineError("Apple beta notes must be candidate-bound")
            try:
                notes = notes_path.read_text()
            except UnicodeError:
                raise PipelineError("Apple beta notes must be UTF-8 text") from None
            if not notes.strip() or len(notes) > 4000:
                raise PipelineError("Apple beta notes are empty or exceed 4000 characters")
            expected = entry["expected_current_sha256"]
            if expected is not None and not HEX.fullmatch(str(expected)):
                raise PipelineError("Expected existing Apple notes digest must be explicit or null")
            desired.append((locale, notes, expected))
        existing = collection(api, "/v1/builds/" + build + "/betaBuildLocalizations")
        by_locale = {}
        for item in existing:
            locale = item.get("attributes", {}).get("locale")
            if not isinstance(locale, str) or locale in by_locale:
                raise PipelineError("Apple beta localizations contain absent or duplicate locales")
            by_locale[locale] = item
        changes, summaries = [], []
        for locale, notes, expected in desired:
            current = by_locale.get(locale)
            old = current.get("attributes", {}).get("whatsNew") if current else None
            if old is not None and not isinstance(old, str):
                raise PipelineError("Malformed Apple beta notes")
            digest = hashlib.sha256(notes.encode()).hexdigest()
            old_digest = hashlib.sha256(old.encode()).hexdigest() if old is not None else None
            if old != notes and old_digest != expected:
                raise PipelineError("Apple beta notes changed since approved selection")
            if old != notes:
                changes.append((locale, notes, current))
            summaries.append({"locale": locale, "sha256": digest})
        # Preflight every locale before any write. A partial failed run is safely retried by reading again.
        for locale, notes, current in changes:
            if current:
                loc = identifier(current["id"], "beta localization ID")
                updated = data(api, "PATCH", "/v1/betaBuildLocalizations/" + loc,
                               {"data": {"type": "betaBuildLocalizations", "id": loc, "attributes": {"whatsNew": notes}}})
            else:
                updated = data(api, "POST", "/v1/betaBuildLocalizations", {"data": {"type": "betaBuildLocalizations",
                               "attributes": {"locale": locale, "whatsNew": notes}, "relationships": {"build": {"data": {"type": "builds", "id": build}}}}})
            if updated.get("attributes", {}).get("locale") != locale or updated.get("attributes", {}).get("whatsNew") != notes:
                raise PipelineError("Apple did not retain the selected localized beta notes")
        return dict(receipt, status="passed", state="beta_notes_updated", localizations=summaries, availability_verified=False)
    if config["operation"] == "testflight":
        return testflight(config, root, candidate, inspected, api, path, state, receipt, build, deadline)
    version = identifier(settings.get("app_store_version_id"), "App Store version ID")
    version_path = "/v1/appStoreVersions/" + version
    version_data = data(api, "GET", version_path + "?include=app")
    attr = version_data.get("attributes", {})
    app_id = version_data.get("relationships", {}).get("app", {}).get("data", {}).get("id")
    if app_id != settings["app_id"] or attr.get("versionString") != inspected["application"]["version"] or attr.get("platform") != settings["platform"]:
        raise PipelineError("Selected App Store version does not match app/version/platform")
    current = api("GET", version_path + "/relationships/build").get("data")
    selected = current.get("id") if isinstance(current, dict) else None
    expected = settings.get("expected_selected_build")
    if selected not in (expected, build):
        raise PipelineError("App Store version already selects another build")
    if selected != build:
        if config["operation"] == "apple-status":
            raise PipelineError("App Store version does not select the candidate build")
        api("PATCH", version_path + "/relationships/build", {"data": {"type": "builds", "id": build}})
    if config["operation"] == "apple-select":
        return dict(receipt, status="passed", state="build_selected", app_store_version_id=version, availability_verified=False)
    if config["operation"] == "apple-status":
        version_state = attr.get("appVersionState", attr.get("appStoreState"))
        available = version_state in ("READY_FOR_DISTRIBUTION", "READY_FOR_SALE") and attr.get("downloadable") is True
        if version_state in ("REJECTED", "INVALID_BINARY", "METADATA_REJECTED"):
            raise PipelineError("App Store submission was rejected")
        return dict(receipt, status="passed" if available else "pending", state=version_state, availability_verified=available)
    submission = state.get("submission_id") or settings.get("submission_id")
    if not submission:
        existing = collection(api, "/v1/reviewSubmissions?filter%5Bapp%5D=" + settings["app_id"] + "&filter%5Bplatform%5D=" + settings["platform"])
        active = [item for item in existing if item.get("attributes", {}).get("state") != "COMPLETE"]
        if active:
            raise PipelineError("Existing review submission requires its explicit ID; refusing duplicate")
        save(path, state, "creating_submission")
        item = data(api, "POST", "/v1/reviewSubmissions", {"data": {"type": "reviewSubmissions", "attributes": {"platform": settings["platform"]},
                    "relationships": {"app": {"data": {"type": "apps", "id": settings["app_id"]}}}}})
        submission = item["id"]
        save(path, state, "submission_created", submission_id=submission)
    identifier(submission)
    subpath = "/v1/reviewSubmissions/" + submission
    sub = data(api, "GET", subpath + "?include=app")
    if sub.get("relationships", {}).get("app", {}).get("data", {}).get("id") != settings["app_id"]:
        raise PipelineError("Review submission belongs to another app")
    items = collection(api, subpath + "/items?include=appStoreVersion")
    versions = [i.get("relationships", {}).get("appStoreVersion", {}).get("data", {}).get("id") for i in items]
    if versions and versions != [version]:
        raise PipelineError("Review submission contains unrelated or duplicate items")
    sub_state = sub.get("attributes", {}).get("state")
    if not versions:
        if sub_state != "READY_FOR_REVIEW":
            raise PipelineError("Review submission cannot accept the selected version")
        data(api, "POST", "/v1/reviewSubmissionItems", {"data": {"type": "reviewSubmissionItems", "relationships": {
            "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission}},
            "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version}}}}})
    if sub_state == "READY_FOR_REVIEW":
        data(api, "PATCH", subpath, {"data": {"type": "reviewSubmissions", "id": submission, "attributes": {"submitted": True}}})
    elif sub_state not in ("WAITING_FOR_REVIEW", "IN_REVIEW", "COMPLETING", "COMPLETE"):
        raise PipelineError("Review submission requires manual resolution")
    receipt.update(state="submitted_for_review", submission_id=submission, app_store_version_id=version, availability_verified=False)
    save(path, state, "done", receipt=receipt)
    return receipt


def execute(config, root, *, api=None):
    if config.get("operation") in ("play-inventory", "apple-inventory"):
        from .store_inventory import execute as inventory
        return inventory(config, root, api=api)
    required = {"schema", "operation", "candidate", "inspection", "expected_identity", "expected_source_sha", "allowed_refs", "token_env", "state_file"}
    strict_keys(config, required | {"play", "apple", "firebase", "timeout", "poll_interval"}, required)
    if config["schema"] != 1:
        raise PipelineError("Unsupported stores schema")
    play_ops = ("play-inspect", "play-upload", "play-promote")
    apple_ops = ("apple-build", "testflight", "apple-select", "apple-submit", "apple-status", "apple-beta-notes")
    operation = config["operation"]
    if operation not in play_ops + apple_ops + ("firebase-distribution",):
        raise PipelineError("Unsupported store operation")
    provider = "firebase" if operation == "firebase-distribution" else ("play" if operation in play_ops else "apple")
    if provider not in config or any(other in config for other in {"play", "apple", "firebase"} - {provider}):
        raise PipelineError("Exactly one matching store configuration is required")
    bounded(config.get("timeout", 900), 1, 3600, "store timeout")
    bounded(config.get("poll_interval", 15), 0.01, 120, "poll interval")
    trusted_context(config["allowed_refs"])
    candidate, inspected = load_candidate(config, root, {"apple"} if provider == "apple" else {"android"})
    token = env_secret(config["token_env"])
    transport = api or API(provider, token, min(120, config.get("timeout", 900)))
    if provider == "firebase":
        from .app_distribution import distribute
        return distribute(config, Path(root), candidate, inspected, transport)
    deadline = time.monotonic() + config.get("timeout", 900) if operation == "testflight" else None
    def bounded_transport(*args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeliveryDeadline("TestFlight verification deadline exceeded")
        previous = transport.timeout if isinstance(transport, API) else None
        try:
            if previous is not None:
                transport.timeout = min(previous, remaining)
            result = transport(*args, **kwargs)
        finally:
            if previous is not None:
                transport.timeout = previous
        if time.monotonic() >= deadline:
            raise DeliveryDeadline("TestFlight verification deadline exceeded")
        return result
    try:
        if operation == "testflight":
            return apple(config, Path(root), candidate, inspected, bounded_transport, deadline=deadline)
        return (play if provider == "play" else apple)(config, Path(root), candidate, inspected, transport)
    except DeliveryDeadline:
        return {"schema": 1, "status": "pending", "operation": operation, "candidate_identity": candidate["identity"],
                "app_id": config["apple"]["app_id"], "build_id": config["apple"]["build_id"],
                "state": "readiness_timeout", "readiness_verified": False,
                "pending_reasons": ["verification_deadline_exceeded"]}
    except (APIError, APIUnavailable) as exc:
        if operation != "testflight" or (isinstance(exc, APIError) and exc.status != 429 and not 500 <= exc.status <= 599):
            raise
        return {"schema": 1, "status": "pending", "operation": operation, "candidate_identity": candidate["identity"],
                "app_id": config["apple"]["app_id"], "build_id": config["apple"]["build_id"],
                "state": "readback_unavailable", "readiness_verified": False,
                "pending_reasons": ["store_temporarily_unavailable"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = execute(load_json(args.config), args.root)
    atomic_json(args.output, result)
    if result["status"] == "pending":
        raise SystemExit(2)


if __name__ == "__main__":
    cli_error(main)
