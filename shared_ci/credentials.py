"""Presence-only contracts and short-lived secret files, without provisioning keys."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

from .common import PipelineError, atomic_json, cli_error, load_json, register_secret, strict_keys

NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
SECRET_NAME = re.compile(r"(?:API_KEY|PRIVATE_KEY|PASSWORD|SECRET|AUTH_TOKEN|ACCESS_TOKEN|CREDENTIAL)")


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise PipelineError("Credential/variable names must be uppercase environment names")
    return value


def preflight(config, environ=None):
    env = os.environ if environ is None else environ
    strict_keys(config, {"schema", "variables", "secrets", "files"}, {"schema"})
    if config["schema"] != 1:
        raise PipelineError("Unsupported credentials schema")
    report = {"schema": 1, "variables": [], "secrets": [], "files": []}
    names = set()
    for category in ("variables", "secrets", "files"):
        entries = config.get(category, [])
        if not isinstance(entries, list):
            raise PipelineError(f"{category} must be a list")
        for entry in entries:
            allowed = {"name", "required", "allowed", "pattern"} if category == "variables" else {"name", "required"}
            if category == "files":
                allowed |= {"encoding", "format", "output_env"}
            strict_keys(entry, allowed, {"name", "required"})
            name = _name(entry["name"])
            if name in names:
                raise PipelineError(f"Duplicate credential declaration: {name}")
            names.add(name)
            if not isinstance(entry["required"], bool):
                raise PipelineError("required must be boolean")
            value = env.get(name, "")
            present = isinstance(value, str) and bool(value.strip())
            if entry["required"] and not present:
                raise PipelineError(f"Required {category} input is missing: {name}")
            if category == "variables":
                if SECRET_NAME.search(name):
                    raise PipelineError(f"Secret-like name declared as a public variable: {name}")
                if present and "allowed" in entry and value not in entry["allowed"]:
                    raise PipelineError(f"Variable does not match its allowed values: {name}")
                if present and "pattern" in entry:
                    try:
                        matches = re.fullmatch(entry["pattern"], value)
                    except (re.error, TypeError) as exc:
                        raise PipelineError("Invalid variable validation pattern") from exc
                    if not matches:
                        raise PipelineError(f"Variable does not match its declared format: {name}")
            if category == "files":
                _name(entry.get("output_env", ""))
                if entry.get("encoding") not in ("base64", "text") or entry.get("format") not in ("json", "pem", "binary"):
                    raise PipelineError("Secret file requires explicit encoding and format")
                if entry["output_env"] in names:
                    raise PipelineError("Secret file output environment name collides")
                names.add(entry["output_env"])
            report[category].append({"name": name, "present": present, "required": entry["required"]})
    return report


def _mask(value):
    register_secret(value)
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        safe = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::add-mask::" + safe, flush=True)
        if "\n" in value or "\r" in value:
            for line in value.splitlines():
                if line and not line.startswith("-----"):
                    print("::add-mask::" + line.replace("%", "%25"), flush=True)


def _strings(value, *, sensitive=False):
    if isinstance(value, str) and sensitive:
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            secret_field = bool(re.search(r"(?:private_key|client_secret|refresh_token|access_token|password|secret|api_key)", key, re.I))
            yield from _strings(child, sensitive=sensitive or secret_field)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, sensitive=sensitive)


@contextlib.contextmanager
def materialize(config, environ=None):
    """Yield explicit variables/secret paths; remove decoded files even after failure."""
    env = os.environ if environ is None else environ
    preflight(config, env)
    with tempfile.TemporaryDirectory(prefix="shared-ci-credentials-", dir="/tmp") as directory:
        result = {}
        for category in ("variables", "secrets"):
            for entry in config.get(category, []):
                value = env.get(entry["name"], "")
                if value:
                    result[entry["name"]] = value
                    if category == "secrets":
                        _mask(value)
        for entry in config.get("files", []):
            value = env.get(entry["name"], "")
            if not value:
                continue
            _mask(value)
            try:
                data = base64.b64decode(value, validate=True) if entry["encoding"] == "base64" else value.encode()
                if not data:
                    raise PipelineError("Decoded secret file is empty")
                if entry["format"] == "json":
                    parsed = json.loads(data)
                    for derived in _strings(parsed):
                        _mask(derived)
                elif entry["format"] == "pem":
                    if b"-----BEGIN " not in data or b"-----END " not in data:
                        raise PipelineError("Invalid PEM secret file")
                    _mask(data.decode())
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise PipelineError("Invalid encoded secret file") from exc
            target = Path(directory) / entry["name"]
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            result[entry["output_env"]] = str(target)
        yield result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = preflight(load_json(args.config))
    if args.output:
        atomic_json(args.output, report)
    else:
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    cli_error(main)
