"""Verified-host Compose executor. Fed by fixed SSH bootstrap, never a shell template."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time


class RemoteError(Exception):
    pass


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def relative_file(root, relative, *, existing=True):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RemoteError("Invalid server-relative path")
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise RemoteError("Server path escapes deployment directory")
    if existing and not path.is_file():
        raise RemoteError("Required server file is missing")
    return path


@contextmanager
def host_lock(path, timeout):
    """All operations on a project share one kernel-managed lock."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RemoteError("Another deployment holds the host lock") from None
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(fd)


def command(argv, *, cwd, environment, deadline, timeout=900, output=None, input_data=None):
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise RemoteError("Invalid command argument array")
    remaining = min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise RemoteError("Deployment deadline exceeded")
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                               stdout=output if output is not None else subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, _stderr = process.communicate(input=input_data, timeout=remaining)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RemoteError("Remote command deadline exceeded") from None
    if process.returncode:
        # Captured output can contain runtime secrets. Never include it in the receipt.
        raise RemoteError("Required remote command failed")
    return (stdout or b"").decode("utf-8", errors="strict")


def checked_command(value):
    if not isinstance(value, dict) or set(value) - {"argv", "timeout"} or "argv" not in value:
        raise RemoteError("Invalid command configuration")
    argv = value["argv"]
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a or "\x00" in a for a in argv):
        raise RemoteError("Invalid command argument array")
    if any(a == "@compose" for a in argv[1:]):
        raise RemoteError("Compose placeholder is allowed only as the executable")
    timeout = value.get("timeout", 900)
    if type(timeout) is not int or not 1 <= timeout <= 18000:
        raise RemoteError("Invalid command timeout")
    return value


def expanded(value, compose):
    argv = checked_command(value)["argv"]
    return compose + argv[1:] if argv[0] == "@compose" else argv


def backup(value, compose, *, directory, environment, deadline, backups):
    """Capture the producer directly: gzip/pipe success cannot hide pg_dump failure."""
    checked_command(value)
    fd, partial = tempfile.mkstemp(prefix="backup-", suffix=".partial", dir=backups)
    partial = Path(partial)
    try:
        with os.fdopen(fd, "wb") as stream:
            command(expanded(value, compose), cwd=directory, environment=environment,
                    deadline=deadline, timeout=value.get("timeout", 900), output=stream)
            stream.flush()
            os.fsync(stream.fileno())
        if partial.stat().st_size == 0:
            raise RemoteError("Backup producer returned an empty backup")
        sha = digest_file(partial)
        saved = partial.with_suffix(".backup")
        os.replace(partial, saved)
        if digest_file(saved) != sha:
            raise RemoteError("Backup checksum verification failed")
        return {"path": str(saved.relative_to(directory)), "sha256": sha, "bytes": saved.stat().st_size}
    finally:
        partial.unlink(missing_ok=True)


def validate_snapshot(snapshot):
    if snapshot.get("data_mode") not in ("stateful", "stateless"):
        raise RemoteError("Explicit stateful/stateless mode is required")
    if snapshot.get("migration_policy") not in ("backward-compatible", "manual-recovery"):
        raise RemoteError("Explicit migration recovery policy is required")
    if snapshot["data_mode"] == "stateful":
        checked_command(snapshot.get("backup"))
    elif snapshot.get("migrations"):
        raise RemoteError("A stateless deployment cannot run database migrations")
    if not isinstance(snapshot.get("health"), list) or not snapshot["health"]:
        raise RemoteError("Product health assertions are required")
    if not isinstance(snapshot.get("migrations"), list):
        raise RemoteError("Migration commands must be a list")
    for item in snapshot["health"] + snapshot["migrations"]:
        checked_command(item)
    names = snapshot.get("compose_names")
    if not isinstance(names, list) or not names or any(not re.fullmatch(r"compose-[0-7]\.yaml", name) for name in names):
        raise RemoteError("Invalid Compose file list")
    if any(name not in snapshot.get("files", {}) for name in names):
        raise RemoteError("Compose source is missing")
    components = snapshot.get("manifest", {}).get("components")
    if not isinstance(components, dict) or not components:
        raise RemoteError("Every service image requires an immutable manifest entry")
    for name, reference in components.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", name):
            raise RemoteError("Invalid service name")
        if not isinstance(reference, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.:/_-]+@sha256:[a-f0-9]{64}", reference):
            raise RemoteError("Every image must be pinned by digest")


def compose_command(snapshot, directory, release_dir):
    env_file = relative_file(directory, snapshot["runtime_env_file"])
    if env_file.stat().st_mode & 0o077:
        raise RemoteError("Runtime environment file must be accessible only to its owner")
    result = ["docker", "compose", "--project-name", snapshot["project"], "--env-file", str(env_file)]
    for profile in snapshot.get("profiles", []):
        result += ["--profile", profile]
    for name in snapshot["compose_names"]:
        result += ["--file", str(relative_file(release_dir, name))]
    return result + ["--file", str(release_dir / "immutable-images.json")]


