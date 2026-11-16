import base64
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci import apple_bridge
from shared_ci.common import PipelineError, atomic_json, fingerprint, sha256_file
from shared_ci.evidence import IDENTITY_FIELDS
from test_stores import fixture, TRUST


class FakeS3:
    def __init__(self):
        self.calls, self.head, self.bytes = [], None, None

    def call(self, service, action, args=(), missing=False):
        self.calls.append((service, action, list(args), missing))
        if action == "head-object":
            return copy.deepcopy(self.head)
        if action == "put-object":
            path = Path(args[args.index("--body") + 1])
            self.bytes = path.read_bytes()
            self.head = {"VersionId": "version-1", "ContentLength": len(self.bytes), "ServerSideEncryption": "aws:kms",
                         "SSEKMSKeyId": args[args.index("--ssekms-key-id") + 1], "ChecksumSHA256": args[args.index("--checksum-sha256") + 1],
                         "ChecksumType": "FULL_OBJECT", "Metadata": json.loads(args[args.index("--metadata") + 1])}
            return {"VersionId": "version-1"}
        if action == "get-object":
            Path(args[-1]).write_bytes(self.bytes)
            return {"VersionId": self.head["VersionId"]}
        raise AssertionError(action)


class AppleBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.archive = self.base / "App.xcarchive"
        self.archive.mkdir()
        files = {"Products/Applications/App.app/App": b"synthetic-binary", "dSYMs/App.app.dSYM/Contents/Resources/DWARF/App": b"synthetic-symbols"}
        _, candidate, inspection = fixture(self.archive, "apple", files, {"artifact_root": "$CI_ARCHIVE_PATH", "build_id": "cloud-run-1"})
        (self.source / "input.txt").write_text("synthetic build inputs")
        self.git(self.source, "init", "-q")
        self.git(self.source, "add", "input.txt")
        self.git(self.source, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        self.sha = self.git(self.source, "rev-parse", "HEAD")
        self.tree = self.git(self.source, "rev-parse", "HEAD^{tree}")
        inspection.update(source_sha=self.sha)
        inspection["assurance"].update(source_sha=self.sha, source_tree=self.tree)
        atomic_json(self.archive / ".shared-ci-evidence/inspection.json", inspection)
        candidate.update(source_sha=self.sha, source_tree=self.tree,
                         checks={".shared-ci-evidence/inspection.json": sha256_file(self.archive / ".shared-ci-evidence/inspection.json")},
                         check_requirements={".shared-ci-evidence/inspection.json": {"kind": "mobile", "config": "input.txt"}})
        candidate["identity"] = fingerprint({key: candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.archive / ".shared-ci-evidence/candidate.json", candidate)
        self.config = {"schema": 1, "operation": "apple-export", "manifest": ".shared-ci-evidence/candidate.json", "inspection": ".shared-ci-evidence/inspection.json",
                       "expected_identity": candidate["identity"], "expected_source_sha": self.sha, "allowed_refs": ["refs/heads/staging"],
                       "cloud": {"workflow_id": "workflow-1", "product_id": "product-1", "build_run_id": "cloud-run-1", "build_number": "123"},
                       "s3": {"bucket": "synthetic-private-builds", "prefix": "cloud-evidence", "region": "us-east-1", "expected_account": "123456789012",
                              "expected_role_arn": "arn:aws:iam::123456789012:role/CloudEvidence", "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/synthetic-key",
                              "credentials": {"AWS_ACCESS_KEY_ID": "TEMP_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY": "TEMP_SECRET_KEY", "AWS_SESSION_TOKEN": "TEMP_SESSION_TOKEN"},
                              "tool": {"prefix": ["aws"], "version": "synthetic-aws 1.0"}}}
        self.env = patch.dict(os.environ, dict(TRUST, CI_XCODE_CLOUD="TRUE", CI_XCODEBUILD_ACTION="archive", CI_XCODEBUILD_EXIT_CODE="0", CI_START_CONDITION="push",
                           CI_GIT_REF="refs/heads/staging", CI_COMMIT=self.sha, CI_BUILD_ID="cloud-run-1", CI_WORKFLOW_ID="workflow-1", CI_BUILD_NUMBER="123",
                           CI_ARCHIVE_PATH=str(self.archive), CI_PRIMARY_REPOSITORY_PATH=str(self.source), ASC_JWT="synthetic-asc-token"), clear=True)
        self.env.start()
        self.s3 = FakeS3()
        self.provider = {"data": {"id": "cloud-run-1", "attributes": {"executionProgress": "COMPLETE", "completionStatus": "SUCCEEDED", "isPullRequestBuild": False,
                                  "startReason": "GIT_REF_CHANGE", "sourceCommit": {"commitSha": self.sha}, "number": 123},
                                  "relationships": {"workflow": {"data": {"id": "workflow-1"}}, "product": {"data": {"id": "product-1"}}}}}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def git(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

    def export(self):
        with patch("shared_ci.apple_bridge.sys.platform", "darwin"):
            return apple_bridge.execute(self.config, self.archive, s3_factory=lambda *_: self.s3)

    def importer(self):
        config = copy.deepcopy(self.config)
        config.update(operation="apple-import", asc_token_env="ASC_JWT")
        config.pop("manifest")
        return apple_bridge.execute(config, self.source, api=lambda *args: self.provider, s3_factory=lambda *_: self.s3)

    def test_private_conditional_export_then_authenticated_import(self):
        exported = self.export()
        self.assertFalse(exported["asc_run_verified"])
        put = next(call for call in self.s3.calls if call[1] == "put-object")
        self.assertIn("--if-none-match", put[2])
        self.assertIn("--checksum-sha256", put[2])
        imported = self.importer()
        self.assertTrue(imported["asc_run_verified"])
        self.assertEqual(imported["object"], exported["object"])
        self.assertTrue((self.source / ".shared-ci-evidence/candidate.json").is_file())
        get = next(call for call in self.s3.calls if call[1] == "get-object")
        self.assertIn("--version-id", get[2])
        self.assertIn("version-1", get[2])

    def test_identical_export_idempotent_no_second_write(self):
        first = self.export()
        self.assertEqual(self.export(), first)
        self.assertEqual(sum(call[1] == "put-object" for call in self.s3.calls), 1)

    def test_existing_different_checksum_refuses_overwrite(self):
        self.export()
        self.s3.head["ChecksumSHA256"] = base64.b64encode(b"x" * 32).decode()
        with self.assertRaisesRegex(PipelineError, "checksum differs"):
            self.export()
        self.assertEqual(sum(call[1] == "put-object" for call in self.s3.calls), 1)

    def test_failed_or_foreign_cloud_run_blocks_s3_download(self):
        self.export()
        baseline = copy.deepcopy(self.provider)
        for change in ({"completionStatus": "FAILED"}, {"executionProgress": "RUNNING"}, {"isPullRequestBuild": True}, {"sourceCommit": {"commitSha": "f" * 40}}, {"number": 999}):
            self.provider = copy.deepcopy(baseline)
            self.provider["data"]["attributes"].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "successful authorized"):
                self.importer()
        self.assertFalse(any(call[1] == "get-object" for call in self.s3.calls))

    def test_object_metadata_wrong_candidate_blocks(self):
        self.export()
        self.s3.head["Metadata"]["candidate-identity"] = "f" * 64
        with self.assertRaisesRegex(PipelineError, "metadata differs"):
            self.importer()

    def test_unversioned_or_unencrypted_object_blocks(self):
        self.export()
        baseline = copy.deepcopy(self.s3.head)
        for change in ({"VersionId": "null"}, {"ServerSideEncryption": "AES256"}, {"SSEKMSKeyId": "another-key"}):
            self.s3.head = dict(baseline, **change)
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "KMS-encrypted"):
                self.importer()

    def test_cloud_export_rejects_linux_pr_and_failed_archive(self):
        with patch("shared_ci.apple_bridge.sys.platform", "linux"), self.assertRaises(PipelineError):
            apple_bridge.execute(self.config, self.archive, s3_factory=lambda *_: self.s3)
        for env in ({"CI_START_CONDITION": "pr_open"}, {"CI_XCODEBUILD_EXIT_CODE": "65"}, {"CI_WORKFLOW_ID": "wrong"}):
            with patch.dict(os.environ, env), self.assertRaises(PipelineError):
                self.export()
        self.assertFalse(self.s3.calls)

    def test_aws_client_requires_temporary_role_private_versioned_bucket(self):
        calls = []
        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            if "--version" in argv:
                result = "synthetic-aws 1.0"
            elif "get-caller-identity" in argv:
                result = json.dumps({"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/CloudEvidence/session"})
            elif "get-bucket-versioning" in argv:
                result = '{"Status":"Enabled"}'
            else:
                result = json.dumps({"PublicAccessBlockConfiguration": {key: True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}})
            return subprocess.CompletedProcess(argv, 0, result, "")
        credentials = {"TEMP_ACCESS_KEY": "synthetic-access", "TEMP_SECRET_KEY": "synthetic-secret", "TEMP_SESSION_TOKEN": "synthetic-session"}
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, credentials), patch("shared_ci.apple_bridge.run", side_effect=command), patch("shared_ci.monitoring.run", side_effect=command):
            apple_bridge.S3(self.config["s3"], work)
        self.assertEqual(calls[1][1]["extra_env"]["AWS_SESSION_TOKEN"], "synthetic-session")
        self.assertNotIn("synthetic-secret", " ".join(calls[1][0]))
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, {"TEMP_ACCESS_KEY": "synthetic-access", "TEMP_SECRET_KEY": "synthetic-secret"}), patch("shared_ci.monitoring.run", side_effect=command), self.assertRaises(PipelineError):
            apple_bridge.S3(self.config["s3"], work)


if __name__ == "__main__":
    unittest.main()
