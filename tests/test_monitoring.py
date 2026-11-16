import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci import monitoring
from shared_ci.common import PipelineError, sha256_file
from test_stores import TRUST, fixture

UUID = "11111111-2222-3333-4444-555555555555"
NATIVE = "dSYMs/App.app.dSYM/Contents/Resources/DWARF/App"
BINARY = "Products/Applications/App.app/App"


def archive(entries):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as z:
        for name, value in entries.items():
            z.writestr(name, value)
    return content.getvalue()


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.env = patch.dict(os.environ, dict(TRUST, SENTRY_TOKEN="synthetic-sentry-secret"), clear=True)
        self.env.start()
        self.native()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def configure(self, files, kind, extra=None):
        common, self.candidate, self.inspected = fixture(self.root, kind, files, extra)
        for key in ("play", "token_env", "state_file"):
            common.pop(key)
        common.update(tool={"prefix": ["fake-cli"], "version": "synthetic-cli 1.0"},
                      sentry={"org": "example", "project": "synthetic", "token_env": "SENTRY_TOKEN", "release": "org.example.app@1.0.0+123", "dist": "123", "environment": "staging"})
        self.config = common

    def native(self):
        files = {BINARY: b"binary-fixture", NATIVE: b"symbols-fixture", "dSYMs/App.app.dSYM/Contents/Info.plist": b"plist-fixture"}
        # Digests in this test receipt stand in for mobile's actual dwarfdump verification.
        import hashlib
        entry = {"binary_path": BINARY, "symbol_path": NATIVE, "binary_sha256": hashlib.sha256(files[BINARY]).hexdigest(),
                 "symbol_sha256": hashlib.sha256(files[NATIVE]).hexdigest(), "uuid": UUID, "arch": "arm64"}
        self.configure(files, "apple", {"debug_symbols": [entry]})
        self.config.update(operation="sentry-native", symbols=[NATIVE])

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if "--version" in args:
            output = "synthetic-cli 1.0\n"
        elif args[0] == "readelf":
            output = "Build ID: abcdef0123456789\n"
        elif "check" in args:
            output = json.dumps({"is_usable": True, "variants": [{"debug_id": UUID}]})
        elif args[1:3] == ["proguard", "uuid"]:
            output = UUID
        else:
            output = "upload complete"
        return subprocess.CompletedProcess(args, 0, output, "")

    def execute(self):
        with patch("shared_ci.monitoring.run", side_effect=self.fake_run):
            return monitoring.execute(self.config, self.root)

    def test_native_requires_expected_ids_and_waits(self):
        result = self.execute()
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["symbolication_verified"])
        command, kwargs = self.calls[-1]
        self.assertIn("--require-all", command)
        self.assertIn("--wait", command)
        self.assertIn(UUID, command)
        self.assertNotIn("synthetic-sentry-secret", " ".join(command))
        self.assertEqual(kwargs["extra_env"]["SENTRY_AUTH_TOKEN"], "synthetic-sentry-secret")
        self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_changed_dsym_fails_before_tool(self):
        (self.root / NATIVE).write_bytes(b"wrong")
        with self.assertRaises(PipelineError):
            self.execute()
        self.assertFalse(self.calls)

    def test_missing_native_binary_proof_fails(self):
        self.config["symbols"] = ["dSYMs/App.app.dSYM/Contents/Info.plist"]
        with self.assertRaisesRegex(PipelineError, "matching binary UUID"):
            self.execute()

    def test_tool_version_pin_enforced_before_credentials(self):
        self.config["tool"]["version"] = "different version"
        with self.assertRaisesRegex(PipelineError, "version"):
            self.execute()
        self.assertNotIn("SENTRY_AUTH_TOKEN", self.calls[0][1]["extra_env"])

    def test_upload_failure_is_required_failure(self):
        def failed(args, **kwargs):
            if "upload" in args:
                raise PipelineError("synthetic upload failed")
            return self.fake_run(args, **kwargs)
        with patch("shared_ci.monitoring.run", side_effect=failed):
            with self.assertRaisesRegex(PipelineError, "upload failed"):
                monitoring.execute(self.config, self.root)

    def test_release_identity_mismatch_blocks(self):
        self.config["sentry"]["dist"] = "124"
        with self.assertRaisesRegex(PipelineError, "release/dist"):
            self.execute()

    def test_source_map_debug_id_matches_injected_bundle(self):
        files = {"app.js": ("console.log(1);\n//# debugId=" + UUID).encode(),
                 "app.js.map": json.dumps({"version": 3, "mappings": "AAAA", "sources": ["app.ts"], "debug_id": UUID}).encode()}
        self.configure(files, "web")
        self.config.update(operation="sentry-sourcemaps", source_maps=[{"javascript": "app.js", "map": "app.js.map"}])
        result = self.execute()
        self.assertEqual(len(result["uploaded_artifacts"]), 2)
        self.assertIn("--strict", self.calls[-1][0])
        self.assertIn("--no-rewrite", self.calls[-1][0])

    def test_source_map_missing_injected_id_fails(self):
        files = {"app.js": b"console.log(1);", "app.js.map": json.dumps({"version": 3, "mappings": "AAAA", "sources": ["app.ts"], "debug_id": UUID}).encode()}
        self.configure(files, "web")
        self.config.update(operation="sentry-sourcemaps", source_maps=[{"javascript": "app.js", "map": "app.js.map"}])
        with self.assertRaisesRegex(PipelineError, "does not match"):
            self.execute()

    def test_r8_uses_aab_embedded_mapping_and_uuid(self):
        mapping = b"org.example.LongName -> a:\n"
        aab = archive({"BUNDLE-METADATA/com.android.tools.build.obfuscation/proguard.map": mapping,
                       "base/assets/sentry-debug-meta.properties": ("io.sentry.ProguardUuids=" + UUID).encode()})
        self.configure({"app.aab": aab, "mapping.txt": mapping}, "android")
        self.config.update(operation="sentry-r8", artifact="app.aab", symbols=["mapping.txt"])
        self.execute()
        self.assertEqual(self.calls[-1][0][1:4], ["proguard", "upload", "--require-one"])

    def test_r8_wrong_mapping_fails(self):
        aab = archive({"BUNDLE-METADATA/com.android.tools.build.obfuscation/proguard.map": b"original"})
        self.configure({"app.aab": aab, "mapping.txt": b"different"}, "android")
        self.config.update(operation="sentry-r8", artifact="app.aab", symbols=["mapping.txt"])
        with self.assertRaisesRegex(PipelineError, "does not match"):
            self.execute()

    def dart(self):
        aab = archive({"base/lib/arm64-v8a/libapp.so": b"elf-library-fixture"})
        self.configure({"app.aab": aab, "app.android-arm64.symbols": b"elf-symbol-fixture"}, "android")
        self.config.update(operation="sentry-dart", artifact="app.aab", symbols=["app.android-arm64.symbols"],
                           dart_libraries={"app.android-arm64.symbols": "base/lib/arm64-v8a/libapp.so"})

    def test_dart_compares_packaged_build_id(self):
        self.dart()
        result = self.execute()
        self.assertEqual(result["debug_ids"], [UUID])
        self.assertEqual(sum(c[0][0] == "readelf" for c in self.calls), 2)

    def test_dart_different_build_id_blocks_upload(self):
        self.dart()
        def differing(args, **kwargs):
            if args[0] == "readelf" and args[-1].endswith("symbols"):
                return subprocess.CompletedProcess(args, 0, "Build ID: ffffffff", "")
            return self.fake_run(args, **kwargs)
        with patch("shared_ci.monitoring.run", side_effect=differing):
            with self.assertRaisesRegex(PipelineError, "another packaged binary"):
                monitoring.execute(self.config, self.root)

    def test_crashlytics_apple_script_hash_and_app_binding(self):
        self.native()
        files = {p: (self.root / p).read_bytes() for p in self.candidate["artifacts"]}
        files.update({"GoogleService-Info.plist": plistlib.dumps({"GOOGLE_APP_ID": "1:123456:ios:abcdef", "BUNDLE_ID": "org.example.app"}), "upload-symbols": b"script-fixture"})
        self.configure(files, "apple", {"debug_symbols": self.inspected["debug_symbols"]})
        self.config.pop("sentry")
        self.config.update(operation="crashlytics-apple", symbols=[NATIVE], firebase={"app_id": "1:123456:ios:abcdef", "app_config": "GoogleService-Info.plist", "script": "upload-symbols", "script_sha256": sha256_file(self.root / "upload-symbols")})
        result = self.execute()
        self.assertEqual(result["provider"], "crashlytics")
        self.assertIn("-gsp", self.calls[-1][0])
        self.config["firebase"]["script_sha256"] = "f" * 64
        with self.assertRaisesRegex(PipelineError, "pinned SDK"):
            self.execute()

    def test_crashlytics_dart_adc_is_env_not_command(self):
        aab = archive({"base/lib/arm64-v8a/libapp.so": b"elf-library-fixture"})
        firebase = {"client": [{"client_info": {"mobilesdk_app_id": "1:123456:android:abcdef", "android_client_info": {"package_name": "org.example.app"}}}]}
        self.configure({"app.aab": aab, "app.symbols": b"elf-symbol-fixture", "google-services.json": json.dumps(firebase).encode()}, "android")
        self.config.pop("sentry")
        (self.root / "temporary-adc.json").write_text("synthetic-not-a-real-credential")
        self.config.update(operation="crashlytics-dart", artifact="app.aab", symbols=["app.symbols"], dart_libraries={"app.symbols": "base/lib/arm64-v8a/libapp.so"},
                           firebase={"app_id": "1:123456:android:abcdef", "app_config": "google-services.json", "credentials_env": "ADC_FILE"})
        with patch.dict(os.environ, {"ADC_FILE": str(self.root / "temporary-adc.json")}):
            result = self.execute()
        self.assertEqual(result["provider"], "crashlytics")
        args, kwargs = self.calls[-1]
        self.assertIn("crashlytics:symbols:upload", args)
        self.assertNotIn(str(self.root / "temporary-adc.json"), args)
        self.assertEqual(kwargs["extra_env"]["GOOGLE_APPLICATION_CREDENTIALS"], str(self.root / "temporary-adc.json"))

    def gradle(self):
        mapping = b"org.example.LongName -> a:\n"
        aab = archive({"BUNDLE-METADATA/com.android.tools.build.obfuscation/proguard.map": mapping})
        firebase = {"client": [{"client_info": {"mobilesdk_app_id": "1:123456:android:abcdef", "android_client_info": {"package_name": "org.example.app"}}}]}
        self.configure({"app.aab": aab, "mapping.txt": mapping, "google-services.json": json.dumps(firebase).encode(), "gradlew": b"fixture-wrapper"}, "android")
        self.config.pop("sentry")
        self.config.update(operation="crashlytics-r8", artifact="app.aab", symbols=["mapping.txt"], firebase={
            "app_id": "1:123456:android:abcdef", "app_config": "google-services.json", "gradle": {
                "wrapper": "gradlew", "wrapper_sha256": sha256_file(self.root / "gradlew"), "project": ".",
                "task": ":app:uploadCrashlyticsMappingFileRelease", "exclude_tasks": [":app:minifyReleaseWithR8"],
                "version": "synthetic-cli 1.0", "build_inputs": ["input.txt"], "cache_env": "TEST_GRADLE_CACHE"}})

    def test_crashlytics_gradle_only_upload_task_can_execute(self):
        self.gradle()
        task = ":app:uploadCrashlyticsMappingFileRelease"
        def gradle_run(args, **kwargs):
            if "--version" in args:
                return self.fake_run(args, **kwargs)
            self.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, task + " SKIPPED\n" if "--dry-run" in args else "> Task " + task + "\n", "")
        with patch.dict(os.environ, {"TEST_GRADLE_CACHE": str(self.root)}), patch("shared_ci.monitoring.run", side_effect=gradle_run):
            result = monitoring.execute(self.config, self.root)
        self.assertEqual(result["status"], "passed")
        self.assertIn("--offline", self.calls[-1][0])

    def test_crashlytics_gradle_rebuild_dependency_fails_dry_run(self):
        self.gradle()
        def gradle_run(args, **kwargs):
            if "--dry-run" in args:
                return subprocess.CompletedProcess(args, 0, ":app:assembleRelease SKIPPED\n:app:uploadCrashlyticsMappingFileRelease SKIPPED\n", "")
            return self.fake_run(args, **kwargs)
        with patch.dict(os.environ, {"TEST_GRADLE_CACHE": str(self.root)}), patch("shared_ci.monitoring.run", side_effect=gradle_run):
            with self.assertRaisesRegex(PipelineError, "other tasks"):
                monitoring.execute(self.config, self.root)

    def test_crashlytics_gradle_skipped_upload_fails(self):
        self.gradle()
        def gradle_run(args, **kwargs):
            if "--version" in args:
                return self.fake_run(args, **kwargs)
            task = ":app:uploadCrashlyticsMappingFileRelease"
            return subprocess.CompletedProcess(args, 0, task + " SKIPPED\n" if "--dry-run" in args else "> Task " + task + " NO-SOURCE\n", "")
        with patch.dict(os.environ, {"TEST_GRADLE_CACHE": str(self.root)}), patch("shared_ci.monitoring.run", side_effect=gradle_run):
            with self.assertRaisesRegex(PipelineError, "skipped"):
                monitoring.execute(self.config, self.root)

    def test_deploy_marker_needs_successful_matching_receipt(self):
        self.config["operation"] = "sentry-deploy"
        self.config["sentry"].update(deployment_receipt="input.txt", deployment_receipt_sha256="f" * 64)
        with self.assertRaises(PipelineError):
            self.execute()

    def test_uploaded_file_mutation_fails_postcheck(self):
        def mutating(args, **kwargs):
            result = self.fake_run(args, **kwargs)
            if "upload" in args:
                (self.root / NATIVE).write_bytes(b"mutated-by-uploader")
            return result
        with patch("shared_ci.monitoring.run", side_effect=mutating):
            with self.assertRaisesRegex(PipelineError, "changed"):
                monitoring.execute(self.config, self.root)


if __name__ == "__main__":
    unittest.main()