def materialize(snapshot, directory, releases):
    validate_snapshot(snapshot)
    runtime = relative_file(directory, snapshot["runtime_env_file"])
    identity = {**snapshot, "runtime_environment_sha256": digest_file(runtime)}
    release_id = fingerprint(identity)[:24]
    destination = releases / release_id
    if destination.exists():
        if destination.is_symlink():
            raise RemoteError("Release directory must not be a symlink")
        existing = json.loads((destination / "snapshot.json").read_text())
        if existing != snapshot:
            raise RemoteError("Stored release identity conflicts with proposed deployment")
        for name, encoded in snapshot["files"].items():
            if relative_file(destination, name).read_bytes() != base64.b64decode(encoded, validate=True):
                raise RemoteError("Preserved deployment files were modified")
    else:
        destination.mkdir(mode=0o700)
        for name, encoded in snapshot["files"].items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", name) or ".." in Path(name).parts:
                raise RemoteError("Invalid deployment source name")
            content = base64.b64decode(encoded, validate=True)
            if len(content) > 1_000_000:
                raise RemoteError("Compose source exceeds limit")
            output = relative_file(destination, name, existing=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
        write_json(destination / "immutable-images.json", {
            "services": {name: {"image": image, "pull_policy": "always"}
                         for name, image in snapshot["manifest"]["components"].items()}})
        write_json(destination / "snapshot.json", snapshot)
    return release_id, destination


def verify_compose(snapshot, compose, *, directory, environment, deadline):
    data = json.loads(command(compose + ["config", "--format", "json"], cwd=directory,
                              environment=environment, deadline=deadline, timeout=60))
    services = {name: value for name, value in data.get("services", {}).items()
                if not value.get("profiles") or set(value["profiles"]) & set(snapshot.get("profiles", []))}
    components = snapshot["manifest"]["components"]
    if set(services) != set(components):
        raise RemoteError("Compose service set differs from immutable component manifest")
    for name, service in services.items():
        if "build" in service or service.get("image") != components[name]:
            raise RemoteError("Remote builds and unverified service images are forbidden")
    # Do not retain expanded Compose output: it contains runtime secrets.


def verify_running(snapshot, compose, *, directory, environment, deadline):
    for name, image in snapshot["manifest"]["components"].items():
        expected = command(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                           cwd=directory, environment=environment, deadline=deadline, timeout=60).strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", expected):
            raise RemoteError("Pulled image identity is invalid")
        ids = command(compose + ["ps", "--quiet", name], cwd=directory,
                      environment=environment, deadline=deadline, timeout=60).split()
        if not ids:
            raise RemoteError("Expected service has no running container")
        for container in ids:
            if not re.fullmatch(r"[a-f0-9]{12,64}", container):
                raise RemoteError("Invalid container identity")
            actual = command(["docker", "inspect", "--format", "{{.Image}}", container],
                             cwd=directory, environment=environment, deadline=deadline, timeout=60).strip()
            if actual != expected:
                raise RemoteError("Running container does not match immutable image")
    for check in snapshot["health"]:
        command(expanded(check, compose), cwd=directory, environment=environment,
                deadline=deadline, timeout=check.get("timeout", 900))


def verify_preserved(snapshot, release_dir):
    if not release_dir.is_dir() or release_dir.is_symlink():
        raise RemoteError("Preserved deployment files are missing")
    if json.loads(relative_file(release_dir, "snapshot.json").read_text()) != snapshot:
        raise RemoteError("Preserved deployment snapshot was modified")
    for name, encoded in snapshot["files"].items():
        if relative_file(release_dir, name).read_bytes() != base64.b64decode(encoded, validate=True):
            raise RemoteError("Preserved deployment files were modified")


def _read_record(path):
    if not path.exists():
        return None
    if path.is_symlink():
        raise RemoteError("State records cannot be symlinks")
    return json.loads(path.read_text())


def execute(request):
    os.umask(0o077)
    directory = Path(request["directory"])
    if not directory.is_absolute() or not directory.is_dir() or directory.resolve() != directory:
        raise RemoteError("Approved deployment directory must already exist without symlinks")
    project = request["project"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", project):
        raise RemoteError("Invalid Compose project")
    state = directory / (".shared-ci-" + project)
    if state.is_symlink():
        raise RemoteError("State directory cannot be a symlink")
    state.mkdir(mode=0o700, exist_ok=True)
    if state.stat().st_mode & 0o077:
        raise RemoteError("Deployment state must be private to its owner")
    releases, backups = state / "releases", state / "backups"
    for path in (releases, backups):
        if path.is_symlink():
            raise RemoteError("State subdirectories cannot be symlinks")
        path.mkdir(mode=0o700, exist_ok=True)
    deadline = time.monotonic() + request["timeout"]
    with host_lock(state / "deployment.lock", request["lock_timeout"]):
        current = _read_record(state / "current.json")
        current_id = current["release_id"] if current else "absent"
        if current and not re.fullmatch(r"[a-f0-9]{24}", current_id):
            raise RemoteError("Invalid stored release identity")
        if request["expected_current_release"] != current_id:
            raise RemoteError("Current server release changed since approval")
        if request["operation"] == "rollback":
            choice = request.get("rollback_target", "previous")
            if choice not in ("previous", "last-healthy"):
                raise RemoteError("Invalid rollback selection")
            attempt = _read_record(state / "last-attempt.json")
            if choice == "last-healthy":
                if not attempt or attempt.get("status") != "failed":
                    raise RemoteError("No failed deployment requires last-healthy recovery")
                target = current
                policy = attempt.get("migration_policy")
            else:
                target = _read_record(state / "previous.json")
                policy = current["snapshot"]["migration_policy"] if current else None
            if not current or not target or request.get("expected_target_release") != target["release_id"]:
                raise RemoteError("Rollback target does not match the preserved selected release")
            if not re.fullmatch(r"[a-f0-9]{24}", target["release_id"]):
                raise RemoteError("Invalid rollback release identity")
            if policy != "backward-compatible":
                raise RemoteError("Database recovery requires the product runbook before image rollback")
            snapshot = target["snapshot"]
            validate_snapshot(snapshot)
            release_id = target["release_id"]
            release_dir = releases / release_id
            verify_preserved(snapshot, release_dir)
        else:
            snapshot = {key: request[key] for key in ("project", "manifest", "files", "compose_names", "runtime_env_file",
                        "data_mode", "backup", "migrations", "health", "migration_policy")}
            snapshot["profiles"] = request.get("profiles", [])
            release_id, release_dir = materialize(snapshot, directory, releases)
        # Previous release references runtime values by file; values never enter state or SSH.
        with tempfile.TemporaryDirectory(prefix="docker-auth-", dir=state) as docker_config:
            environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                           "HOME": docker_config, "DOCKER_CONFIG": docker_config,
                           "CI": "true", "NO_COLOR": "1", "LANG": "C.UTF-8"}
            auth = request.get("registry")
            if auth:
                if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?", auth["server"]):
                    raise RemoteError("Invalid registry endpoint")
                command(["docker", "login", auth["server"], "--username", auth["username"], "--password-stdin"],
                        cwd=directory, environment=environment, deadline=deadline, timeout=60,
                        input_data=(auth["password"] + "\n").encode())
            compose = compose_command(snapshot, directory, release_dir)
            verify_compose(snapshot, compose, directory=directory, environment=environment, deadline=deadline)
            try:
                backup_record = None
                backup_snapshot = current["snapshot"] if current and current["snapshot"]["data_mode"] == "stateful" else snapshot
                if backup_snapshot["data_mode"] == "stateful":
                    old_compose = (compose_command(backup_snapshot, directory, releases / current_id)
                                   if current else compose)
                    backup_record = backup(backup_snapshot["backup"], old_compose, directory=directory,
                                           environment=environment, deadline=deadline, backups=backups)
                command(compose + ["pull"], cwd=directory, environment=environment, deadline=deadline, timeout=900)
                if request["operation"] != "rollback":
                    for migration in snapshot["migrations"]:
                        command(expanded(migration, compose), cwd=directory, environment=environment,
                                deadline=deadline, timeout=migration.get("timeout", 900))
                command(compose + ["up", "--detach", "--no-build", "--wait", "--wait-timeout", "300"],
                        cwd=directory, environment=environment, deadline=deadline, timeout=330)
                verify_running(snapshot, compose, directory=directory, environment=environment, deadline=deadline)
            except (RemoteError, ValueError, OSError):
                write_json(state / "last-attempt.json", {"status": "failed", "release_id": release_id,
                           "previous_release_id": current_id, "automatic_rollback": False,
                           "migration_policy": snapshot["migration_policy"],
                           "backup": backup_record})
                raise
        result = {"schema": "shared-ci.compose-deployment/v1", "status": "healthy",
                  "operation": request["operation"], "release_id": release_id,
                  "previous_release_id": current_id, "manifest": snapshot["manifest"],
                  "backup": backup_record, "snapshot": snapshot}
        if current and current_id != release_id:
            write_json(state / "previous.json", current)
        write_json(state / "current.json", result)
        write_json(state / "last-attempt.json", {k: v for k, v in result.items() if k != "snapshot"})
        return {k: v for k, v in result.items() if k != "snapshot"}


if __name__ == "__main__":
    try:
        print(json.dumps(execute(REQUEST), sort_keys=True))
    except (RemoteError, OSError, ValueError, KeyError, TypeError) as error:
        # Never echo subprocess output or untrusted request fields.
        message = str(error) if isinstance(error, RemoteError) else "Invalid remote configuration or execution failure"
        raise SystemExit(message) from None
