import copy
import hashlib
import io
import json
import os
import unittest
import zipfile
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.evidence import AuthenticatedReceipt, fetch_receipt


class ReceiptOriginTests(unittest.TestCase):
    def setUp(self):
        self.head = "b" * 40
        self.merge = "c" * 40
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("receipt.json", json.dumps({"status": "passed"}))
        self.blob = archive.getvalue()
        self.metadata = {"status": "in_progress", "conclusion": None, "head_sha": self.head, "head_branch": "develop",
                         "path": ".github/workflows/ci.yml", "head_repository": {"full_name": "owner/example"},
                         "run_attempt": 2, "event": "pull_request", "run_started_at": "2026-01-01T00:00:00Z",
                         "updated_at": "2026-01-01T00:01:00Z"}
        self.job = {"id": 88, "run_id": 32, "name": "quality / run", "status": "completed", "conclusion": "success",
                    "started_at": "2026-01-01T00:02:00Z", "completed_at": "2026-01-01T00:06:00Z"}
        self.item = {"id": 11, "name": "checks", "expired": False, "created_at": "2026-01-01T00:05:00Z",
                     "digest": "sha256:" + hashlib.sha256(self.blob).hexdigest(),
                     "workflow_run": {"id": 32, "head_sha": self.head}}
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/example",
                                          "GITHUB_RUN_ID": "32", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": self.merge})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, path, token, binary=False):
        if binary:
            return self.blob
        if "/jobs?" in path:
            return {"jobs": [self.job]}
        if "/artifacts?" in path:
            return {"artifacts": [self.item]}
        return self.metadata

    def fetch(self, **kwargs):
        options = {"producer_job": "quality / run", "run_attempt": 2, "allowed_branches": ["develop"], **kwargs}
        return fetch_receipt("owner/example", "32", "checks", self.head, ".github/workflows/ci.yml", "synthetic",
                             request=self.request, **options)

    def test_same_run_pr_uses_completed_producer_and_keeps_distinct_head_and_merge(self):
        result = self.fetch()
        self.assertIsInstance(result, AuthenticatedReceipt)
        self.assertTrue(result.validate_integrity())
        self.assertEqual(result["provenance"]["source_sha"], self.head)
        self.assertEqual(result["provenance"]["event"], "pull_request")
        self.assertEqual(result["provenance"]["head_branch"], "develop")
        self.assertEqual(result["provenance"]["run_attempt"], 2)

    def test_failed_wrong_producer_branch_attempt_or_artifact_window_are_refused(self):
        original = copy.deepcopy(self.job)
        for key, value in (("conclusion", "failure"), ("status", "in_progress"), ("name", "other"), ("run_id", 999)):
            with self.subTest(key=key):
                self.job = {**original, key: value}
                with self.assertRaises(PipelineError):
                    self.fetch()
        self.job = original
        for change in ({"allowed_branches": ["main"]}, {"allowed_branches": []}, {"run_attempt": 1}, {"producer_job": None},
                       {"allowed_events": ["push", "workflow_dispatch"]}):
            with self.subTest(change=change), self.assertRaises(PipelineError):
                self.fetch(**change)
        self.item["created_at"] = "2026-01-01T00:01:00Z"
        with self.assertRaises(PipelineError):
            self.fetch()

    def test_current_run_identity_is_required_and_legacy_api_cannot_accept_in_progress(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}), self.assertRaises(PipelineError):
            self.fetch()
        with self.assertRaises(PipelineError):
            fetch_receipt("owner/example", "32", "checks", self.head, ".github/workflows/ci.yml", "synthetic", request=self.request)

    def test_completed_run_still_requires_explicit_attempt_and_branch_policy_when_selected(self):
        self.metadata.update(status="completed", conclusion="success", updated_at="2026-01-01T00:07:00Z")
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "99"}):
            result = self.fetch(producer_job=None)
        self.assertEqual(result["receipt"]["status"], "passed")
        self.metadata["head_branch"] = "feature/untrusted"
        with self.assertRaises(PipelineError):
            self.fetch(producer_job=None)

    def test_earlier_attempt_receipt_keeps_selected_attempt_provenance(self):
        previous = {**self.metadata, "id": 32, "run_attempt": 1, "status": "completed", "conclusion": "failure",
                    "updated_at": "2026-01-01T00:07:00Z"}
        self.metadata.update(id=32, run_started_at="2026-01-01T01:00:00Z", updated_at="2026-01-01T01:01:00Z")
        self.job.update(run_attempt=1, head_sha=self.head)
        seen = []
        def request(path, token, binary=False):
            seen.append(path)
            if path == "/repos/owner/example/actions/runs/32/attempts/1":
                return previous
            return self.request(path, token, binary)
        result = fetch_receipt("owner/example", "32", "checks", self.head, ".github/workflows/ci.yml", "synthetic",
                               request=request, producer_job="quality / run", run_attempt=1,
                               allowed_branches=["develop"], allowed_events=["pull_request"])
        self.assertIn("/repos/owner/example/actions/runs/32/attempts/1", seen)
        self.assertEqual(result["provenance"]["run_attempt"], 1)
        self.assertEqual(result["provenance"]["head_branch"], previous["head_branch"])
        self.assertEqual(result["provenance"]["event"], previous["event"])
        self.assertEqual(result["provenance"]["source_sha"], previous["head_sha"])
        self.assertTrue(result.validate_integrity())


if __name__ == "__main__":
    unittest.main()
