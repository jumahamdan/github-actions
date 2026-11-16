"""Run declared scanners and enforce fresh, explicit findings policy."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import tempfile
from pathlib import Path

from .common import PipelineError, atomic_json, cli_error, confined_path, fingerprint, load_json, run, sha256_file, strict_keys


def scanner_intelligence(item, root, cwd):
    rules = item["rules_identity"]
    if rules == "builtin:" + item["version"]:
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


def _finding(identifier, severity, component):
    if not identifier or not isinstance(identifier, str) or severity.lower() not in LEVELS:
        raise PipelineError("Malformed security finding")
    return {"id": identifier, "severity": severity.lower(), "component": str(component)}


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
                for invocation in analysis.get("invocations", []):
                    if invocation.get("executionSuccessful") is False:
                        raise PipelineError("SARIF reports an unsuccessful scanner")
                rules = {rule.get("id"): rule for rule in analysis["tool"]["driver"].get("rules", [])}
                for finding in analysis.get("results", []):
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
    except (KeyError, TypeError, AttributeError) as exc:
        raise PipelineError(f"Malformed {kind} security report") from exc
    # No source snippets, matched secrets, raw tool messages or stack traces in the receipt.
    return list({(f["id"], f["component"]): f for f in findings}.values())


def policy(findings, threshold, exceptions, at=None):
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


def scan(config, root):
    strict_keys(config, {"schema", "scans", "threshold", "exceptions", "max_age_hours"},
                {"schema", "scans", "threshold", "max_age_hours"})
    if config["schema"] != 1 or not isinstance(config["scans"], list) or not config["scans"]:
        raise PipelineError("Security scanning needs at least one real scanner")
    if not isinstance(config["max_age_hours"], (int, float)) or not 0 < config["max_age_hours"] <= 168:
        raise PipelineError("Security freshness must be explicitly bounded to at most seven days")
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
            version = run(item["version_command"], cwd=cwd, timeout=30).stdout.strip()
            if not isinstance(item["version"], str) or not re.search(r"(?<![\w.])" + re.escape(item["version"]) + r"(?![\w.])", version):
                raise PipelineError("Security scanner version does not match its pin")
            report_path = Path(temporary) / f"{index}.json"
            command = [part.replace("{report}", str(report_path)) for part in item["command"]]
            if item["format"] == "gitleaks" and not any(part.startswith("--redact") for part in command):
                raise PipelineError("Gitleaks must redact findings at source")
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
    counts = policy(all_findings, config["threshold"], config.get("exceptions", []))
    result = {"schema": 1, "status": "failed" if counts["blocking"] else "passed", "counts": counts,
              "findings": all_findings,
              "scanners": records, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "max_age_hours": config["max_age_hours"], "policy_identity": fingerprint(config)}
    return result


def require_fresh(receipt, expected_policy_identity, *, at=None):
    at = at or dt.datetime.now(dt.timezone.utc)
    if receipt.get("status") != "passed" or receipt.get("policy_identity") != expected_policy_identity:
        raise PipelineError("Security evidence is failed or policy has changed")
    try:
        checked = dt.datetime.fromisoformat(receipt["checked_at"])
        age = (at - checked).total_seconds() / 3600
        fresh = 0 <= age <= receipt["max_age_hours"]
    except (KeyError, ValueError, TypeError) as exc:
        raise PipelineError("Security evidence timestamp is invalid") from exc
    if not fresh:
        raise PipelineError("Security evidence is stale; refresh vulnerability intelligence")
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
