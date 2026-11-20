# Durable Xcode Cloud evidence through private S3

Apple explicitly excludes files created by custom build scripts from downloadable Xcode Cloud artifacts. The native artifact API exposes archives, exports, logs and test/build products; its metadata contains a download URL and size, but no provider digest or custom receipt upload endpoint. Merely placing candidate JSON inside an archive does not preserve it after the build. This optional bridge uploads the custom candidate bundle to existing private AWS storage during the post-archive hook, then authenticates the completed Apple run before a later GitHub job imports it.

`python -m shared_ci.apple_bridge --config bridge.json --root ROOT --output receipt.json`

## Existing account prerequisites

Adoption must configure an existing private S3 bucket with versioning enabled, all four S3 public-access blocks enabled, an existing KMS key and narrowly scoped existing roles. The library neither creates keys/resources nor chooses a product's cloud account. Both operations require supplied temporary AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN roles through explicit environment-name mapping, a pinned AWS CLI and expected AWS account/role ARN. STS caller identity is checked; static credentials without a session token are refused. Xcode Cloud does not receive GitHub OIDC automatically: the product must supply its existing short-lived credential mechanism for the archive hook. Credentials never enter the candidate or public configuration examples.

The writer needs only the required bucket metadata reads, KMS operations and conditional writes/heads for the configured evidence prefix. The reader needs the metadata reads and version-specific object reads/KMS decryption. Reading SHA256 through HeadObject with SSE-KMS also needs the KMS permissions described by AWS. Deny object/version deletion and unconditional overwrites for pipeline roles; apply the account's approved retention/lifecycle policy. This code does not enable irreversible Object Lock retention or provision IAM. S3 version IDs identify immutable bytes; conditional writes prevent this adapter from replacing an existing logical key. Deletion by a separate administrator affects availability, which the importer detects.

## Export in the native post-archive hook

Use operation:apple-export, --root "$CI_ARCHIVE_PATH", and the same reviewed immutable-library loader as mobile verification/monitoring. Config includes manifest, inspection, expected_identity, expected_source_sha, allowed_refs, cloud identity and s3 settings. Both manifest and inspection must be under `.shared-ci-evidence/`; the archive payload fingerprint excludes that reserved evidence directory. Keep all copied configurations and receipt inputs there. Native binaries/dSYMs remain ordinary hash-bound candidate artifacts outside it.

Export requires actual macOS, a successful current Xcode Cloud archive, an explicit trusted push/manual branch, and matching CI_COMMIT, CI_WORKFLOW_ID, CI_BUILD_ID and CI_BUILD_NUMBER. The signed Apple inspection must belong to that build. Packaging verifies the clean source checkout using CI_PRIMARY_REPOSITORY_PATH, candidate inputs/artifacts and typed check contracts. It does not treat the ongoing Cloud run as already completed: the export receipt says asc_run_verified:false.

The module makes a deterministic, explicit-file ZIP outside source/archive directories and writes exactly one object to:

`PREFIX/WORKFLOW_ID/BUILD_RUN_ID/SOURCE_SHA/candidate.zip`

It uses S3 PutObject with If-None-Match `*`, explicit SSE-KMS encryption, a full SHA256 checksum and metadata identifying the candidate/source/Cloud run. A matching existing object version is reused only when its exact checksum and metadata match; differing bytes fail. Upload completion is followed by a checksum/encryption/version check. A lost response can be retried by checking the existing immutable object, without rebuilding or overwriting it. Size is bounded (default 2 GiB, explicit maximum 5 GB for a single PutObject), as are file count and command deadlines. No multipart or unbounded sync is performed.

The export receipt records bucket/key/version/SHA256, candidate identity and Cloud IDs, never credentials. Retain the selected candidate identity in the caller's trusted release-selection record; do not accept arbitrary identity supplied solely by a downloaded object. Cloud hooks can fail if this required export fails. Do not schedule GitHub acceptance until Apple reports the whole run succeeded.

## Import in a later GitHub job

Use operation:apple-import with the same expected candidate/source/cloud/S3 selection and inspection path, plus asc_token_env naming an existing short-lived ASC JWT. The GitHub job must be on an explicit trusted push/manual branch. Import first calls Apple's supported ciBuildRuns endpoint and requires COMPLETE/SUCCEEDED, the exact workflow and product, matching source commit and build number, and a non-PR push/manual start.

Only after that independent Apple check does the module read the private S3 object. Set s3.version_id from the export receipt when available. Otherwise the first authenticated HeadObject pins a version before retrieval; all subsequent retrieval uses that exact version. KMS key, full-object SHA256, metadata, size and version must match. The downloaded ZIP then passes the same explicit-file, secret-pattern, traversal/symlink/duplicate/hash and typed-check validation as [GitHub artifact transport](artifacts.md). The signed native inspection must match the authenticated Cloud run/build.

Import into a clean source checkout with the same Git tree. Existing files must be byte-identical; differing source/configuration is never overwritten. The receipt provides the selected object and ASC provenance. Subsequent GitHub jobs can publish the verified retained candidate using the ordinary GitHub artifact transport, or feed its paths directly to store/symbol/release adapters. No Linux native inspection is substituted for the native receipt.

This path does not rely on Apple retaining custom files and does not claim an Apple artifact SHA256. SHA256 is verified against the private S3 object's full checksum and the candidate's file records. Apple-native artifacts can still be downloaded separately for diagnostics; they cannot replace the required custom checks. Tests use synthetic archives, local Git and mocked Apple/AWS calls; no bucket, role, key, upload or app account was created or accessed during library validation.

Primary contracts: [Apple custom build scripts](https://developer.apple.com/documentation/xcode/writing-custom-build-scripts), [Apple artifact metadata](https://developer.apple.com/documentation/appstoreconnectapi/ciartifact/attributes-data.dictionary), Apple's [OpenAPI specification](https://developer.apple.com/sample-code/app-store-connect/app-store-connect-openapi-specification.zip), [AWS PutObject](https://docs.aws.amazon.com/cli/latest/reference/s3api/put-object.html) and [HeadObject](https://docs.aws.amazon.com/cli/latest/reference/s3api/head-object.html).
