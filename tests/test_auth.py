import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from shared_ci.auth import apple_token
from shared_ci.common import PipelineError


class AuthTests(unittest.TestCase):
    def test_existing_key_produces_ten_minute_es256_token(self):
        with tempfile.TemporaryDirectory() as d:
            key=Path(d)/"key.pem"
            subprocess.run(["openssl","genpkey","-algorithm","EC","-pkeyopt","ec_paramgen_curve:P-256","-out",str(key)],check=True,capture_output=True)
            key.chmod(0o600)
            token=apple_token(key,"ABCDEFGHIJ","12345678-1234-1234-1234-123456789012",now=1000)
            header,payload,signature=token.split(".")
            self.assertEqual(json.loads(base64.urlsafe_b64decode(payload+"=="))["exp"],1600)
            self.assertEqual(len(base64.urlsafe_b64decode(signature+"==")),64)
            key.chmod(0o644)
            with self.assertRaises(PipelineError):
                apple_token(key,"ABCDEFGHIJ","12345678-1234-1234-1234-123456789012")
