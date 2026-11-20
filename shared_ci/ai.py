"""Credential-isolated AI evaluations with private raw output and public-safe receipts.

This runner enforces process/concurrency limits. Arbitrary product providers and
model-based graders require their own call/spend controls; accounting is not a
promise to stop an upstream provider before it bills a request.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request

from .common import (PipelineError, atomic_json, cli_error, confined_path,
                     fingerprint, load_json, run, sha256_file, strict_keys,
                     trusted_context)

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
PROVIDER_ENV = {
    "openai": {"OPENAI_API_KEY"},
    "anthropic": {"ANTHROPIC_API_KEY"},
    "gemini": {"GOOGLE_API_KEY", "GEMINI_API_KEY"},
    "bedrock": {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION"},
    "http": set(),
    "mock": set(),
}
SAFE_ASSERTIONS = {"equals", "contains", "icontains", "not-contains", "not-icontains", "contains-any", "contains-all", "regex", "not-regex", "is-json", "starts-with", "contains-json", "cost", "latency"}
CONFIG_KEYS = {"schema_version", "mode", "engine", "promptfoo", "evaluation", "providers", "limits", "identity", "live_enabled", "trusted_refs", "allow_pull_requests", "release", "variables", "revision_probe"}


def integer(value, name, minimum=1, maximum=1000000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PipelineError(f"Invalid {name}; expected integer {minimum}..{maximum}")
    return value


def number(value, name, minimum=0, maximum=1):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise PipelineError(f"Invalid {name}")
    return value


def argv(value, name):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x or "\x00" in x for x in value):
        raise PipelineError(f"{name} must be a nonempty argument array")
    return value


def validate(config):
    strict_keys(config, CONFIG_KEYS, {"schema_version", "mode", "promptfoo", "evaluation", "providers", "limits", "identity"})
    if config["schema_version"] != 1 or isinstance(config["schema_version"], bool):
        raise PipelineError("Unsupported AI configuration schema")
    if config["mode"] not in {"mock", "live", "review"}:
        raise PipelineError("AI mode must be mock, live or review")
    if config.get("engine", "promptfoo") not in {"promptfoo", "product"}:
        raise PipelineError("Unsupported AI engine")
    for name in ("release", "live_enabled", "allow_pull_requests"):
        if name in config and not isinstance(config[name], bool):
            raise PipelineError(f"{name} must be boolean")
    if config.get("release") and config["mode"] != "live":
        raise PipelineError("Only live evaluation can certify a release")
    tool = strict_keys(config["promptfoo"], {"command", "version"}, {"command", "version"})
    argv(tool["command"], "promptfoo.command")
    if not isinstance(tool["version"], str) or not VERSION.fullmatch(tool["version"]):
        raise PipelineError("Pin an exact promptfoo version, never latest or a range")
    ev = strict_keys(config["evaluation"], {"config", "inputs", "expected_results", "minimum_pass_rate", "gate", "command"}, {"config", "inputs", "expected_results", "minimum_pass_rate"})
    integer(ev["expected_results"], "expected_results")
    number(ev["minimum_pass_rate"], "minimum_pass_rate")
    if not isinstance(ev["inputs"], list) or not ev["inputs"] or any(not isinstance(x, str) for x in ev["inputs"]):
        raise PipelineError("Declare the prompt/dataset/grader inputs")
    for name in ("gate", "command"):
        if name in ev:
            argv(ev[name], f"evaluation.{name}")
    if config.get("engine", "promptfoo") == "product" and "command" not in ev:
        raise PipelineError("Product engine requires an evaluation command")
    if config.get("engine", "promptfoo") == "promptfoo" and "command" in ev:
        raise PipelineError("Direct promptfoo engine does not accept a command override")
    identity = strict_keys(config["identity"], {"candidate", "source_revision"}, {"candidate"})
    if not isinstance(identity["candidate"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", identity["candidate"]):
        raise PipelineError("Use a bounded, nonsecret candidate identifier")
    if "source_revision" in identity and not REVISION.fullmatch(str(identity["source_revision"])):
        raise PipelineError("source_revision must be a full Git SHA")
    limits = strict_keys(config["limits"], {"timeout_seconds", "call_timeout_seconds", "max_concurrency", "max_retries", "max_results", "max_output_tokens", "max_reported_tokens", "max_reported_cost_usd", "upstream_budget"}, {"timeout_seconds", "call_timeout_seconds", "max_concurrency", "max_retries", "max_results", "max_output_tokens", "upstream_budget"})
    integer(limits["timeout_seconds"], "timeout_seconds", 1, 21600)
    integer(limits["call_timeout_seconds"], "call_timeout_seconds", 1, limits["timeout_seconds"])
    integer(limits["max_concurrency"], "max_concurrency", 1, 32)
    integer(limits["max_retries"], "max_retries", 0, 5)
    integer(limits["max_results"], "max_results")
    integer(limits["max_output_tokens"], "max_output_tokens", 1, 1000000)
    if ev["expected_results"] > limits["max_results"]:
        raise PipelineError("Expected evaluation count exceeds max_results")
    if limits["upstream_budget"] not in {"not-applicable", "product-enforced"}:
        raise PipelineError("Declare the upstream budget boundary")
    if config["mode"] != "mock" and limits["upstream_budget"] != "product-enforced":
        raise PipelineError("Live providers require product/account-side spending controls")
    if "max_reported_tokens" in limits:
        integer(limits["max_reported_tokens"], "max_reported_tokens")
    if "max_reported_cost_usd" in limits:
        number(limits["max_reported_cost_usd"], "max_reported_cost_usd", 0, 1000000)
    if not isinstance(config["providers"], list) or not config["providers"]:
        raise PipelineError("Explicit providers and models are required")
    ids = set()
    for provider in config["providers"]:
        strict_keys(provider, {"kind", "id", "model", "env"}, {"kind", "id", "model", "env"})
        kind = provider["kind"]
        if kind not in PROVIDER_ENV:
            raise PipelineError("Unsupported provider kind")
        if not all(isinstance(provider[x], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}", provider[x]) for x in ("id", "model")):
            raise PipelineError("Provider and model IDs must be explicit nonsecret identifiers")
        if provider["id"] in ids:
            raise PipelineError("Duplicate provider identifier")
        ids.add(provider["id"])
        if kind == "mock" and provider["id"] != "echo":
            raise PipelineError("Direct mock provider must be echo")
        if config["mode"] == "mock" and kind != "mock":
            raise PipelineError("Mock evaluations cannot declare paid providers")
        if config["mode"] != "mock" and kind == "mock":
            raise PipelineError("Live/review evaluation cannot certify an echo provider")
        if not isinstance(provider["env"], dict):
            raise PipelineError("Provider env is an explicit destination-to-source name mapping")
        for target, source in provider["env"].items():
            if not isinstance(target, str) or not ENV_NAME.fullmatch(target) or not isinstance(source, str) or not ENV_NAME.fullmatch(source):
                raise PipelineError("Invalid provider environment mapping")
            if kind not in {"http"} and target not in PROVIDER_ENV[kind]:
                raise PipelineError("Environment variable is outside the selected provider contract")
            if kind == "http" and not target.startswith("AI_TARGET_"):
                raise PipelineError("HTTP target credentials must use AI_TARGET_ names")
        required = {"openai": {"OPENAI_API_KEY"}, "anthropic": {"ANTHROPIC_API_KEY"}, "bedrock": {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION"}}.get(kind, set())
        if not required.issubset(provider["env"]):
            raise PipelineError("Required provider credential mapping is missing")
        if kind == "gemini" and not ({"GOOGLE_API_KEY", "GEMINI_API_KEY"} & provider["env"].keys()):
            raise PipelineError("Gemini credential mapping is missing")
    variables = config.get("variables", {})
    if not isinstance(variables, dict):
        raise PipelineError("variables must map explicit AI_VAR_ names to nonsecret values")
    for key, value in variables.items():
        if not isinstance(key, str) or not key.startswith("AI_VAR_") or not ENV_NAME.fullmatch(key) or not isinstance(value, (str, int, bool)):
            raise PipelineError("Invalid AI variable")
    return config


def provider_environment(config):
    """Map only selected, explicitly named credentials. Values never enter receipts."""
    result = {}
    for provider in config["providers"]:
        for target, source in provider["env"].items():
            value = os.environ.get(source)
            if not value:
                raise PipelineError(f"Required AI environment variable is absent: {source}")
            if target in result and result[target] != value:
                raise PipelineError("Conflicting provider environment mappings")
            result[target] = value
    return result


def check_mock_tree(value):
    """Echo does not make model calls, but LLM assertions/custom code could."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"extensions", "transform", "transformRequest", "transformResponse", "assertScoringFunction", "redteam", "provider", "env", "nunjucksFilters", "tools", "webhook"}:
                raise PipelineError("Mock configuration contains executable or remote configuration")
            if key == "assert":
                if not isinstance(item, list) or not item:
                    raise PipelineError("Mock assertions must be nonempty")
                for assertion in item:
                    if not isinstance(assertion, dict) or assertion.get("type") not in SAFE_ASSERTIONS:
                        raise PipelineError("Mock mode permits deterministic assertions only")
            check_mock_tree(item)
    elif isinstance(value, list):
        for item in value:
            check_mock_tree(item)
    elif isinstance(value, str) and ("file://" in value or "{{ env." in value or value.startswith(("https://", "http://"))):
        raise PipelineError("Mock configuration cannot import files, environment or URLs")



