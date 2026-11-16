"""Exact-image container build, publication, promotion and verified-host deployment.

The library never provisions a server, reads an ambient Docker login, or rebuilds
an image remotely. All provider writes require a trusted GitHub job context.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .common import (PipelineError, atomic_json, cli_error, confined_path,
                     fingerprint, load_json, run, sha256_file, strict_keys,
                     trusted_context)

DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}\Z")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
REPOSITORY = re.compile(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
SENSITIVE_NAME = re.compile(r"SECRET|PASSWORD|TOKEN|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY|API_KEY", re.I)
CANDIDATE_SCHEMA = "shared-ci.container-candidate/v1"
PUBLICATION_SCHEMA = "shared-ci.container-publication/v1"
MANIFEST_SCHEMA = "shared-ci.container-set/v1"
BOOTSTRAP = "python3 -c 'import sys,json,base64; p=json.load(sys.stdin); exec(compile(base64.b64decode(p.pop(\"script\")), \"<shared-ci>\", \"exec\"), {\"__name__\":\"__main__\", \"REQUEST\":p})'"


def _text(value, label):
    if not isinstance(value, str) or not value or any(x in value for x in ("\x00", "\n", "\r")):
        raise PipelineError(f"Invalid {label}")
    return value


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PipelineError(f"Invalid {label}")
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise PipelineError("source_sha must be a complete lowercase commit SHA")
    return value


def image_reference(value, *, immutable=False):
    """Require an explicit registry/repository; no option-like or shell syntax."""
    value = _text(value, "image reference")
    if "@" in value:
        repository, digest = value.split("@", 1)
        if not DIGEST.fullmatch(digest):
            raise PipelineError("Image digest must be sha256")
    else:
        if immutable or ":" not in value.rsplit("/", 1)[-1]:
            raise PipelineError("Expected a digest-pinned image" if immutable else "Image tag is required")
        repository, tag = value.rsplit(":", 1)
        if not TAG.fullmatch(tag):
            raise PipelineError("Invalid image tag")
    if not REPOSITORY.fullmatch(repository):
        raise PipelineError("Image requires an explicit registry/repository")
    return value


def _repository(reference):
    image_reference(reference)
    return reference.split("@", 1)[0] if "@" in reference else reference.rsplit(":", 1)[0]


def _env(name):
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
        raise PipelineError("Invalid credential environment name")
    value = os.environ.get(name)
    if not value:
        raise PipelineError(f"Required credential is absent: {name}")
    return value


def _registry(config):
    if config is None:
        return None
    strict_keys(config, {"server", "username_env", "password_env"}, {"server", "username_env", "password_env"})
    server = _text(config["server"], "registry server")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?", server):
        raise PipelineError("Invalid registry server")
    return {"server": server, "username": _env(config["username_env"]),
            "password": _env(config["password_env"])}


@contextmanager
def docker_environment(root, registry=None):
    # An empty config prevents accidental use of ~/.docker/config.json credentials.
    with tempfile.TemporaryDirectory(prefix="shared-ci-docker-") as directory:
        env = {"DOCKER_CONFIG": directory}
        auth = _registry(registry)
        if auth:
            run(["docker", "login", auth["server"], "--username", auth["username"], "--password-stdin"],
                cwd=root, extra_env=env, timeout=60, input_data=auth["password"] + "\n")
        yield env


def _descriptor(reference, root, environment, *, absent_ok=False):
    image_reference(reference)
    result = run(["docker", "buildx", "imagetools", "inspect", reference,
                  "--format", "{{json .Manifest}}"], cwd=root, extra_env=environment,
                 timeout=120, check=False)
    if result.returncode:
        if absent_ok and re.search(r"manifest unknown|manifest_unknown|not found", result.stderr, re.I):
            return None
        raise PipelineError("Cannot verify registry manifest")
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise PipelineError("Registry returned an invalid manifest descriptor") from exc
    if not isinstance(data, dict) or not DIGEST.fullmatch(str(data.get("digest", ""))):
        raise PipelineError("Registry descriptor has no valid digest")
    if "@" in reference and reference.rsplit("@", 1)[1] != data["digest"]:
        raise PipelineError("Registry digest does not match the requested image")
    return data


def _remote_image_id(reference, root, environment):
    result = run(["docker", "buildx", "imagetools", "inspect", reference, "--raw"],
                 cwd=root, extra_env=environment, timeout=120)
    try:
        digest = json.loads(result.stdout)["config"]["digest"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("Expected a single-platform image manifest with a config digest") from exc
    if not DIGEST.fullmatch(str(digest)):
        raise PipelineError("Registry image has no valid config digest")
    return digest


def _local_image_id(reference, root, environment):
    value = run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                cwd=root, extra_env=environment, timeout=60).stdout.strip()
    if not DIGEST.fullmatch(value):
        raise PipelineError("Docker returned an invalid image ID")
    return value


def _argv(value):
    if not isinstance(value, list) or not value:
        raise PipelineError("Expected a nonempty command argument array")
    for item in value:
        _text(item, "command argument")
    return value


def build(config, root):
    strict_keys(config, {"operation", "context", "dockerfile", "source_sha", "archive", "receipt",
                         "tests", "build_args", "platform", "timeout"},
                {"operation", "context", "dockerfile", "source_sha", "archive", "receipt", "tests"})
    source = _sha(config["source_sha"])
    context = confined_path(root, config["context"])
    dockerfile = confined_path(root, config["dockerfile"])
    archive = confined_path(root, config["archive"], must_exist=False)
    receipt = confined_path(root, config["receipt"], must_exist=False)
    if not context.is_dir() or not dockerfile.is_file() or archive == receipt:
        raise PipelineError("Invalid build paths")
    tests = config["tests"]
    if not isinstance(tests, list) or not tests:
        raise PipelineError("A container candidate requires real configured tests")
    for test in tests:
        strict_keys(test, {"argv", "cwd", "timeout"}, {"argv"})
        _argv(test["argv"])
        confined_path(root, test.get("cwd", "."))
        _integer(test.get("timeout", 900), 1, 21600, "test timeout")
    build_args = config.get("build_args", {})
    if not isinstance(build_args, dict):
        raise PipelineError("build_args must be an object of non-secret strings")
    command = ["docker", "build", "--file", str(dockerfile)]
    local_tag = "shared-ci-candidate:" + fingerprint(config)[:24]
    command += ["--tag", local_tag]
    for name, value in sorted(build_args.items()):
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name) or SENSITIVE_NAME.search(name):
            raise PipelineError("Secrets are not permitted as Docker build arguments")
        command += ["--build-arg", name + "=" + _text(value, "build argument")]
    if "platform" in config:
        platform = _text(config["platform"], "platform")
        if not re.fullmatch(r"linux/(?:amd64|arm64)(?:/v[0-9]+)?", platform):
            raise PipelineError("Build supports one explicit Linux platform")
        command += ["--platform", platform]
    command.append(str(context))
    timeout = _integer(config.get("timeout", 1800), 1, 21600, "build timeout")
    if run(["git", "rev-parse", "HEAD"], cwd=root, timeout=30).stdout.strip() != source:
        raise PipelineError("Build source SHA differs from the checked-out commit")
    run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, timeout=30)
    with docker_environment(root) as environment:
        run(command, cwd=root, extra_env=environment, timeout=timeout)
        image_id = _local_image_id(local_tag, root, environment)
        verified = []
        for test in tests:
            argv = [image_id if arg == "{image}" else arg for arg in test["argv"]]
            run(argv, cwd=confined_path(root, test.get("cwd", ".")),
                extra_env={**environment, "CANDIDATE_IMAGE": image_id},
                timeout=test.get("timeout", 900))
            verified.append({"command_sha256": fingerprint(test), "status": "passed", "image_id": image_id})
        if _local_image_id(local_tag, root, environment) != image_id:
            raise PipelineError("Candidate image tag changed during tests")
        archive.parent.mkdir(parents=True, exist_ok=True)
        run(["docker", "image", "save", "--output", str(archive), image_id],
            cwd=root, extra_env=environment, timeout=timeout)
    if not archive.is_file() or not archive.stat().st_size:
        raise PipelineError("Docker candidate archive is missing or empty")
    result = {"status": "passed", "schema": CANDIDATE_SCHEMA, "source_sha": source, "image_id": image_id,
              "archive": {"path": str(archive.relative_to(root)), "sha256": sha256_file(archive)},
              "tests": verified, "config_sha256": fingerprint(config)}
    atomic_json(receipt, result)
    return result


def publish(config, root):
    strict_keys(config, {"operation", "candidate", "tags", "registry", "allowed_refs", "receipt"},
                {"operation", "candidate", "tags", "allowed_refs", "receipt"})
    context = trusted_context(config["allowed_refs"])
    candidate = load_json(confined_path(root, config["candidate"]))
    strict_keys(candidate, {"status", "schema", "source_sha", "image_id", "archive", "tests", "config_sha256"},
                {"status", "schema", "source_sha", "image_id", "archive", "tests", "config_sha256"})
    if candidate["status"] != "passed" or candidate["schema"] != CANDIDATE_SCHEMA or not DIGEST.fullmatch(str(candidate["image_id"])):
        raise PipelineError("Invalid container candidate")
    _sha(candidate["source_sha"])
    if not isinstance(candidate["tests"], list) or not candidate["tests"]:
        raise PipelineError("Candidate test evidence must be a nonempty list")
    if any(t.get("status") != "passed" or t.get("image_id") != candidate["image_id"]
                                      for t in candidate["tests"] if isinstance(t, dict)):
        raise PipelineError("Candidate has no matching successful test evidence")
    for test in candidate["tests"]:
        strict_keys(test, {"command_sha256", "status", "image_id"}, {"command_sha256", "status", "image_id"})
        if not re.fullmatch(r"[a-f0-9]{64}", str(test["command_sha256"])):
            raise PipelineError("Invalid candidate test command identity")
    if not re.fullmatch(r"[a-f0-9]{64}", str(candidate["config_sha256"])):
        raise PipelineError("Invalid build configuration identity")
    strict_keys(candidate["archive"], {"path", "sha256"}, {"path", "sha256"})
    archive = confined_path(root, candidate["archive"]["path"])
    if not archive.is_file() or not archive.stat().st_size or sha256_file(archive) != candidate["archive"]["sha256"]:
        raise PipelineError("Candidate archive checksum mismatch")
    tags = config["tags"]
    if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) for tag in tags) or len(tags) != len(set(tags)):
        raise PipelineError("Publication requires unique tagged destinations")
    for tag in tags:
        image_reference(tag)
        if "@" in tag:
            raise PipelineError("Publication destinations must be tags")
    outputs = {}
    with docker_environment(root, config.get("registry")) as environment:
        # Preflight all names before publication; never overwrite a different candidate.
        existing = {tag: _descriptor(tag, root, environment, absent_ok=True) for tag in tags}
        for tag, descriptor in existing.items():
            if descriptor and _remote_image_id(_repository(tag) + "@" + descriptor["digest"], root, environment) != candidate["image_id"]:
                raise PipelineError("Publication tag already identifies another image")
        run(["docker", "image", "load", "--input", str(archive)], cwd=root, extra_env=environment, timeout=900)
        if _local_image_id(candidate["image_id"], root, environment) != candidate["image_id"]:
            raise PipelineError("Loaded image does not match tested candidate")
        for tag in tags:
            if not existing[tag]:
                run(["docker", "image", "tag", candidate["image_id"], tag], cwd=root, extra_env=environment)
                run(["docker", "image", "push", tag], cwd=root, extra_env=environment, timeout=1800)
            descriptor = _descriptor(tag, root, environment)
            immutable = _repository(tag) + "@" + descriptor["digest"]
            if _remote_image_id(immutable, root, environment) != candidate["image_id"]:
                raise PipelineError("Published image is not the tested image")
            outputs[tag] = immutable
    result = {"status": "passed", "schema": PUBLICATION_SCHEMA, "source_sha": candidate["source_sha"],
              "image_id": candidate["image_id"], "images": outputs,
              "candidate_sha256": sha256_file(confined_path(root, config["candidate"])), "context": context}
    atomic_json(confined_path(root, config["receipt"], must_exist=False), result)
    return result


def promote(config, root):
    strict_keys(config, {"operation", "source", "target", "expected_current_digest", "registry", "allowed_refs", "receipt"},
                {"operation", "source", "target", "expected_current_digest", "allowed_refs", "receipt"})
    context = trusted_context(config["allowed_refs"])
    source = image_reference(config["source"], immutable=True)
    target = image_reference(config["target"])
    if "@" in target or _repository(source) != _repository(target):
        raise PipelineError("Promotion must preserve the source registry repository")
    expected = config["expected_current_digest"]
    if expected != "absent" and not DIGEST.fullmatch(str(expected)):
        raise PipelineError("Expected destination digest or 'absent' is required")
    digest = source.rsplit("@", 1)[1]
    with docker_environment(root, config.get("registry")) as environment:
        _descriptor(source, root, environment)
        current = _descriptor(target, root, environment, absent_ok=True)
        actual = current["digest"] if current else "absent"
        if actual != digest:
            if actual != expected:
                raise PipelineError("Promotion destination changed since approval")
            run(["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", target, source],
                cwd=root, extra_env=environment, timeout=900)
        if _descriptor(target, root, environment)["digest"] != digest:
            raise PipelineError("Promotion did not preserve the registry manifest digest")
    result = {"status": "passed", "schema": "shared-ci.container-promotion/v1", "source": source, "target": target,
              "digest": digest, "previous_digest": actual, "context": context}
    atomic_json(confined_path(root, config["receipt"], must_exist=False), result)
    return result


def manifest(config, root):
    strict_keys(config, {"operation", "source_sha", "components", "receipt", "registry", "allowed_refs"},
                {"operation", "source_sha", "components", "receipt"})
    source = _sha(config["source_sha"])
    if config.get("registry") is not None:
        trusted_context(config.get("allowed_refs"))
    if not isinstance(config["components"], dict) or not config["components"]:
        raise PipelineError("Component manifest must contain every deployed service image")
    images = {}
    with docker_environment(root, config.get("registry")) as environment:
        for name, component in config["components"].items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise PipelineError("Invalid Compose component name")
            if isinstance(component, str):
                image = image_reference(component, immutable=True)
            else:
                strict_keys(component, {"publication", "tag"}, {"publication", "tag"})
                record = load_json(confined_path(root, component["publication"]))
                if record.get("status") != "passed" or record.get("schema") != PUBLICATION_SCHEMA:
                    raise PipelineError("Component publication receipt is invalid")
                try:
                    image = image_reference(record["images"][component["tag"]], immutable=True)
                except KeyError as exc:
                    raise PipelineError("Component is absent from publication receipt") from exc
            _descriptor(image, root, environment)
            images[name] = image
    result = {"status": "passed", "schema": MANIFEST_SCHEMA, "source_sha": source, "components": images}
    result["release_id"] = fingerprint(result)[:24]
    atomic_json(confined_path(root, config["receipt"], must_exist=False), result)
    return result


def _ssh_settings(config, root, temporary):
    strict_keys(config, {"host", "user", "port", "private_key_env", "known_hosts_env", "known_hosts_file"},
                {"host", "user", "private_key_env"})
    host, user = _text(config["host"], "SSH host"), _text(config["user"], "SSH user")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", host) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise PipelineError("Invalid SSH destination")
    port = _integer(config.get("port", 22), 1, 65535, "SSH port")
    if ("known_hosts_env" in config) == ("known_hosts_file" in config):
        raise PipelineError("Exactly one pre-verified known-hosts source is required")
    known = (_env(config["known_hosts_env"]) if "known_hosts_env" in config
             else confined_path(root, config["known_hosts_file"]).read_text())
    if not known.strip() or any(line.startswith("@") for line in known.splitlines()):
        raise PipelineError("Expected explicit verified host keys, not markers or an empty file")
    keyfile, hostfile = Path(temporary) / "identity", Path(temporary) / "known_hosts"
    for path, value in ((keyfile, _env(config["private_key_env"])), (hostfile, known)):
        path.write_text(value.rstrip("\n") + "\n")
        path.chmod(0o600)
    return ["ssh", "-F", "/dev/null", "-i", str(keyfile), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(hostfile),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", user + "@" + host, BOOTSTRAP]


def deploy(config, root):
    allowed = {"operation", "ssh", "directory", "project", "manifest", "compose_files", "runtime_env_file",
               "data_mode", "backup", "migrations", "health", "migration_policy", "registry", "allowed_refs", "support_files", "profiles",
               "receipt", "timeout", "lock_timeout", "expected_current_release", "expected_target_release", "rollback_target"}
    required = {"operation", "ssh", "directory", "project", "allowed_refs", "receipt", "expected_current_release"}
    strict_keys(config, allowed, required)
    context = trusted_context(config["allowed_refs"])
    directory = _text(config["directory"], "server directory")
    if not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", directory) or ".." in Path(directory).parts:
        raise PipelineError("Deployment directory must be a safe absolute server path")
    if not isinstance(config["project"], str) or not NAME.fullmatch(config["project"]):
        raise PipelineError("Invalid Compose project name")
    payload = {key: config[key] for key in ("operation", "directory", "project", "expected_current_release")}
    payload["timeout"] = _integer(config.get("timeout", 1800), 60, 18000, "deployment timeout")
    payload["lock_timeout"] = _integer(config.get("lock_timeout", 30), 1, 300, "lock timeout")
    payload["registry"] = _registry(config.get("registry"))
    if config["operation"] == "rollback":
        if "expected_target_release" not in config:
            raise PipelineError("Rollback requires the expected previous release")
        payload["expected_target_release"] = config["expected_target_release"]
        payload["rollback_target"] = config.get("rollback_target", "previous")
    else:
        for field in ("manifest", "compose_files", "runtime_env_file", "data_mode", "health", "migration_policy"):
            if field not in config:
                raise PipelineError(f"Deployment requires {field}")
        record = load_json(confined_path(root, config["manifest"]))
        if record.get("status") != "passed" or record.get("schema") != MANIFEST_SCHEMA or not isinstance(record.get("components"), dict) or not record["components"]:
            raise PipelineError("Invalid component manifest")
        _sha(record.get("source_sha"))
        for name, image in record["components"].items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise PipelineError("Invalid component name")
            image_reference(image, immutable=True)
        if record.get("release_id") != fingerprint({k: v for k, v in record.items() if k != "release_id"})[:24]:
            raise PipelineError("Component manifest identity mismatch")
        files = config["compose_files"]
        if not isinstance(files, list) or not files or len(files) > 8:
            raise PipelineError("At least one Compose file is required")
        payload["files"] = {}
        for index, relative in enumerate(files):
            content = confined_path(root, relative).read_bytes()
            if len(content) > 1_000_000:
                raise PipelineError("Compose source is too large")
            payload["files"][f"compose-{index}.yaml"] = base64.b64encode(content).decode()
        payload["compose_names"] = sorted(payload["files"])
        support = config.get("support_files", {})
        if not isinstance(support, dict):
            raise PipelineError("support_files must map remote relative names to tracked source paths")
        for name, relative in support.items():
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", name)
                    or ".." in Path(name).parts or name in payload["files"]
                    or re.search(r"(?:^|/)(?:\.env|id_rsa|id_ed25519)|\.(?:pem|key|p8|p12|pfx)$", name, re.I)):
                raise PipelineError("Unsafe support-file destination")
            content = confined_path(root, relative).read_bytes()
            if len(content) > 1_000_000:
                raise PipelineError("Support file is too large")
            payload["files"][name] = base64.b64encode(content).decode()
        payload["manifest"] = record
        for field in ("runtime_env_file", "data_mode", "health", "migration_policy"):
            payload[field] = config[field]
        profiles = config.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(p, str) or not NAME.fullmatch(p) for p in profiles):
            raise PipelineError("Invalid Compose profile selection")
        payload["profiles"] = profiles
        payload["backup"] = config.get("backup")
        payload["migrations"] = config.get("migrations", [])
    script = Path(__file__).resolve().parent.parent / "scripts" / "containers" / "remote.py"
    payload["script"] = base64.b64encode(script.read_bytes()).decode()
    with tempfile.TemporaryDirectory(prefix="shared-ci-ssh-") as temporary:
        command = _ssh_settings(config["ssh"], root, temporary)
        completed = run(command, cwd=root, input_data=json.dumps(payload), timeout=payload["timeout"] + payload["lock_timeout"] + 90)
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise PipelineError("Remote deployment returned invalid evidence") from exc
    if (not isinstance(result, dict) or result.get("status") != "healthy"
            or result.get("schema") != "shared-ci.compose-deployment/v1"
            or result.get("operation") != config["operation"]
            or not re.fullmatch(r"[a-f0-9]{24}", str(result.get("release_id", "")))
            or result.get("previous_release_id") != config["expected_current_release"]):
        raise PipelineError("Remote deployment did not return matching healthy evidence")
    if config["operation"] == "deploy" and result.get("manifest") != payload["manifest"]:
        raise PipelineError("Remote deployment manifest differs from selected candidate")
    if config["operation"] == "rollback" and result["release_id"] != config["expected_target_release"]:
        raise PipelineError("Remote rollback did not restore the selected release")
    result["context"] = context
    result["health_status"] = result["status"]
    result["status"] = "passed"
    atomic_json(confined_path(root, config["receipt"], must_exist=False), result)
    return result


OPERATIONS = {"build": build, "publish": publish, "promote": promote, "manifest": manifest,
              "deploy": deploy, "rollback": deploy}


def execute(config, root):
    root = Path(root).resolve()
    if not isinstance(config, dict) or config.get("operation") not in OPERATIONS:
        raise PipelineError("Unknown container operation")
    return OPERATIONS[config["operation"]](config, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    result = execute(load_json(args.config), args.root)
    print(json.dumps({"status": "passed", "operation": load_json(args.config)["operation"],
                      "schema": result.get("schema", "")}))


if __name__ == "__main__":
    cli_error(main)
