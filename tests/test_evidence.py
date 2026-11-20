import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from shared_ci.common import PipelineError
from shared_ci.assurance import stamp
from shared_ci.evidence import create, fetch_receipt, verify


class EvidenceTests(unittest.TestCase):
    def make_candidate(self, root):
        def git(*args):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "user.name", "Fixture")
        (root / "source.txt").write_text("tested source")
        (root / "check-config.json").write_text('{"version":1}')
        git("add", "source.txt", "check-config.json")
        git("commit", "-qm", "fixture")
        (root / "app.bin").write_bytes(b"tested artifact")
        (root / "checks.json").write_text(json.dumps(stamp({"status":"passed"}, "quality", root / "check-config.json", root)))
        return create({"schema": 1, "product": "example", "version": "1.0.0", "build": "1",
                       "library_sha": "a" * 40, "inputs": ["source.txt", "check-config.json"], "artifacts": ["app.bin"],
                       "toolchain": {"compiler": "1.2.3"}, "checks": [{"path":"checks.json", "kind":"quality", "config":"check-config.json"}]}, root)

    def test_source_attribution_and_check_contract_cannot_be_relabelled(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = self.make_candidate(root)
            candidate["source_sha"] = "c" * 40
            with self.assertRaises(PipelineError):
                verify(candidate, root)

    def test_authenticated_coverage_bytes_are_verified(self):
        content = b"coverage fixture"
        digest = hashlib.sha256(content).hexdigest()
        receipt = {"status":"passed", "coverage":{"coverage/lcov.info":digest}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("receipt.json", json.dumps(receipt))
            archive.writestr("coverage/" + digest, content)
        data = buf.getvalue()
        def request(path, token, binary=False):
            if binary:
                return data
            if "/artifacts?" in path:
                return {"artifacts":[{"id":7,"name":"checks","expired":False,"digest":"sha256:"+hashlib.sha256(data).hexdigest()}]}
            return {"status":"completed","conclusion":"success","head_sha":"b"*40,"path":".github/workflows/ci.yml",
                    "head_repository":{"full_name":"owner/example"},"event":"push"}
        result = fetch_receipt("owner/example", "4", "checks", "b"*40, ".github/workflows/ci.yml", "synthetic",
                               request=request, coverage_paths=["coverage/lcov.info"])
        self.assertEqual(result["coverage_files"]["coverage/lcov.info"], content)

    def test_changed_artifact_and_input_fail(self):
        for name in ("source.txt", "app.bin", "checks.json"):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                candidate = self.make_candidate(root)
                self.assertEqual(verify(candidate, root)["status"], "passed")
                (root / name).write_text("tampered")
                with self.assertRaises(PipelineError):
                    verify(candidate, root)

    def test_changed_toolchain_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = self.make_candidate(root)
            with self.assertRaises(PipelineError):
                verify(candidate, root, expected_toolchain={"compiler": "9.9.9"})

    def test_forged_receipt_cannot_replace_trusted_step_digest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);candidate=self.make_candidate(root)
            config={k:candidate[k] for k in ("schema","product","version","build","library_sha","toolchain")}
            config.update(inputs=list(candidate["inputs"]),artifacts=list(candidate["artifacts"]),checks=[{"path":p,**r} for p,r in candidate["check_requirements"].items()])
            with self.assertRaisesRegex(PipelineError,"trusted workflow"):
                create(config,root,expected_checks={"checks.json":"0"*64})

    def test_download_verifies_provenance_digest_and_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("receipt.json", '{"status":"passed"}')
        blob = buf.getvalue()
        metadata = {"status": "completed", "conclusion": "success", "head_sha": "b" * 40,
                    "path": ".github/workflows/ci.yml", "head_repository": {"full_name": "owner/example"}, "event": "push"}
        artifact = {"id": 7, "name": "evidence", "expired": False, "digest": "sha256:" + hashlib.sha256(blob).hexdigest()}
        def request(path, token, binary=False):
            if binary:
                return blob
            return {"artifacts": [artifact]} if "/artifacts?" in path else metadata
        args = ("owner/example", "3", "evidence", "b" * 40, ".github/workflows/ci.yml", "synthetic")
        self.assertEqual(fetch_receipt(*args, request=request)["receipt"]["status"], "passed")
        metadata["conclusion"] = "failure"
        with self.assertRaises(PipelineError):
            fetch_receipt(*args, request=request)
        metadata["conclusion"] = "success"
        artifact["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(PipelineError):
            fetch_receipt(*args, request=request)


if __name__ == "__main__":
    unittest.main()
