"""Run declared quality checks with locked tools and verifiable test evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file, strict_keys

PROFILES = {"node", "python", "deno", "flutter", "swift", "php", "shell", "powershell"}
KEYS = {"version", "profile", "working_directory", "toolchain", "lockfiles", "install", "checks",
        "required_checks", "inputs", "immutable_paths", "coverage", "environment_names", "receipt", "timeout"}


def _list(value, name, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise PipelineError(f"{name} must be {'a nonempty' if nonempty else 'a'} list")
    return value


def argv(value):
    _list(value, "command", nonempty=True)
    if not all(isinstance(v, str) and v and "\x00" not in v for v in value):
        raise PipelineError("command must contain nonempty string arguments")
    command = Path(value[0]).name.lower()
    if command in {"echo", "true", "false", "printf", "exit"}:
        raise PipelineError("placeholder commands do not constitute quality checks")
    if command in {"sh", "bash", "zsh", "cmd", "pwsh", "powershell"} and any(
        v.lower() in {"-c", "-command", "/c", "-encodedcommand"} for v in value[1:]
    ):
        raise PipelineError("use an inspected script file, not an inline shell command")
    if any(v.lower() in {"--passwithnotests", "--passwithnotests=true", "--allow-no-tests"} for v in value):
        raise PipelineError("empty-test success flags are prohibited")
    return value


def _positive(value, name, maximum=3600):
    if type(value) is not int or not 1 <= value <= maximum:
        raise PipelineError(f"{name} must be an integer between 1 and {maximum}")
    return value


def snapshot(root, paths):
    """Hash explicit files/directories, refusing symlinks or paths outside root."""
    root = Path(root).resolve()
    result = {}
    for rel in _list(paths, "input paths", nonempty=True):
        if not isinstance(rel, str):
            raise PipelineError("input paths must be strings")
        path = confined_path(root, rel)
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in files:
            if item.is_symlink():
                raise PipelineError(f"input symlink is not allowed: {item.relative_to(root)}")
            if item.is_file():
                key = item.relative_to(root).as_posix()
                confined_path(root, key)
                result[key] = sha256_file(item)
    if not result:
        raise PipelineError("input selection contains no files")
    return result


def input_digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _report_path(root, spec):
    if not isinstance(spec, dict):
        raise PipelineError("test_report must be an object")
    strict_keys(spec, {"path", "format"}, required=("path", "format"))
    if spec["format"] not in {"junit", "flutter-machine"}:
        raise PipelineError("test report format must be junit or flutter-machine")
    return confined_path(root, spec["path"], must_exist=False)


def parse_test_report(path, format_name):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise PipelineError("required test report is absent or empty")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise PipelineError("test report exceeds 32 MiB")
    try:
        if format_name == "junit":
            text = path.read_text(encoding="utf-8")
            if "<!DOCTYPE" in text or "<!ENTITY" in text:
                raise PipelineError("XML entities are not allowed in test reports")
            doc = ET.fromstring(text)
            if doc.tag not in {"testsuite", "testsuites"}:
                raise PipelineError("unrecognized JUnit report root")
            cases = list(doc.iter("testcase"))
            skipped = sum(case.find("skipped") is not None for case in cases)
            failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
            # Fail also on suite-level infrastructure errors, including inconsistent producer reports.
            for suite in doc.iter("testsuite"):
                for attribute in ("tests", "failures", "errors", "skipped"):
                    count = int(suite.get(attribute, "0"))
                    if count < 0:
                        raise PipelineError("negative JUnit count")
                    if attribute in {"failures", "errors"}:
                        failed += count
            executed = len(cases) - skipped
        elif format_name == "flutter-machine":
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not all(isinstance(event, dict) for event in events):
                raise PipelineError("invalid Flutter test event")
            done = [event for event in events if event.get("type") == "done"]
            if len(done) != 1 or done[0].get("success") is not True:
                raise PipelineError("Flutter report has no successful completion event")
            starts = {event["test"]["id"]: event["test"] for event in events
                      if event.get("type") == "testStart" and isinstance(event.get("test"), dict)}
            # Ignore the framework's hidden loading/setup tests, not actual skipped user cases.
            ends = [event for event in events if event.get("type") == "testDone"
                    and event.get("testID") in starts and starts[event["testID"]].get("hidden") is not True]
            ended = {event["testID"] for event in ends}
            expected = {key for key, test in starts.items() if test.get("hidden") is not True}
            if ended != expected or len(ended) != len(ends):
                raise PipelineError("Flutter report has missing or duplicate test completions")
            skipped = sum(event.get("skipped") is True for event in ends)
            executed = len(ends) - skipped
            failed = sum(event.get("result") != "success" and event.get("skipped") is not True for event in ends)
            failed += sum(event.get("type") == "error" for event in events)
        else:
            raise PipelineError("unsupported test report format")
    except (ValueError, TypeError, KeyError, ET.ParseError, UnicodeError) as exc:
        raise PipelineError("malformed test report") from exc
    if executed < 1:
        raise PipelineError("test report contains no executed tests")
    if failed:
        raise PipelineError("test report contains failed tests or errors")
    return {"executed": executed, "skipped": skipped, "failed": 0, "sha256": sha256_file(path)}


def _locked_install(profile, install, locks):
    if install is None:
        if profile in {"node", "python", "deno", "flutter"}:
            raise PipelineError(f"{profile} requires a locked installation command")
        return
    command = argv(install)
    name = Path(command[0]).name
    if any(option in command for option in {"--no-lock", "--no-package-lock", "--package-lock=false", "--immutable=false", "--no-immutable", "--frozen=false", "--no-frozen-lockfile", "--frozen-lockfile=false", "--no-enforce-lockfile"}):
        raise PipelineError("installation flags cannot disable lockfile enforcement")
    lock_names = {Path(p).name for p in locks}
    if profile == "node":
        valid = ((name == "npm" and command[1:2] == ["ci"] and bool(lock_names & {"package-lock.json", "npm-shrinkwrap.json"}))
                 or (name == "pnpm" and command[1:2] == ["install"] and "--frozen-lockfile" in command and "pnpm-lock.yaml" in lock_names)
                 or (name == "yarn" and command[1:2] == ["install"] and bool(set(command) & {"--immutable", "--frozen-lockfile"}) and "yarn.lock" in lock_names))
    elif profile == "python":
        valid = ((name == "uv" and command[1:2] == ["sync"] and bool(set(command) & {"--frozen", "--locked"}) and "uv.lock" in lock_names)
                 or (name in {"pip", "pip3", "python", "python3"} and "install" in command and "--require-hashes" in command and bool(locks)))
    elif profile == "deno":
        valid = name == "deno" and command[1:2] == ["install"] and bool(set(command) & {"--frozen", "--frozen-lockfile"}) and "deno.lock" in lock_names
    elif profile == "flutter":
        valid = name == "flutter" and command[1:3] == ["pub", "get"] and "--enforce-lockfile" in command and "pubspec.lock" in lock_names
    else:
        valid = False
    if not valid:
        raise PipelineError(f"installation for {profile} must use its supported locked mode")


def _check_npm_script(command, cwd):
    name = Path(command[0]).name
    if name not in {"npm", "pnpm", "yarn"}:
        return
    args = command[1:]
    script = args[1] if len(args) > 1 and args[0] == "run" else ("test" if args[:1] == ["test"] else None)
    if script is None:
        return
    manifest = cwd / "package.json"
    if not manifest.is_file():
        raise PipelineError("package.json is required for a package script")
    scripts = load_json(manifest).get("scripts", {})
    value = scripts.get(script)
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"required package script is missing: {script}")
    if re.fullmatch(r"\s*(?:echo\b[^;&|]*|printf\b[^;&|]*|true|exit\s+0)\s*", value):
        raise PipelineError(f"placeholder package script is not a check: {script}")
    if re.search(r"\|\|\s*(?:true\b|exit\s+0\b)", value) or "--passWithNoTests" in value:
        raise PipelineError(f"package script masks failures: {script}")


def execute(config, root, *, config_hash=None):
    if not isinstance(config, dict):
        raise PipelineError("quality config must be an object")
    strict_keys(config, KEYS, required=("version", "profile", "toolchain", "checks", "required_checks", "inputs"))
    if type(config["version"]) is not int or config["version"] != 1 or not isinstance(config["profile"], str) or config["profile"] not in PROFILES:
        raise PipelineError("unsupported quality version or profile")
    root = Path(root).resolve()
    cwd = confined_path(root, config.get("working_directory", "."))
    if not cwd.is_dir():
        raise PipelineError("working_directory must be a directory")
    timeout = _positive(config.get("timeout", 900), "timeout")
    environment = _list(config.get("environment_names", []), "environment_names")
    if not all(isinstance(n, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", n) for n in environment):
        raise PipelineError("invalid environment variable name")
    # This lane is credential-free. Credentialed scanners/evaluations have separate modules.
    if any(re.search(r"(?:TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|PRIVATE_KEY)", n) for n in environment):
        raise PipelineError("quality checks must not receive credential variables")
    inputs = snapshot(root, config["inputs"])
    immutable_paths = config.get("immutable_paths", config["inputs"])
    immutable = snapshot(root, immutable_paths)
    locks = _list(config.get("lockfiles", []), "lockfiles")
    lock_hashes = {name: sha256_file(confined_path(root, name)) for name in locks}
    _locked_install(config["profile"], config.get("install"), locks)
    tools = []
    for tool in _list(config["toolchain"], "toolchain", nonempty=True):
        strict_keys(tool, {"command", "version"}, required=("command", "version"))
        argv(tool["command"])
    tool_commands = [tool["command"] for tool in config["toolchain"]]
    tool_names = {Path(command[0]).name for command in tool_commands if command and isinstance(command[0], str)}
    primary = {"node": {"node"}, "python": {"python", "python3"}, "deno": {"deno"},
               "flutter": {"flutter"}, "swift": {"swiftlint"}, "php": {"php"},
               "shell": {"bash", "shellcheck"}, "powershell": {"pwsh", "powershell"}}
    if not tool_names & primary[config["profile"]]:
        raise PipelineError("the profile's primary tool must have a pinned version probe")
    if config["profile"] == "node" and Path(config["install"][0]).name not in tool_names:
        raise PipelineError("the Node package manager must have a pinned version probe")
    for tool in _list(config["toolchain"], "toolchain", nonempty=True):
        strict_keys(tool, {"command", "version"}, required=("command", "version"))
        version = tool["version"]
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9.-]+)?", version):
            raise PipelineError("tool versions must be exact numeric versions")
        command = argv(tool["command"])
        output = run(command, cwd=cwd, env_names=environment, timeout=timeout)
        combined = output.stdout + output.stderr
        if not re.search(r"(?<![\w.])v?" + re.escape(version) + r"(?![\w.])", combined):
            raise PipelineError(f"installed tool does not match required version {version}")
        tools.append({"command": command, "version": version})
    if config.get("install") is not None:
        run(argv(config["install"]), cwd=cwd, env_names=environment, timeout=timeout)
    required = _list(config["required_checks"], "required_checks", nonempty=True)
    checks = _list(config["checks"], "checks", nonempty=True)
    names = [check.get("name") for check in checks if isinstance(check, dict)]
    if len(names) != len(checks) or not all(isinstance(n, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", n) for n in names) or len(set(names)) != len(names):
        raise PipelineError("checks need unique simple names")
    if not all(isinstance(name, str) and name in names for name in required):
        raise PipelineError("a required check is missing")
    results = []
    for check in checks:
        strict_keys(check, {"name", "command", "kind", "test_report", "timeout"}, required=("name", "command", "kind"))
        kind = check["kind"]
        if kind not in {"format", "lint", "typecheck", "test", "build", "validate"}:
            raise PipelineError("unsupported check kind")
        if (kind == "test") != ("test_report" in check):
            raise PipelineError("test checks require test_report; other checks cannot claim test evidence")
        command = argv(check["command"])
        _check_npm_script(command, cwd)
        report = _report_path(root, check["test_report"]) if kind == "test" else None
        if report is not None and (report.relative_to(root).as_posix() in inputs or report.relative_to(root).as_posix() in lock_hashes):
            raise PipelineError("test report cannot overwrite an input or lockfile")
        if report is not None:
            report.parent.mkdir(parents=True, exist_ok=True)
            if report.exists():
                if not report.is_file():
                    raise PipelineError("test report path must be a regular file")
                report.unlink()  # A prior run's results can never prove this execution.
        output = run(command, cwd=cwd, env_names=environment, timeout=_positive(check.get("timeout", timeout), "check timeout"))
        if report is not None and check["test_report"]["format"] == "flutter-machine":
            if report.is_symlink():
                raise PipelineError("test report cannot be a symlink")
            report.write_text(output.stdout, encoding="utf-8")
        result = {"name": check["name"], "kind": kind, "status": "success"}
        if report is not None:
            result["test_summary"] = parse_test_report(report, check["test_report"]["format"])
        if snapshot(root, immutable_paths) != immutable:
            raise PipelineError("quality check changed declared immutable source or configuration")
        results.append(result)
    if any(sha256_file(confined_path(root, name)) != digest for name, digest in lock_hashes.items()):
        raise PipelineError("installation/checks changed a declared lockfile")
    coverage = {}
    for name in _list(config.get("coverage", []), "coverage"):
        path = confined_path(root, name)
        if not path.is_file() or path.stat().st_size == 0:
            raise PipelineError("coverage report is missing or empty")
        coverage[name] = sha256_file(path)
    receipt = {"schema_version": 1, "kind": "quality", "status": "success", "profile": config["profile"],
               "created_at": datetime.now(timezone.utc).isoformat(), "input_digest": input_digest(inputs),
               "inputs": inputs, "input_paths": config["inputs"], "lockfiles": lock_hashes, "toolchain": tools, "checks": results,
               "coverage": coverage, "config_sha256": config_hash}
    import os
    receipt["provenance"] = {"repository": os.environ.get("GITHUB_REPOSITORY"), "run_id": os.environ.get("GITHUB_RUN_ID"),
                             "sha": os.environ.get("GITHUB_SHA")}
    if config.get("receipt"):
        output_path = confined_path(root, config["receipt"], must_exist=False)
        if output_path.relative_to(root).as_posix() in set(inputs) | set(coverage) | set(lock_hashes):
            raise PipelineError("receipt cannot overwrite source, locks or coverage")
        atomic_json(output_path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        config_path = confined_path(Path(args.root).resolve(), args.config)
        receipt = execute(load_json(config_path), args.root, config_hash=sha256_file(config_path))
        print(json.dumps({"status": receipt["status"], "profile": receipt["profile"], "checks": len(receipt["checks"])}))
    except (PipelineError, OSError, ValueError) as exc:
        print(f"quality: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
