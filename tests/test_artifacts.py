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

from shared_ci import artifacts
from shared_ci.common import PipelineError, atomic_json, fingerprint, sha256_file
from shared_ci.evidence import IDENTITY_FIELDS
from test_stores import fixture


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        _, self.candidate, self.inspected = fixture(self.root)
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
        self.destination = self.base / "bundle"
        self.package_config = {"schema": 1, "operation": "package", "manifest": "candidate.json", "expected_identity": self.candidate["identity"], "expected_source_sha": self.sha, "output_dir_env": "TEST_BUNDLE_OUTPUT"}
        self.env = patch.dict(os.environ, {"TEST_BUNDLE_OUTPUT": str(self.destination), "ARTIFACT_TOKEN": "synthetic-token"})
        self.env.start()
        self.fetch_config = {"schema": 1, "operation": "fetch", "repository": "example/synthetic", "run_id": "42", "run_attempt": 2,
                             "artifact_name": "release-candidate", "expected_workflow": ".github/workflows/build.yml", "expected_source_sha": self.sha,
                             "expected_identity": self.candidate["identity"], "token_env": "ARTIFACT_TOKEN", "allowed_events": ["push", "workflow_dispatch"]}
        self.metadata = {"status": "completed", "conclusion": "success", "head_sha": self.sha, "path": ".github/workflows/build.yml@main",
                         "head_repository": {"full_name": "example/synthetic"}, "run_attempt": 2, "event": "push",
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
                   {"run_attempt": 1}, {"event": "pull_request_target"}, {"conclusion": "failure"}]
        for change in changes:
            old = copy.deepcopy(self.metadata)
            self.metadata.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(PipelineError, "Producer run"):
                self.fetch()
            self.metadata = old
        self.assertFalse((self.target / "app.aab").exists())

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
