"""Small fail-closed primitives. No provider credentials are inherited by default."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


class PipelineError(ValueError):
    """An expected, safe-to-display pipeline failure."""


class CommandFailure(PipelineError):
    """Command metadata only; arbitrary output is never attached to this error."""

    def __init__(self, message, *, returncode=None, timed_out=False, elapsed_seconds=0):
        super().__init__(message)
        self.returncode = returncode
        self.timed_out = timed_out
        self.elapsed_seconds = round(elapsed_seconds, 3)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PipelineError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(Path(path).read_text(), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(PipelineError("Non-finite JSON number")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Cannot read valid JSON: {Path(path).name}") from exc


def strict_keys(value, allowed: Iterable[str], required: Iterable[str] = ()):
    if not isinstance(value, dict):
        raise PipelineError("Expected a JSON object")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown or missing:
        raise PipelineError(f"Configuration fields: unknown={sorted(unknown)}, missing={sorted(missing)}")
    return value


def confined_path(root, relative, must_exist=True):
    root = Path(root).resolve()
    if not isinstance(relative, (str, Path)) or not str(relative) or Path(relative).is_absolute():
        raise PipelineError("Expected a nonempty workspace-relative path")
    candidate = root / relative
    if ".." in Path(relative).parts:
        raise PipelineError("Parent traversal is not permitted")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise PipelineError("Path escapes the workspace")
    if must_exist and not resolved.exists():
        raise PipelineError(f"Required workspace path is absent: {relative}")
    return resolved


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PipelineError("Refusing to write a receipt through a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION)", re.I)
_DERIVED_SECRETS = set()


def register_secret(value):
    if value:
        _DERIVED_SECRETS.add(str(value))


def redact(text, values):
    result = str(text)
    for value in sorted({str(v) for v in values if v}, key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    return result


def redact_known_secrets(text):
    return redact(text, list(_DERIVED_SECRETS) + [value for name, value in os.environ.items() if _SENSITIVE.search(name)])


def scoped_environment(names=(), extra=None):
    # No ambient cloud, model, registry, GitHub or package credentials.
    base = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "TEMP", "SystemRoot") if key in os.environ}
    base.update({"CI": "true", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise PipelineError("Invalid environment variable name")
        if name in os.environ:
            base[name] = os.environ[name]
    for name, value in (extra or {}).items():
        if not _ENV_NAME.fullmatch(name) or not isinstance(value, (str, int, float, bool)):
            raise PipelineError("Invalid explicit environment value")
        base[name] = str(value)
    return base


def run(argv, *, cwd, env_names=(), extra_env=None, timeout=900,
        capture_file=None, check=True, input_data=None):
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(x, str) or "\x00" in x for x in argv):
        raise PipelineError("Commands must be nonempty argument arrays")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0 or timeout > 21600:
        raise PipelineError("Command timeout must be between 0 and 21600 seconds")
    env = scoped_environment(env_names, extra_env)
    sensitive = [v for k, v in env.items() if _SENSITIVE.search(k)] + list(_DERIVED_SECRETS)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except OSError as exc:
        raise CommandFailure(f"Cannot start command: {Path(argv[0]).name}", elapsed_seconds=time.monotonic() - started) from exc
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise CommandFailure(f"Command deadline exceeded: {Path(argv[0]).name}", returncode=proc.returncode,
                             timed_out=True, elapsed_seconds=time.monotonic() - started) from exc
    stdout, stderr = redact(stdout, sensitive), redact(stderr, sensitive)
    if capture_file:
        path = Path(capture_file)
        if path.is_symlink():
            raise PipelineError("Refusing to write logs through a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(stdout + stderr)
    result = subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
    if check and result.returncode:
        # Tool output may include values derived from credentials. Never echo it automatically.
        raise CommandFailure(f"Command failed: {Path(argv[0]).name} (exit {result.returncode})",
                             returncode=result.returncode, elapsed_seconds=time.monotonic() - started)
    return result


def trusted_context(allowed_refs, allow_pull_requests=False):
    """Refuse credentialed execution outside an explicitly allowed GitHub context."""
    if not isinstance(allowed_refs, list) or not allowed_refs or any(
        not isinstance(ref, str) or not ref.startswith("refs/heads/") or any(c in ref for c in "*?[]\n\r")
        for ref in allowed_refs
    ):
        raise PipelineError("Trusted refs must be explicit refs/heads names")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise PipelineError("Credentialed operation requires a verified GitHub job context")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    ref = os.environ.get("GITHUB_REF", "")
    if event == "pull_request" and allow_pull_requests:
        payload = load_json(os.environ.get("GITHUB_EVENT_PATH", ""))
        pr = payload.get("pull_request", {})
        head = pr.get("head", {})
        if head.get("repo", {}).get("full_name") != os.environ.get("GITHUB_REPOSITORY"):
            raise PipelineError("Fork pull requests cannot receive provider credentials")
        ref = "refs/heads/" + head.get("ref", "")
    elif event not in ("push", "workflow_dispatch"):
        raise PipelineError("Untrusted event for a credentialed operation")
    if ref not in allowed_refs:
        raise PipelineError("Ref is not authorized for this credentialed operation")
    return {"repository": os.environ.get("GITHUB_REPOSITORY", ""), "ref": ref,
            "event": event, "sha": os.environ.get("GITHUB_SHA", "")}


def cli_error(main):
    try:
        main()
    except PipelineError as exc:
        raise SystemExit(str(exc)) from None
