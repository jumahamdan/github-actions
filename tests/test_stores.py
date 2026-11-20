import copy
import hashlib
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError, atomic_json, fingerprint, sha256_file
from shared_ci import stores
from shared_ci.evidence import IDENTITY_FIELDS


TRUST = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/staging", "STORE_TOKEN": "test-secret-never-real"}


def fixture(root, kind="android", files=None, inspection_extra=None):
    files = files or {"app.aab": b"fake-fixture-aab"}
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / "input.txt").write_text("synthetic build inputs")
    records = {p: {"sha256": sha256_file(root / p), "bytes": (root / p).stat().st_size} for p in files}
    inspected = {"schema": 1, "status": "passed", "kind": kind, "source_sha": "a" * 40,
                 "application": {"id": "org.example.app", "version": "1.0.0", "build": "123"},
                 "artifacts": records, "upload_signer_sha256": "e" * 64, "distribution_ready": True}
    inspected.update(inspection_extra or {})
    inspected["assurance"] = {"kind": "mobile", "source_sha": "a" * 40, "source_tree": "b" * 40, "config_sha256": sha256_file(root / "input.txt")}
    atomic_json(root / "inspection.json", inspected)
    candidate = {"schema": 1, "status": "passed", "source_sha": "a" * 40, "source_tree": "b" * 40,
                 "created_at": "2026-01-01T00:00:00+00:00",
                 "product": "synthetic-app", "version": "1.0.0", "build": "123", "library_sha": "c" * 40,
                 "inputs": {"input.txt": {"sha256": sha256_file(root / "input.txt"), "bytes": (root / "input.txt").stat().st_size}},
                 "artifacts": records, "checks": {"inspection.json": sha256_file(root / "inspection.json")}, "check_requirements": {"inspection.json": {"kind": "mobile", "config": "input.txt"}}, "toolchain": {"fixture": "1"}}
    candidate["identity"] = fingerprint({k: candidate[k] for k in IDENTITY_FIELDS})
    atomic_json(root / "candidate.json", candidate)
    common = {"schema": 1, "candidate": "candidate.json", "inspection": "inspection.json", "expected_identity": candidate["identity"],
              "expected_source_sha": "a" * 40, "allowed_refs": ["refs/heads/staging"]}
    config = dict(common, operation="play-upload", token_env="STORE_TOKEN", state_file="state.json",
                  play={"package": "org.example.app", "artifact": "app.aab", "upload_signer_sha256": "e" * 64,
                        "track": "internal", "expected_track_digest": fingerprint({"track": "internal", "releases": []}),
                        "release_status": "completed", "review_policy": "submit"})
    return config, candidate, inspected


class PlayAPI:
    def __init__(self, digest):
        self.calls, self.bundles, self.target = [], [], {"track": "internal", "releases": []}
        self.digest, self.commit_errors, self.uploads = digest, [], 0

    def __call__(self, method, path, body=None, upload=None):
        self.calls.append((method, path, body, upload))
        if ":commit" in path:
            if self.commit_errors:
                raise self.commit_errors.pop(0)
            return {"id": "edit-1"}
        if path.endswith("/edits"):
            return {"id": "edit-1"}
        if "/bundles" in path:
            if method == "POST":
                self.uploads += 1
                item = {"versionCode": 123, "sha256": self.digest}
                self.bundles = [item]
                return item
            return {"bundles": self.bundles}
        if path.endswith("/tracks/beta"):
            return {"track": "beta", "releases": [{"status": "completed", "versionCodes": ["123"]}]}
        if "/tracks/" in path:
            if method == "PUT":
                self.target = copy.deepcopy(body)
            return self.target
        if method == "DELETE":
            return {}
        raise AssertionError((method, path, body))


