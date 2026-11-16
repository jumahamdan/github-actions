import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from shared_ci import containers as c
from shared_ci.common import PipelineError, fingerprint, sha256_file

SPEC = importlib.util.spec_from_file_location("container_remote", Path(__file__).resolve().parents[1] / "scripts/containers/remote.py")
remote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote)
IMAGE_ID = "sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
SOURCE = "c" * 40
IMAGE = "registry.example/team/app@" + DIGEST
TRUST = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/staging", "GITHUB_SHA": SOURCE, "GITHUB_REPOSITORY": "example/product"}


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        self.calls = []

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        output = SOURCE + "\n" if argv == ["git", "rev-parse", "HEAD"] else ""
        if argv[:3] == ["docker", "image", "inspect"]:
            output = IMAGE_ID + "\n"
        if argv[:3] == ["docker", "image", "save"]:
            Path(argv[argv.index("--output") + 1]).write_bytes(b"tested image archive")
        return subprocess.CompletedProcess(argv, 0, output, "")

    def build_config(self):
        return {"operation": "build", "context": ".", "dockerfile": "Dockerfile", "source_sha": SOURCE,
                "archive": "out/candidate.tar", "receipt": "out/candidate.json",
                "build_args": {"APP_VERSION": "1.0.0"}, "tests": [{"argv": ["fixture-smoke", "{image}"]}]}

    def test_build_saves_only_tested_image_and_hashes_archive(self):
        with patch.object(c, "run", self.fake_run):
            result = c.execute(self.build_config(), self.root)
        self.assertEqual(result["archive"]["sha256"], sha256_file(self.root / "out/candidate.tar"))
        self.assertEqual(result["tests"][0]["image_id"], IMAGE_ID)
        self.assertIn(["fixture-smoke", IMAGE_ID], [a for a, _ in self.calls])
        self.assertEqual(sum(a[:2] == ["docker", "build"] for a, _ in self.calls), 1)
        configs = {k["extra_env"]["DOCKER_CONFIG"] for _, k in self.calls if "extra_env" in k}
        self.assertTrue(all(not Path(p).exists() for p in configs))

    def test_failure_never_creates_candidate(self):
        def failed(argv, **kwargs):
            if argv[0] == "fixture-smoke":
                raise PipelineError("intentional fixture failure")
            return self.fake_run(argv, **kwargs)
        with patch.object(c, "run", failed), self.assertRaises(PipelineError):
            c.execute(self.build_config(), self.root)
        self.assertFalse((self.root / "out/candidate.json").exists())
        self.assertFalse(any(a[:3] == ["docker", "image", "save"] for a, _ in self.calls))

    def test_rejects_missing_tests_secret_build_args_and_path_escape(self):
        for change in ({"tests": []}, {"build_args": {"API_KEY": "synthetic"}}, {"context": "../outside"}):
            with self.subTest(change=change), self.assertRaises(PipelineError):
                c.execute({**self.build_config(), **change}, self.root)

    def test_rejects_option_injection_and_mutable_deploy_images(self):
        for value in ("--help", "registry.example/a:tag;echo", "registry.example/a:latest", "a@" + DIGEST):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                c.image_reference(value, immutable=True)

    def publication_config(self):
        return {"operation": "publish", "candidate": "out/candidate.json", "tags": ["registry.example/team/app:1.0.0"],
                "allowed_refs": ["refs/heads/staging"], "receipt": "out/published.json"}

    def test_publish_uses_archive_without_rebuild_and_verifies_remote(self):
        with patch.object(c, "run", self.fake_run):
            c.execute(self.build_config(), self.root)
        self.calls.clear()
        with patch.dict(os.environ, TRUST), patch.object(c, "run", self.fake_run), \
             patch.object(c, "_descriptor", side_effect=[None, {"digest": DIGEST}]), \
             patch.object(c, "_remote_image_id", return_value=IMAGE_ID):
            result = c.execute(self.publication_config(), self.root)
        self.assertEqual(list(result["images"].values()), [IMAGE])
        self.assertFalse(any(a[:2] == ["docker", "build"] for a, _ in self.calls))
        self.assertTrue(any(a[:3] == ["docker", "image", "load"] for a, _ in self.calls))
        self.assertTrue(any(a[:3] == ["docker", "image", "push"] for a, _ in self.calls))

    def test_corrupt_archive_prevents_publish(self):
        with patch.object(c, "run", self.fake_run):
            c.execute(self.build_config(), self.root)
        (self.root / "out/candidate.tar").write_bytes(b"tampered")
        with patch.dict(os.environ, TRUST), patch.object(c, "run", self.fake_run), self.assertRaises(PipelineError):
            c.execute(self.publication_config(), self.root)
        self.assertFalse(any(a[:3] == ["docker", "image", "push"] for a, _ in self.calls))

    def test_untrusted_publish_never_contacts_registry(self):
        with patch.dict(os.environ, {**TRUST, "GITHUB_EVENT_NAME": "pull_request_target"}), \
             patch.object(c, "run") as execute, self.assertRaises(PipelineError):
            c.execute(self.publication_config(), self.root)
        execute.assert_not_called()

    def test_existing_different_tag_blocks_all_pushes(self):
        with patch.object(c, "run", self.fake_run):
            c.execute(self.build_config(), self.root)
        with patch.dict(os.environ, TRUST), patch.object(c, "run", self.fake_run), \
             patch.object(c, "_descriptor", return_value={"digest": DIGEST}), \
             patch.object(c, "_remote_image_id", return_value="sha256:" + "d" * 64), self.assertRaises(PipelineError):
            c.execute(self.publication_config(), self.root)
        self.assertFalse(any(a[:3] == ["docker", "image", "push"] for a, _ in self.calls))

    def test_promotion_preserves_digest_and_handles_retry(self):
        config = {"operation": "promote", "source": IMAGE, "target": "registry.example/team/app:stable",
                  "expected_current_digest": "absent", "allowed_refs": ["refs/heads/staging"], "receipt": "promoted.json"}
        with patch.dict(os.environ, TRUST), patch.object(c, "run", self.fake_run), \
             patch.object(c, "_descriptor", side_effect=[{"digest": DIGEST}, None, {"digest": DIGEST}]):
            c.execute(config, self.root)
        create = [a for a, _ in self.calls if a[:4] == ["docker", "buildx", "imagetools", "create"]]
        self.assertEqual(len(create), 1)
        self.assertIn("--prefer-index=false", create[0])
        self.calls.clear()
        with patch.dict(os.environ, TRUST), patch.object(c, "run", self.fake_run), \
             patch.object(c, "_descriptor", return_value={"digest": DIGEST}):
            c.execute(config, self.root)
        self.assertFalse(self.calls)

    def test_changed_destination_blocks_promotion(self):
        config = {"operation": "promote", "source": IMAGE, "target": "registry.example/team/app:stable",
                  "expected_current_digest": "absent", "allowed_refs": ["refs/heads/staging"], "receipt": "promoted.json"}
        with patch.dict(os.environ, TRUST), patch.object(c, "run", self.fake_run), \
             patch.object(c, "_descriptor", side_effect=[{"digest": DIGEST}, {"digest": "sha256:" + "e" * 64}]), self.assertRaises(PipelineError):
            c.execute(config, self.root)
        self.assertFalse(self.calls)

    def test_registry_auth_stays_on_stdin_and_temporary_config_is_removed(self):
        registry = {"server": "registry.example", "username_env": "REGISTRY_USER", "password_env": "REGISTRY_PASSWORD"}
        with patch.dict(os.environ, {"REGISTRY_USER": "fixture", "REGISTRY_PASSWORD": "synthetic-password"}), \
             patch.object(c, "run", self.fake_run):
            with c.docker_environment(self.root, registry) as environment:
                directory = Path(environment["DOCKER_CONFIG"])
                self.assertTrue(directory.is_dir())
        self.assertFalse(directory.exists())
        argv, kwargs = self.calls[0]
        self.assertNotIn("synthetic-password", " ".join(argv))
        self.assertEqual(kwargs["input_data"], "synthetic-password\n")

    def test_manifest_contains_every_pinned_component_and_verifies_registry(self):
        config = {"operation": "manifest", "source_sha": SOURCE,
                  "components": {"app": IMAGE, "db": "registry.example/team/db@" + "sha256:" + "d" * 64}, "receipt": "manifest.json"}
        with patch.object(c, "_descriptor", return_value={"digest": DIGEST}) as descriptor:
            result = c.execute(config, self.root)
        self.assertEqual(descriptor.call_count, 2)
        self.assertEqual(set(result["components"]), {"app", "db"})
        self.assertEqual(result["release_id"], fingerprint({k: v for k, v in result.items() if k != "release_id"})[:24])

    def deployment_config(self):
        manifest = {"status": "passed", "schema": c.MANIFEST_SCHEMA, "source_sha": SOURCE, "components": {"app": IMAGE}}
        manifest["release_id"] = fingerprint(manifest)[:24]
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        (self.root / "compose.yaml").write_text("services: {}\n")
        return {"operation": "deploy", "ssh": {"host": "deploy.example", "user": "deploy",
                "private_key_env": "SSH_PRIVATE_KEY", "known_hosts_env": "SSH_KNOWN_HOSTS"},
                "directory": "/srv/example", "project": "fixture", "manifest": "manifest.json",
                "compose_files": ["compose.yaml"], "runtime_env_file": "runtime.env", "data_mode": "stateless",
                "health": [{"argv": ["check-health"]}], "migration_policy": "backward-compatible",
                "expected_current_release": "absent", "allowed_refs": ["refs/heads/staging"], "receipt": "deployment.json"}, manifest

    def test_deploy_stdin_transport_and_receipt_identity(self):
        config, manifest = self.deployment_config()
        def fake_ssh(argv, **kwargs):
            self.calls.append((argv, kwargs))
            request = json.loads(kwargs["input_data"])
            self.assertEqual(request["manifest"], manifest)
            self.assertTrue(request["script"])
            result = {"schema": "shared-ci.compose-deployment/v1", "status": "healthy", "operation": "deploy",
                      "release_id": "d" * 24, "previous_release_id": "absent", "manifest": manifest}
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
        credentials = {**TRUST, "SSH_PRIVATE_KEY": "synthetic-private", "SSH_KNOWN_HOSTS": "deploy.example ssh-ed25519 synthetic"}
        with patch.dict(os.environ, credentials), patch.object(c, "run", fake_ssh):
            result = c.execute(config, self.root)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["health_status"], "healthy")
        argv = self.calls[0][0]
        self.assertFalse(Path(argv[argv.index("-i") + 1]).exists())
        self.assertNotIn("synthetic-private", " ".join(argv))
        self.assertNotIn("synthetic-private", (self.root / "deployment.json").read_text())

    def test_remote_wrong_manifest_is_rejected(self):
        config, _manifest = self.deployment_config()
        result = {"schema": "shared-ci.compose-deployment/v1", "status": "healthy", "operation": "deploy",
                  "release_id": "d" * 24, "previous_release_id": "absent", "manifest": {}}
        credentials = {**TRUST, "SSH_PRIVATE_KEY": "synthetic-private", "SSH_KNOWN_HOSTS": "deploy.example ssh-ed25519 synthetic"}
        with patch.dict(os.environ, credentials), \
             patch.object(c, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(result), "")), \
             self.assertRaisesRegex(PipelineError, "manifest differs"):
            c.execute(config, self.root)
        self.assertFalse((self.root / "deployment.json").exists())

    def test_private_manifest_collection_requires_trust(self):
        config = {"operation": "manifest", "source_sha": SOURCE, "components": {"app": IMAGE}, "receipt": "manifest.json",
                  "registry": {"server": "registry.example", "username_env": "REGISTRY_USER", "password_env": "REGISTRY_PASSWORD"},
                  "allowed_refs": ["refs/heads/staging"]}
        with patch.dict(os.environ, {**TRUST, "GITHUB_EVENT_NAME": "pull_request_target"}), \
             patch.object(c, "run") as execute, self.assertRaises(PipelineError):
            c.execute(config, self.root)
        execute.assert_not_called()

    def test_source_mismatch_prevents_build(self):
        with patch.object(c, "run", return_value=subprocess.CompletedProcess([], 0, "d" * 40, "")) as execute, \
             self.assertRaisesRegex(PipelineError, "source SHA differs"):
            c.execute(self.build_config(), self.root)
        self.assertEqual(execute.call_count, 1)

    def test_ssh_uses_verified_keys_and_fixed_bootstrap(self):
        config = {"host": "deploy.example", "user": "deploy", "private_key_env": "SSH_PRIVATE_KEY", "known_hosts_env": "SSH_KNOWN_HOSTS"}
        env = {"SSH_PRIVATE_KEY": "synthetic-key", "SSH_KNOWN_HOSTS": "deploy.example ssh-ed25519 synthetic"}
        with patch.dict(os.environ, env):
            command = c._ssh_settings(config, self.root, self.root)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentityAgent=none", command)
        self.assertEqual(command[-1], c.BOOTSTRAP)
        self.assertNotIn("synthetic-key", " ".join(command))
        with patch.dict(os.environ, env), self.assertRaises(PipelineError):
            c._ssh_settings({**config, "host": "host;touch /tmp/wrong"}, self.root, self.root)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_umask = os.umask(0o077)
        self.addCleanup(os.umask, self.old_umask)
        (self.root / "runtime.env").write_text("SYNTHETIC_CONFIG=fixture\n")
        (self.root / "runtime.env").chmod(0o600)
        self.log = self.root / "commands.jsonl"
        self.running = self.root / "running.json"
        binary = self.root / "bin"
        binary.mkdir()
        script = '''#!PYTHON
import hashlib,json,sys
from pathlib import Path
args=sys.argv[1:]
log=Path(LOG)
with log.open('a') as f: f.write(json.dumps(args)+'\\n')
running=Path(RUNNING)
def identity(image): return 'sha256:'+hashlib.sha256(image.encode()).hexdigest()
if args[:1]==['compose']:
 override=Path(args[[i for i,x in enumerate(args) if x=='--file'][-1]+1])
 services=json.loads(override.read_text())['services']
 if 'config' in args: print(json.dumps({'services': services}))
 elif 'up' in args: running.write_text(json.dumps(services))
 elif 'ps' in args: print(hashlib.sha256(args[-1].encode()).hexdigest())
elif args[:2]==['image','inspect']: print(identity(args[-1]))
elif args[:1]==['inspect']:
 for name,value in json.loads(running.read_text()).items():
  if hashlib.sha256(name.encode()).hexdigest()==args[-1]: print(identity(value['image']))
'''.replace('PYTHON', sys.executable).replace('LOG', repr(str(self.log))).replace('RUNNING', repr(str(self.running)))
        (binary / "docker").write_text(script)
        (binary / "docker").chmod(0o700)
        self.env = patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, image=IMAGE):
        manifest = {"schema": c.MANIFEST_SCHEMA, "source_sha": SOURCE, "components": {"app": image}}
        manifest["release_id"] = fingerprint(manifest)[:24]
        return {"operation": "deploy", "directory": str(self.root), "project": "fixture",
                "expected_current_release": "absent", "timeout": 120, "lock_timeout": 1,
                "registry": None, "manifest": manifest,
                "files": {"compose-0.yaml": base64.b64encode(b"services: {}\n").decode()},
                "compose_names": ["compose-0.yaml"], "runtime_env_file": "runtime.env", "data_mode": "stateful",
                "backup": {"argv": [sys.executable, "-c", "print('synthetic database dump')"]},
                "migrations": [], "health": [{"argv": [sys.executable, "-c", "pass"]}],
                "migration_policy": "backward-compatible"}

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_deploy_records_backup_hash_and_verifies_running_image(self):
        result = remote.execute(self.request())
        self.assertEqual(result["status"], "healthy")
        backup_path = self.root / result["backup"]["path"]
        self.assertEqual(result["backup"]["sha256"], sha256_file(backup_path))
        up = [a for a in self.commands() if "up" in a]
        self.assertEqual(len(up), 1)
        self.assertIn("--no-build", up[0])
        self.assertIn("--wait", up[0])
        self.assertTrue(any(a[:1] == ["inspect"] for a in self.commands()))
        self.assertFalse(list((self.root / ".shared-ci-fixture").glob("docker-auth-*")))

    def test_backup_producer_failure_blocks_pull_migration_and_up(self):
        request = self.request()
        marker = self.root / "migration-ran"
        request["backup"] = {"argv": [sys.executable, "-c", "import sys; print('partial'); sys.exit(7)"]}
        request["migrations"] = [{"argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}]
        with self.assertRaisesRegex(remote.RemoteError, "command failed"):
            remote.execute(request)
        self.assertFalse(marker.exists())
        self.assertFalse(any("pull" in a or "up" in a for a in self.commands()))
        backups = self.root / ".shared-ci-fixture/backups"
        self.assertFalse(list(backups.iterdir()))
        self.assertFalse((self.root / ".shared-ci-fixture/current.json").exists())

    def test_empty_backup_is_not_success(self):
        request = self.request()
        request["backup"] = {"argv": [sys.executable, "-c", "pass"]}
        with self.assertRaisesRegex(remote.RemoteError, "empty backup"):
            remote.execute(request)
        self.assertFalse(any("up" in a for a in self.commands()))

    def test_host_lock_blocks_concurrent_deployment(self):
        lock = self.root / "lock"
        with remote.host_lock(lock, 0.1):
            with self.assertRaisesRegex(remote.RemoteError, "host lock"):
                with remote.host_lock(lock, 0.02):
                    self.fail("second deployment obtained lock")
        with remote.host_lock(lock, 0.1):
            pass

    def test_health_failure_preserves_previous_manifest(self):
        first = remote.execute(self.request())
        request = self.request("registry.example/team/app@sha256:" + "d" * 64)
        request["expected_current_release"] = first["release_id"]
        request["health"] = [{"argv": [sys.executable, "-c", "raise SystemExit(4)"]}]
        with self.assertRaises(remote.RemoteError):
            remote.execute(request)
        current = json.loads((self.root / ".shared-ci-fixture/current.json").read_text())
        self.assertEqual(current["release_id"], first["release_id"])
        failure = json.loads((self.root / ".shared-ci-fixture/last-attempt.json").read_text())
        self.assertFalse(failure["automatic_rollback"])
        self.assertEqual(failure["status"], "failed")

    def test_rollback_uses_preserved_previous_images_without_volume_removal(self):
        first = remote.execute(self.request())
        request = self.request("registry.example/team/app@sha256:" + "d" * 64)
        request["expected_current_release"] = first["release_id"]
        second = remote.execute(request)
        result = remote.execute({"operation": "rollback", "directory": str(self.root), "project": "fixture",
                                 "expected_current_release": second["release_id"], "expected_target_release": first["release_id"],
                                 "timeout": 120, "lock_timeout": 1, "registry": None})
        self.assertEqual(result["release_id"], first["release_id"])
        self.assertTrue(result["backup"]["bytes"])
        self.assertFalse(any("down" in a or "--volumes" in a or "--build" in a for a in self.commands()))

    def test_manual_database_recovery_prevents_automatic_image_rollback(self):
        first = remote.execute(self.request())
        request = self.request("registry.example/team/app@sha256:" + "d" * 64)
        request.update(expected_current_release=first["release_id"], migration_policy="manual-recovery")
        second = remote.execute(request)
        with self.assertRaisesRegex(remote.RemoteError, "runbook"):
            remote.execute({"operation": "rollback", "directory": str(self.root), "project": "fixture",
                            "expected_current_release": second["release_id"], "expected_target_release": first["release_id"],
                            "timeout": 120, "lock_timeout": 1, "registry": None})

    def test_changed_current_release_prevents_any_commands(self):
        request = self.request()
        request["expected_current_release"] = "wrong"
        with self.assertRaisesRegex(remote.RemoteError, "changed since approval"):
            remote.execute(request)
        self.assertFalse(self.commands())

    def test_last_healthy_recovery_after_failed_first_upgrade(self):
        first = remote.execute(self.request())
        request = self.request("registry.example/team/app@sha256:" + "d" * 64)
        request["expected_current_release"] = first["release_id"]
        request["health"] = [{"argv": [sys.executable, "-c", "raise SystemExit(4)"]}]
        with self.assertRaises(remote.RemoteError):
            remote.execute(request)
        result = remote.execute({"operation": "rollback", "directory": str(self.root), "project": "fixture",
                                 "expected_current_release": first["release_id"], "expected_target_release": first["release_id"],
                                 "rollback_target": "last-healthy", "timeout": 120, "lock_timeout": 1, "registry": None})
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["release_id"], first["release_id"])

    def test_tampered_preserved_files_block_rollback(self):
        first = remote.execute(self.request())
        request = self.request("registry.example/team/app@sha256:" + "d" * 64)
        request["expected_current_release"] = first["release_id"]
        second = remote.execute(request)
        saved = self.root / ".shared-ci-fixture/releases" / first["release_id"] / "compose-0.yaml"
        saved.write_text("tampered content")
        with self.assertRaisesRegex(remote.RemoteError, "modified"):
            remote.execute({"operation": "rollback", "directory": str(self.root), "project": "fixture",
                            "expected_current_release": second["release_id"], "expected_target_release": first["release_id"],
                            "timeout": 120, "lock_timeout": 1, "registry": None})

    def test_remote_command_deadline_stops_process(self):
        with self.assertRaisesRegex(remote.RemoteError, "deadline"):
            remote.command([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.root,
                           environment={"PATH": os.environ["PATH"]}, deadline=time.monotonic() + 0.05)

    def test_public_runtime_env_and_remote_build_are_rejected(self):
        (self.root / "runtime.env").chmod(0o644)
        with self.assertRaisesRegex(remote.RemoteError, "owner"):
            remote.execute(self.request())
        (self.root / "runtime.env").chmod(0o600)
        with patch.object(remote, "command", return_value=json.dumps({"services": {"app": {"image": IMAGE, "build": "."}}})), \
             self.assertRaisesRegex(remote.RemoteError, "Remote builds"):
            remote.verify_compose(self.request(), ["docker", "compose"], directory=self.root,
                                  environment={}, deadline=time.monotonic() + 30)


if __name__ == "__main__":
    unittest.main()
