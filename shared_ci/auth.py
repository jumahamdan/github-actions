"""Create a short-lived App Store Connect token from an existing signing key."""

import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path

from .common import PipelineError
from .credentials import _mask


def _encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def apple_token(key_path, key_id, issuer, *, now=None):
    if not re.fullmatch(r"[A-Z0-9]{10}", key_id) or not re.fullmatch(r"[a-fA-F0-9-]{36}", issuer):
        raise PipelineError("App Store Connect key ID and issuer must be explicit")
    key = Path(key_path)
    if not key.is_file() or key.is_symlink() or key.stat().st_mode & 0o077:
        raise PipelineError("Apple key must be an existing private mode-0600 file")
    public = subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"], capture_output=True, timeout=15)
    if public.returncode or b"\x06\x08\x2a\x86\x48\xce\x3d\x03\x01\x07" not in public.stdout:
        raise PipelineError("Apple key must be a valid P-256 private key")
    issued = int(time.time() if now is None else now)
    header = {"alg":"ES256", "kid":key_id, "typ":"JWT"}
    payload = {"iss":issuer,"iat":issued,"exp":issued+600,"aud":"appstoreconnect-v1"}
    signing = ".".join(_encoded(json.dumps(x,separators=(",", ":")).encode()) for x in (header,payload))
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=signing.encode(), capture_output=True, timeout=15)
    signature = signed.stdout
    if signed.returncode or len(signature) < 8 or signature[0] != 0x30 or signature[1] != len(signature)-2:
        raise PipelineError("Apple token signing failed")
    integers, offset = [], 2
    for _ in range(2):
        if offset+2 > len(signature) or signature[offset] != 2:
            raise PipelineError("Invalid ECDSA signature encoding")
        size = signature[offset+1]
        integer = signature[offset+2:offset+2+size]
        integer = integer.lstrip(b"\0")
        if not integer or len(integer)>32:
            raise PipelineError("Invalid ECDSA signature integer")
        integers.append(integer.rjust(32,b"\0"))
        offset += 2+size
    if offset != len(signature):
        raise PipelineError("Invalid ECDSA trailing signature data")
    token = signing+"."+_encoded(b"".join(integers))
    _mask(token)
    return token


def existing_apple_credentials():
    if os.environ.get("ASC_PRIVATE_KEY_PATH"):
        if os.environ.get("ASC_BEARER_TOKEN"):
            raise PipelineError("Choose one Apple authentication source")
        return apple_token(os.environ["ASC_PRIVATE_KEY_PATH"], os.environ.get("ASC_KEY_ID", ""), os.environ.get("ASC_ISSUER_ID", ""))
    return None
