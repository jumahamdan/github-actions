"""Private versioned S3 bridge for custom Xcode Cloud candidate evidence."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from .artifacts import extract_bundle, import_files, package, verify_manifest
from .common import PipelineError, atomic_json, cli_error, confined_path, load_json, run, sha256_file, strict_keys, trusted_context
from .monitoring import pinned_tool
from .stores import API, SHA, HEX, data, env_secret, identifier, load_candidate


def cloud_identity(config, api):
    cloud = config["cloud"]
    path = "/v1/ciBuildRuns/" + identifier(cloud["build_run_id"])
    item = data(api, "GET", path + "?include=workflow,product")
    attrs, relations = item.get("attributes", {}), item.get("relationships", {})
    if (item["id"] != cloud["build_run_id"] or attrs.get("executionProgress") != "COMPLETE"
            or attrs.get("completionStatus") != "SUCCEEDED" or attrs.get("isPullRequestBuild") is not False
            or attrs.get("startReason") not in ("GIT_REF_CHANGE", "MANUAL", "MANUAL_REBUILD")
            or attrs.get("sourceCommit", {}).get("commitSha") != config["expected_source_sha"]
            or str(attrs.get("number")) != cloud["build_number"]
            or relations.get("workflow", {}).get("data", {}).get("id") != cloud["workflow_id"]
            or relations.get("product", {}).get("data", {}).get("id") != cloud["product_id"]):
        raise PipelineError("Apple Cloud run is not the successful authorized workflow/product/source/build")
    return {"build_run_id": item["id"], "workflow_id": cloud["workflow_id"], "product_id": cloud["product_id"],
            "source_sha": config["expected_source_sha"], "build_number": cloud["build_number"]}


def cloud_export_context(config, root):
    cloud = config["cloud"]
    if (sys.platform != "darwin" or os.environ.get("CI_XCODE_CLOUD", "").lower() != "true"
            or os.environ.get("CI_XCODEBUILD_ACTION") != "archive" or os.environ.get("CI_XCODEBUILD_EXIT_CODE") != "0"
            or os.environ.get("CI_START_CONDITION") not in ("push", "manual", "manual_rebuild")
            or os.environ.get("CI_GIT_REF") not in config["allowed_refs"]
            or os.environ.get("CI_COMMIT") != config["expected_source_sha"]
            or os.environ.get("CI_BUILD_ID") != cloud["build_run_id"] or os.environ.get("CI_WORKFLOW_ID") != cloud["workflow_id"]
            or os.environ.get("CI_BUILD_NUMBER") != cloud["build_number"]
            or not os.environ.get("CI_ARCHIVE_PATH") or Path(os.environ["CI_ARCHIVE_PATH"]).resolve() != Path(root).resolve()):
        raise PipelineError("Bridge export requires the current successful trusted Xcode Cloud archive")
    if not os.environ.get("CI_PRIMARY_REPOSITORY_PATH"):
        raise PipelineError("Xcode Cloud source checkout is missing")


def validate(config):
    required = {"schema", "operation", "expected_identity", "expected_source_sha", "allowed_refs", "cloud", "s3", "inspection"}
    strict_keys(config, required | {"manifest", "asc_token_env", "max_bytes", "max_files"}, required)
    if config["schema"] != 1 or config["operation"] not in ("apple-export", "apple-import"):
        raise PipelineError("Unsupported Apple bridge operation")
    if not HEX.fullmatch(str(config["expected_identity"])) or not SHA.fullmatch(str(config["expected_source_sha"])):
        raise PipelineError("Bridge requires trusted candidate and source identities")
    refs = config["allowed_refs"]
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", ref) for ref in refs):
        raise PipelineError("Bridge trusted branches must be explicit")
    cloud = config["cloud"]
    strict_keys(cloud, {"workflow_id", "product_id", "build_run_id", "build_number"}, {"workflow_id", "product_id", "build_run_id", "build_number"})
    for field in cloud:
        identifier(cloud[field], "Apple Cloud " + field)
    if not cloud["build_number"].isdigit() or int(cloud["build_number"]) < 1:
        raise PipelineError("Bridge requires a positive Cloud build number")
    s3 = config["s3"]
    required_s3 = {"bucket", "prefix", "region", "expected_account", "expected_role_arn", "kms_key_arn", "credentials", "tool"}
    strict_keys(s3, required_s3 | {"version_id"}, required_s3)
    if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", str(s3["bucket"]))
            or not re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,199}", str(s3["prefix"]))
            or "//" in s3["prefix"] or s3["prefix"].endswith("/")
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", str(s3["region"]))
            or not re.fullmatch(r"[0-9]{12}", str(s3["expected_account"]))):
        raise PipelineError("Invalid S3 bridge location/account")
    account = s3["expected_account"]
    if not re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):iam::" + account + r":role/[A-Za-z0-9_+=,.@/-]+", str(s3["expected_role_arn"])):
        raise PipelineError("Expected bridge role ARN is required")
    if not re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):kms:" + re.escape(s3["region"]) + ":" + account + r":key/[A-Za-z0-9-]+", str(s3["kms_key_arn"])):
        raise PipelineError("Explicit same-account/region KMS key ARN is required")
    if "version_id" in s3 and (not isinstance(s3["version_id"], str) or not s3["version_id"] or s3["version_id"] == "null" or any(c in s3["version_id"] for c in "\r\n\x00")):
        raise PipelineError("Invalid immutable S3 object version")
    maximum = config.get("max_bytes", 2 * 1024**3)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1024 <= maximum <= 5_000_000_000:
        raise PipelineError("Bridge single-object size must be between 1 KiB and 5 GB")
    count = config.get("max_files", 20000)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100000:
        raise PipelineError("Invalid bridge file-count limit")
    return maximum


class S3:
    def __init__(self, config, temporary):
        self.config, self.cwd = config, temporary
        credential_roles = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        strict_keys(config["credentials"], credential_roles, credential_roles)
        clean = {"HOME": temporary, "AWS_CONFIG_FILE": str(Path(temporary) / "empty-config"),
                 "AWS_SHARED_CREDENTIALS_FILE": str(Path(temporary) / "empty-credentials"),
                 "AWS_EC2_METADATA_DISABLED": "true", "AWS_REGION": config["region"], "AWS_DEFAULT_REGION": config["region"],
                 "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "2"}
        Path(clean["AWS_CONFIG_FILE"]).write_text("")
        Path(clean["AWS_SHARED_CREDENTIALS_FILE"]).write_text("")
        self.prefix = pinned_tool(config["tool"], temporary, clean)
        self.env = dict(clean, **{role: env_secret(name) for role, name in config["credentials"].items()})
        identity = self.call("sts", "get-caller-identity")
        role_name = config["expected_role_arn"].split("/")[-1]
        partition = config["expected_role_arn"].split(":")[1]
        if (identity.get("Account") != config["expected_account"] or not re.fullmatch(
                "arn:" + partition + ":sts::" + config["expected_account"] + ":assumed-role/" + re.escape(role_name) + r"/[A-Za-z0-9+=,.@_-]+", str(identity.get("Arn", "")))):
            raise PipelineError("Temporary bridge credentials belong to another AWS account/role")
        if self.call("s3api", "get-bucket-versioning").get("Status") != "Enabled":
            raise PipelineError("Bridge bucket versioning must already be enabled")
        block = self.call("s3api", "get-public-access-block").get("PublicAccessBlockConfiguration", {})
        if any(block.get(k) is not True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
            raise PipelineError("Bridge bucket must block all public access")

    def call(self, service, action, args=(), *, missing=False):
        command = self.prefix + [service, action, "--output", "json", "--no-cli-pager", "--cli-connect-timeout", "30", "--cli-read-timeout", "60"]
        if service == "s3api":
            command += ["--bucket", self.config["bucket"], "--expected-bucket-owner", self.config["expected_account"]]
        result = run(command + list(args), cwd=self.cwd, extra_env=self.env, timeout=900, check=False)
        if result.returncode:
            if missing and "(404)" in result.stderr:
                return None
            raise PipelineError("AWS bridge operation failed; no provider output retained")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise PipelineError("AWS bridge returned malformed JSON") from None
        if not isinstance(value, dict):
            raise PipelineError("AWS bridge response must be an object")
        return value


def object_key(config):
    cloud = config["cloud"]
    return "/".join((config["s3"]["prefix"], cloud["workflow_id"], cloud["build_run_id"], config["expected_source_sha"], "candidate.zip"))


def metadata(config, digest):
    return {"candidate-identity": config["expected_identity"], "source-sha": config["expected_source_sha"],
            "workflow-id": config["cloud"]["workflow_id"], "build-run-id": config["cloud"]["build_run_id"],
            "product-id": config["cloud"]["product_id"], "build-number": config["cloud"]["build_number"], "bundle-sha256": digest}


def head_object(client, config, key, *, missing=False, version=None):
    args = ["--key", key, "--checksum-mode", "ENABLED"]
    if version:
        args += ["--version-id", version]
    return client.call("s3api", "head-object", args, missing=missing)


def checked_head(head, config, maximum, expected_digest=None):
    if not isinstance(head, dict):
        raise PipelineError("S3 bridge object is absent")
    version = head.get("VersionId")
    if (not isinstance(version, str) or not version or version == "null"
            or head.get("ServerSideEncryption") != "aws:kms" or head.get("SSEKMSKeyId") != config["s3"]["kms_key_arn"]
            or not isinstance(head.get("ContentLength"), int) or not 0 < head["ContentLength"] <= maximum):
        raise PipelineError("Bridge object is not a bounded KMS-encrypted immutable version")
    try:
        digest = base64.b64decode(head.get("ChecksumSHA256", ""), validate=True).hex()
    except (ValueError, TypeError):
        raise PipelineError("Bridge object has no valid full SHA256 checksum") from None
    if len(digest) != 64 or (expected_digest is not None and digest != expected_digest) or head.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
        raise PipelineError("Bridge object checksum differs or is not a full-object SHA256")
    if head.get("Metadata") != metadata(config, digest):
        raise PipelineError("Bridge object metadata differs from trusted candidate/Cloud selection")
    if config["s3"].get("version_id") and version != config["s3"]["version_id"]:
        raise PipelineError("Bridge object is not the selected immutable version")
    return version, digest


def deterministic_zip(directory, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in sorted(Path(directory).rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(directory).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100600 << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)


def native_inspection(config, root, manifest):
    candidate, inspected = load_candidate(dict(config, candidate=manifest), root, {"apple"})
    if (inspected.get("distribution_ready") is not True or inspected.get("artifact_root") != "$CI_ARCHIVE_PATH"
            or inspected.get("build_id") != config["cloud"]["build_run_id"]
            or inspected["application"]["build"] != config["cloud"]["build_number"]):
        raise PipelineError("Bridge requires the verified signed archive from the selected Cloud build")
    return candidate


def execute(config, root, *, api=None, s3_factory=S3):
    maximum = validate(config)
    root = Path(root).resolve()
    exporting = config["operation"] == "apple-export"
    if exporting:
        cloud_export_context(config, root)
        if (not isinstance(config.get("manifest"), str) or not config["manifest"].startswith(".shared-ci-evidence/")
                or not isinstance(config["inspection"], str) or not config["inspection"].startswith(".shared-ci-evidence/")):
            raise PipelineError("Cloud bridge manifest must live under reserved archive evidence directory")
    else:
        trusted_context(config["allowed_refs"])
        token = env_secret(config.get("asc_token_env"))
        cloud_identity(config, api or API("apple", token, 60))
    key = object_key(config)
    with tempfile.TemporaryDirectory(prefix="shared-apple-bridge-") as temporary:
        tmp = Path(temporary)
        client = s3_factory(config["s3"], temporary)
        archive = tmp / "candidate.zip"
        if exporting:
            bundle = tmp / "bundle"
            env_name = "SHARED_APPLE_BRIDGE_BUNDLE"
            previous = os.environ.get(env_name)
            os.environ[env_name] = str(bundle)
            try:
                package({"schema": 1, "operation": "package", "manifest": config["manifest"], "expected_identity": config["expected_identity"],
                         "expected_source_sha": config["expected_source_sha"], "output_dir_env": env_name,
                         "source_root_env": "CI_PRIMARY_REPOSITORY_PATH", "max_bytes": maximum, "max_files": config.get("max_files", 20000)}, root)
            finally:
                if previous is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = previous
            candidate = native_inspection(config, root, config["manifest"])
            if candidate["build"] != config["cloud"]["build_number"]:
                raise PipelineError("Candidate build differs from current Cloud build")
            deterministic_zip(bundle, archive)
            if archive.stat().st_size > maximum:
                raise PipelineError("Bridge archive exceeds single-object limit")
            digest = sha256_file(archive)
            head = head_object(client, config, key, missing=True)
            if head is None:
                checksum = base64.b64encode(bytes.fromhex(digest)).decode()
                result = client.call("s3api", "put-object", ["--key", key, "--body", str(archive), "--if-none-match", "*",
                            "--server-side-encryption", "aws:kms", "--ssekms-key-id", config["s3"]["kms_key_arn"],
                            "--checksum-algorithm", "SHA256", "--checksum-sha256", checksum,
                            "--metadata", json.dumps(metadata(config, digest), separators=(",", ":"))])
                version = result.get("VersionId")
                if not isinstance(version, str) or not version or version == "null":
                    raise PipelineError("S3 did not return an immutable uploaded version")
                head = head_object(client, config, key, version=version)
            version, digest = checked_head(head, config, maximum, digest)
            manifest = config["manifest"]
        else:
            head = head_object(client, config, key, version=config["s3"].get("version_id"))
            version, digest = checked_head(head, config, maximum)
            result = client.call("s3api", "get-object", ["--key", key, "--version-id", version, "--checksum-mode", "ENABLED", str(archive)])
            if result.get("VersionId") != version or archive.stat().st_size != head["ContentLength"] or sha256_file(archive) != digest:
                raise PipelineError("Downloaded bridge bytes differ from the selected object version/checksum")
            stage = tmp / "retained"
            stage.mkdir()
            candidate, index = extract_bundle(archive, stage, config, maximum, config.get("max_files", 20000))
            native_inspection(config, stage, index["manifest"])
            if candidate["build"] != config["cloud"]["build_number"]:
                raise PipelineError("Retained candidate differs from authenticated Cloud build number")
            if run(["git", "rev-parse", "HEAD^{tree}"], cwd=root).stdout.strip() != candidate["source_tree"] or run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
                raise PipelineError("Bridge import requires the matching clean source tree")
            import_files(stage, root, index["files"])
            verify_manifest(candidate, root, config["expected_identity"], config["expected_source_sha"])
            manifest = index["manifest"]
    return {"schema": 1, "status": "passed", "operation": config["operation"], "candidate_identity": config["expected_identity"],
            "source_sha": config["expected_source_sha"], "manifest": manifest,
            "object": {"bucket": config["s3"]["bucket"], "key": key, "version_id": version, "sha256": digest},
            "cloud": config["cloud"], "asc_run_verified": not exporting, "apple_artifact_digest_claimed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    atomic_json(args.output, execute(load_json(args.config), args.root))


if __name__ == "__main__":
    cli_error(main)
