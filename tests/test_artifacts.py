import copy
import datetime as dt
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci import artifacts, stores
from shared_ci.common import PipelineError, atomic_json, fingerprint, load_json, sha256_file
from shared_ci.evidence import IDENTITY_FIELDS
from shared_ci.gate import evaluate
from test_stores import fixture, PlayAPI, TRUST


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.store_config, self.candidate, self.inspected = fixture(self.root)
        self.git(self.root, "init", "-q")
        self.git(self.root, "add", "input.txt")
        self.git(self.root, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        self.sha = self.git(self.root, "rev-parse", "HEAD")
        self.tree = self.git(self.root, "rev-parse", "HEAD^{tree}")
        self.inspected.update(source_sha=self.sha)
        self.inspected["assurance"].update(source_sha=self.sha, source_tree=self.tree)
        atomic_json(self.root / "inspection.json", self.inspected)
        self.candidate.update(source_sha=self.sha, source_tree=self.tree)
        self.candidate["checks"]["inspection.json"] = sha256_file(self.root / "inspection.json")
        self.rehash()
        self.store_config.update(expected_identity=self.candidate["identity"], expected_source_sha=self.sha)
        self.destination = self.base / "bundle"
        self.package_config = {"schema": 1, "operation": "package", "manifest": "candidate.json", "expected_identity": self.candidate["identity"], "expected_source_sha": self.sha, "output_dir_env": "TEST_BUNDLE_OUTPUT"}
        self.env = patch.dict(os.environ, {"TEST_BUNDLE_OUTPUT": str(self.destination), "ARTIFACT_TOKEN": "synthetic-token"})
        self.env.start()
        self.fetch_config = {"schema": 1, "operation": "fetch", "repository": "example/synthetic", "run_id": "42", "run_attempt": 2,
                             "artifact_name": "release-candidate", "expected_workflow": ".github/workflows/build.yml", "expected_source_sha": self.sha,
                             "expected_identity": self.candidate["identity"], "token_env": "ARTIFACT_TOKEN", "allowed_events": ["push", "workflow_dispatch"], "allowed_branches": ["staging"]}
        self.metadata = {"status": "completed", "conclusion": "success", "head_sha": self.sha, "path": ".github/workflows/build.yml@main",
                         "head_repository": {"full_name": "example/synthetic"}, "run_attempt": 2, "event": "push", "head_branch": "staging",
                         "run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T01:00:00Z"}
        self.item = {"id": 99, "name": "release-candidate", "expired": False, "created_at": "2026-01-01T00:30:00Z",
                     "workflow_run": {"id": 42, "head_sha": self.sha}}
        self.api_calls = []
        self.target = self.base / "target"
        self.git(self.base, "clone", "--quiet", "--no-hardlinks", str(self.root), str(self.target))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

    def rehash(self):
        self.candidate["identity"] = fingerprint({key: self.candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.root / "candidate.json", self.candidate)

    def bundle(self):
        result = artifacts.package(self.package_config, self.root)
        self.archive = self.base / "artifact.zip"
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(self.destination.rglob("*")):
                if path.is_file():
                    z.write(path, str(path.relative_to(self.destination)))
        self.update_digest()
        return result

    def update_digest(self):
        self.item.update(digest="sha256:" + sha256_file(self.archive), size_in_bytes=self.archive.stat().st_size)

    def request(self, path, token):
        self.api_calls.append((path, token))
        self.assertEqual(token, "synthetic-token")
        return {"artifacts": [self.item]} if "/artifacts?" in path else self.metadata

    def download(self, path, token, destination, max_bytes):
        self.assertEqual(path, "/repos/example/synthetic/actions/artifacts/99/zip")
        shutil.copyfile(self.archive, destination)

    def fetch(self):
        return artifacts.fetch(self.fetch_config, self.target, request=self.request, downloader=self.download)

    def add_zip(self, name, content=b"extra"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive, "a") as z:
                z.writestr(name, content)
        self.update_digest()

    def test_roundtrip_verifies_origin_and_preserves_tracked_source(self):
        result = self.bundle()
        self.assertEqual(result["status"], "passed")
        self.assertFalse((self.destination / ".git").exists())
        before = (self.target / "input.txt").stat().st_mtime_ns
        imported = self.fetch()
        self.assertEqual(imported["status"], "passed")
        self.assertEqual(imported["provenance"]["artifact_id"], 99)
        self.assertEqual(before, (self.target / "input.txt").stat().st_mtime_ns)
        self.assertEqual((self.root / "app.aab").read_bytes(), (self.target / "app.aab").read_bytes())
        self.assertFalse((self.target / "bundle.json").exists())
        self.assertEqual(self.git(self.target, "status", "--porcelain", "--untracked-files=no"), "")
        self.fetch()  # identical artifacts are safe to import again

    def test_rendered_android_staging_submission_allows_cross_run_candidate_but_not_release(self):
        from test_caller_templates import rendered

        staging, staging_files = rendered("android-staging")
        release, release_files = rendered("mobile-release", platform="android")
        staging_policy = json.loads(staging_files[staging["jobs"]["required"]["with"]["config"]])
        release_policy = json.loads(release_files[release["jobs"]["required"]["with"]["config"]])
        api = PlayAPI(sha256_file(self.root / "app.aab"))
        with patch.dict(os.environ, TRUST), patch("shared_ci.stores.verify_aab",
                                                  return_value=(self.root / "app.aab", api.digest)):
            receipt = stores.execute(self.store_config, self.root, api=api)
            self.assertEqual((receipt["status"], receipt["state"]), ("pending", "submitted"))
            self.assertIs(receipt["availability_verified"], False)
            results = {name: {"result": "success", "outputs": {"status": "passed"}}
                       for name in staging["jobs"] if name != "required"}
            results["distribute"]["outputs"] = {key: receipt[key] for key in ("status", "state")}
            gate = evaluate(staging_policy, results)
        self.assertEqual(gate["checks"]["distribute"], "submitted")
        self.assertEqual(gate["status"], "passed")
        self.assertIs(receipt["availability_verified"], False)

        # Successful upstream jobs plus this successful gate make a successful producer
        # attempt. Keep the production import's whole-run trust check intact.
        self.metadata["conclusion"] = "success" if gate["status"] == "passed" else "failure"
        producer = release["jobs"]["distribute"]["with"]
        self.fetch_config["expected_workflow"] = producer["artifact-workflow"]
        self.fetch_config["allowed_branches"] = json.loads(producer["artifact-allowed-branches"])
        self.metadata.update(path=producer["artifact-workflow"] + "@staging", event="workflow_dispatch")
        self.fetch_config["artifact_name"] = self.item["name"] = "android-verify-candidate-run-42-attempt-2"
        self.bundle()
        with patch.dict(os.environ, {**TRUST, "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "84",
                                     "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "example/synthetic"}):
            imported = self.fetch()
            self.assertEqual(imported["status"], "passed")
            self.assertEqual(imported["provenance"]["run_id"], "42")
            self.assertEqual((self.root / "app.aab").read_bytes(), (self.target / "app.aab").read_bytes())
            with self.assertRaisesRegex(PipelineError, "pending"):
                evaluate(release_policy, {"distribute": results["distribute"]})
            # A failed producer attempt still cannot be imported from another run.
            self.metadata["conclusion"] = "failure"
            with self.assertRaisesRegex(PipelineError, "Producer run"):
                self.fetch()

    def test_package_fresh_outside_workspace_only(self):
        with patch.dict(os.environ, {"TEST_BUNDLE_OUTPUT": str(self.root / "out")}):
            with self.assertRaisesRegex(PipelineError, "outside"):
                artifacts.package(self.package_config, self.root)
        self.bundle()
        with self.assertRaisesRegex(PipelineError, "fresh"):
            artifacts.package(self.package_config, self.root)

    def test_package_rejects_changed_inputs(self):
        (self.root / "input.txt").write_text("changed")
        with self.assertRaisesRegex(PipelineError, "changed"):
            artifacts.package(self.package_config, self.root)

    def test_secret_names_and_content_rejected(self):
        for name in (".env", "x/.env.production", "private.p8", "key.properties", "vault/config.json", ".ssh/id_rsa", "..\\private.txt", "a/../b", "a:b"):
            with self.subTest(name=name), self.assertRaises(PipelineError):
                artifacts.safe_name(name)
        path = self.base / "secret.txt"
        path.write_text('prefix {"private_key": "synthetic"}')
        with self.assertRaisesRegex(PipelineError, "Credential-like"):
            artifacts.scan_file(path)

    def test_symlink_source_rejected(self):
        (self.root / "app.aab").unlink()
        (self.base / "outside.aab").write_bytes(b"fake-fixture-aab")
        (self.root / "app.aab").symlink_to(self.base / "outside.aab")
        with self.assertRaises(PipelineError):
            artifacts.package(self.package_config, self.root)

    def test_wrong_run_source_workflow_fork_attempt_event_fail(self):
        self.bundle()
        changes = [{"head_sha": "f" * 40}, {"path": ".github/workflows/untrusted.yml"}, {"head_repository": {"full_name": "fork/other"}},
                   {"run_attempt": 1}, {"event": "pull_request_target"}, {"conclusion": "failure"}, {"head_branch": "feature/unapproved"}]
        for change in changes:
            old = copy.deepcopy(self.metadata)
            self.metadata.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "Producer run"):
                self.fetch()
            self.metadata = old
        self.assertFalse((self.target / "app.aab").exists())

    def test_branch_allowlist_is_explicit_and_has_no_wildcards(self):
        self.bundle()
        for branches in ([], ["*"], ["refs/heads/staging"], ["staging", "staging"]):
            self.fetch_config["allowed_branches"] = branches
            with self.subTest(branches=branches), self.assertRaisesRegex(PipelineError, "branch"):
                self.fetch()

    def test_stale_previous_attempt_artifact_rejected(self):
        self.bundle()
        self.item["created_at"] = "2025-12-31T23:59:59Z"
        with self.assertRaisesRegex(PipelineError, "stale"):
            self.fetch()

    def test_missing_or_duplicate_artifact_rejected(self):
        self.bundle()
        for items in ([], [self.item, self.item]):
            def request(path, token):
                return {"artifacts": items} if "/artifacts?" in path else self.metadata
            with self.assertRaisesRegex(PipelineError, "Exactly one"):
                artifacts.fetch(self.fetch_config, self.target, request=request, downloader=self.download)

    def test_download_digest_mismatch_rejected(self):
        self.bundle()
        self.item["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(PipelineError, "digest differs"):
            self.fetch()

    def test_unexpected_file_rejected_even_in_authenticated_archive(self):
        self.bundle()
        self.add_zip("extra.txt")
        with self.assertRaisesRegex(PipelineError, "inventory"):
            self.fetch()

    def test_zip_traversal_and_symlink_rejected(self):
        self.bundle()
        self.add_zip("../outside")
        with self.assertRaises(PipelineError):
            self.fetch()
        self.archive.unlink()
        with zipfile.ZipFile(self.archive, "w") as z:
            for path in self.destination.rglob("*"):
                if path.is_file():
                    z.write(path, str(path.relative_to(self.destination)))
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
        self.update_digest()
        with self.assertRaisesRegex(PipelineError, "links"):
            self.fetch()

    def test_duplicate_zip_names_rejected(self):
        self.bundle()
        self.add_zip("app.aab")
        with self.assertRaisesRegex(PipelineError, "duplicate"):
            self.fetch()

    def test_existing_conflict_does_not_partially_import(self):
        self.bundle()
        (self.target / "app.aab").write_bytes(b"existing-different")
        with self.assertRaisesRegex(PipelineError, "conflicts"):
            self.fetch()
        self.assertFalse((self.target / "candidate.json").exists())
        self.assertEqual((self.target / "app.aab").read_bytes(), b"existing-different")

    def test_existing_symlink_rejected_even_identical_content(self):
        self.bundle()
        (self.target / "app.aab").symlink_to(self.root / "app.aab")
        with self.assertRaisesRegex(PipelineError, "symbolic"):
            self.fetch()

    def test_candidate_identity_selection_mismatch_rejected(self):
        self.bundle()
        self.fetch_config["expected_identity"] = "f" * 64
        with self.assertRaisesRegex(PipelineError, "trusted selection"):
            self.fetch()

    def test_source_tree_mismatch_refuses_import(self):
        self.bundle()
        (self.target / "other.txt").write_text("different tree")
        self.git(self.target, "add", "other.txt")
        self.git(self.target, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "changed")
        with self.assertRaisesRegex(PipelineError, "source tree differs"):
            self.fetch()
        self.assertFalse((self.target / "candidate.json").exists())

    def test_package_file_and_byte_limits(self):
        self.package_config["max_files"] = 1
        with self.assertRaisesRegex(PipelineError, "count"):
            artifacts.package(self.package_config, self.root)

    def same_run(self):
        self.bundle()
        self.metadata.update(status="in_progress", conclusion=None, updated_at="2026-01-01T00:00:01Z")
        self.fetch_config["producer_job"] = "Build Android"
        self.jobs = [{"id": 123, "run_id": 42, "name": "Build Android", "status": "completed", "conclusion": "success",
                      "started_at": "2026-01-01T00:01:00Z", "completed_at": "2026-01-01T00:40:00Z"}]
        self.current_env = {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
                            "GITHUB_REPOSITORY": "example/synthetic", "GITHUB_SHA": self.sha}

    def same_run_request(self, path, token):
        return {"jobs": self.jobs} if "/jobs?" in path else self.request(path, token)

    def test_same_running_workflow_accepts_completed_producer(self):
        self.same_run()
        with patch.dict(os.environ, self.current_env):
            result = artifacts.fetch(self.fetch_config, self.target, request=self.same_run_request, downloader=self.download)
        self.assertEqual(result["status"], "passed")

    def earlier_attempt(self):
        self.same_run()
        self.fetch_config["run_attempt"] = 1
        self.previous = dict(self.metadata, id=42, run_attempt=1, status="completed", conclusion="failure",
                             updated_at="2026-01-01T01:00:00Z")
        self.metadata.update(run_started_at="2026-01-02T00:00:00Z", updated_at="2026-01-02T00:00:01Z")
        self.jobs[0].update(head_sha=self.sha, head_branch="staging")

    def earlier_request(self, path, token):
        self.api_calls.append((path, token))
        if path == "/repos/example/synthetic/actions/runs/42/attempts/1":
            return self.previous
        if "/jobs?" in path:
            self.assertIn("/attempts/1/jobs?", path)
            return {"jobs": self.jobs}
        return self.request(path, token)

    def fetch_earlier(self):
        return artifacts.fetch(self.fetch_config, self.target, request=self.earlier_request, downloader=self.download)

    def test_failed_jobs_rerun_imports_successful_build_from_earlier_failed_attempt(self):
        self.earlier_attempt()
        with patch.dict(os.environ, self.current_env):
            result = self.fetch_earlier()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["provenance"]["run_attempt"], 1)
        self.assertEqual(load_json(self.target / "candidate.json")["identity"], self.candidate["identity"])
        self.assertEqual((self.target / "app.aab").read_bytes(), (self.root / "app.aab").read_bytes())
        paths = [path for path, _ in self.api_calls]
        self.assertIn("/repos/example/synthetic/actions/runs/42/attempts/1", paths)
        self.assertTrue(any("/attempts/1/jobs?" in path for path in paths))
        self.assertFalse(any("/attempts/2/jobs?" in path for path in paths))

    def test_cross_run_can_select_successful_earlier_attempt_while_latest_reruns(self):
        self.earlier_attempt()
        self.previous["conclusion"] = "success"
        with patch.dict(os.environ, {**self.current_env, "GITHUB_RUN_ID": "999"}):
            result = self.fetch_earlier()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["provenance"]["run_attempt"], 1)

    def test_earlier_attempt_rejects_incomplete_failed_missing_or_ambiguous_producer(self):
        self.earlier_attempt()
        original = copy.deepcopy(self.jobs)
        variants = [[], original + original, [dict(original[0], status="in_progress", conclusion=None)],
                    [dict(original[0], conclusion="failure")], [dict(original[0], conclusion="cancelled")],
                    [dict(original[0], run_id=999)], [dict(original[0], head_sha="f" * 40)],
                    [dict(original[0], head_branch="main")]]
        with patch.dict(os.environ, self.current_env):
            for jobs in variants:
                self.jobs = jobs
                with self.subTest(jobs=jobs), self.assertRaisesRegex(PipelineError, "producer job"):
                    self.fetch_earlier()
        self.assertFalse((self.target / "candidate.json").exists())

    def test_earlier_attempt_requires_exact_source_branch_event_workflow_run_and_attempt(self):
        self.earlier_attempt()
        original = copy.deepcopy(self.previous)
        variants = [{"id": 99}, {"head_sha": "f" * 40}, {"head_repository": {"full_name": "fork/other"}},
                    {"head_branch": "feature/unapproved"}, {"event": "pull_request_target"},
                    {"path": ".github/workflows/other.yml"}, {"run_attempt": 2},
                    {"status": "in_progress", "conclusion": None}, {"conclusion": "unknown"}]
        with patch.dict(os.environ, self.current_env):
            for change in variants:
                self.previous = {**original, **change}
                with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "Producer run"):
                    self.fetch_earlier()
        self.assertFalse((self.target / "candidate.json").exists())

    def test_earlier_attempt_requires_producer_and_original_job_artifact_window(self):
        self.earlier_attempt()
        with patch.dict(os.environ, self.current_env):
            self.fetch_config.pop("producer_job")
            with self.assertRaisesRegex(PipelineError, "explicit completed producer"):
                self.fetch_earlier()
            self.fetch_config["producer_job"] = "Build Android"
            for created in ("2026-01-01T00:00:59Z", "2026-01-01T00:40:01Z", "2026-01-02T00:00:00Z"):
                self.item["created_at"] = created
                with self.subTest(created=created), self.assertRaisesRegex(PipelineError, "stale"):
                    self.fetch_earlier()
        self.assertFalse((self.target / "candidate.json").exists())

    def test_foreign_earlier_failed_attempt_cannot_borrow_current_run_exception(self):
        self.earlier_attempt()
        with patch.dict(os.environ, {**self.current_env, "GITHUB_RUN_ID": "999"}), self.assertRaisesRegex(PipelineError, "Producer run"):
            self.fetch_earlier()
        self.assertFalse((self.target / "candidate.json").exists())

    def test_current_run_cannot_select_a_future_attempt(self):
        self.same_run()
        with patch.dict(os.environ, {**self.current_env, "GITHUB_RUN_ATTEMPT": "1"}), self.assertRaisesRegex(PipelineError, "current or earlier"):
            artifacts.fetch(self.fetch_config, self.target, request=self.same_run_request, downloader=self.download)

    def test_same_running_pr_authenticates_api_head_independent_of_merge_sha(self):
        self.same_run()
        self.metadata["event"] = "pull_request"
        # The retained candidate and its real Git checkout use merge SHA A;
        # REST run and artifact metadata identify PR head B.
        head = "f" * 40
        self.metadata["head_sha"] = head
        self.item["workflow_run"]["head_sha"] = head
        self.fetch_config["run_head_sha"] = head
        self.fetch_config["allowed_events"] = ["pull_request"]
        with patch.dict(os.environ, self.current_env):
            result = artifacts.fetch(self.fetch_config, self.target, request=self.same_run_request, downloader=self.download)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["source_sha"], self.sha)
        self.assertEqual(result["provenance"]["run_head_sha"], head)
        self.assertEqual(load_json(self.target / "candidate.json")["source_sha"], self.sha)

    def test_pr_merge_candidate_cannot_substitute_run_head_for_bundle_source(self):
        self.same_run()
        head = "f" * 40
        self.metadata.update(event="pull_request", head_sha=head)
        self.item["workflow_run"]["head_sha"] = head
        self.fetch_config.update(run_head_sha=head, expected_source_sha=head, allowed_events=["pull_request"])
        with patch.dict(os.environ,self.current_env),self.assertRaisesRegex(PipelineError,"Bundle index differs"):
            artifacts.fetch(self.fetch_config,self.target,request=self.same_run_request,downloader=self.download)
        self.assertFalse((self.target / "candidate.json").exists())

    def test_transport_checks_historical_security_without_refreshing_candidate(self):
        policy = {"threshold": "high", "max_age_hours": 24, "exceptions": []}
        atomic_json(self.root / "security-policy.json", policy)
        created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
        security = {"schema": 1, "status": "passed", "policy_identity": fingerprint(policy), "max_age_hours": 24,
                    "checked_at": (created - dt.timedelta(hours=1)).isoformat(),
                    "assurance": {"kind": "security", "source_sha": self.sha, "source_tree": self.tree,
                                  "config_sha256": sha256_file(self.root / "security-policy.json")}}
        atomic_json(self.root / "security.json", security)
        self.candidate["inputs"]["security-policy.json"] = {"sha256": sha256_file(self.root / "security-policy.json"), "bytes": (self.root / "security-policy.json").stat().st_size}
        self.candidate["checks"]["security.json"] = sha256_file(self.root / "security.json")
        self.candidate["check_requirements"]["security.json"] = {"kind": "security", "config": "security-policy.json"}
        self.candidate["created_at"] = created.isoformat()
        self.rehash()
        self.package_config["expected_identity"] = self.fetch_config["expected_identity"] = self.candidate["identity"]
        self.bundle()
        result = self.fetch()
        self.assertEqual(result["candidate_identity"], self.candidate["identity"])
        with self.assertRaisesRegex(PipelineError, "stale"):
            artifacts.verify_checks(self.candidate, self.target)

    def test_same_running_workflow_rejects_running_failed_or_duplicate_producer(self):
        self.same_run()
        baseline = copy.deepcopy(self.jobs)
        variants = [[], baseline + baseline, [dict(baseline[0], status="in_progress", conclusion=None)], [dict(baseline[0], conclusion="failure")]]
        with patch.dict(os.environ, self.current_env):
            for jobs in variants:
                self.jobs = jobs
                with self.subTest(jobs=jobs), self.assertRaisesRegex(PipelineError, "producer job"):
                    artifacts.fetch(self.fetch_config, self.target, request=self.same_run_request, downloader=self.download)
        self.assertFalse((self.target / "app.aab").exists())

    def test_same_running_workflow_requires_explicit_producer(self):
        self.same_run()
        self.fetch_config.pop("producer_job")
        with patch.dict(os.environ, self.current_env), self.assertRaisesRegex(PipelineError, "explicit"):
            artifacts.fetch(self.fetch_config, self.target, request=self.same_run_request, downloader=self.download)

    def test_foreign_in_progress_run_is_never_accepted(self):
        self.same_run()
        self.current_env["GITHUB_RUN_ID"] = "999"
        with patch.dict(os.environ, self.current_env), self.assertRaisesRegex(PipelineError, "Producer run"):
            artifacts.fetch(self.fetch_config, self.target, request=self.same_run_request, downloader=self.download)

    def test_stream_download_does_not_forward_token_to_signed_url(self):
        import urllib.error
        from unittest.mock import MagicMock
        response = MagicMock()
        response.read.side_effect = [b"zip", b""]
        error = urllib.error.HTTPError("https://api.github.com/anything", 302, "redirect", {"Location": "https://example.blob.core.windows.net/artifact?signature=synthetic"}, None)
        opener = MagicMock()
        opener.open.side_effect = [error, response]
        with patch("shared_ci.artifacts.urllib.request.build_opener", return_value=opener):
            artifacts.download("/repos/example/synthetic/actions/artifacts/99/zip", "synthetic-token", self.base / "download.zip", 1024)
        first, second = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(first.headers["Authorization"], "Bearer synthetic-token")
        self.assertNotIn("Authorization", second.headers)
        self.assertNotIn("synthetic-token", second.full_url)


if __name__ == "__main__":
    unittest.main()
