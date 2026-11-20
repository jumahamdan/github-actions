import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError, atomic_json, confined_path, load_json, run, trusted_context


class CommonTests(unittest.TestCase):
    def test_json_rejects_duplicates_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "input.json"
            for bad in ('{"x":1,"x":2}', '{"x":NaN}'):
                p.write_text(bad)
                with self.assertRaises(PipelineError):
                    load_json(p)

    def test_paths_reject_traversal_and_external_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"
            root.mkdir()
            (root / "link").symlink_to(Path(d))
            for path in ("../outside", "/tmp", "link/outside"):
                with self.assertRaises(PipelineError):
                    confined_path(root, path, False)

    def test_no_ambient_secret_and_explicit_secret_is_redacted(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-sensitive-value"}):
            result = run([sys.executable, "-c", "import os; print(os.getenv('OPENAI_API_KEY','absent'))"], cwd=".")
            self.assertEqual(result.stdout.strip(), "absent")
            result = run([sys.executable, "-c", "import os; print(os.environ['OPENAI_API_KEY'])"],
                         cwd=".", env_names=["OPENAI_API_KEY"])
            self.assertEqual(result.stdout.strip(), "[REDACTED]")

    def test_error_and_timeout_fail(self):
        for command, timeout in (([sys.executable, "-c", "raise SystemExit(9)"], 3),
                                 ([sys.executable, "-c", "import time; time.sleep(10)"], 0.03)):
            with self.assertRaises(PipelineError):
                run(command, cwd=".", timeout=timeout)

    def test_explicit_stdin_without_shell(self):
        result = run([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"], cwd=".", input_data="hello")
        self.assertEqual(result.stdout.strip(), "5")

    def test_receipt_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "receipt.json"
            atomic_json(p, {"status": "passed"})
            self.assertEqual(load_json(p)["status"], "passed")
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_trust_refuses_targets_and_wrong_branches(self):
        for event, ref in (("pull_request_target", "refs/heads/main"), ("push", "refs/heads/feature")):
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": event, "GITHUB_REF": ref}):
                with self.assertRaises(PipelineError):
                    trusted_context(["refs/heads/main"])

    def test_fork_cannot_use_same_branch_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "event.json"
            p.write_text(json.dumps({"pull_request": {"head": {"ref": "main", "repo": {"full_name": "fork/project"}}}}))
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request",
                                         "GITHUB_EVENT_PATH": str(p), "GITHUB_REPOSITORY": "owner/project"}):
                with self.assertRaises(PipelineError):
                    trusted_context(["refs/heads/main"], True)


if __name__ == "__main__":
    unittest.main()