def reject_implicit_graders(value):
    """Native direct profile never lets Promptfoo choose a paid default grader.

    Generated/model-graded suites use the explicit product runner contract,
    whose complete target/attacker/judge configuration is owned by that product.
    """
    deterministic = SAFE_ASSERTIONS | {"javascript", "python", "not-equals", "contains-html", "is-valid-openai-tools-call", "is-valid-openai-function-call", "is-valid-json-schema", "rouge-n", "bleu", "levenshtein", "perplexity"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "assert":
                if not isinstance(item, list) or any(not isinstance(a, dict) or a.get("type") not in deterministic for a in item):
                    raise PipelineError("Model-graded or unsupported assertions require an explicit product evaluation adapter")
            if key == "provider":
                raise PipelineError("Per-test/grader provider overrides require the product evaluation adapter")
            reject_implicit_graders(item)
    elif isinstance(value, list):
        for item in value:
            reject_implicit_graders(item)


def runtime_config(config, source_path, root, private):
    """Direct engine takes JSON; product engine can adapt existing YAML scripts."""
    data = load_json(source_path)
    if not isinstance(data, dict):
        raise PipelineError("Promptfoo config must be a JSON object")
    if any(key in data for key in ("env", "outputPath", "commandLineOptions", "tracing")):
        raise PipelineError("Promptfoo config cannot override environment, output or execution controls")
    if config["mode"] == "mock":
        check_mock_tree(data)
    else:
        reject_implicit_graders(data)
    providers = data.get("providers")
    if not isinstance(providers, list) or not providers:
        raise PipelineError("Promptfoo providers must be explicit")
    declared = {p["id"]: p for p in config["providers"]}
    observed = set()
    normalized = []
    cap = config["limits"]["max_output_tokens"]
    for value in providers:
        p = {"id": value} if isinstance(value, str) else copy.deepcopy(value)
        if not isinstance(p, dict) or p.get("id") not in declared:
            raise PipelineError("Promptfoo config uses an undeclared provider")
        observed.add(p["id"])
        pc = p.setdefault("config", {})
        if not isinstance(pc, dict) or "env" in p:
            raise PipelineError("Provider configuration must use the shared credential mapping")
        if any(key.lower() in {"apikey", "accesskeyid", "secretaccesskey", "sessiontoken", "credentials"} for key in pc):
            raise PipelineError("Inline provider credentials are forbidden")
        pc["maxRetries"] = config["limits"]["max_retries"]
        kind = declared[p["id"]]["kind"]
        if kind == "openai":
            if not p["id"].startswith(("openai:responses:", "openai:chat:")) or not p["id"].endswith(":" + declared[p["id"]]["model"]):
                raise PipelineError("OpenAI requires an explicit endpoint and matching model")
            field = "max_output_tokens" if p["id"].startswith("openai:responses:") else "max_completion_tokens"
            for key in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
                pc.pop(key, None)
            pc[field] = cap
            if field == "max_output_tokens":
                pc["store"] = False
        elif kind == "anthropic":
            if p["id"] != "anthropic:messages:" + declared[p["id"]]["model"]:
                raise PipelineError("Anthropic requires an explicit matching Messages model")
            pc["max_tokens"] = cap
        elif kind == "gemini":
            if p["id"] != "google:" + declared[p["id"]]["model"]:
                raise PipelineError("Gemini requires an explicit matching model")
            pc["maxOutputTokens"] = cap
            if "generationConfig" in pc:
                if not isinstance(pc["generationConfig"], dict):
                    raise PipelineError("Invalid Gemini generationConfig")
                pc["generationConfig"]["maxOutputTokens"] = cap
        elif kind == "bedrock":
            if p["id"] != "bedrock:converse:" + declared[p["id"]]["model"]:
                raise PipelineError("Direct Bedrock profile uses Converse with an explicit model")
            pc["maxTokens"] = cap
        normalized.append(p)
    if observed != set(declared):
        raise PipelineError("Provider inventory differs from the actual evaluation config")
    data["providers"] = normalized
    data["basePath"] = str(source_path.parent)
    data["sharing"] = False
    data["writeLatestResults"] = False
    opts = data.setdefault("evaluateOptions", {})
    if not isinstance(opts, dict):
        raise PipelineError("Invalid evaluateOptions")
    opts.update({"maxConcurrency": config["limits"]["max_concurrency"], "timeoutMs": config["limits"]["call_timeout_seconds"] * 1000, "maxEvalTimeMs": config["limits"]["timeout_seconds"] * 1000, "cache": False})
    path = private / "runtime-config.json"
    atomic_json(path, data)
    return path


def parse_results(path, expected, maximum, require_assertions=True):
    data = load_json(path)
    if not isinstance(data, dict):
        raise PipelineError("Promptfoo result must be an object")
    body = data.get("results")
    if isinstance(body, list):
        rows, stats = body, data.get("stats")
    elif isinstance(body, dict):
        rows = body.get("results", body.get("outputs"))
        stats = body.get("stats")
    else:
        raise PipelineError("Promptfoo result structure is missing")
    if not isinstance(rows, list) or not rows or len(rows) != expected or len(rows) > maximum:
        raise PipelineError("Evaluation result count is empty, incomplete or outside its declared bound")
    if not isinstance(stats, dict):
        raise PipelineError("Evaluation statistics are missing")
    passed = failed = errors = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("success"), bool):
            raise PipelineError("Malformed evaluation result row")
        if isinstance(row.get("response"), dict) and row["response"].get("cached") is True:
            raise PipelineError("Fresh evaluation unexpectedly contains a cached response")
        if row.get("failureReason") == 2 or (row.get("error") and row.get("failureReason") != 1):
            errors += 1
            if row["success"]:
                raise PipelineError("Provider error reported as success")
        if row["success"]:
            grading = row.get("gradingResult")
            if require_assertions and (not isinstance(grading, dict) or grading.get("pass") is not True or not isinstance(grading.get("componentResults"), list) or not grading["componentResults"] or any(not isinstance(c, dict) or not isinstance(c.get("pass"), bool) for c in grading["componentResults"])):
                raise PipelineError("Passing evaluation lacks assertion evidence")
            passed += 1
        else:
            failed += 1
    successes = integer(stats.get("successes"), "reported successes", 0)
    failures = integer(stats.get("failures"), "reported failures", 0)
    reported_errors = integer(stats.get("errors", errors), "reported errors", 0)
    # Promptfoo versions have counted provider errors either separately or within
    # failures. Both require a complete row set and identical explicit error count.
    if successes != passed or failures not in {failed, failed - errors} or reported_errors != errors:
        raise PipelineError("Evaluation statistics disagree with result rows")
    tokens = None
    usage = stats.get("tokenUsage")
    if isinstance(usage, dict) and "total" in usage:
        tokens = integer(usage["total"], "reported total tokens", 0, 10**15)
    costs = [row.get("cost", row.get("response", {}).get("cost") if isinstance(row.get("response"), dict) else None) for row in rows]
    cost = None
    if all(value is not None for value in costs):
        cost = sum(number(value, "reported cost", 0, 10**9) for value in costs)
    return {"total": len(rows), "passed": passed, "failed": failed, "provider_errors": errors, "pass_rate": passed / len(rows), "reported_tokens": tokens, "estimated_cost_usd": cost}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PipelineError("Revision probe redirects are forbidden")


