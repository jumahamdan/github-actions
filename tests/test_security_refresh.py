import copy
import datetime as dt
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared_ci.assurance import stamp
from shared_ci.common import PipelineError, fingerprint, sha256_file
from shared_ci.evidence import AuthenticatedReceipt, IDENTITY_FIELDS, create, fetch_receipt, security_refresh_context, verify_checks
from shared_ci.security import SecurityRefresh, authorize_refresh, authorize_step_refresh, require_policy_fresh, scan


class SecurityRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tool = self.root / "semgrep"
        tool.write_text(f"#!{sys.executable}\nimport sys\nprint('1.0.0' if '--version' in sys.argv else '{{\"results\":[],\"errors\":[]}}')\n")
        tool.chmod(0o755)
        rule = self.root / "rules.yml"; rule.write_text("rules: []\n")
        self.policy = {"schema": 1, "threshold": "high", "max_age_hours": 24, "scans": [{
            "id": "fixture", "format": "semgrep", "command": [str(tool), "scan", "--config", "rules.yml", "--json", "."],
            "version_command": [str(tool), "--version"], "version": "1.0.0", "output": "stdout",
            "rules_identity": {"rules.yml": sha256_file(rule)}, "database_identity": "none"}]}
        for name in ("build-policy.json", "promotion-policy.json"):
            (self.root / name).write_text(json.dumps(self.policy))
        (self.root / "source.txt").write_text("immutable tested source")
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", "source.txt", "build-policy.json", "promotion-policy.json", "rules.yml")
        self.git("commit", "-qm", "build source")
        self.original_source = self.git("rev-parse", "HEAD")
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/product",
                                          "GITHUB_RUN_ID": "1", "GITHUB_SHA": self.original_source})
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / "app.bin").write_bytes(b"the original compiled artifact")
        built_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
        class BuildClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return built_at.astimezone(tz) if tz else built_at.replace(tzinfo=None)
        with patch("shared_ci.security.dt.datetime", BuildClock):
            self.build_receipt = scan(self.policy, self.root)
            stamp(self.build_receipt, "security", self.root / "build-policy.json", self.root)
            (self.root / "build-security.json").write_text(json.dumps(self.build_receipt))
            self.candidate = create({"schema": 1, "product": "fixture", "version": "1.0.0", "build": "1", "library_sha": "a" * 40,
                                     "toolchain": {"compiler": "1.0.0"}, "inputs": ["source.txt", "build-policy.json", "rules.yml"],
                                     "artifacts": ["app.bin"], "checks": [{"path": "build-security.json", "kind": "security", "config": "build-policy.json"}]}, self.root)
        (self.root / "candidate.json").write_text(json.dumps(self.candidate))
        self.git("commit", "--allow-empty", "-qm", "promotion merge")
        self.source = self.git("rev-parse", "HEAD")
        os.environ.update(GITHUB_RUN_ID="2", GITHUB_SHA=self.source)
        self.policy_path = self.root / "promotion-policy.json"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def fresh_scan(self):
        receipt = scan(self.policy, self.root, candidate=self.candidate)
        return stamp(receipt, "security", self.policy_path, self.root)

    def fetch(self, receipt):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("receipt.json", json.dumps(receipt))
        blob = buffer.getvalue()
        def request(path, token, binary=False):
            if binary:
                return blob
            if "/artifacts?" in path:
                return {"artifacts": [{"name": "promotion-security", "id": 7, "expired": False,
                                       "digest": "sha256:" + hashlib.sha256(blob).hexdigest()}]}
            return {"status": "completed", "conclusion": "success", "head_sha": self.source, "path": ".github/workflows/promote.yml",
                    "head_repository": {"full_name": "owner/product"}, "event": "push"}
        return fetch_receipt("owner/product", "2", "promotion-security", self.source, ".github/workflows/promote.yml", "synthetic", request=request)

    def authorize(self, receipt=None):
        return authorize_refresh(self.fetch(receipt or self.fresh_scan()), self.policy_path, sha256_file(self.policy_path), self.source)

    def rebind_candidate(self):
        (self.root / "build-security.json").write_text(json.dumps(self.build_receipt))
        self.candidate["checks"]["build-security.json"] = sha256_file(self.root / "build-security.json")
        self.candidate["identity"] = fingerprint({key: self.candidate[key] for key in IDENTITY_FIELDS})

    def test_old_candidate_imports_historically_but_current_gate_stays_closed(self):
        verify_checks(self.candidate, self.root, historical_security=True)
        with self.assertRaisesRegex(PipelineError, "stale"):
            verify_checks(self.candidate, self.root)

    def test_fresh_scan_authorizes_same_candidate_without_changing_artifacts_or_identity(self):
        before = {name: (self.root / name).read_bytes() for name in ("app.bin", "candidate.json", "build-security.json")}
        proof = self.authorize()
        with security_refresh_context({"build-security.json": proof}):
            verify_checks(self.candidate, self.root)
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        self.assertEqual(proof.summary()["candidate_identity"], self.candidate["identity"])
        self.assertEqual(proof.summary()["origin"]["source_sha"], self.source)
        self.assertNotEqual(self.source, self.candidate["source_sha"])
        with self.assertRaisesRegex(PipelineError, "stale"):
            verify_checks(self.candidate, self.root)

    def test_context_resets_after_exception_and_rejects_raw_json_or_unknown_check(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with security_refresh_context({"build-security.json": self.authorize()}):
                raise RuntimeError("fixture")
        with self.assertRaisesRegex(PipelineError, "stale"):
            verify_checks(self.candidate, self.root)
        with self.assertRaises(PipelineError):
            with security_refresh_context({"build-security.json": {"status": "passed"}}):
                pass
        with self.assertRaises(PipelineError):
            verify_checks(self.candidate, self.root, security_refresh={"unknown.json": self.authorize()})
        with self.assertRaises(PipelineError):
            verify_checks(self.candidate, self.root, historical_security=True, security_refresh={"build-security.json": self.authorize()})

    def test_raw_origin_and_modified_authenticated_objects_are_rejected(self):
        verified = self.fetch(self.fresh_scan())
        self.assertIsInstance(verified, AuthenticatedReceipt)
        with self.assertRaisesRegex(PipelineError, "origin"):
            authorize_refresh(dict(verified), self.policy_path, sha256_file(self.policy_path), self.source)
        with self.assertRaises(PipelineError):
            AuthenticatedReceipt(dict(verified))
        verified["receipt"]["checked_at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(PipelineError, "modified"):
            authorize_refresh(verified, self.policy_path, sha256_file(self.policy_path), self.source)
        with self.assertRaises(PipelineError):
            SecurityRefresh({}, self.policy_path, "a" * 64, {})

    def test_local_step_output_requires_trusted_current_context_and_matching_digest(self):
        receipt = self.fresh_scan()
        path = self.root / "fresh.json"
        path.write_text(json.dumps(receipt))
        args = (path, sha256_file(path), self.policy_path, sha256_file(self.policy_path), self.source)
        proof = authorize_step_refresh(*args)
        verify_checks(self.candidate, self.root, security_refresh={"build-security.json": proof})
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), self.assertRaises(PipelineError):
            authorize_step_refresh(*args)
        path.write_text(json.dumps({**receipt, "checked_at": "2000-01-01T00:00:00+00:00"}))
        with self.assertRaisesRegex(PipelineError, "digest"):
            authorize_step_refresh(*args)

    def test_refresh_does_not_rescue_a_build_scan_already_stale_when_candidate_was_created(self):
        self.build_receipt["checked_at"] = (dt.datetime.fromisoformat(self.candidate["created_at"]) - dt.timedelta(days=2)).isoformat()
        self.rebind_candidate()
        with patch("shared_ci.security.run") as command, self.assertRaisesRegex(PipelineError, "stale"):
            scan(self.policy, self.root, candidate=self.candidate)
        command.assert_not_called()
        with self.assertRaisesRegex(PipelineError, "stale"):
            verify_checks(self.candidate, self.root, historical_security=True)

    def test_candidate_creation_time_is_identity_bound_and_never_future(self):
        self.candidate["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        with self.assertRaisesRegex(PipelineError, "identity"):
            verify_checks(self.candidate, self.root, historical_security=True)
        self.candidate["created_at"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        self.rebind_candidate()
        with self.assertRaisesRegex(PipelineError, "future"):
            verify_checks(self.candidate, self.root, historical_security=True)

    def test_refresh_binds_current_selected_policy_and_is_revalidated_before_use(self):
        verified = self.fetch(self.fresh_scan())
        with self.assertRaises(PipelineError):
            authorize_refresh(verified, self.policy_path, "0" * 64, self.source)
        with self.assertRaises(PipelineError):
            authorize_refresh(verified, self.policy_path, sha256_file(self.policy_path), self.original_source)
        proof = authorize_refresh(verified, self.policy_path, sha256_file(self.policy_path), self.source)
        self.policy_path.write_text(json.dumps({**self.policy, "threshold": "critical"}))
        with self.assertRaisesRegex(PipelineError, "policy changed"):
            verify_checks(self.candidate, self.root, security_refresh={"build-security.json": proof})

    def test_stale_failed_future_unbound_or_wrong_candidate_refresh_cannot_promote(self):
        receipt = self.fresh_scan()
        mutations = [lambda x: x.update(checked_at="2000-01-01T00:00:00+00:00"),
                     lambda x: x.update(checked_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()),
                     lambda x: x.update(status="failed"), lambda x: x.update(max_age_hours=168),
                     lambda x: x.update(scanners=[]), lambda x: x["counts"].update(blocking=1),
                     lambda x: x.pop("candidate_binding"), lambda x: x["candidate_binding"].update(identity="b" * 64),
                     lambda x: x["candidate_binding"]["artifacts"]["app.bin"].update(sha256="c" * 64),
                     lambda x: x["assurance"].update(config_sha256="d" * 64)]
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(receipt)
            mutate(changed)
            with self.subTest(index=index), self.assertRaises(PipelineError):
                proof = self.authorize(changed)
                verify_checks(self.candidate, self.root, security_refresh={"build-security.json": proof})

    def test_mutating_or_missing_candidate_bytes_block_refresh(self):
        proof = self.authorize()
        (self.root / "app.bin").write_bytes(b"different compiled bytes")
        with self.assertRaisesRegex(PipelineError, "bytes changed"):
            verify_checks(self.candidate, self.root, security_refresh={"build-security.json": proof})
        with self.assertRaisesRegex(PipelineError, "bytes changed"):
            scan(self.policy, self.root, candidate=self.candidate)

    def test_scanner_cannot_modify_candidate_while_rescanning(self):
        policy = copy.deepcopy(self.policy)
        (self.root / "semgrep").write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nif '--version' not in sys.argv: Path('app.bin').write_bytes(b'changed')\nprint('1.0.0' if '--version' in sys.argv else '{{\"results\":[],\"errors\":[]}}')\n")
        with self.assertRaisesRegex(PipelineError, "bytes changed"):
            scan(policy, self.root, candidate=self.candidate)

    def test_freshness_window_does_not_extend_a_security_exception(self):
        checked = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        config = copy.deepcopy(self.policy)
        config["exceptions"] = [{"id": "finding", "component": "component", "owner": "owner", "reason": "fixture",
                                 "expires_at": (checked + dt.timedelta(hours=1)).isoformat()}]
        receipt = {"status": "passed", "checked_at": checked.isoformat(), "max_age_hours": 24, "policy_identity": fingerprint(config)}
        self.assertTrue(require_policy_fresh(receipt, config, at=checked))
        with self.assertRaisesRegex(PipelineError, "exception expired"):
            require_policy_fresh(receipt, config)


if __name__ == "__main__":
    unittest.main()