class AppleAPI:
    def __init__(self):
        self.calls, self.processing, self.group_builds = [], "VALID", []
        self.beta = {"internalBuildState": "IN_BETA_TESTING", "externalBuildState": "WAITING_FOR_BETA_REVIEW"}
        self.selected, self.submissions, self.items = None, [], []
        self.app_id, self.sub_state, self.store_state = "100", "READY_FOR_REVIEW", "WAITING_FOR_REVIEW"
        self.localizations = []

    def resource(self, id, attributes=None, relationships=None):
        return {"data": {"id": id, "attributes": attributes or {}, "relationships": relationships or {}}}

    def __call__(self, method, path, body=None, upload=None):
        self.calls.append((method, path, body))
        if path == "/v1/builds/200/betaBuildLocalizations":
            return {"data": self.localizations}
        if path == "/v1/betaBuildLocalizations" and method == "POST":
            item = {"id": "locale" + str(len(self.localizations)), "attributes": body["data"]["attributes"]}
            self.localizations.append(item)
            return {"data": item}
        if path.startswith("/v1/betaBuildLocalizations/") and method == "PATCH":
            item = next(item for item in self.localizations if item["id"] == path.split("/")[-1])
            item["attributes"].update(body["data"]["attributes"])
            return {"data": item}
        if path == "/v1/builds/200":
            return self.resource("200", {"version": "123", "expired": False, "processingState": self.processing})
        if path.endswith("/preReleaseVersion"):
            return self.resource("pre", {"version": "1.0.0", "platform": "IOS"})
        if path.endswith("/app"):
            return self.resource(self.app_id, {"bundleId": "org.example.app"})
        if path.endswith("/buildBetaDetail"):
            return self.resource("detail", self.beta)
        if "/betaGroups/" in path:
            if path.endswith("/relationships/builds"):
                if method == "POST":
                    self.group_builds.extend(body["data"])
                    return {}
                return {"data": self.group_builds}
            return self.resource("group1", {"isInternalGroup": True})
        if path == "/v1/appStoreVersions/300?include=app":
            return self.resource("300", {"versionString": "1.0.0", "platform": "IOS", "appVersionState": self.store_state, "downloadable": self.store_state == "READY_FOR_DISTRIBUTION"}, {"app": {"data": {"id": "100"}}})
        if path.endswith("/relationships/build"):
            if method == "PATCH":
                self.selected = body["data"]["id"]
                return body
            return {"data": {"id": self.selected} if self.selected else None}
        if path.startswith("/v1/reviewSubmissions?"):
            return {"data": self.submissions}
        if path == "/v1/reviewSubmissions" and method == "POST":
            return self.resource("sub1")
        if path.startswith("/v1/reviewSubmissions/sub1/items"):
            return {"data": self.items}
        if path.startswith("/v1/reviewSubmissions/sub1"):
            if method == "PATCH":
                self.sub_state = "WAITING_FOR_REVIEW"
            return self.resource("sub1", {"state": self.sub_state}, {"app": {"data": {"id": "100"}}})
        if path == "/v1/reviewSubmissionItems":
            item = {"id": "item1", "relationships": {"appStoreVersion": {"data": {"id": "300"}}}}
            self.items.append(item)
            return {"data": item}
        raise AssertionError((method, path, body))


class StoresTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config, self.candidate, self.inspected = fixture(self.root)
        self.api = PlayAPI(self.candidate["artifacts"]["app.aab"]["sha256"])
        self.env = patch.dict(os.environ, TRUST, clear=True)
        self.env.start()
        self.jar = patch("shared_ci.stores.verify_aab", return_value=(self.root / "app.aab", self.api.digest))
        self.jar.start()

    def tearDown(self):
        self.jar.stop()
        self.env.stop()
        self.tmp.cleanup()

    def apple(self, operation="testflight"):
        self.config, self.candidate, self.inspected = fixture(self.root, "apple")
        self.config.pop("play")
        self.config.update(operation=operation, apple={"app_id": "100", "build_id": "200", "platform": "IOS", "group_ids": ["group1"], "app_store_version_id": "300", "expected_selected_build": None})
        self.api = AppleAPI()

    def test_upload_exact_bytes_and_no_cancellation_default(self):
        result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["state"], "submitted")
        self.assertFalse(result["availability_verified"])
        commit = next(c for c in self.api.calls if ":commit" in c[1])
        self.assertIn("ERROR_IF_IN_REVIEW", commit[1])
        self.assertIsNone(commit[2])
        self.assertEqual(self.api.uploads, 1)

    def test_completed_receipt_retry_does_not_republish(self):
        first = stores.execute(self.config, self.root, api=self.api)
        n = len(self.api.calls)
        self.assertEqual(stores.execute(self.config, self.root, api=self.api), first)
        self.assertEqual(len(self.api.calls), n)

    def test_existing_bundle_reuses_matching_hash(self):
        self.api.bundles = [{"versionCode": 123, "sha256": self.api.digest}]
        stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(self.api.uploads, 0)

    def test_conflicting_existing_version_fails(self):
        self.api.bundles = [{"versionCode": 123, "sha256": "f" * 64}]
        with self.assertRaisesRegex(PipelineError, "different bytes"):
            stores.execute(self.config, self.root, api=self.api)

    def test_track_drift_fails_before_upload(self):
        self.api.target["releases"] = [{"versionCodes": ["122"]}]
        with self.assertRaisesRegex(PipelineError, "changed"):
            stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(self.api.uploads, 0)

    def test_promote_existing_bundle_without_upload(self):
        self.config["operation"] = "play-promote"
        self.config["play"]["source_track"] = "beta"
        self.api.bundles = [{"versionCode": 123, "sha256": self.api.digest}]
        stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(self.api.uploads, 0)

    def test_only_documented_review_rejection_allows_defer(self):
        self.config["play"]["allow_review_defer_fallback"] = True
        self.api.commit_errors = [stores.APIError(400, "review_must_be_deferred")]
        result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["state"], "console_review_required")
        self.assertIn("changesNotSentForReview=true", self.api.calls[-1][1])

    def test_unrelated_400_does_not_fallback(self):
        self.config["play"]["allow_review_defer_fallback"] = True
        self.api.commit_errors = [stores.APIError(400)]
        with self.assertRaises(stores.APIError):
            stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(sum(":commit" in c[1] for c in self.api.calls), 1)

    def test_uncertain_commit_retry_refuses(self):
        self.api.commit_errors = [PipelineError("connection lost")]
        with self.assertRaises(PipelineError):
            stores.execute(self.config, self.root, api=self.api)
        with self.assertRaisesRegex(PipelineError, "uncertain"):
            stores.execute(self.config, self.root, api=self.api)

    def test_draft_remains_pending(self):
        self.config["play"]["release_status"] = "draft"
        result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual((result["status"], result["state"]), ("pending", "draft"))

    def test_changed_artifact_and_receipt_fail(self):
        (self.root / "app.aab").write_bytes(b"changed")
        with self.assertRaisesRegex(PipelineError, "changed"):
            stores.execute(self.config, self.root, api=self.api)
        self.assertFalse(self.api.calls)

    def test_inspection_not_bound_fails(self):
        self.config["inspection"] = "input.txt"
        with self.assertRaisesRegex(PipelineError, "not bound"):
            stores.execute(self.config, self.root, api=self.api)

    def test_source_selection_mismatch_fails(self):
        self.config["expected_source_sha"] = "f" * 40
        with self.assertRaisesRegex(PipelineError, "trusted caller"):
            stores.execute(self.config, self.root, api=self.api)

    def test_untrusted_ref_and_unknown_config_fail(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}):
            with self.assertRaises(PipelineError):
                stores.execute(self.config, self.root, api=self.api)
        self.config["typo"] = True
        with self.assertRaises(PipelineError):
            stores.execute(self.config, self.root, api=self.api)
        self.assertFalse(self.api.calls)

    def test_apple_group_assignment_is_idempotent(self):
        self.apple()
        result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["state"], "testing_ready")
        self.assertEqual(self.api.group_builds, [{"type": "builds", "id": "200"}])
        stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(len(self.api.group_builds), 1)

    def test_rendered_apple_staging_requires_actual_testflight_readiness(self):
        from shared_ci.gate import evaluate
        from test_caller_templates import rendered

        workflow, files = rendered("apple-staging")
        policy = json.loads(files[workflow["jobs"]["required"]["with"]["config"]])
        self.apple()
        self.api.beta["internalBuildState"] = "PROCESSING"
        receipt = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual((receipt["status"], receipt["state"]), ("pending", "beta_processing_or_review"))
        results = {"import": {"result": "success", "outputs": {"status": "passed"}},
                   "distribute": {"result": "success", "outputs": {key: receipt[key] for key in ("status", "state")}}}
        with self.assertRaisesRegex(PipelineError, "pending"):
            evaluate(policy, results)
        self.api.beta["internalBuildState"] = "IN_BETA_TESTING"
        receipt = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual((receipt["status"], receipt["state"]), ("passed", "testing_ready"))
        results["distribute"]["outputs"] = {key: receipt[key] for key in ("status", "state")}
        self.assertEqual(evaluate(policy, results)["status"], "passed")
        self.assertEqual(len(self.api.group_builds), 1)

    def test_apple_wrong_app_fails(self):
        self.apple()
        self.api.app_id = "wrong"
        with self.assertRaisesRegex(PipelineError, "differs"):
            stores.execute(self.config, self.root, api=self.api)

    def test_apple_processing_timeout_is_pending(self):
        self.apple("apple-build")
        self.api.processing = "PROCESSING"
        with patch("shared_ci.stores.time.monotonic", side_effect=[0, 1000]):
            result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["status"], "pending")

    def test_apple_failed_processing_blocks(self):
        self.apple()
        self.api.processing = "FAILED"
        with self.assertRaisesRegex(PipelineError, "rejected"):
            stores.execute(self.config, self.root, api=self.api)

    def test_apple_build_selection_conflict_blocks(self):
        self.apple("apple-select")
        self.api.selected = "another"
        with self.assertRaisesRegex(PipelineError, "another build"):
            stores.execute(self.config, self.root, api=self.api)

    def test_apple_submission_uses_review_submission_api(self):
        self.apple("apple-submit")
        result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["submission_id"], "sub1")
        self.assertTrue(any(c[0] == "PATCH" and c[1] == "/v1/reviewSubmissions/sub1" for c in self.api.calls))
        self.assertFalse(any("appStoreVersionSubmissions" in c[1] for c in self.api.calls))

    def test_apple_active_submission_requires_explicit_id(self):
        self.apple("apple-submit")
        self.api.submissions = [{"id": "sub1", "attributes": {"state": "IN_REVIEW"}}]
        with self.assertRaisesRegex(PipelineError, "explicit ID"):
            stores.execute(self.config, self.root, api=self.api)

    def test_apple_status_only_passes_downloadable_release(self):
        self.apple("apple-status")
        self.api.selected = "200"
        self.assertEqual(stores.execute(self.config, self.root, api=self.api)["status"], "pending")
        self.api.store_state = "READY_FOR_DISTRIBUTION"
        self.assertTrue(stores.execute(self.config, self.root, api=self.api)["availability_verified"])

    def beta_notes(self):
        self.apple("apple-beta-notes")
        (self.root / "notes.txt").write_text("Synthetic testing notes")
        self.candidate["inputs"]["notes.txt"] = {"sha256": sha256_file(self.root / "notes.txt"), "bytes": (self.root / "notes.txt").stat().st_size}
        self.candidate["identity"] = fingerprint({key: self.candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.root / "candidate.json", self.candidate)
        self.config["expected_identity"] = self.candidate["identity"]
        self.config["apple"]["localizations"] = [{"locale": "en-US", "notes": "notes.txt", "expected_current_sha256": None}]

    def test_apple_beta_notes_create_and_idempotent_retry(self):
        self.beta_notes()
        result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["state"], "beta_notes_updated")
        stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 1)
        self.assertEqual(len(self.api.localizations), 1)

    def test_apple_beta_notes_drift_blocks_all_locale_writes(self):
        self.beta_notes()
        self.config["apple"]["localizations"].append({"locale": "fr-FR", "notes": "notes.txt", "expected_current_sha256": None})
        self.api.localizations = [{"id": "fr", "attributes": {"locale": "fr-FR", "whatsNew": "unapproved edit"}}]
        with self.assertRaisesRegex(PipelineError, "changed since"):
            stores.execute(self.config, self.root, api=self.api)
        self.assertFalse(any(method in ("POST", "PATCH") for method, _, _ in self.api.calls))

    def test_apple_beta_notes_patch_matching_existing_digest(self):
        self.beta_notes()
        self.api.localizations = [{"id": "en", "attributes": {"locale": "en-US", "whatsNew": "old"}}]
        self.config["apple"]["localizations"][0]["expected_current_sha256"] = hashlib.sha256(b"old").hexdigest()
        stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(self.api.localizations[0]["attributes"]["whatsNew"], "Synthetic testing notes")

    def test_source_tree_in_candidate_identity_cannot_change(self):
        self.candidate["source_tree"] = "f" * 40
        atomic_json(self.root / "candidate.json", self.candidate)
        with self.assertRaisesRegex(PipelineError, "trusted caller"):
            stores.execute(self.config, self.root, api=self.api)

    def test_wrong_check_source_fails_even_with_rehashed_identity(self):
        inspected = copy.deepcopy(self.inspected)
        inspected["assurance"]["source_sha"] = "f" * 40
        atomic_json(self.root / "inspection.json", inspected)
        self.candidate["checks"]["inspection.json"] = sha256_file(self.root / "inspection.json")
        self.candidate["identity"] = fingerprint({k: self.candidate[k] for k in IDENTITY_FIELDS})
        self.config["expected_identity"] = self.candidate["identity"]
        atomic_json(self.root / "candidate.json", self.candidate)
        with self.assertRaisesRegex(PipelineError, "not bound"):
            stores.execute(self.config, self.root, api=self.api)

    def test_state_cannot_overwrite_candidate(self):
        self.config["state_file"] = "candidate.json"
        with self.assertRaisesRegex(PipelineError, "overwrite"):
            stores.execute(self.config, self.root, api=self.api)

    def test_aab_jar_signer_must_match(self):
        self.jar.stop()
        outputs = [subprocess.CompletedProcess([], 4, "jar verified.", ""), subprocess.CompletedProcess([], 0, "SHA256: " + "EE:" * 31 + "EE", "")]
        with patch("shared_ci.stores.run", side_effect=outputs):
            path, digest = stores.verify_aab(self.config["play"], self.root, self.candidate, self.inspected)
        self.assertEqual(digest, self.api.digest)
        with patch("shared_ci.stores.run", return_value=subprocess.CompletedProcess([], 16, "jar verified.", "")):
            with self.assertRaisesRegex(PipelineError, "signature"):
                stores.verify_aab(self.config["play"], self.root, self.candidate, self.inspected)
        self.jar.start()

    def test_api_headers_secret_not_url_and_redirects_disabled(self):
        api = stores.API("play", "synthetic-secret")
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id":"one"}'
        with patch.object(api.opener, "open", return_value=response) as opening:
            api("POST", "/androidpublisher/v3/applications/org.example/edits", {})
        request = opening.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-secret")
        self.assertNotIn("synthetic-secret", request.full_url)
        self.assertIsNone(stores.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_apple_api_refreshes_existing_key_before_jwt_expiry(self):
        auth = types.ModuleType("shared_ci.auth")
        auth.apple_token = unittest.mock.Mock(side_effect=["first-token", "second-token", "third-token"])
        env = {"ASC_PRIVATE_KEY_PATH": "/temporary/existing-key.p8", "ASC_KEY_ID": "EXAMPLE1234", "ASC_ISSUER_ID": "synthetic-issuer"}
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data":{"id":"200"}}'
        with patch.dict(os.environ, env), patch.dict("sys.modules", {"shared_ci.auth": auth}), patch("shared_ci.stores.time.time", side_effect=[100, 639, 640, 1250]):
            api = stores.API("apple", "initial-wrapper-token")
            with patch.object(api.opener, "open", return_value=response) as opening:
                for _ in range(4):
                    api("GET", "/v1/builds/200")
        self.assertEqual([call.args[0].headers["Authorization"] for call in opening.call_args_list],
                         ["Bearer first-token", "Bearer first-token", "Bearer second-token", "Bearer third-token"])
        self.assertEqual([call.kwargs["now"] for call in auth.apple_token.call_args_list], [100, 640, 1250])
        self.assertTrue(all(call.args == (env["ASC_PRIVATE_KEY_PATH"], env["ASC_KEY_ID"], env["ASC_ISSUER_ID"]) for call in auth.apple_token.call_args_list))

    def test_static_apple_token_remains_caller_managed(self):
        api = stores.API("apple", "caller-short-lived-token")
        with patch("shared_ci.stores.time.time", side_effect=AssertionError("No local key signing")):
            self.assertEqual(api.authorization(), "Bearer caller-short-lived-token")

    def test_other_providers_never_use_apple_private_key(self):
        with patch.dict(os.environ, {"ASC_PRIVATE_KEY_PATH": "/temporary/existing-key.p8"}):
            for provider in ("play", "firebase"):
                api = stores.API(provider, "google-token")
                self.assertIsNone(api.apple_key)
                self.assertEqual(api.authorization(), "Bearer google-token")

    def test_apple_refresh_failure_blocks_request(self):
        auth = types.ModuleType("shared_ci.auth")
        auth.apple_token = unittest.mock.Mock(side_effect=PipelineError("Apple token signing failed"))
        with patch.dict(os.environ, {"ASC_PRIVATE_KEY_PATH": "/temporary/existing-key.p8"}), patch.dict("sys.modules", {"shared_ci.auth": auth}):
            api = stores.API("apple", "old-token")
            with patch.object(api.opener, "open") as opening, self.assertRaisesRegex(PipelineError, "signing failed"):
                api("GET", "/v1/builds/200")
            opening.assert_not_called()


if __name__ == "__main__":
    unittest.main()
