import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from shared_ci.assurance import library_identity, stamp
from shared_ci.common import PipelineError, sha256_file
from shared_ci.evidence import create, verify
from shared_ci.quality import execute as run_quality
from shared_ci.reuse import execute, main


class ReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "product"
        self.root.mkdir()
        self.library = Path(temp.name) / "library"
        (self.library / "shared_ci").mkdir(parents=True)
        (self.library / "shared_ci/quality.py").write_text("# fixture quality implementation\n")
        (self.root / "source.txt").write_text("the tested source\n")
        (self.root / "deps.lock").write_text("dependency@1\n")
        (self.root / "test_runner.py").write_text(
            "from pathlib import Path\nassert 2 + 2 == 4\n"
            "Path('executions.txt').write_text(Path('executions.txt').read_text() + 'run\\n' if Path('executions.txt').exists() else 'run\\n')\n"
            "Path('report.xml').write_text('<testsuite><testcase name=\"arithmetic\"/></testsuite>')\n"
            "Path('coverage.info').write_text('TN:unit\\nSF:source.txt\\nDA:1,1\\nend_of_record\\n')\n")
        tool = self.root / "bash"
        tool.write_text(f"#!{sys.executable}\nprint('bash version 5.2.0')\n")
        tool.chmod(0o755)
        self.quality = {"version": 1, "profile": "shell", "toolchain": [{"command": [str(tool), "--version"], "version": "5.2.0"}],
                        "inputs": ["source.txt", "test_runner.py"], "lockfiles": ["deps.lock"],
                        "checks": [{"name": "tests", "kind": "test", "command": [sys.executable, "test_runner.py"],
                                    "test_report": {"path": "report.xml", "format": "junit"}}],
                        "required_checks": ["tests"], "coverage": ["coverage.info"]}
        self.quality_path = self.root / "quality.json"
        self.quality_path.write_text(json.dumps(self.quality))
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", "source.txt", "test_runner.py", "quality.json", "deps.lock", "bash")
        self.git("commit", "-qm", "tested source")
        self.source = self.git("rev-parse", "HEAD")
        self.run_head = "b" * 40
        self.env = patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/product", "GITHUB_RUN_ID": "17", "GITHUB_SHA": self.source,
                                          "GH_TOKEN": "synthetic-read-only-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.receipt = run_quality(self.quality, self.root, config_hash=sha256_file(self.quality_path))
        self.receipt["library"] = {"identity": library_identity(self.library), "module_sha256": "f" * 64, "reference": "c" * 40}
        stamp(self.receipt, "quality", self.quality_path, self.root)
        self.config = {"schema": 1, "quality_config": "quality.json", "max_age_seconds": 86400,
                       "source": {"repository": "owner/product", "workflow": ".github/workflows/ci.yml", "run_id": "17",
                                  "run_head_sha": self.run_head, "source_sha": self.source, "artifact": "quality-checks"}}
        self.metadata = {"status": "completed", "conclusion": "success", "head_sha": self.run_head,
                         "path": ".github/workflows/ci.yml", "head_repository": {"full_name": "owner/product"}, "event": "pull_request"}
        self.artifact = {"id": 9, "name": "quality-checks", "expired": False}
        self.requests = []
        self.repack()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def repack(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("receipt.json", json.dumps(self.receipt))
            for path, digest in self.receipt.get("coverage", {}).items():
                archive.writestr("coverage/" + digest, (self.root / path).read_bytes())
        self.blob = data.getvalue()
        self.artifact["digest"] = "sha256:" + hashlib.sha256(self.blob).hexdigest()

    def request(self, path, token, binary=False):
        self.assertEqual(token, "synthetic-read-only-token")
        self.requests.append(path)
        if binary:
            return self.blob
        if "/artifacts?" in path:
            return {"artifacts": [self.artifact]}
        return self.metadata

    def reuse(self, config=None):
        return execute(config or self.config, self.root, request=self.request, library_root=self.library)

    def test_real_quality_run_reused_on_new_commit_without_executing_checks(self):
        old_tree = self.receipt["assurance"]["source_tree"]
        (self.root / "README.md").write_text("only unrelated documentation changed\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "promotion documentation")
        result = self.reuse()
        self.assertEqual((self.root / "executions.txt").read_text(), "run\n")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["provenance"], self.receipt["provenance"])
        self.assertEqual(result["created_at"], self.receipt["created_at"])
        self.assertEqual(result["assurance"]["source_sha"], self.git("rev-parse", "HEAD"))
        self.assertNotEqual(result["assurance"]["source_tree"], old_tree)
        self.assertEqual(result["reused_from"]["source_tree"], old_tree)
        self.assertEqual(result["reused_from"]["run_head_sha"], self.run_head)
        self.assertEqual(result["reused_from"]["source_sha"], self.source)
        self.assertEqual(result["coverage"], {})
        self.assertEqual(result["reused_from"]["coverage"], self.receipt["coverage"])
        (self.root / "reused.json").write_text(json.dumps(result))
        (self.root / "app.bin").write_bytes(b"built candidate")
        candidate = create({"schema": 1, "product": "fixture", "version": "1.0.0", "build": "1", "library_sha": "a" * 40,
                            "inputs": ["quality.json", "source.txt"], "artifacts": ["app.bin"], "toolchain": {"compiler": "1.0.0"},
                            "checks": [{"path": "reused.json", "kind": "quality", "config": "quality.json"}]}, self.root)
        self.assertEqual(verify(candidate, self.root)["status"], "passed")

    def test_same_tree_new_merge_sha_can_reuse(self):
        self.git("commit", "--allow-empty", "-qm", "merge identity")
        result = self.reuse()
        self.assertNotEqual(result["assurance"]["source_sha"], self.source)
        self.assertEqual(result["assurance"]["source_tree"], self.receipt["assurance"]["source_tree"])

    def test_changed_selected_input_or_lock_fails_even_after_commit(self):
        for path in ("source.txt", "test_runner.py", "deps.lock"):
            with self.subTest(path=path):
                original = (self.root / path).read_text()
                (self.root / path).write_text(original + "changed\n")
                self.git("add", path)
                self.git("commit", "-qm", "changed input")
                with self.assertRaisesRegex(PipelineError, "Fresh quality"):
                    self.reuse()
                (self.root / path).write_text(original)
                self.git("add", path)
                self.git("commit", "-qm", "restore input")

    def test_new_file_in_selected_directory_invalidates(self):
        folder = self.root / "src"
        folder.mkdir()
        (folder / "first").write_text("one")
        self.quality["inputs"].append("src")
        self.quality_path.write_text(json.dumps(self.quality))
        self.git("add", "src", "quality.json")
        self.git("commit", "-qm", "new selected directory")
        self.receipt = run_quality(self.quality, self.root, config_hash=sha256_file(self.quality_path))
        self.source = self.git("rev-parse", "HEAD")
        self.config["source"]["source_sha"] = self.source
        self.receipt["provenance"]["sha"] = self.source
        self.receipt["library"] = {"identity": library_identity(self.library)}
        stamp(self.receipt, "quality", self.quality_path, self.root)
        self.repack()
        (folder / "second").write_text("two")
        with self.assertRaisesRegex(PipelineError, "file content changed"):
            self.reuse()

    def test_changed_config_including_required_checks_or_tool_version_fails(self):
        for change in ({"required_checks": ["tests", "new"]}, {"inputs": ["source.txt"]},
                       {"toolchain": [{"command": ["bash", "--version"], "version": "9.0.0"}]}):
            with self.subTest(change=change):
                self.quality_path.write_text(json.dumps({**self.quality, **change}))
                with self.assertRaisesRegex(PipelineError, "configuration assurance differs"):
                    self.reuse()
        self.quality_path.write_text(json.dumps(self.quality))

    def test_missing_failed_duplicate_empty_tests_or_tampered_receipt_fail(self):
        changes = [lambda r: r.update(checks=[]),
                   lambda r: r["checks"].append(copy.deepcopy(r["checks"][0])),
                   lambda r: r["checks"][0].update(status="cancelled"),
                   lambda r: r["checks"][0].update(kind="lint"),
                   lambda r: r["checks"][0]["test_summary"].update(executed=0),
                   lambda r: r["checks"][0]["test_summary"].update(executed=True),
                   lambda r: r["checks"][0]["test_summary"].update(failed=1),
                   lambda r: r["checks"][0]["test_summary"].update(skipped=-1),
                   lambda r: r["checks"][0]["test_summary"].update(sha256="invalid"),
                   lambda r: r.update(input_digest="0" * 64),
                   lambda r: r.update(input_paths=["test_runner.py", "source.txt"]),
                   lambda r: r.update(lockfiles={}),
                   lambda r: r.update(toolchain=[]),
                   lambda r: r.update(config_sha256="0" * 64),
                   lambda r: r["assurance"].update(source_sha="f" * 40),
                   lambda r: r["assurance"].update(source_tree="bad"),
                   lambda r: r.update(provenance={"repository": "other/product", "run_id": "17", "sha": self.source}),
                   lambda r: r.update(reused_from={}),
                   lambda r: r.update(library={})]
        original = copy.deepcopy(self.receipt)
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.receipt = copy.deepcopy(original)
                change(self.receipt)
                self.repack()
                with self.assertRaises(PipelineError):
                    self.reuse()

    def test_original_freshness_cannot_be_extended(self):
        for when in (datetime.now(timezone.utc) - timedelta(days=2), datetime.now(timezone.utc) + timedelta(hours=1),
                     datetime.now().replace(tzinfo=None)):
            with self.subTest(when=when):
                self.receipt["created_at"] = when.isoformat()
                self.repack()
                with self.assertRaisesRegex(PipelineError, "timestamp"):
                    self.reuse()

    def test_authenticated_run_metadata_artifact_bytes_and_expiry_fail_closed(self):
        original = copy.deepcopy(self.metadata)
        for key, value in (("conclusion", "failure"), ("status", "in_progress"), ("head_sha", "f" * 40),
                           ("path", ".github/workflows/other.yml"), ("head_repository", {"full_name": "fork/product"}),
                           ("event", "pull_request_target")):
            with self.subTest(key=key):
                self.metadata = {**original, key: value}
                with self.assertRaises(PipelineError):
                    self.reuse()
        self.metadata = original
        self.artifact["expired"] = True
        with self.assertRaises(PipelineError):
            self.reuse()
        self.artifact["expired"] = False
        self.blob += b"tampered"
        with self.assertRaisesRegex(PipelineError, "digest"):
            self.reuse()

    def test_explicit_env_ids_supported_but_missing_ids_never_guessed(self):
        config = copy.deepcopy(self.config)
        config["source"].update(run_id={"env": "QUALITY_RUN_ID"}, run_head_sha={"env": "QUALITY_RUN_HEAD_SHA"},
                                source_sha={"env": "QUALITY_TESTED_SHA"})
        with patch.dict(os.environ, {"QUALITY_RUN_ID": "17", "QUALITY_RUN_HEAD_SHA": self.run_head, "QUALITY_TESTED_SHA": self.source}):
            self.assertEqual(self.reuse(config)["status"], "passed")
        with patch.dict(os.environ, {"QUALITY_RUN_ID": ""}):
            with self.assertRaises(PipelineError):
                self.reuse(config)
        with patch.dict(os.environ, {"GH_TOKEN": ""}):
            with self.assertRaises(PipelineError):
                self.reuse()

    def test_nonreproducible_environment_and_build_credentials_require_fresh_run(self):
        for change in ({"environment_names": ["BUILD_MODE"]}, {"build_credentials": ["SENTRY_AUTH_TOKEN"]}):
            with self.subTest(change=change):
                self.quality_path.write_text(json.dumps({**self.quality, **change}))
                with self.assertRaisesRegex(PipelineError, "environment-dependent"):
                    self.reuse()
        self.assertEqual(self.requests, [])

    def test_dirty_unselected_tracked_file_still_refused(self):
        (self.root / "bash").write_text("modified tool")
        with self.assertRaisesRegex(PipelineError, "modified tracked"):
            self.reuse()

    def test_library_code_workflow_helper_or_tool_lock_change_invalidates(self):
        for path in ("shared_ci/quality.py", "shared_ci/ai_support/helper.py", "scripts/run-module.py",
                     "actions/v2-run/action.yml", ".github/workflows/v2-quality.yml", "tooling/ai/package-lock.json"):
            with self.subTest(path=path):
                file = self.library / path
                file.parent.mkdir(parents=True, exist_ok=True)
                old = file.read_bytes() if file.exists() else None
                file.write_text("changed implementation")
                with self.assertRaisesRegex(PipelineError, "library implementation changed"):
                    self.reuse()
                file.unlink() if old is None else file.write_bytes(old)
        (self.library / "README.md").write_text("documentation only")
        self.assertEqual(self.reuse()["status"], "passed")

    def test_symlink_implementation_and_missing_library_refused(self):
        (self.library / "shared_ci/linked.py").symlink_to(self.root / "test_runner.py")
        with self.assertRaisesRegex(PipelineError, "symlink"):
            library_identity(self.library)
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(PipelineError, "missing"):
                library_identity(empty)

    def test_config_schema_paths_and_bounds(self):
        for key, value in (("schema", True), ("typo", True), ("max_age_seconds", 0), ("max_age_seconds", 604801),
                           ("max_age_seconds", True), ("quality_config", "../outside")):
            with self.subTest(key=key, value=value), self.assertRaises(PipelineError):
                self.reuse({**self.config, key: value})
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "other/product"}):
            with self.assertRaisesRegex(PipelineError, "current product"):
                self.reuse()

    def test_cli_does_not_overwrite_source_or_leave_stale_success(self):
        path = self.root / "reuse.json"
        path.write_text(json.dumps(self.config))
        original = self.quality_path.read_bytes()
        with patch.object(sys, "argv", ["reuse", "--root", str(self.root), "--config", "reuse.json", "--output", "quality.json"]):
            self.assertEqual(main(), 1)
        self.assertEqual(self.quality_path.read_bytes(), original)
        output = self.root / "reused.json"
        output.write_text('{"status":"passed"}')
        with patch.dict(os.environ, {"GH_TOKEN": ""}), patch.object(sys, "argv", ["reuse", "--root", str(self.root), "--config", "reuse.json", "--output", "reused.json"]):
            self.assertEqual(main(), 1)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
