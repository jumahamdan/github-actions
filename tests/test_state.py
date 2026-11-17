import copy
import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci import state, stores
from shared_ci.common import PipelineError, atomic_json, fingerprint, load_json, sha256_file
from test_stores import TRUST, PlayAPI, fixture


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "first"
        self.root.mkdir()
        self.config, self.candidate, _ = fixture(self.root)
        self.provider = PlayAPI(self.candidate["artifacts"]["app.aab"]["sha256"])
        self.env = patch.dict(os.environ, dict(TRUST, GITHUB_REPOSITORY="example/app", GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="2", GH_STATE_TOKEN="synthetic-state-token"), clear=True)
        self.env.start()
        self.selection = {"schema": 1, "operation": "restore", "repository": "example/app", "run_id": "42", "run_attempt": 1,
                          "run_head_sha": "a" * 40,
                          "artifact_name": "store-state-attempt-1", "expected_workflow": ".github/workflows/store.yml", "producer_job": "Store / Execute",
                          "allowed_branches": ["staging"], "allowed_events": ["workflow_dispatch"], "allowed_conclusions": ["success", "failure", "cancelled"], "token_env": "GH_STATE_TOKEN"}
        self.metadata = {"id": 42, "run_attempt": 1, "status": "completed", "conclusion": "failure", "head_sha": "a" * 40,
                         "path": ".github/workflows/store.yml@staging", "head_repository": {"full_name": "example/app"}, "head_branch": "staging", "event": "workflow_dispatch",
                         "run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:30:00Z"}
        self.job = {"id": 123, "run_id": 42, "name": "Store / Execute", "status": "completed", "conclusion": "failure",
                    "head_sha": "a" * 40, "head_branch": "staging", "started_at": "2026-01-01T00:01:00Z", "completed_at": "2026-01-01T00:29:00Z"}
        self.item = {"id": 99, "name": "store-state-attempt-1", "expired": False, "created_at": "2026-01-01T00:28:00Z",
                     "workflow_run": {"id": 42, "head_sha": "a" * 40}}
        self.calls = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def prepare(self, *, completed=False):
        if not completed:
            self.provider.commit_errors = [PipelineError("lost Play response")]
        with patch("shared_ci.stores.verify_aab", return_value=(self.root / "app.aab", self.provider.digest)):
            if completed:
                stores.execute(self.config, self.root, api=self.provider)
            else:
                with self.assertRaises(PipelineError):
                    stores.execute(self.config, self.root, api=self.provider)
        self.envelope = state.snapshot(self.config, self.root)
        self.new_root = self.base / "rerun"
        shutil.copytree(self.root, self.new_root, ignore=shutil.ignore_patterns("state.json"))
        self.archive = self.base / "state.zip"
        self.write_archive()

    def write_archive(self, extra=None):
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("operation-state.json", json.dumps(self.envelope))
            for name, content in (extra or {}).items():
                z.writestr(name, content)
        self.item.update(digest="sha256:" + sha256_file(self.archive), size_in_bytes=self.archive.stat().st_size)

    def request(self, path, token):
        self.calls.append(path)
        self.assertEqual(token, "synthetic-state-token")
        if "/artifacts?" in path:
            return {"artifacts": [self.item]}
        if "/jobs?" in path:
            return {"jobs": [self.job]}
        self.assertTrue(path.endswith("/attempts/1"))
        return self.metadata

    def downloader(self, path, token, output, max_bytes):
        self.assertEqual(path, "/repos/example/app/actions/artifacts/99/zip")
        shutil.copyfile(self.archive, output)

    def restore(self, config=None, request=None):
        return state.restore(self.selection, self.new_root, store_config=config or self.config, request=request or self.request, downloader=self.downloader)

    def test_failed_attempt_restores_uncertain_commit_and_refuses_republish(self):
        self.prepare()
        result = self.restore()
        self.assertEqual(result["phase"], "commit_started")
        self.assertEqual((self.new_root / "state.json").read_bytes(), (self.root / "state.json").read_bytes())
        self.assertEqual(result["provenance"]["producer_conclusion"], "failure")
        transport = unittest.mock.Mock()
        with self.assertRaisesRegex(PipelineError, "uncertain"):
            stores.execute(self.config, self.new_root, api=transport)
        transport.assert_not_called()

    def previous_selection(self):
        self.selection.update(run_attempt="previous", artifact_name="stores-run-{run_id}-attempt-{run_attempt}")
        self.selection.pop("run_head_sha")

    def test_previous_selector_first_attempt_does_not_fetch_or_create_state(self):
        self.previous_selection()
        request, download = unittest.mock.Mock(), unittest.mock.Mock()
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40, "GH_STATE_TOKEN": ""}):
            result = state.restore(self.selection, self.root, store_config=self.config, request=request, downloader=download)
        self.assertFalse(result["restored"])
        self.assertEqual(result["reason"], "first_run_attempt")
        self.assertNotIn("phase", result)
        self.assertFalse((self.root / "state.json").exists())
        request.assert_not_called()
        download.assert_not_called()

    def test_previous_selector_attempt_two_restores_one_and_still_refuses_uncertain_commit(self):
        self.prepare()
        self.previous_selection()
        self.item["name"] = "stores-run-42-attempt-1"
        with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}):
            result = self.restore()
        self.assertTrue(result["restored"])
        self.assertEqual(result["provenance"]["run_attempt"], 1)
        transport = unittest.mock.Mock()
        with self.assertRaisesRegex(PipelineError, "uncertain"):
            stores.execute(self.config, self.new_root, api=transport)
        transport.assert_not_called()

    def test_previous_selector_attempt_three_fetches_exact_attempt_two(self):
        self.prepare()
        self.previous_selection()
        self.metadata["run_attempt"] = 2
        self.item["name"] = "stores-run-42-attempt-2"
        def request(path, token):
            self.calls.append(path)
            if "/artifacts?" in path: return {"artifacts": [self.item]}
            if "/jobs?" in path:
                self.assertIn("/attempts/2/jobs?", path)
                return {"jobs": [self.job]}
            self.assertTrue(path.endswith("/attempts/2"))
            return self.metadata
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "3", "GITHUB_SHA": "a" * 40}):
            self.assertEqual(self.restore(request=request)["provenance"]["run_attempt"], 2)
        self.assertFalse(any("/attempts/1" in path for path in self.calls))

    def test_previous_selector_refuses_foreign_run_bad_attempt_head_and_unknown_placeholders(self):
        self.previous_selection()
        original = copy.deepcopy(self.selection)
        changes = ({"run_id": "41"}, {"repository": "foreign/app"}, {"run_head_sha": "f" * 40},
                   {"artifact_name": "store-{unknown}"}, {"artifact_name": "store-{run_id.__class__}"},
                   {"artifact_name": "store-{run_attempt:04d}"})
        for change in changes:
            with self.subTest(change=change), patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40}):
                with self.assertRaises(PipelineError):
                    state.restore({**original, **change}, self.root, store_config=self.config, request=self.request)
        for attempt in ("0", "-1", "", "not-a-number"):
            with self.subTest(attempt=attempt), patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": attempt, "GITHUB_SHA": "a" * 40}):
                with self.assertRaisesRegex(PipelineError, "positive attempt"):
                    state.restore(original, self.root, store_config=self.config, request=self.request)
        self.assertFalse(self.calls)

    def test_artifact_placeholders_are_rejected_for_explicit_attempts(self):
        self.prepare()
        self.selection["artifact_name"] = "stores-run-{run_id}-attempt-{run_attempt}"
        with self.assertRaisesRegex(PipelineError, "Invalid state artifact"):
            self.restore()
        self.assertFalse(self.calls)

    def test_previous_missing_or_stale_state_is_never_a_fresh_operation(self):
        self.prepare()
        self.previous_selection()
        self.item["name"] = "stores-run-42-attempt-1"
        self.item["created_at"] = "2025-12-31T23:00:00Z"
        with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}), self.assertRaisesRegex(PipelineError, "selected producer attempt"):
            self.restore()
        self.item["expired"] = True
        with patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}), self.assertRaisesRegex(PipelineError, "selected producer attempt"):
            self.restore()
        self.assertFalse((self.new_root / "state.json").exists())

    def test_completed_pending_state_resumes_without_duplicate_commit(self):
        self.prepare(completed=True)
        self.metadata["conclusion"] = "success"
        self.job["conclusion"] = "success"
        self.restore()
        transport = unittest.mock.Mock()
        receipt = stores.execute(self.config, self.new_root, api=transport)
        self.assertEqual(receipt["status"], "pending")
        transport.assert_not_called()

    def test_cancelled_prior_attempt_is_explicitly_allowed(self):
        self.prepare()
        self.metadata["conclusion"] = "cancelled"
        self.job["conclusion"] = "cancelled"
        self.assertEqual(self.restore()["phase"], "commit_started")

    def test_promotion_run_head_can_differ_from_original_candidate_source(self):
        self.prepare()
        self.selection["run_head_sha"] = "b" * 40
        self.metadata["head_sha"] = "b" * 40
        self.job["head_sha"] = "b" * 40
        self.item["workflow_run"]["head_sha"] = "b" * 40
        result = self.restore()
        self.assertEqual(result["provenance"]["run_head_sha"], "b" * 40)
        self.assertEqual(result["candidate_identity"], self.candidate["identity"])
        self.assertEqual(self.envelope["source_sha"], "a" * 40)

    def test_wrong_selected_prior_run_head_refused(self):
        self.prepare()
        self.selection["run_head_sha"] = "b" * 40
        with self.assertRaisesRegex(PipelineError, "untrusted source"):
            self.restore()

    def test_failed_conclusion_requires_explicit_allowlist(self):
        self.prepare()
        self.selection["allowed_conclusions"] = ["success"]
        with self.assertRaisesRegex(PipelineError, "outcome"):
            self.restore()

    def test_wrong_run_branch_workflow_source_and_fork_refused(self):
        self.prepare()
        baseline = copy.deepcopy(self.metadata)
        for change in ({"head_branch": "feature/other"}, {"head_sha": "f" * 40}, {"path": ".github/workflows/untrusted.yml"},
                       {"head_repository": {"full_name": "fork/app"}}, {"event": "pull_request_target"}, {"run_attempt": 2}, {"status": "in_progress"}):
            self.metadata = dict(baseline, **change)
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "untrusted"):
                self.restore()
        self.assertFalse((self.new_root / "state.json").exists())

    def test_current_or_future_attempt_refused(self):
        self.prepare()
        for attempt in (2, 3):
            self.selection["run_attempt"] = attempt
            with self.assertRaisesRegex(PipelineError, "current or a future"):
                self.restore()
        self.assertFalse(self.calls)

    def test_state_from_foreign_repository_is_never_accepted(self):
        self.prepare()
        self.selection["repository"] = "example/other"
        with self.assertRaisesRegex(PipelineError, "current repository"):
            self.restore()

    def test_running_missing_or_duplicate_job_refused(self):
        self.prepare()
        for jobs in ([], [self.job, self.job], [dict(self.job, status="in_progress")]):
            def request(path, token):
                return {"jobs": jobs} if "/jobs?" in path else self.request(path, token)
            with self.subTest(jobs=jobs), self.assertRaisesRegex(PipelineError, "producer job"):
                self.restore(request=request)

    def test_duplicate_state_artifacts_refused(self):
        self.prepare()
        def request(path, token):
            return {"artifacts": [self.item, dict(self.item, id=100)]} if "/artifacts?" in path else self.request(path, token)
        with self.assertRaisesRegex(PipelineError, "Exactly one state artifact"):
            self.restore(request=request)

    def test_producer_job_branch_must_equal_selected_run_branch(self):
        self.prepare()
        self.selection["allowed_branches"] = ["staging", "main"]
        self.job["head_branch"] = "main"
        with self.assertRaisesRegex(PipelineError, "producer job"):
            self.restore()

    def test_malformed_current_attempt_refused(self):
        self.prepare()
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "invalid"}), self.assertRaisesRegex(PipelineError, "current or a future"):
            self.restore()

    def test_stale_artifact_from_other_attempt_refused(self):
        self.prepare()
        self.item["created_at"] = "2025-12-31T23:00:00Z"
        with self.assertRaisesRegex(PipelineError, "selected producer attempt"):
            self.restore()

    def test_wrong_archive_digest_refused(self):
        self.prepare()
        self.item["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(PipelineError, "digest"):
            self.restore()

    def test_exact_archive_file_allowlist_refuses_extra_source(self):
        self.prepare()
        self.write_archive({"src/app.py": "untrusted source"})
        with self.assertRaisesRegex(PipelineError, "only operation-state"):
            self.restore()
        self.assertFalse((self.new_root / "src").exists())

    def test_wrong_candidate_or_operation_fingerprint_refused(self):
        self.prepare()
        for changed in (dict(self.config, expected_identity="f" * 64), dict(self.config, operation="play-promote")):
            with self.subTest(config=changed["operation"]), self.assertRaisesRegex(PipelineError, "another operation"):
                self.restore(config=changed)

    def test_payload_digest_detects_state_edit(self):
        self.prepare()
        self.envelope["state"]["phase"] = "track_updated"
        self.write_archive()
        with self.assertRaisesRegex(PipelineError, "payload digest"):
            self.restore()

    def test_existing_different_state_is_never_replaced(self):
        self.prepare()
        existing = dict(self.envelope["state"], phase="uploaded")
        atomic_json(self.new_root / "state.json", existing)
        before = (self.new_root / "state.json").read_bytes()
        with self.assertRaisesRegex(PipelineError, "conflicts"):
            self.restore()
        self.assertEqual((self.new_root / "state.json").read_bytes(), before)

    def test_existing_identical_state_is_idempotent(self):
        self.prepare()
        self.restore()
        self.assertEqual(self.restore()["phase"], "commit_started")

    def test_state_destination_symlink_refused(self):
        self.prepare()
        (self.new_root / "state.json").symlink_to(self.root / "state.json")
        with self.assertRaisesRegex(PipelineError, "symbolic"):
            self.restore()

    def test_snapshot_rejects_unknown_fields_and_missing_state(self):
        with self.assertRaisesRegex(PipelineError, "No durable"):
            state.snapshot(self.config, self.root)
        self.prepare()
        invalid = dict(self.envelope["state"], auth_token="do-not-retain")
        atomic_json(self.root / "state.json", invalid)
        with self.assertRaisesRegex(PipelineError, "unknown"):
            state.snapshot(self.config, self.root)

    def test_optional_receipt_must_match_selected_candidate(self):
        self.prepare()
        self.write_archive({"receipt.json": json.dumps({"status": "pending", "operation": "play-upload", "candidate_identity": "f" * 64})})
        with self.assertRaisesRegex(PipelineError, "Optional store receipt"):
            self.restore()


if __name__ == "__main__":
    unittest.main()