def revision_probe(probe, environment, expected, timeout):
    strict_keys(probe, {"url_env", "token_env", "header", "method", "body"}, {"url_env", "header"})
    url = environment.get(probe["url_env"], "")
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise PipelineError("Revision probe requires a credential-free HTTPS URL")
    header = probe["header"]
    if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]+", header):
        raise PipelineError("Invalid revision header")
    method = probe.get("method", "GET")
    if method not in {"GET", "POST"}:
        raise PipelineError("Revision probe must use GET or POST")
    headers = {"Accept": "application/json"}
    if "token_env" in probe:
        token = environment.get(probe["token_env"])
        if not token:
            raise PipelineError("Revision probe token is missing")
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(probe.get("body", {})).encode() if method == "POST" else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            observed = response.headers.get(header)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise PipelineError("Revision probe failed") from exc
    if observed != expected:
        raise PipelineError("Target revision differs from the candidate")
    return expected


def execute(config, root, output, source_revision=None):
    config = validate(config)
    output = Path(output)
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise PipelineError("Unsafe AI receipt destination")
    root = Path(root).resolve()
    ev = config["evaluation"]
    evaluation_path = confined_path(root, ev["config"])
    files = sorted(set(ev["inputs"] + [ev["config"]]))
    if any(output.resolve() == confined_path(root, name) for name in files):
        raise PipelineError("Receipt cannot overwrite an evaluation input")
    output.unlink(missing_ok=True)
    inputs = {name: sha256_file(confined_path(root, name)) for name in files}
    revision = run(["git", "rev-parse", "HEAD"], cwd=root, timeout=10).stdout.strip()
    if not REVISION.fullmatch(revision):
        raise PipelineError("Cannot establish source revision")
    expected_revision = source_revision or config["identity"].get("source_revision") or revision
    if revision != expected_revision:
        raise PipelineError("Checked-out source differs from expected revision")
    live = config["mode"] != "mock"
    if live:
        if config.get("live_enabled") is not True:
            raise PipelineError("Live AI execution is not explicitly enabled")
        trusted_context(config.get("trusted_refs"), config.get("allow_pull_requests", False))
    credentials = provider_environment(config) if live else {}
    if config.get("release") and any(p["kind"] == "http" for p in config["providers"]) and "revision_probe" not in config:
        raise PipelineError("HTTP release evaluation requires a target revision probe")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="shared-ai-") as tmp:
        private = Path(tmp)
        home = private / "home"
        home.mkdir(mode=0o700)
        empty_env = private / "empty.env"
        empty_env.write_text("")
        raw = private / "results.json"
        isolation = {
            "HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(empty_env), "AWS_CONFIG_FILE": str(empty_env),
            "AWS_EC2_METADATA_DISABLED": "true", "GOOGLE_APPLICATION_CREDENTIALS": str(empty_env),
            "DOTENV_PATH": str(empty_env), "DOTENV_CONFIG_PATH": str(empty_env), "EVALS_ENV": str(empty_env),
            "PROMPTFOO_CONFIG_DIR": str(private / "promptfoo"), "PROMPTFOO_CACHE_PATH": str(private / "cache"),
            "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_UPDATE": "1",
            "PROMPTFOO_DISABLE_REDTEAM_REMOTE_GENERATION": "true", "PROMPTFOO_DISABLE_REMOTE_GENERATION": "true",
            "PROMPTFOO_DISABLE_SHARING": "true", "PROMPTFOO_CACHE_ENABLED": "false",
            "PROMPTFOO_LOG_DIR": str(private / "logs"), "PROMPTFOO_TRACING_ENABLED": "false",
            "PROMPTFOO_OTEL_ENABLED": "false",
            "PROMPTFOO_VERSION": config["promptfoo"]["version"], "EVAL_USE_CACHE": "0",
            "PROMPTFOO_EVAL_TIMEOUT_MS": str(config["limits"]["call_timeout_seconds"] * 1000),
            "PROMPTFOO_MAX_EVAL_TIME_MS": str(config["limits"]["timeout_seconds"] * 1000),
            "AI_RESULTS_PATH": str(raw), "AI_EXPECTED_REVISION": revision, "EXPECTED_REVISION": revision,
            "AI_MAX_OUTPUT_TOKENS": str(config["limits"]["max_output_tokens"]),
            "AI_MAX_CONCURRENCY": str(config["limits"]["max_concurrency"]),
            "AI_MAX_RETRIES": str(config["limits"]["max_retries"]),
            "AWS_BEDROCK_MAX_RETRIES": str(config["limits"]["max_retries"]),
            **config.get("variables", {}),
        }
        def remaining():
            left = config["limits"]["timeout_seconds"] - (time.monotonic() - started)
            if left <= 0:
                raise PipelineError("AI operation exceeded its total deadline")
            return left
        # Tool validation has no credentials, even for a live evaluation.
        check = run(config["promptfoo"]["command"] + ["--version"], cwd=private, extra_env=isolation, timeout=remaining())
        versions = re.findall(r"(?<![\w.])(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)(?![\w.])", check.stdout)
        if versions != [config["promptfoo"]["version"]]:
            raise PipelineError("Installed promptfoo version does not match the pinned version")
        environment = {**isolation, **credentials}
        target_revision = None
        if "revision_probe" in config:
            if not live:
                raise PipelineError("Mock runs cannot probe a live target")
            target_revision = revision_probe(config["revision_probe"], environment, revision, min(remaining(), config["limits"]["call_timeout_seconds"]))
        if config.get("engine", "promptfoo") == "promptfoo":
            runtime = runtime_config(config, evaluation_path, root, private)
            command = config["promptfoo"]["command"] + ["eval", "--config", str(runtime), "--env-file", str(empty_env), "--output", str(raw), "--no-cache", "--no-write", "--no-progress-bar", "--no-table", "--max-concurrency", str(config["limits"]["max_concurrency"])]
            result = run(command, cwd=private, extra_env=environment, timeout=remaining(), check=False)
            if result.returncode not in {0, 100}:
                raise PipelineError("Promptfoo execution failed before a usable evaluation completed")
        else:
            # Product code is trusted code, not a sandbox. It must honor these
            # output/environment contracts and implement its own upstream budget.
            environment["AI_EVAL_CONFIG"] = str(evaluation_path)
            result = run(ev["command"], cwd=root, extra_env=environment, timeout=remaining(), check=False)
            if result.returncode != 0:
                raise PipelineError("Product evaluation runner failed")
        if raw.is_symlink() or not raw.is_file() or raw.stat().st_size > 100 * 1024 * 1024:
            raise PipelineError("Required private evaluation report is missing or unsafe")
        summary = parse_results(raw, ev["expected_results"], config["limits"]["max_results"], config["mode"] != "review")
        if result.returncode == 100 and summary["failed"] == 0:
            raise PipelineError("Promptfoo failure exit contradicts a passing report")
        if summary["provider_errors"]:
            raise PipelineError("AI evaluation contains provider errors")
        limits = config["limits"]
        for field, limit in (("reported_tokens", "max_reported_tokens"), ("estimated_cost_usd", "max_reported_cost_usd")):
            if limit in limits and (summary[field] is None or summary[field] > limits[limit]):
                raise PipelineError("AI usage accounting is absent or exceeds the configured post-run gate")
        gate_passed = None
        if "gate" in ev:
            gate = run(ev["gate"], cwd=root, extra_env=environment, timeout=remaining(), check=False)
            gate_passed = gate.returncode == 0
            if not gate_passed:
                raise PipelineError("Product-owned AI gate failed")
        threshold_passed = summary["pass_rate"] >= ev["minimum_pass_rate"]
        receipt = {
            "schema_version": 1, "kind": "ai-evaluation", "mode": config["mode"],
            "status": "advisory" if config["mode"] == "review" else ("passed" if threshold_passed else "failed"),
            "candidate": config["identity"]["candidate"], "source_revision": revision,
            "target_revision": target_revision, "input_hashes": inputs,
            "configuration_sha256": fingerprint(config), "promptfoo_version": config["promptfoo"]["version"],
            "providers": [{k: p[k] for k in ("kind", "id", "model")} for p in config["providers"]],
            "summary": summary, "threshold_passed": threshold_passed, "product_gate_passed": gate_passed,
            "release_evaluation": config.get("release", False), "response_cache": "disabled",
            "bounds": {"deadline_seconds": limits["timeout_seconds"], "max_concurrency": limits["max_concurrency"], "configured_retries": limits["max_retries"], "output_tokens_requested": limits["max_output_tokens"], "direct_output_cap_applied": config.get("engine", "promptfoo") == "promptfoo" and all(p["kind"] in {"openai", "anthropic", "gemini", "bedrock"} for p in config["providers"]), "upstream_budget_declared": limits["upstream_budget"], "usage_limits": "post-run accounting, not a hard provider spending cap"},
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        atomic_json(output, receipt)
        if not threshold_passed and config["mode"] != "review":
            raise PipelineError("AI evaluation did not meet its configured threshold")
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-revision")
    args = parser.parse_args()
    execute(load_json(args.config), args.root, args.output, args.source_revision)


if __name__ == "__main__":
    cli_error(main)
