import copy
import importlib.util
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import zipfile
from unittest.mock import Mock, patch

import test_reuse
from shared_ci import quality, quality_discovery
from shared_ci.common import PipelineError


SPEC = importlib.util.spec_from_file_location("discovery_wrapper", Path(__file__).resolve().parents[1] / "scripts/run-module.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRAPPER)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = test_reuse.ReuseTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        os.environ["GITHUB_RUN_ID"] = "18"
        self.f.run_head = self.f.source
        self.f.metadata.update(head_sha=self.f.source, pull_requests=[{
            "number": 7, "url": "https://api.github.com/repos/owner/product/pulls/7",
            "base": {"ref": "develop"}, "head": {"sha": self.f.source}}])
        self.f.artifact.update(name="ci-quality-run-17-attempt-1", workflow_run={"id": 17, "head_sha": self.f.source})
        self.config = {"schema": 1, "quality_config": "quality.json", "max_age_seconds": 86400,
                       "source": {"repository": "owner/product", "workflow": ".github/workflows/ci.yml",
                                  "artifact_prefix": "ci-quality", "producer_job": "quality / run",
                                  "allowed_branches": ["develop"], "allowed_events": ["pull_request"]}}
        self.listing = [self.f.metadata]
        self.calls = []

    def request(self, path, token, binary=False):
        self.calls.append(path)
        if "/actions/workflows/" in path:
            return {"workflow_runs": self.listing}
        return self.f.request(path, token, binary)

    def discover(self):
        return quality_discovery.discover(self.config, self.f.root, self.f.quality_path,
                                          request=self.request, library_root=self.f.library)

    def dispatch(self):
        (self.f.root / "discovery.json").write_text(json.dumps(self.config))
        original = quality_discovery.discover
        def lookup(config, root, path):
            return original(config, root, path, request=self.request, library_root=self.f.library)
        with patch.dict(os.environ, {"SHARED_QUALITY_REUSE_CONFIG": "discovery.json"}), \
                patch.object(quality_discovery, "discover", side_effect=lookup):
            return WRAPPER.dispatch("quality", quality, self.f.quality, self.f.root, self.f.quality_path,
                                    self.f.root / "result.json", "", Mock())

    def test_matching_original_is_discovered_without_reexecuting_or_redownloading(self):
        self.f.git("commit", "--allow-empty", "-qm", "promotion")
        result = self.dispatch()
        self.assertEqual(result["reused_from"]["run_id"], "17")
        self.assertEqual(result["created_at"], self.f.receipt["created_at"])
        self.assertEqual((self.f.root / "executions.txt").read_text(), "run\n")
        self.assertEqual(sum(path.endswith("/zip") for path in self.calls), 1)
        self.assertEqual(self.calls[0], "/repos/owner/product/actions/workflows/ci.yml/runs?status=success&per_page=20&page=1")

    def test_missing_or_unavailable_evidence_runs_fresh_quality(self):
        self.listing = []
        result = self.dispatch()
        self.assertNotIn("reused_from", result)
        self.assertEqual((self.f.root / "executions.txt").read_text(), "run\nrun\n")
        with patch.object(self, "request", side_effect=PipelineError("unavailable private details")):
            self.assertIsNone(self.discover())

    def test_raw_transport_and_malformed_authenticated_receipts_run_fresh(self):
        for error in (TimeoutError("private timeout detail"), OSError("private connection detail")):
            with self.subTest(error=type(error).__name__), patch.object(self, "request", side_effect=error):
                result = self.dispatch()
                self.assertEqual(result["status"], "passed")
                self.assertNotIn("reused_from", result)
        body = io.BytesIO()
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr("receipt.json", json.dumps(["not a receipt object"]))
        self.f.blob = body.getvalue()
        self.f.artifact["digest"] = "sha256:" + hashlib.sha256(self.f.blob).hexdigest()
        result = self.dispatch()
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("reused_from", result)
        self.assertEqual((self.f.root / "executions.txt").read_text(), "run\n" * 4)

    def test_expired_changed_and_chained_receipts_require_fresh_quality(self):
        original = copy.deepcopy(self.f.receipt)
        mutations = [
            lambda receipt: receipt.update(created_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat()),
            lambda receipt: receipt.update(reused_from={"run_id": "16"}),
            lambda receipt: receipt.update(config_sha256="0" * 64),
            lambda receipt: receipt.update(toolchain=[]),
            lambda receipt: receipt.update(library={"identity": "0" * 64}),
            lambda receipt: receipt.update(inputs={"source.txt": "0" * 64}),
            lambda receipt: receipt["checks"][0].update(status="failed"),
        ]
        for mutation in mutations:
            self.f.receipt = copy.deepcopy(original)
            mutation(self.f.receipt)
            self.f.repack()
            self.assertIsNone(self.discover())
        result = self.dispatch()
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("reused_from", result)

    def test_changed_candidate_inputs_run_fresh(self):
        (self.f.root / "source.txt").write_text("new product input\n")
        self.f.git("add", "source.txt")
        self.f.git("commit", "-qm", "changed product")
        result = self.dispatch()
        self.assertNotIn("reused_from", result)
        self.assertEqual((self.f.root / "executions.txt").read_text(), "run\nrun\n")

    def test_fresh_quality_failure_still_blocks_promotion(self):
        self.listing = []
        (self.f.root / "test_runner.py").write_text("raise SystemExit(1)\n")
        with self.assertRaises(PipelineError):
            self.dispatch()

    def test_repository_workflow_base_fork_and_unrelated_source_are_not_eligible(self):
        original = copy.deepcopy(self.f.metadata)
        mutations = [
            lambda item: item.update(head_repository={"full_name": "other/product"}),
            lambda item: item.update(path=".github/workflows/other.yml"),
            lambda item: item.update(path=None),
            lambda item: item.update(conclusion="failure"),
            lambda item: item["pull_requests"][0]["base"].update(ref="other-release"),
            lambda item: item["pull_requests"][0].update(url="https://api.github.com/repos/other/product/pulls/7"),
            lambda item: item["pull_requests"][0]["head"].update(sha="f" * 40),
            lambda item: item.update(pull_requests=[]),
            lambda item: item.update(head_sha="f" * 40, pull_requests=[{
                "number": 7, "url": "https://api.github.com/repos/owner/product/pulls/7",
                "base": {"ref": "develop"}, "head": {"sha": "f" * 40}}]),
        ]
        for mutation in mutations:
            self.f.metadata.clear()
            self.f.metadata.update(copy.deepcopy(original))
            mutation(self.f.metadata)
            self.assertIsNone(self.discover())
        self.assertFalse(any(path.endswith("/zip") for path in self.calls))

    def test_artifact_namespace_and_digest_cannot_cross_product_evidence(self):
        self.f.artifact["name"] = "other-product-run-17-attempt-1"
        self.assertIsNone(self.discover())
        self.f.artifact["name"] = "ci-quality-run-17-attempt-1"
        self.f.artifact["digest"] = "sha256:" + "0" * 64
        self.assertIsNone(self.discover())

    def test_exact_push_source_must_be_in_candidate_ancestry(self):
        self.config["source"]["allowed_events"] = ["push"]
        self.f.metadata.update(event="push", head_branch="develop", pull_requests=[])
        self.assertIsNotNone(self.discover())
        self.f.metadata["head_sha"] = "f" * 40
        self.assertIsNone(self.discover())

    def test_current_run_and_excess_listing_are_not_discovered(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "17"}):
            self.assertIsNone(self.discover())
        self.config["max_runs"] = 1
        self.listing = [self.f.metadata, self.f.metadata]
        self.assertIsNone(self.discover())

    def test_invalid_policy_fails_before_fresh_or_network(self):
        for change in [{"schema": True}, {"max_runs": 0}, {"max_age_seconds": 0}, {"quality_config": "source.txt"}]:
            with patch.dict(self.config, change), self.assertRaises(PipelineError):
                self.dispatch()
        self.assertFalse(self.calls)

    def test_legacy_quality_dispatch_stays_fresh_without_discovery(self):
        with patch.dict(os.environ, {"SHARED_QUALITY_REUSE_CONFIG": ""}), \
                patch.object(quality_discovery, "discover", side_effect=AssertionError("unexpected discovery")):
            result = WRAPPER.dispatch("quality", quality, self.f.quality, self.f.root, self.f.quality_path,
                                      self.f.root / "result.json", "", Mock())
        self.assertNotIn("reused_from", result)


if __name__ == "__main__":
    unittest.main()
