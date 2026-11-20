"""Bounded HTTP, DNS and TLS verification for explicitly configured targets."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import PipelineError, atomic_json, confined_path, load_json, strict_keys, trusted_context


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise PipelineError(f"{name} is outside its supported range")
    return value


def _host(value):
    if not isinstance(value, str) or not value or any(c.isspace() or c in '/@\\\x00' for c in value):
        raise PipelineError("invalid health-check host")
    return value


def _select(value, path):
    if not isinstance(path, list) or not path:
        raise PipelineError("JSON assertion needs a nonempty path")
    try:
        for part in path:
            if not isinstance(part, (str, int)) or isinstance(part, bool):
                raise ValueError()
            value = value[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise PipelineError("expected JSON field is absent") from exc
    return value


def _http(check, timeout):
    strict_keys(check, {"kind", "url", "status", "headers", "body_contains", "json_equals", "header_env", "max_bytes"}, required=("kind", "url"))
    parsed = urlsplit(check["url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise PipelineError("HTTP health target must be an HTTP(S) URL without embedded credentials")
    if any(re.search(r"token|secret|password|api.?key", key, re.I) for key, _ in parse_qsl(parsed.query)):
        raise PipelineError("health credentials belong in header_env, not URL parameters")
    expected_status = check.get("status", [200])
    if not isinstance(expected_status, list) or not expected_status or any(type(n) is not int or not 100 <= n <= 599 for n in expected_status):
        raise PipelineError("invalid expected HTTP status list")
    request_headers = {}
    header_env = check.get("header_env", {})
    if not isinstance(header_env, dict):
        raise PipelineError("header_env must be an object")
    for name, variable in header_env.items():
        if not re.fullmatch(r"[A-Za-z0-9-]+", name) or not isinstance(variable, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", variable):
            raise PipelineError("invalid health request header declaration")
        value = os.environ.get(variable, "")
        if not value or any(c in value for c in '\r\n'):
            raise PipelineError("required health header credential is absent or invalid")
        if parsed.scheme != "https":
            raise PipelineError("credentialed HTTP probes require HTTPS")
        request_headers[name] = value
    limit = _number(check.get("max_bytes", 1024 * 1024), "max_bytes", 1, 8 * 1024 * 1024)
    if type(limit) is not int:
        raise PipelineError("max_bytes must be an integer")
    opener = build_opener(_NoRedirect())
    try:
        response = opener.open(Request(check["url"], headers=request_headers, method="GET"), timeout=timeout)
    except HTTPError as exc:
        response = exc  # Explicit status assertions may legitimately expect e.g. 401.
    except (URLError, OSError) as exc:
        raise PipelineError("HTTP health request failed") from exc
    with response:
        status = response.getcode()
        payload = response.read(limit + 1)
        headers = {key.lower(): value for key, value in response.headers.items()}
    if status not in expected_status:
        raise PipelineError("HTTP status did not match the health assertion")
    if len(payload) > limit:
        raise PipelineError("HTTP health response exceeds configured size limit")
    expected_headers = check.get("headers", {})
    if not isinstance(expected_headers, dict):
        raise PipelineError("expected headers must be an object")
    for key, value in expected_headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or headers.get(key.lower()) != value:
            raise PipelineError("HTTP response header did not match")
    snippets = check.get("body_contains", [])
    if not isinstance(snippets, list) or not all(isinstance(text, str) and text for text in snippets):
        raise PipelineError("body_contains must be a list of nonempty strings")
    text = payload.decode("utf-8", errors="replace")
    if any(part not in text for part in snippets):
        raise PipelineError("HTTP response body did not match")
    assertions = check.get("json_equals", [])
    if not isinstance(assertions, list):
        raise PipelineError("json_equals must be a list")
    if assertions:
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise PipelineError("HTTP response is not valid JSON") from exc
        for assertion in assertions:
            strict_keys(assertion, {"path", "value"}, required=("path", "value"))
            if _select(value, assertion["path"]) != assertion["value"]:
                raise PipelineError("HTTP JSON assertion did not match")
    return {"kind": "http", "host": parsed.hostname, "status_code": status, "response_sha256": hashlib.sha256(payload).hexdigest()}


def _dns(check, timeout):
    strict_keys(check, {"kind", "host", "expected_addresses"}, required=("kind", "host"))
    host = _host(check["host"])
    expected = check.get("expected_addresses", [])
    if not isinstance(expected, list):
        raise PipelineError("expected_addresses must be a list")
    try:
        expected = {str(ipaddress.ip_address(value)) for value in expected}
    except ValueError as exc:
        raise PipelineError("invalid expected IP address") from exc
    results = queue.Queue(maxsize=1)
    def resolve():
        try:
            results.put((True, socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)))
        except OSError:
            results.put((False, None))
    # getaddrinfo has no timeout argument. A daemon worker bounds this caller without
    # leaving a non-daemon executor that prevents process exit on DNS failure.
    threading.Thread(target=resolve, daemon=True).start()
    try:
        ok, entries = results.get(timeout=timeout)
    except queue.Empty as exc:
        raise PipelineError("DNS health deadline exceeded") from exc
    if not ok:
        raise PipelineError("DNS resolution failed")
    addresses = {str(ipaddress.ip_address(entry[4][0])) for entry in entries}
    if not addresses or not expected.issubset(addresses):
        raise PipelineError("DNS health addresses did not match")
    return {"kind": "dns", "host": host, "addresses": sorted(addresses)}


def _tls(check, timeout, root):
    strict_keys(check, {"kind", "host", "port", "minimum_days_remaining", "ca_file"}, required=("kind", "host"))
    host = _host(check["host"])
    port = _number(check.get("port", 443), "TLS port", 1, 65535)
    if type(port) is not int:
        raise PipelineError("TLS port must be an integer")
    days = _number(check.get("minimum_days_remaining", 14), "minimum_days_remaining", 0, 3650)
    ca = str(confined_path(root, check["ca_file"])) if check.get("ca_file") else None
    context = ssl.create_default_context(cafile=ca)
    try:
        with socket.create_connection((host, port), timeout=timeout) as plain:
            with context.wrap_socket(plain, server_hostname=host) as secured:
                certificate = secured.getpeercert()
                expires = ssl.cert_time_to_seconds(certificate["notAfter"])
    except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
        raise PipelineError("TLS certificate/hostname verification failed") from exc
    remaining = (expires - time.time()) / 86400
    if remaining < days:
        raise PipelineError("TLS certificate is too close to expiry")
    return {"kind": "tls", "host": host, "port": port, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()}


def _bounded_probe(check, timeout, root):
    results = queue.Queue(maxsize=1)
    def probe():
        try:
            if check.get("kind") == "http": result = _http(check, timeout)
            elif check.get("kind") == "dns": result = _dns(check, timeout)
            elif check.get("kind") == "tls": result = _tls(check, timeout, root)
            else: raise PipelineError("unsupported health-check kind")
            results.put((True, result))
        except Exception as exc:
            results.put((False, exc))
    # Socket timeouts alone do not bound slow streaming responses or name lookup.
    # All probes are read-only; a daemon worker also bounds total wall time.
    threading.Thread(target=probe, daemon=True).start()
    try:
        ok, result = results.get(timeout=timeout)
    except queue.Empty as exc:
        raise PipelineError("health probe exceeded its wall-clock deadline") from exc
    if not ok:
        if isinstance(result, PipelineError): raise result
        raise PipelineError("health probe failed") from result
    return result


def execute(config, root):
    strict_keys(config, {"schema", "checks", "attempts", "retry_seconds", "timeout", "receipt", "allowed_refs"}, required=("schema", "checks"))
    if type(config["schema"]) is not int or config["schema"] != 1:
        raise PipelineError("unsupported health configuration schema")
    checks = config["checks"]
    if not isinstance(checks, list) or not checks or not all(isinstance(c, dict) for c in checks):
        raise PipelineError("health checks must be nonempty objects")
    attempts = _number(config.get("attempts", 3), "attempts", 1, 10)
    if type(attempts) is not int:
        raise PipelineError("attempts must be an integer")
    delay = _number(config.get("retry_seconds", 2), "retry_seconds", 0, 30)
    timeout = _number(config.get("timeout", 10), "timeout", 0.1, 60)
    if any(check.get("header_env") for check in checks):
        trusted_context(config.get("allowed_refs"))
    outputs = []
    for check in checks:
        for attempt in range(attempts):
            try:
                result = _bounded_probe(check, timeout, root)
                result["attempts"] = attempt + 1
                outputs.append(result)
                break
            except PipelineError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(delay)
    receipt = {"schema": 1, "kind": "health", "status": "passed", "checks": outputs}
    if config.get("receipt"):
        atomic_json(confined_path(root, config["receipt"], must_exist=False), receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True); parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        result = execute(load_json(confined_path(args.root, args.config)), args.root)
        print(json.dumps({"status": result["status"], "checks": len(result["checks"])}))
    except (PipelineError, OSError, ValueError) as exc:
        print(f"health: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
