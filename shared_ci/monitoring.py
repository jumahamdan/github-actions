"""Upload required symbols for a verified candidate without changing SDKs or artifact bytes."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import sys
import tempfile
import urllib.parse
import uuid
import zipfile
from pathlib import Path

from .common import PipelineError, atomic_json, cli_error, confined_path, load_json, run, sha256_file, strict_keys, trusted_context
from .stores import HEX, bounded, env_secret, identifier, load_candidate


def pinned_tool(config, cwd, extra):
    strict_keys(config, {"prefix", "version"}, {"prefix", "version"})
    prefix = config["prefix"]
    if not isinstance(prefix, list) or not prefix or any(not isinstance(x, str) or not x or "\x00" in x for x in prefix):
        raise PipelineError("Tool prefix must be an argument array")
    if not isinstance(config["version"], str) or not config["version"].strip():
        raise PipelineError("Exact tool version output is required")
    output = run(prefix + ["--version"], cwd=cwd, extra_env=extra, timeout=60)
    if output.stdout.strip() != config["version"]:
        raise PipelineError("Monitoring tool version differs from its pin")
    return prefix


def retained(root, candidate, relative):
    path = confined_path(root, relative)
    if relative not in candidate["artifacts"] or not path.is_file():
        raise PipelineError("Required symbol or binary is not a retained candidate artifact")
    return path


def normalize_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise PipelineError("Invalid debug UUID") from None


def native_symbols(config, root, candidate, inspected):
    entries = inspected.get("debug_symbols")
    selected = config.get("symbols")
    if not isinstance(entries, list) or not entries or not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
        raise PipelineError("Native symbols require retained UUID/architecture inspection")
    ids, paths, covered = set(), [], set()
    for entry in entries:
        if entry.get("symbol_path") not in selected:
            continue
        binary = retained(root, candidate, entry["binary_path"])
        symbol = retained(root, candidate, entry["symbol_path"])
        if sha256_file(binary) != entry.get("binary_sha256") or sha256_file(symbol) != entry.get("symbol_sha256") or not entry.get("arch"):
            raise PipelineError("Native binary/symbol identity changed since UUID inspection")
        ids.add(normalize_uuid(entry.get("uuid")))
        paths.append(symbol)
        covered.add(entry["symbol_path"])
    if covered != set(selected):
        raise PipelineError("Every required native symbol must have a matching binary UUID")
    # All target UUIDs must upload; silently selecting only the host app loses widget crashes.
    if set(selected) != {entry.get("symbol_path") for entry in entries}:
        raise PipelineError("Native upload omitted an inspected target's symbols")
    return sorted(set(paths)), sorted(ids)


def archive_member(archive, member):
    if member not in archive.namelist() or archive.namelist().count(member) != 1:
        raise PipelineError("Packaged symbol identity entry is missing or duplicated")
    entry = archive.getinfo(member)
    if entry.file_size > 512 * 1024 * 1024:
        raise PipelineError("Packaged symbol identity entry exceeds limit")
    return archive.read(member)


def android_symbols(config, root, candidate, inspected, temporary):
    artifact = config.get("artifact")
    aab = retained(root, candidate, artifact)
    if artifact not in inspected["artifacts"] or aab.suffix != ".aab":
        raise PipelineError("Android symbols require the inspected AAB")
    selected = config.get("symbols")
    if not isinstance(selected, list) or not selected:
        raise PipelineError("Required symbol list is empty")
    paths = [retained(root, candidate, p) for p in selected]
    try:
        with zipfile.ZipFile(aab) as archive:
            if config["operation"].endswith("r8"):
                if len(paths) != 1 or archive_member(archive, "BUNDLE-METADATA/com.android.tools.build.obfuscation/proguard.map") != paths[0].read_bytes():
                    raise PipelineError("R8 mapping does not match the mapping retained inside the AAB")
                return paths, archive_member(archive, "base/assets/sentry-debug-meta.properties").decode() if config["operation"] == "sentry-r8" else None
            pairs = config.get("dart_libraries")
            if not isinstance(pairs, dict) or set(pairs) != set(selected):
                raise PipelineError("Each Dart symbol file needs its packaged libapp.so entry")
            for index, (relative, member) in enumerate(pairs.items()):
                if not re.fullmatch(r"base/lib/(arm64-v8a|armeabi-v7a|x86_64)/libapp\.so", str(member)):
                    raise PipelineError("Invalid packaged Dart library")
                binary = Path(temporary) / f"libapp-{index}.so"
                binary.write_bytes(archive_member(archive, member))
                ids = []
                for path in (binary, retained(root, candidate, relative)):
                    result = run(["readelf", "--notes", str(path)], cwd=temporary, timeout=60)
                    matches = re.findall(r"Build ID:\s*([0-9a-fA-F]+)", result.stdout)
                    if len(matches) != 1:
                        raise PipelineError("Dart ELF file has no unique GNU build ID")
                    ids.append(matches[0].lower())
                if ids[0] != ids[1]:
                    raise PipelineError("Dart symbols belong to another packaged binary")
    except (zipfile.BadZipFile, UnicodeError):
        raise PipelineError("Malformed Android symbol archive") from None
    return paths, None


def source_maps(config, root, candidate):
    pairs = config.get("source_maps")
    if not isinstance(pairs, list) or not pairs:
        raise PipelineError("Required source map pairs are missing")
    paths, ids = [], set()
    for pair in pairs:
        strict_keys(pair, {"javascript", "map"}, {"javascript", "map"})
        js, mapping = retained(root, candidate, pair["javascript"]), retained(root, candidate, pair["map"])
        value = load_json(mapping)
        debug_id = normalize_uuid(value.get("debug_id", value.get("debugId")))
        if (value.get("version") != 3 or not value.get("mappings") or not value.get("sources")
                or debug_id not in js.read_text().lower() or debug_id in ids):
            raise PipelineError("Source map is invalid, duplicated, or does not match injected JavaScript debug ID")
        ids.add(debug_id)
        paths.extend([js, mapping])
    return paths


def sentry(config, root, candidate, inspected, temporary):
    settings = config["sentry"]
    strict_keys(settings, {"org", "project", "token_env", "url", "release", "environment", "dist", "finalize", "deployment_receipt", "deployment_receipt_sha256"},
                {"org", "project", "token_env", "release", "environment", "dist"})
    for key in ("org", "project", "environment"):
        identifier(settings[key], key)
    expected_release = inspected["application"]["id"] + "@" + candidate["version"] + "+" + candidate["build"]
    if settings["release"] != expected_release or settings["dist"] != candidate["build"]:
        raise PipelineError("Sentry release/dist must identify the selected app version and build")
    url = settings.get("url", "https://sentry.io/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PipelineError("Sentry endpoint must be explicit HTTPS without credentials")
    clean = {"HOME": temporary, "SENTRY_LOAD_DOTENV": "0", "SENTRY_DISABLE_UPDATE_CHECK": "true", "SENTRY_NO_PROGRESS_BAR": "1", "SENTRY_LOG_LEVEL": "warn"}
    prefix = pinned_tool(config["tool"], temporary, clean)
    extra = dict(clean, SENTRY_AUTH_TOKEN=env_secret(settings["token_env"]), SENTRY_URL=url,
                 SENTRY_ORG=settings["org"], SENTRY_PROJECT=settings["project"], SENTRY_RELEASE=settings["release"], SENTRY_DIST=settings["dist"])
    operation = config["operation"]
    timeout = config.get("timeout", 900)
    ids, paths = [], []
    if operation == "sentry-native":
        paths, ids = native_symbols(config, root, candidate, inspected)
    elif operation in ("sentry-dart", "sentry-r8"):
        paths, properties = android_symbols(config, root, candidate, inspected, temporary)
        if operation == "sentry-r8":
            result = run(prefix + ["proguard", "uuid", str(paths[0])], cwd=temporary, extra_env=clean, timeout=60)
            debug_id = normalize_uuid(result.stdout.strip())
            embedded = re.search(r"(?m)^io\.sentry\.ProguardUuids\s*=\s*([^\r\n]+)", properties)
            if not embedded or debug_id not in [normalize_uuid(x.strip()) for x in embedded[1].split("|")]:
                raise PipelineError("Sentry mapping UUID is not embedded in the tested AAB")
            run(prefix + ["proguard", "upload", "--require-one", str(paths[0])], cwd=temporary, extra_env=extra, timeout=timeout)
        else:
            for path in paths:
                result = run(prefix + ["debug-files", "check", "--json", str(path)], cwd=temporary, extra_env=clean, timeout=60)
                try:
                    value = json.loads(result.stdout)
                    variants = value["variants"]
                    if value.get("is_usable") is not True or not variants:
                        raise ValueError()
                    ids.extend(normalize_uuid(v["debug_id"]) for v in variants)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    raise PipelineError("Sentry could not identify required Dart debug files") from None
    elif operation == "sentry-sourcemaps":
        paths = source_maps(config, root, candidate)
        run(prefix + ["sourcemaps", "upload", "--release", settings["release"], "--dist", settings["dist"], "--validate", "--strict", "--wait", "--no-rewrite"] + [str(p) for p in paths],
            cwd=temporary, extra_env=extra, timeout=timeout)
    elif operation == "sentry-release":
        run(prefix + ["releases", "new", settings["release"]], cwd=temporary, extra_env=extra, timeout=timeout)
        if settings.get("finalize"):
            run(prefix + ["releases", "finalize", settings["release"]], cwd=temporary, extra_env=extra, timeout=timeout)
    elif operation == "sentry-deploy":
        proof = confined_path(root, settings.get("deployment_receipt", ""))
        deployed = load_json(proof)
        if (sha256_file(proof) != settings.get("deployment_receipt_sha256") or deployed.get("status") != "passed"
                or deployed.get("candidate_identity") != candidate["identity"] or deployed.get("environment") != settings["environment"]):
            raise PipelineError("Sentry deploy marker requires matching successful deployment evidence")
        run(prefix + ["deploys", "new", "--release", settings["release"], "-e", settings["environment"]], cwd=temporary, extra_env=extra, timeout=timeout)
    if operation in ("sentry-native", "sentry-dart"):
        args = ["debug-files", "upload", "--wait", "--require-all"]
        for debug_id in sorted(set(ids)):
            args += ["--id", debug_id]
        run(prefix + args + [str(p) for p in paths], cwd=temporary, extra_env=extra, timeout=timeout)
    return {"provider": "sentry", "release": settings["release"], "environment": settings["environment"], "dist": settings["dist"],
            "uploaded_artifacts": [str(p.relative_to(root)) for p in paths], "debug_ids": sorted(set(ids))}


def firebase_app(settings, inspected, root, candidate):
    app = settings["app_id"]
    platform = "ios" if inspected["kind"] == "apple" else "android"
    if not re.fullmatch(r"1:[0-9]+:" + platform + r":[0-9a-f]+", str(app)):
        raise PipelineError("Invalid Firebase app ID for inspected platform")
    path = confined_path(root, settings["app_config"])
    # App configuration has public identifiers, but must remain bound to selected build inputs.
    if settings["app_config"] not in candidate["inputs"] and settings["app_config"] not in candidate["artifacts"]:
        raise PipelineError("Firebase app configuration is not candidate-bound")
    if platform == "ios":
        try:
            value = plistlib.loads(path.read_bytes())
        except (plistlib.InvalidFileException, ValueError):
            raise PipelineError("Malformed Firebase Apple app configuration") from None
        if value.get("GOOGLE_APP_ID") != app or value.get("BUNDLE_ID") != inspected["application"]["id"]:
            raise PipelineError("Firebase Apple app identity mismatch")
    else:
        value = load_json(path)
        matches = [c for c in value.get("client", []) if c.get("client_info", {}).get("mobilesdk_app_id") == app
                   and c.get("client_info", {}).get("android_client_info", {}).get("package_name") == inspected["application"]["id"]]
        if len(matches) != 1:
            raise PipelineError("Firebase Android app identity mismatch")
    return path


def crashlytics(config, root, candidate, inspected, temporary):
    settings = config["firebase"]
    strict_keys(settings, {"app_id", "app_config", "credentials_env", "script", "script_sha256", "gradle"}, {"app_id", "app_config"})
    app_config = firebase_app(settings, inspected, root, candidate)
    operation = config["operation"]
    clean = {"HOME": temporary, "XDG_CONFIG_HOME": temporary, "FIREBASE_CLI_DISABLE_UPDATE_CHECK": "true"}
    timeout = config.get("timeout", 900)
    if operation == "crashlytics-apple":
        if inspected["kind"] != "apple":
            raise PipelineError("Crashlytics Apple requires Apple archive inspection")
        paths, ids = native_symbols(config, root, candidate, inspected)
        script = confined_path(root, settings.get("script", ""))
        if not HEX.fullmatch(str(settings.get("script_sha256", ""))) or sha256_file(script) != settings["script_sha256"]:
            raise PipelineError("Firebase upload-symbols script does not match pinned SDK bytes")
        # SDK's upload-symbols uses its plist app configuration; no global Firebase login.
        bundles = []
        for path in paths:
            dsym = next((p for p in path.parents if p.suffix == ".dSYM" and p.is_relative_to(root)), None)
            if dsym is None:
                raise PipelineError("Apple symbol file must be inside its retained .dSYM bundle")
            files = [p for p in dsym.rglob("*") if p.is_file()]
            if any(str(p.relative_to(root)) not in candidate["artifacts"] for p in files):
                raise PipelineError("All dSYM bundle files must be retained before upload")
            bundles.append(dsym)
        run([str(script), "-gsp", str(app_config), "-p", "ios"] + [str(p) for p in sorted(set(bundles))], cwd=temporary, extra_env=clean, timeout=timeout)
    else:
        if inspected["kind"] != "android":
            raise PipelineError("Android Crashlytics requires Android inspection")
        paths, _ = android_symbols(config, root, candidate, inspected, temporary)
        if operation == "crashlytics-dart":
            prefix = pinned_tool(config["tool"], temporary, clean)
            credential = env_secret(settings.get("credentials_env"))
            if not Path(credential).is_file():
                raise PipelineError("Firebase ADC credential file is missing")
            extra = dict(clean, GOOGLE_APPLICATION_CREDENTIALS=credential)
            # Copy only the selected files: Firebase scans supplied directories recursively.
            symbols = Path(temporary) / "symbols"
            symbols.mkdir()
            for index, path in enumerate(paths):
                shutil.copyfile(path, symbols / f"{index}-{path.name}")
            run(prefix + ["crashlytics:symbols:upload", "--non-interactive", "--app", settings["app_id"], str(symbols)], cwd=temporary, extra_env=extra, timeout=timeout)
        else:
            gradle = settings.get("gradle")
            strict_keys(gradle, {"wrapper", "wrapper_sha256", "project", "task", "exclude_tasks", "version", "build_inputs", "cache_env"},
                        {"wrapper", "wrapper_sha256", "project", "task", "exclude_tasks", "version", "build_inputs", "cache_env"})
            wrapper = confined_path(root, gradle["wrapper"])
            if sha256_file(wrapper) != gradle["wrapper_sha256"]:
                raise PipelineError("Gradle wrapper hash mismatch")
            project = confined_path(root, gradle["project"])
            if not re.fullmatch(r":[A-Za-z0-9_]+:uploadCrashlyticsMappingFile[A-Za-z0-9_]+", gradle["task"]):
                raise PipelineError("Expected a specific Crashlytics mapping upload task")
            if not isinstance(gradle["build_inputs"], list) or not gradle["build_inputs"]:
                raise PipelineError("Pinned Gradle plugin/dependency/wrapper inputs are required")
            if any(p not in candidate["inputs"] for p in gradle["build_inputs"]):
                raise PipelineError("Gradle upload configuration is not candidate-bound")
            cache = Path(env_secret(gradle["cache_env"])).resolve()
            if not cache.is_dir():
                raise PipelineError("Retained Gradle dependency cache is missing")
            clean["GRADLE_USER_HOME"] = str(cache)
            prefix = pinned_tool({"prefix": [str(wrapper)], "version": gradle["version"]}, project, clean)
            args = ["--offline", "--no-daemon", "--console=plain", "--rerun-tasks", gradle["task"]]
            for task in gradle["exclude_tasks"]:
                if not re.fullmatch(r":[A-Za-z0-9_:]+", str(task)) or task == gradle["task"]:
                    raise PipelineError("Invalid excluded Gradle task")
                args += ["-x", task]
            planned = run(prefix + args + ["--dry-run"], cwd=project, extra_env=clean, timeout=120)
            tasks = re.findall(r"(?m)^(:\S+)\s+SKIPPED\s*$", planned.stdout)
            if tasks != [gradle["task"]]:
                raise PipelineError("Crashlytics upload would execute other tasks or no upload; retain build outputs and exclude dependencies")
            # Gradle plugin uses google-services configuration; dependencies are cached, no rebuild.
            result = run(prefix + args, cwd=project, extra_env=clean, timeout=timeout)
            task_lines = re.findall(r"(?m)^> Task (:\S+)([^\n]*)$", result.stdout)
            if task_lines != [(gradle["task"], "")]:
                raise PipelineError("Crashlytics mapping task was skipped, rebuilt other tasks, or did not execute")
    return {"provider": "crashlytics", "firebase_app_id": settings["app_id"],
            "uploaded_artifacts": [str(p.relative_to(root)) for p in paths]}


def execution_context(config, root):
    if config.get("execution_context", "github") == "github":
        trusted_context(config["allowed_refs"])
        return
    if config.get("execution_context") != "xcode-cloud" or config["operation"] not in ("sentry-native", "crashlytics-apple"):
        raise PipelineError("Xcode Cloud monitoring supports native archive symbol uploads only")
    refs = config["allowed_refs"]
    if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", ref) for ref in refs)
            or os.environ.get("CI_XCODE_CLOUD", "").lower() != "true" or sys.platform != "darwin"
            or os.environ.get("CI_XCODEBUILD_ACTION") != "archive" or os.environ.get("CI_XCODEBUILD_EXIT_CODE") != "0"
            or os.environ.get("CI_START_CONDITION") not in ("push", "manual", "manual_rebuild")
            or os.environ.get("CI_GIT_REF") not in refs or os.environ.get("CI_COMMIT") != config["expected_source_sha"]
            or not os.environ.get("CI_ARCHIVE_PATH") or Path(os.environ["CI_ARCHIVE_PATH"]).resolve() != Path(root).resolve()):
        raise PipelineError("Native monitoring requires the successful authorized Xcode Cloud archive on its Apple host")


def execute(config, root):
    required = {"schema", "operation", "candidate", "inspection", "expected_identity", "expected_source_sha", "allowed_refs"}
    strict_keys(config, required | {"tool", "sentry", "firebase", "symbols", "artifact", "dart_libraries", "source_maps", "timeout", "execution_context"}, required)
    operations = {"sentry-native", "sentry-dart", "sentry-r8", "sentry-sourcemaps", "sentry-release", "sentry-deploy",
                  "crashlytics-apple", "crashlytics-dart", "crashlytics-r8"}
    if config["schema"] != 1 or config["operation"] not in operations:
        raise PipelineError("Unsupported monitoring operation")
    bounded(config.get("timeout", 900), 1, 3600, "monitoring timeout")
    execution_context(config, root)
    root = Path(root).resolve()
    candidate, inspected = load_candidate(config, root, {"apple", "android", "web"})
    if config.get("execution_context") == "xcode-cloud" and (inspected.get("artifact_root") != "$CI_ARCHIVE_PATH"
            or inspected.get("build_id") != os.environ.get("CI_BUILD_ID") or inspected["application"]["build"] != os.environ.get("CI_BUILD_NUMBER")):
        raise PipelineError("Native monitoring inspection belongs to another Xcode Cloud archive/build")
    if config["operation"] == "crashlytics-apple" and sys.platform != "darwin":
        raise PipelineError("Firebase Apple upload-symbols requires the Apple host")
    provider = "sentry" if config["operation"].startswith("sentry-") else "firebase"
    if provider not in config or ("firebase" if provider == "sentry" else "sentry") in config:
        raise PipelineError("Exactly one matching monitoring provider is required")
    with tempfile.TemporaryDirectory(prefix="shared-monitoring-") as temporary:
        result = (sentry if provider == "sentry" else crashlytics)(config, root, candidate, inspected, temporary)
    # No uploader may alter candidate binaries, maps, symbols or check receipts.
    load_candidate(config, root, {inspected["kind"]})
    records = {path: candidate["artifacts"][path] for path in result.get("uploaded_artifacts", [])}
    return dict(result, artifact_records=records, schema=1, status="passed", operation=config["operation"], candidate_identity=candidate["identity"],
                source_sha=candidate["source_sha"], symbolication_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    atomic_json(args.output, execute(load_json(args.config), args.root))


if __name__ == "__main__":
    cli_error(main)
