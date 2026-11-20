import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci import stores
from shared_ci.common import PipelineError, atomic_json, fingerprint, sha256_file
from shared_ci.evidence import IDENTITY_FIELDS
from test_stores import TRUST, fixture


class FirebaseAPI:
    def __init__(self):
        self.releases, self.calls, self.distribution_error = [], [], None

    def __call__(self, method, path, body=None, upload=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"releases": self.releases}
        if method == "POST" and path.endswith(":distribute"):
            if self.distribution_error:
                raise self.distribution_error
            return {}
        raise AssertionError((method, path))


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "source"
        self.root.mkdir()
        appconfig = {"client": [{"client_info": {"mobilesdk_app_id": "1:123456:android:abcdef", "android_client_info": {"package_name": "org.example.app"}}}]}
        files = {"app.apk": b"synthetic-apk", "symbols.txt": b"synthetic-symbols", "notes.txt": b"Synthetic release notes", "google-services.json": json.dumps(appconfig).encode()}
        self.config, self.candidate, self.inspection = fixture(self.root, "android", files)
        self.inspection["apk_sha256"] = self.candidate["artifacts"]["app.apk"]["sha256"]
        atomic_json(self.root / "inspection.json", self.inspection)
        self.device = {"schema": 1, "status": "passed", "operation": "device-test", "app_sha256": self.inspection["apk_sha256"], "source_revision": "a" * 40,
                       "assurance": dict(self.inspection["assurance"])}
        self.symbols = {"schema": 1, "status": "passed", "source_sha": "a" * 40, "uploaded_artifacts": ["symbols.txt"],
                        "artifact_records": {"symbols.txt": self.candidate["artifacts"]["symbols.txt"]},
                        "assurance": dict(self.inspection["assurance"], kind="monitoring")}
        self.rehash()
        self.config.pop("play")
        self.config.update(operation="firebase-distribution", firebase={"app_id": "1:123456:android:abcdef", "app_config": "google-services.json", "artifact": "app.apk",
                           "credentials_env": "TEST_ADC_FILE", "tool": {"prefix": ["firebase"], "version": "synthetic-cli 1.0"}, "group_aliases": ["internal-testers"],
                           "release_notes": "notes.txt", "device_check": "device.json", "symbol_checks": {"symbols.json": ["symbols.txt"]}})
        self.adc = Path(self.tmp.name) / "adc.json"
        self.adc.write_text("synthetic-not-an-actual-credential")
        self.env = patch.dict(os.environ, dict(TRUST, TEST_ADC_FILE=str(self.adc)), clear=True)
        self.env.start()
        self.api, self.commands = FirebaseAPI(), []
        self.release = {"name": "projects/123456/apps/1:123456:android:abcdef/releases/release1", "displayVersion": "1.0.0", "buildVersion": "123",
                        "releaseNotes": {"text": "Synthetic release notes"}, "binaryDownloadUri": "https://example.invalid/signed-url-not-retained"}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def rehash(self):
        for path, value, kind in [("inspection.json", self.inspection, "mobile"), ("device.json", self.device, "mobile"), ("symbols.json", self.symbols, "monitoring")]:
            atomic_json(self.root / path, value)
            self.candidate["checks"][path] = sha256_file(self.root / path)
            self.candidate["check_requirements"][path] = {"kind": kind, "config": "input.txt"}
        self.candidate["identity"] = fingerprint({key: self.candidate[key] for key in IDENTITY_FIELDS})
        atomic_json(self.root / "candidate.json", self.candidate)
        self.config["expected_identity"] = self.candidate["identity"]

    def command(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, "synthetic-cli 1.0", "")
        self.api.releases = [copy.deepcopy(self.release)]
        return subprocess.CompletedProcess(argv, 0, '{"status":"success"}', "")

    def execute(self):
        with patch("shared_ci.monitoring.run", side_effect=self.command), patch("shared_ci.app_distribution.run", side_effect=self.command):
            return stores.execute(self.config, self.root, api=self.api)

    def test_exact_apk_upload_then_verified_group_distribution(self):
        result = self.execute()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["artifact_sha256"], self.inspection["apk_sha256"])
        self.assertFalse(result["provider_binary_sha256_available"])
        self.assertNotIn("binaryDownloadUri", result)
        argv, kwargs = self.commands[-1]
        self.assertIn("appdistribution:distribute", argv)
        self.assertNotIn("--groups", argv)
        self.assertNotIn(str(self.adc), argv)
        self.assertEqual(kwargs["extra_env"]["GOOGLE_APPLICATION_CREDENTIALS"], str(self.adc))
        self.assertEqual(self.api.calls[-1][2], {"groupAliases": ["internal-testers"]})
        self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_completed_retry_does_not_notify_groups_again(self):
        result = self.execute()
        count = len(self.api.calls)
        self.assertEqual(self.execute(), result)
        self.assertEqual(len(self.api.calls), count)
        self.assertEqual(len(self.commands), 2)

    def test_existing_version_requires_reconciliation_without_upload(self):
        self.api.releases = [self.release]
        with self.assertRaisesRegex(PipelineError, "already has"):
            self.execute()
        self.assertFalse(self.commands)

    def test_mismatched_device_apk_fails_before_provider(self):
        self.device["app_sha256"] = "f" * 64
        self.rehash()
        with self.assertRaisesRegex(PipelineError, "exact APK"):
            self.execute()
        self.assertFalse(self.api.calls)

    def test_mismatched_symbol_bytes_fail_before_provider(self):
        self.symbols["artifact_records"] = {"symbols.txt": {"sha256": "f" * 64, "bytes": 17}}
        self.rehash()
        with self.assertRaisesRegex(PipelineError, "symbols"):
            self.execute()
        self.assertFalse(self.api.calls)

    def test_missing_required_symbol_check_refuses(self):
        self.config["firebase"]["symbol_checks"] = {}
        with self.assertRaisesRegex(PipelineError, "symbol"):
            self.execute()

    def test_malformed_cli_json_blocks_and_retry_does_not_upload_again(self):
        def malformed(argv, **kwargs):
            if "--version" in argv:
                return self.command(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, "not-json", "")
        with patch("shared_ci.monitoring.run", side_effect=self.command), patch("shared_ci.app_distribution.run", side_effect=malformed):
            with self.assertRaisesRegex(PipelineError, "malformed JSON"):
                stores.execute(self.config, self.root, api=self.api)
        with self.assertRaisesRegex(PipelineError, "uncertain"):
            self.execute()

    def test_different_returned_app_blocks_distribution(self):
        self.release["name"] = "projects/999/apps/other/releases/release1"
        with self.assertRaisesRegex(PipelineError, "another app"):
            self.execute()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_notes_mismatch_blocks_distribution(self):
        self.release["releaseNotes"]["text"] = "wrong"
        with self.assertRaisesRegex(PipelineError, "notes differ"):
            self.execute()

    def test_lost_distribution_response_blocks_duplicate_notification(self):
        self.api.distribution_error = PipelineError("connection lost")
        with self.assertRaises(PipelineError):
            self.execute()
        count = len(self.api.calls)
        with self.assertRaisesRegex(PipelineError, "uncertain"):
            self.execute()
        self.assertEqual(len(self.api.calls), count)

    def test_processing_timeout_pending_then_resume_without_upload(self):
        def no_release(argv, **kwargs):
            result = self.command(argv, **kwargs)
            self.api.releases = []
            return result
        with patch("shared_ci.monitoring.run", side_effect=self.command), patch("shared_ci.app_distribution.run", side_effect=no_release), patch("shared_ci.app_distribution.time.monotonic", side_effect=[0, 1000]):
            result = stores.execute(self.config, self.root, api=self.api)
        self.assertEqual(result["status"], "pending")
        count = len(self.commands)
        self.api.releases = [self.release]
        self.assertEqual(self.execute()["status"], "passed")
        self.assertEqual(len(self.commands), count)


if __name__ == "__main__":
    unittest.main()
