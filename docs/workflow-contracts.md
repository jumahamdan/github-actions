# Reusable workflow contracts

Generated from the callable workflows. Supply public account IDs, role ARNs, model IDs, environment names and paths through `with:`; map existing credentials explicitly through `secrets:`. Never put credential values in a configuration file or commit. Optional secrets are required when the selected operation uses that provider. See [credential ownership and rotation](credentials.md) and [integration](integration.md).

Every workflow below also requires the caller permissions declared in its YAML; a called workflow cannot raise the caller’s token permissions. Environment protections and provider access must be configured in the consuming repository/account.

## v2-ai-live

[Workflow source](../.github/workflows/v2-ai-live.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `environment` | required | Explicit configured GitHub environment |
| `aws-role` | '' | Optional distinct Bedrock evaluation role |
| `aws-region` | 'us-east-1' | AWS region |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `AI_TARGET_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-ai-mock

[Workflow source](../.github/workflows/v2-ai-mock.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-android-build

[Workflow source](../.github/workflows/v2-android-build.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `deno-version` | '2.4.5' | Exact Deno version |
| `flutter-version` | '3.47.6' | Exact Flutter version |
| `php-version` | '8.3' | Exact PHP runtime series |
| `profile` | 'flutter' | Android build profile |
| `environment` | required | Configured GitHub environment |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_PASSWORD`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-apple-import

[Workflow source](../.github/workflows/v2-apple-import.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `environment` | required | Explicit configured GitHub environment |
| `aws-role` | required | Explicit deployment role |
| `aws-region` | required | Explicit AWS region |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `credentials-config` | required | Named credential contract relative to caller root |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `apple-bridge-selection` | required | Approved candidate/source, configured Cloud workflow/product plus run/build, and immutable S3 version JSON |
| `asc-key-id` | required | Existing App Store Connect API key identifier |
| `asc-issuer-id` | required | Existing App Store Connect issuer identifier |

**Named secrets:** `ASC_PRIVATE_KEY_BASE64` (required).

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-aws

[Workflow source](../.github/workflows/v2-aws.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Deployment target; also the GitHub environment when use-github-environment is true |
| `use-github-environment` | True | Attach the job to a GitHub environment; disable for branch-scoped OIDC |
| `aws-role` | required | Explicit deployment role |
| `aws-region` | required | Explicit AWS region |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-checks

[Workflow source](../.github/workflows/v2-checks.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `deno-version` | '2.4.5' | Exact Deno version |
| `flutter-version` | '3.47.6' | Exact Flutter version |
| `php-version` | '8.3' | Exact PHP runtime series |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `profile` | required | Declared profile |
| `reuse-config` | '' | Optional authenticated quality discovery policy; missing evidence runs fresh checks |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-codeql

[Workflow source](../.github/workflows/v2-codeql.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `config` | required | Product security policy JSON containing exactly one CodeQL SARIF scan |
| `language` | required | One Linux CodeQL language (actions, cpp, csharp, go, java, javascript, python, ruby, rust) |
| `build-mode` | 'none' | Explicit none or manual; Go and Kotlin require manual in this workflow |
| `build-config` | '' | Product JSON argv/toolchain configuration required for manual builds |
| `go-version` | '' | Exact Go version required for manual Go analysis |
| `java-version` | '' | Exact Temurin version required for manual Java/Kotlin analysis |
| `dotnet-version` | '' | Exact .NET SDK version required for manual C# analysis |
| `root` | '.' | Product source root relative to workspace |
| `entitled` | required | Caller verified CodeQL licensing for this repository |
| `advanced-setup-confirmed` | required | Caller disabled default setup and duplicate CodeQL workflows |
| `upload-to-github` | False | Explicitly upload raw SARIF to entitled GitHub code scanning |
| `artifact-name` | required | Unique aggregate security receipt artifact name |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `receipt-sha256`, `state`, `candidate-identity`, `source-sha`, `candidate-manifest`, `run-id`, `run-attempt`, `receipt-artifact-name`.

## v2-container-build

[Workflow source](../.github/workflows/v2-container-build.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-container-delivery

[Workflow source](../.github/workflows/v2-container-delivery.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Explicit configured GitHub environment |
| `current-release` | '' | Runtime expected current server release: 24 lowercase hex characters or absent |
| `container-selection` | '' | Approved retained publication/tag or rollback target selection JSON |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-device-tests

[Workflow source](../.github/workflows/v2-device-tests.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `environment` | required | Configured GitHub environment |
| `google-workload-identity-provider` | required | Existing Google WIF provider |
| `google-service-account` | required | Scoped existing Google service account |
| `tool-bundles` | 'google-sdk' | Reviewed built-in tool bundles |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-docs

[Workflow source](../.github/workflows/v2-docs.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-evidence

[Workflow source](../.github/workflows/v2-evidence.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `operation` | required | Explicit module operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-firebase

[Workflow source](../.github/workflows/v2-firebase.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Explicit configured GitHub environment |
| `google-workload-identity-provider` | required | Google federation provider |
| `google-service-account` | required | Scoped deployment service account |
| `google-scopes` | 'https://www.googleapis.com/auth/cloud-platform' | OAuth scopes |
| `tool-bundles` | 'google-sdk' | Reviewed built-in tool bundles |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-health

[Workflow source](../.github/workflows/v2-health.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-mobile-stores

[Workflow source](../.github/workflows/v2-mobile-stores.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Explicit configured GitHub environment |
| `google-workload-identity-provider` | '' | Existing Google WIF provider |
| `google-service-account` | '' | Scoped existing Google service account |
| `asc-key-id` | '' | Existing App Store Connect key ID |
| `asc-issuer-id` | '' | Existing App Store Connect issuer ID |
| `google-scopes` | 'https://www.googleapis.com/auth/androidpublisher' | Explicit Play or Firebase OAuth scope |
| `state-restore` | '' | Exact prior attempt state selector JSON; required for mutating retries |
| `store-selection` | '' | Moving Apple build/version IDs or Play expected track digest JSON; fixed app/package/track remain in config |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `ASC_BEARER_TOKEN`, `GOOGLE_OAUTH_ACCESS_TOKEN`, `ASC_PRIVATE_KEY_BASE64`, `STATE_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-mobile-verify

[Workflow source](../.github/workflows/v2-mobile-verify.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `environment` | required | Configured GitHub environment |
| `tool-bundles` | 'bundletool,android-build-tools' | Reviewed built-in tool bundles |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_PASSWORD`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-monitoring

[Workflow source](../.github/workflows/v2-monitoring.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Explicit configured GitHub environment |
| `google-workload-identity-provider` | '' | Existing Google WIF provider |
| `google-service-account` | '' | Scoped existing Google service account |
| `prior-deployment` | '' | Authenticated exact deployment receipt selector JSON for sentry-deploy |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `SENTRY_AUTH_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-plan

[Workflow source](../.github/workflows/v2-plan.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`, `plan`.

## v2-release-notes

[Workflow source](../.github/workflows/v2-release-notes.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `operation` | required | Explicit module operation |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `GH_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-release-publish

[Workflow source](../.github/workflows/v2-release-publish.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Explicit configured GitHub environment |
| `operation` | required | Explicit module operation |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `RELEASE_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-required

[Workflow source](../.github/workflows/v2-required.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `job-results` | required | toJSON(needs) from caller |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-reuse

[Workflow source](../.github/workflows/v2-reuse.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `quality-run-id` | required | quality-run-id |
| `quality-run-head-sha` | required | quality-run-head-sha |
| `quality-tested-sha` | required | quality-tested-sha |
| `quality-run-attempt` | required | Exact producing quality attempt; use producer run-attempt output |
| `quality-artifact` | required | Exact producing receipt-artifact-name output |
| `quality-producer-job` | '' | Exact completed quality job name; required for same-run reuse |
| `quality-allowed-branches` | required | Explicit JSON array of approved producer branch names |
| `quality-allowed-events` | required | Explicit JSON array of approved push, pull_request or workflow_dispatch events |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `GH_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-security

[Workflow source](../.github/workflows/v2-security.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-sonar

[Workflow source](../.github/workflows/v2-sonar.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `quality-run-id` | required | Verified source quality run |
| `quality-run-attempt` | required | Exact producer quality attempt, including an earlier attempt retained during a retry |
| `quality-producer-job` | '' | Exact completed quality job name; required for same-run coverage import |
| `quality-allowed-branches` | required | Explicit JSON array of allowed producer head branch names, without refs/heads or wildcards |
| `quality-allowed-events` | '["push","workflow_dispatch"]' | Explicit JSON event allowlist; add pull_request only for approved same-repository PR coverage |
| `quality-source-sha` | required | Expected quality source SHA |
| `quality-artifact` | required | Quality receipt+coverage artifact |
| `quality-workflow` | required | Trusted quality workflow path |
| `environment` | required | Configured GitHub environment |
| `quality-run-head-sha` | '' | API run head SHA, distinct from PR tested merge SHA |
| `allow-pull-request` | False | Explicit opt-in for same-repository PR analysis on configured trusted refs |
| `tool-bundles` | 'sonar' | Reviewed built-in tool bundles |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `SONAR_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-web-deploy

[Workflow source](../.github/workflows/v2-web-deploy.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `security-refresh` | '' | JSON mapping original security checks to authenticated current scan selectors and policy hashes |
| `environment` | required | Explicit configured GitHub environment |
| `prior-deployment` | '' | Exact successful prior deployment receipt selector JSON for immutable provider promotion |

**Named secrets:** `ARTIFACT_READ_TOKEN`, `CLOUDFLARE_API_TOKEN`, `VERCEL_TOKEN`, `NETLIFY_AUTH_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## v2-wordpress-package

[Workflow source](../.github/workflows/v2-wordpress-package.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `config` | required | Configuration path relative to caller root |
| `root` | '.' | Caller root relative to workspace |
| `python-version` | '3.12.3' | Exact Python version |
| `node-version` | '22.23.4' | Exact Node version |
| `binary-manifest` | '' | Caller checksum manifest for extra CLI tools |
| `npm-tools` | '' | Caller lockfile-based CLI tool directory |
| `artifact-name` | required | Unique artifact base name; actual receipt and candidate names include run/attempt |
| `python-tools` | '' | Hash-locked caller pip requirements file |
| `credentials-config` | '' | Named credential contract relative to caller root |
| `receipt-path` | '' | Retained module receipt path relative to caller root |
| `candidate-config` | '' | Optional evidence.create configuration after this operation |
| `candidate-manifest` | '.ci-output/candidate.json' | Retained candidate manifest path |
| `candidate-retention-days` | 90 | Retention covering promotion and symbol recovery |
| `check-digests` | '{}' | Trusted required receipt digests from prior step/job outputs |
| `check-receipts` | '{}' | JSON mapping retained check paths to authenticated receipt selectors for candidate composition |
| `candidate-build` | '' | Trusted allocated build number for candidate config build=workflow; creation only |
| `artifact-run-id` | '' | Producer run ID; empty disables import |
| `artifact-run-attempt` | '1' | Producer run attempt |
| `artifact-source-sha` | '' | Expected original candidate commit |
| `artifact-identity` | '' | Expected selected candidate identity |
| `artifact-workflow` | '' | Trusted producer workflow path |
| `artifact-input-name` | '' | Unique candidate artifact to import |
| `artifact-producer-job` | '' | Completed producer job name for same-run transport |
| `artifact-allowed-branches` | '[]' | JSON array of trusted producer branch names; required whenever importing |
| `artifact-run-head-sha` | '' | Explicit producer REST run head SHA; defaults to candidate source SHA for push |
| `tool-bundles` | '' | Reviewed built-in tool bundles |
| `receipt-retention-days` | 90 | Retain operation receipts and retry state for this many days |
| `artifact-allowed-events` | '["push","workflow_dispatch"]' | Explicit producer event array; pull_request is for uncredentialed verification only |
| `php-version` | '8.3' | PHP version |

**Named secrets:** `ARTIFACT_READ_TOKEN`.

**Outputs:** `status`, `candidate-identity`, `source-sha`, `receipt-sha256`, `state`, `candidate-manifest`, `receipt-artifact-name`, `candidate-artifact-name`, `run-id`, `run-attempt`.

## wiki-publish

[Workflow source](../.github/workflows/wiki-publish.yml)

| Input | Required / default | Meaning |
| --- | --- | --- |
| `source-directory` | 'docs/wiki' | Reviewed Markdown page directory |
| `adopt-existing-home` | False | Replace the initial Home page on the first publication |

**Named secrets:** none.

**Outputs:** none.
