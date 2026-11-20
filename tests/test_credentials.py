import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.credentials import materialize, preflight


class CredentialTests(unittest.TestCase):
    def test_presence_report_never_contains_values(self):
        config = {"schema": 1, "secrets": [{"name": "API_TOKEN", "required": True}]}
        value = "synthetic-value-do-not-report"
        report = preflight(config, {"API_TOKEN": value})
        self.assertNotIn(value, json.dumps(report))
        with self.assertRaises(PipelineError):
            preflight(config, {"API_TOKEN": ""})

    def test_public_variable_cannot_declare_api_key(self):
        with self.assertRaises(PipelineError):
            preflight({"schema": 1, "variables": [{"name": "OPENAI_API_KEY", "required": False}]}, {})

    def test_secret_files_private_and_removed_on_failure(self):
        cfg = {"schema": 1, "files": [{"name": "SA_JSON", "required": True,
               "encoding": "base64", "format": "json", "output_env": "GOOGLE_APPLICATION_CREDENTIALS"}]}
        blob = base64.b64encode(b'{"private_key":"synthetic-key"}').decode()
        target = None
        with self.assertRaisesRegex(RuntimeError, "expected"):
            with materialize(cfg, {"SA_JSON": blob}) as env:
                target = Path(env["GOOGLE_APPLICATION_CREDENTIALS"])
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(target.read_text())["private_key"], "synthetic-key")
                raise RuntimeError("expected")
        self.assertFalse(target.exists())

    def test_invalid_blob_rejected(self):
        cfg = {"schema": 1, "files": [{"name": "SIGNING", "required": True,
               "encoding": "base64", "format": "binary", "output_env": "SIGNING_FILE"}]}
        with self.assertRaises(PipelineError):
            with materialize(cfg, {"SIGNING": "not-base64"}):
                pass

    def test_json_masks_credentials_without_masking_public_project_metadata(self):
        cfg={"schema":1,"files":[{"name":"SA_JSON","required":True,"encoding":"text","format":"json","output_env":"GOOGLE_APPLICATION_CREDENTIALS"}]}
        value=json.dumps({"type":"service_account","project_id":"public-project","private_key":"synthetic-key","client_email":"account@example.invalid"})
        with patch("shared_ci.credentials._mask") as mask:
            with materialize(cfg,{"SA_JSON":value}):pass
        masked=[c.args[0] for c in mask.call_args_list]
        self.assertIn("synthetic-key",masked)
        self.assertNotIn("public-project",masked)
        self.assertNotIn("service_account",masked)


if __name__ == "__main__":
    unittest.main()
