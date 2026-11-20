"""Run declared scanners and enforce fresh, explicit findings policy."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path

from .common import PipelineError, atomic_json, cli_error, confined_path, fingerprint, load_json, run, sha256_file, strict_keys


def semgrep_rules(item, root, cwd):
    """Bind every CLI config to one explicit local YAML file and its checksum."""
    rules, command = item.get("rules_identity"), item.get("command")
    if not isinstance(rules, dict) or not rules:
        raise PipelineError("Semgrep requires checksummed local rule files; binary versions do not pin registry rules")
    if (not isinstance(command, list) or len(command) < 3 or not all(isinstance(arg, str) and arg for arg in command)
            or Path(command[0]).name != "semgrep" or command[1] != "scan"
            or item.get("version_command") != [command[0], "--version"]):
        raise PipelineError("Semgrep requires an explicit scan command and the same executable's version probe")
    root, cwd = Path(root).resolve(), Path(cwd).resolve()
    declared, observed = {}, {}
    for relative, digest in rules.items():
        path = confined_path(root, relative)
        original = root / relative
        if (not path.is_file() or path.suffix not in (".yml", ".yaml")
                or any(p.is_symlink() for p in (original, *original.parents))
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise PipelineError("Semgrep rules must be regular local YAML files with exact checksums")
        actual = sha256_file(path)
        if actual != digest or path in declared:
            raise PipelineError("Semgrep rules differ from their reviewed checksums or duplicate a path")
        declared[path] = relative
        observed[relative] = actual
    selected, index = [], 2
    while index < len(command):
        argument = command[index]
        if argument == "--":
            break
        if argument in ("-e", "--pattern") or argument.startswith(("--pattern=", "-e")):
            raise PipelineError("Semgrep inline patterns are outside the checksummed rules contract")
        value = None
        if argument in ("--config", "-c", "-f"):
            index += 1
            if index >= len(command):
                raise PipelineError("Semgrep config flag has no local rule file")
            value = command[index]
        elif argument.startswith("--config="):
            value = argument.split("=", 1)[1]
        elif argument.startswith(("-c", "-f")) and not argument.startswith("--"):
            value = argument[2:]
        if value is not None:
            if (not value or value == "auto" or ":" in value or value.startswith(("p/", "r/", "s/", "semgrep.dev/"))
                    or any(ord(c) < 32 for c in value)):
                raise PipelineError("Semgrep remote, registry and auto configs are forbidden; vendor reviewed rule files")
            original = cwd / value
            path = original.resolve()
            if (not path.is_relative_to(root) or not path.is_file()
                    or any(p.is_symlink() for p in (original, *original.parents)) or path not in declared):
                raise PipelineError("Semgrep command config is not an exact declared local rule file")
            selected.append(path)
        index += 1
    if not selected or len(selected) != len(set(selected)) or set(selected) != set(declared):
        raise PipelineError("Semgrep command must select every checksummed rule file exactly once")
    return observed


def scanner_intelligence(item, root, cwd):
    rules = item["rules_identity"]
    if item["format"] == "semgrep":
        observed_rules = semgrep_rules(item, root, cwd)
    elif rules == "builtin:" + item["version"]:
        observed_rules = rules
    elif isinstance(rules, dict) and rules:
        observed_rules = {}
        for relative, digest in rules.items():
            observed_rules[relative] = sha256_file(confined_path(root, relative))
            if observed_rules[relative] != digest:
                raise PipelineError("Security rules differ from their reviewed checksums")
    else:
        raise PipelineError("Rules require version-bound builtins or checksummed local files")
    database = item["database_identity"]
    if item["format"] == "trivy":
        strict_keys(database, {"command", "max_age_hours"}, {"command", "max_age_hours"})
        if type(database["max_age_hours"]) not in (int, float) or not 0 < database["max_age_hours"] <= 168:
            raise PipelineError("Trivy database age must be bounded")
        observed = json.loads(run(database["command"], cwd=cwd, timeout=30).stdout)
        db = observed.get("VulnerabilityDB", {})
        try:
            updated = dt.datetime.fromisoformat(db["UpdatedAt"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds() / 3600
        except (KeyError, ValueError, TypeError) as exc:
            raise PipelineError("Trivy database freshness evidence is missing") from exc
        if not 0 <= age <= database["max_age_hours"] or not db.get("Version"):
            raise PipelineError("Trivy vulnerability database is stale or invalid")
        observed_database = {"version": db["Version"], "updated_at": db["UpdatedAt"], "metadata_sha256": fingerprint(db)}
    elif item["format"] in ("npm", "pip-audit") and database == "online":
        if any(part in ("--offline", "--prefer-offline") for part in item["command"]):
            raise PipelineError("Online advisory scan cannot use offline results")
        observed_database = "online-query-at-scan-time"
    elif item["format"] in ("gitleaks", "semgrep", "sarif") and database == "none":
        observed_database = "not-used"
    else:
        raise PipelineError("Scanner database requires an observed freshness contract")
    return observed_rules, observed_database

LEVELS = {"info": 0, "low": 1, "moderate": 2, "medium": 2, "high": 3, "critical": 4, "unknown": 4}
_REFRESH_PROOF = object()


def _candidate_binding(candidate, root):
    from .evidence import IDENTITY_FIELDS, file_records
    if (not isinstance(candidate, dict) or candidate.get("schema") != 1 or candidate.get("status") != "passed"
            or any(key not in candidate for key in IDENTITY_FIELDS)
            or candidate.get("identity") != fingerprint({key: candidate[key] for key in IDENTITY_FIELDS})):
        raise PipelineError("Security scan requires an intact selected candidate identity")
    actual = {group: file_records(root, list(candidate.get(group, {}))) for group in ("inputs", "artifacts")}
    if any(candidate[group] != actual[group] for group in actual):
        raise PipelineError("Security candidate inputs or artifact bytes changed")
    return {"identity": candidate["identity"], "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"], **actual}


class SecurityRefresh:
    """An authenticated separate scan; never reconstructed from a JSON boolean."""

    def __init__(self, receipt, policy_path, policy_sha256, origin, *, _proof=None):
        if _proof is not _REFRESH_PROOF:
            raise PipelineError("Security refresh must be authorized from verified evidence")
        self._receipt = copy.deepcopy(receipt)
        self._receipt_fingerprint = fingerprint(receipt)
        self._policy_path = Path(policy_path)
        self._policy_sha256 = policy_sha256
        self._origin = copy.deepcopy(origin)

    def validate(self, candidate, root):
        if fingerprint(self._receipt) != self._receipt_fingerprint:
            raise PipelineError("Authorized security refresh was modified")
        if sha256_file(self._policy_path) != self._policy_sha256:
            raise PipelineError("Current security refresh policy changed after authorization")
        policy = load_json(self._policy_path)
        require_policy_fresh(self._receipt, policy)
        if self._receipt.get("candidate_binding") != _candidate_binding(candidate, root):
            raise PipelineError("Fresh security evidence does not bind this candidate and its exact retained files")
        return self.summary()

    def summary(self):
        return {"origin": copy.deepcopy(self._origin), "receipt_fingerprint": self._receipt_fingerprint,
                "policy_sha256": self._policy_sha256, "checked_at": self._receipt["checked_at"],
                "candidate_identity": self._receipt["candidate_binding"]["identity"]}


def authorize_refresh(verified, policy_path, expected_policy_sha256, expected_source_sha):
    """Authorize only a fetch_receipt result and an explicitly selected current policy."""
    from .evidence import AuthenticatedReceipt
    if not isinstance(verified, AuthenticatedReceipt):
        raise PipelineError("Security refresh origin must be an authenticated receipt fetch")
    verified.validate_integrity()
    receipt, origin = verified["receipt"], verified["provenance"]
    return _authorize_receipt(receipt, origin, policy_path, expected_policy_sha256, expected_source_sha)


def authorize_step_refresh(receipt_path, expected_receipt_sha256, policy_path, expected_policy_sha256, expected_source_sha):
    """Use only a digest captured from an immutable same-job action's step output.

    The trusted wrapper owns this digest parameter; never load it from product JSON
    or accept a subprocess-provided value. File content alone is not origin proof.
    """
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_REPOSITORY")
            or not str(os.environ.get("GITHUB_RUN_ID", "")).isdigit()
            or os.environ.get("GITHUB_SHA") != expected_source_sha):
        raise PipelineError("Local security refresh requires its trusted current GitHub job context")
    if (not isinstance(expected_receipt_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256)
            or sha256_file(receipt_path) != expected_receipt_sha256):
        raise PipelineError("Local security receipt differs from the trusted step output digest")
    origin = {"kind": "trusted-step", "repository": os.environ["GITHUB_REPOSITORY"],
              "run_id": os.environ["GITHUB_RUN_ID"], "source_sha": expected_source_sha,
              "receipt_sha256": expected_receipt_sha256}
    return _authorize_receipt(load_json(receipt_path), origin, policy_path, expected_policy_sha256, expected_source_sha)


def _authorize_receipt(receipt, origin, policy_path, expected_policy_sha256, expected_source_sha):
    if (not isinstance(expected_policy_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_policy_sha256)
            or sha256_file(policy_path) != expected_policy_sha256
            or not isinstance(expected_source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha)):
        raise PipelineError("Explicit current policy checksum and tested promotion source are required")
    policy = load_json(policy_path)
    require_policy_fresh(receipt, policy)
    counts = receipt.get("counts", {})
    if (receipt.get("schema") != 1 or not isinstance(receipt.get("scanners"), list) or not receipt["scanners"]
            or not isinstance(counts, dict) or type(counts.get("blocking")) is not int or counts["blocking"] != 0):
        raise PipelineError("Security refresh lacks a complete passing scanner verdict")
    if receipt.get("provenance") != {"repository": origin["repository"], "run_id": origin["run_id"], "sha": expected_source_sha}:
        raise PipelineError("Security refresh does not belong to the verified run and tested promotion source")
    assurance = receipt.get("assurance", {})
    if (not isinstance(assurance, dict) or set(assurance) != {"kind", "source_sha", "source_tree", "config_sha256"}
            or assurance.get("kind") != "security" or assurance.get("source_sha") != expected_source_sha
            or not re.fullmatch(r"[0-9a-f]{40}", str(assurance.get("source_tree", "")))
            or assurance.get("config_sha256") != expected_policy_sha256):
        raise PipelineError("Security refresh lacks matching current source and policy assurance")
    binding = receipt.get("candidate_binding", {})
    if (not isinstance(binding, dict) or set(binding) != {"identity", "source_sha", "source_tree", "inputs", "artifacts"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("identity", "")))):
        raise PipelineError("Security refresh is not bound to an immutable candidate")
    return SecurityRefresh(receipt, policy_path, expected_policy_sha256, origin, _proof=_REFRESH_PROOF)


def _finding(identifier, severity, component):
    if not identifier or not isinstance(identifier, str) or severity.lower() not in LEVELS:
        raise PipelineError("Malformed security finding")
    return {"id": identifier, "severity": severity.lower(), "component": str(component)}


def _deduplicate(findings):
    """A repeated finding can increase severity, never hide an earlier blocker."""
    unique = {}
    for finding in findings:
        key = (finding["id"], finding["component"])
        if key not in unique or LEVELS[finding["severity"]] > LEVELS[unique[key]["severity"]]:
            unique[key] = finding
    return list(unique.values())


def _require_redaction(command):
    # Gitleaks' uint flag defaults to 100 only when explicitly present without
    # a value. Prefix matching also accepts --redact=0/false and is insufficient.
    options = command[:command.index("--")] if "--" in command else command
    redaction = [part for part in options if part.startswith("--redact")]
    if not redaction or any(part not in {"--redact", "--redact=100"} for part in redaction):
        raise PipelineError("Gitleaks requires explicit full redaction (--redact or --redact=100)")
    for index, part in enumerate(options[:-1]):
        if part == "--redact" and re.fullmatch(r"(?:[0-9]+|true|false)", options[index + 1], re.I):
            raise PipelineError("Use --redact=100; separated redaction values are ambiguous")


def normalize(kind, report):
    findings = []
    try:
        if kind == "npm":
            if "error" in report or not isinstance(report.get("vulnerabilities"), dict):
                raise PipelineError("npm audit did not return a successful audit schema")
            for name, value in report["vulnerabilities"].items():
                for via in value.get("via", []):
                    if isinstance(via, dict):
                        findings.append(_finding(str(via.get("source") or via.get("url")), via["severity"], name))
                if not value.get("via") or all(isinstance(v, str) for v in value["via"]):
                    findings.append(_finding(name, value["severity"], name))
        elif kind == "pip-audit":
            if not isinstance(report.get("dependencies"), list):
                raise PipelineError("pip-audit dependencies are absent")
            for dep in report["dependencies"]:
                if dep.get("skip_reason"):
                    raise PipelineError("A required dependency was not audited")
                for vuln in dep["vulns"]:
                    # pip-audit's JSON does not guarantee severity: unknown blocks until triaged.
                    findings.append(_finding(vuln["id"], "unknown", dep["name"]))
        elif kind == "gitleaks":
            if not isinstance(report, list):
                raise PipelineError("Invalid gitleaks JSON")
            for leak in report:
                findings.append(_finding(leak["RuleID"] + ":" + str(leak.get("Fingerprint", "")), "critical", leak["File"]))
        elif kind == "trivy":
            if not isinstance(report.get("Results"), list):
                raise PipelineError("Trivy scan results are absent")
            for item in report["Results"]:
                for vuln in item.get("Vulnerabilities", []) or []:
                    findings.append(_finding(vuln["VulnerabilityID"], vuln["Severity"], vuln["PkgName"]))
                for problem in item.get("Misconfigurations", []) or []:
                    if problem.get("Status") != "PASS":
                        findings.append(_finding(problem["ID"], problem["Severity"], item["Target"]))
                for secret in item.get("Secrets", []) or []:
                    findings.append(_finding(secret["RuleID"], "critical", item["Target"]))
        elif kind == "semgrep":
            if report.get("errors") or not isinstance(report.get("results"), list):
                raise PipelineError("Semgrep errors or absent results")
            for result in report["results"]:
                severity = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}.get(result["extra"]["severity"], "unknown")
                findings.append(_finding(result["check_id"], severity, result["path"]))
        elif kind == "sarif":
            if report.get("version") != "2.1.0" or not report.get("runs"):
                raise PipelineError("SARIF lacks a successful analysis run")
            for analysis in report["runs"]:
                if not analysis.get("tool", {}).get("driver", {}).get("name"):
                    raise PipelineError("SARIF has no scanner identity")
                if not isinstance(analysis.get("results"), list):
                    raise PipelineError("SARIF run results are absent or malformed")
                for invocation in analysis.get("invocations", []):
                    if invocation.get("executionSuccessful") is False:
                        raise PipelineError("SARIF reports an unsuccessful scanner")
                rules = {rule.get("id"): rule for rule in analysis["tool"]["driver"].get("rules", [])}
                for finding in analysis["results"]:
                    level = {"error": "high", "warning": "medium", "note": "low", "none": "info"}.get(finding.get("level", "warning"), "unknown")
                    security_score = rules.get(finding.get("ruleId"), {}).get("properties", {}).get("security-severity")
                    if security_score is not None:
                        try:
                            score = float(security_score)
                        except (TypeError, ValueError) as exc:
                            raise PipelineError("Invalid SARIF security severity") from exc
                        if not 0 <= score <= 10:
                            raise PipelineError("Invalid SARIF security severity")
                        level = "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"
                    location = finding.get("locations", [{}])[0].get("physicalLocation", {}).get("artifactLocation", {}).get("uri", "unknown")
                    findings.append(_finding(finding["ruleId"], level, location))
        else:
            raise PipelineError("Unsupported security report format")
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise PipelineError(f"Malformed {kind} security report") from exc
    # No source snippets, matched secrets, raw tool messages or stack traces in the receipt.
    return _deduplicate(findings)


def policy(findings, threshold, exceptions, at=None):
    findings = _deduplicate(findings)
    at = at or dt.datetime.now(dt.timezone.utc)
    if threshold not in LEVELS:
        raise PipelineError("Unknown security severity threshold")
    accepted = {}
    for entry in exceptions:
        strict_keys(entry, {"id", "component", "owner", "reason", "expires_at"},
                    {"id", "component", "owner", "reason", "expires_at"})
        if not all(isinstance(v, str) and v.strip() for v in entry.values()):
            raise PipelineError("Security exception must be fully documented")
        try:
            expiry = dt.datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
            valid = expiry.tzinfo is not None and expiry > at
        except (ValueError, TypeError) as exc:
            raise PipelineError("Malformed security exception expiry") from exc
        if not valid:
            raise PipelineError("Security exception expired")
        accepted[(entry["id"], entry["component"])] = entry
    blocking = [f for f in findings if LEVELS[f["severity"]] >= LEVELS[threshold]
                and (f["id"], f["component"]) not in accepted]
    return {"findings": len(findings), "blocking": len(blocking), "excepted": sum(
        (f["id"], f["component"]) in accepted for f in findings)}


def scan(config, root, *, candidate=None):
    strict_keys(config, {"schema", "scans", "threshold", "exceptions", "max_age_hours"},
                {"schema", "scans", "threshold", "max_age_hours"})
    if config["schema"] != 1 or not isinstance(config["scans"], list) or not config["scans"]:
        raise PipelineError("Security scanning needs at least one real scanner")
    if not isinstance(config["max_age_hours"], (int, float)) or not 0 < config["max_age_hours"] <= 168:
        raise PipelineError("Security freshness must be explicitly bounded to at most seven days")
    binding = None
    if candidate is not None:
        from .evidence import verify_checks
        binding = _candidate_binding(candidate, root)
        verify_checks(candidate, root, historical_security=True)
    all_findings, records = [], []
    seen = set()
    with tempfile.TemporaryDirectory(prefix="shared-ci-security-") as temporary:
        for index, item in enumerate(config["scans"]):
            strict_keys(item, {"id", "format", "command", "version_command", "version", "cwd", "output",
                               "timeout", "allowed_exit_codes", "rules_identity", "database_identity"},
                        {"id", "format", "command", "version_command", "version", "output", "rules_identity", "database_identity"})
            if item["id"] in seen:
                raise PipelineError("Duplicate security scanner identity")
            seen.add(item["id"])
            cwd = confined_path(root, item.get("cwd", "."))
            if item["format"] == "semgrep":
                semgrep_rules(item, root, cwd)  # Refuse remote rule selection before any scanner process.
            version = run(item["version_command"], cwd=cwd, timeout=30).stdout.strip()
            if not isinstance(item["version"], str) or not re.search(r"(?<![\w.])" + re.escape(item["version"]) + r"(?![\w.])", version):
                raise PipelineError("Security scanner version does not match its pin")
            report_path = Path(temporary) / f"{index}.json"
            command = [part.replace("{report}", str(report_path)) for part in item["command"]]
            if item["format"] == "gitleaks":
                _require_redaction(command)
            completed = run(command, cwd=cwd, timeout=item.get("timeout", 900), check=False)
            if completed.returncode not in item.get("allowed_exit_codes", [0, 1]):
                raise PipelineError("Security scanner execution failed")
            if item["output"] == "stdout":
                try:
                    raw = json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    raise PipelineError("Security scanner output is missing/malformed") from exc
            elif item["output"] == "file":
                raw = load_json(report_path)
            else:
                raise PipelineError("Scanner output must be file or stdout")
            findings = normalize(item["format"], raw)
            observed_rules, observed_database = scanner_intelligence(item, root, cwd)
            if completed.returncode and not findings:
                raise PipelineError("Scanner exited unsuccessfully without valid findings")
            all_findings.extend(findings)
            records.append({"id": item["id"], "format": item["format"], "version": item["version"],
                            "rules": observed_rules, "database": observed_database, "finding_count": len(findings)})
    all_findings = _deduplicate(all_findings)
    counts = policy(all_findings, config["threshold"], config.get("exceptions", []))
    result = {"schema": 1, "status": "failed" if counts["blocking"] else "passed", "counts": counts,
              "findings": all_findings,
              "scanners": records, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "max_age_hours": config["max_age_hours"], "policy_identity": fingerprint(config)}
    result["provenance"] = {"repository": os.environ.get("GITHUB_REPOSITORY"),
                            "run_id": os.environ.get("GITHUB_RUN_ID"), "sha": os.environ.get("GITHUB_SHA")}
    if binding is not None:
        if _candidate_binding(candidate, root) != binding:
            raise PipelineError("Security scanner changed the selected candidate")
        verify_checks(candidate, root, historical_security=True)
        result["candidate_binding"] = binding
    return result


def require_fresh(receipt, expected_policy_identity, *, at=None, expected_max_age_hours=None):
    at = at or dt.datetime.now(dt.timezone.utc)
    if receipt.get("status") != "passed" or receipt.get("policy_identity") != expected_policy_identity:
        raise PipelineError("Security evidence is failed or policy has changed")
    maximum = receipt.get("max_age_hours")
    if (type(maximum) not in (int, float) or not 0 < maximum <= 168
            or (expected_max_age_hours is not None and maximum != expected_max_age_hours)):
        raise PipelineError("Security evidence freshness bound differs from selected policy")
    try:
        checked = dt.datetime.fromisoformat(receipt["checked_at"].replace("Z", "+00:00"))
        if checked.tzinfo is None:
            raise ValueError("missing timezone")
        age = (at - checked).total_seconds() / 3600
        fresh = 0 <= age <= maximum
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise PipelineError("Security evidence timestamp is invalid") from exc
    if not fresh:
        raise PipelineError("Security evidence is stale; refresh vulnerability intelligence")
    return True


def require_policy_fresh(receipt, config, *, at=None):
    """Fresh intelligence never extends an exception beyond its explicit expiry."""
    require_fresh(receipt, fingerprint(config), at=at, expected_max_age_hours=config.get("max_age_hours"))
    policy([], config["threshold"], config.get("exceptions", []), at=at)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    receipt = scan(load_json(args.config), args.root)
    atomic_json(args.output, receipt)
    if receipt["status"] != "passed":
        raise PipelineError("Security findings exceed the release policy")


if __name__ == "__main__":
    cli_error(main)
