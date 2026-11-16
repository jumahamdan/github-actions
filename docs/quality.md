# Quality checks

`python3 -m shared_ci.quality --config .ci/quality.json --root .` checks an explicit language profile. Configuration version 1 supports Node/TypeScript, Python, Deno, Flutter/Dart, Swift lint, PHP, shell and PowerShell. Copy and adapt the matching `examples/quality-*.json`; these are configuration contracts for product-owned code, not runnable demonstrations of nonexistent example applications. Example versions are exact illustrative pins, not automatic product upgrades.

## Required configuration

- `profile`: one of `node`, `python`, `deno`, `flutter`, `swift`, `php`, `shell`, `powershell`.
- `toolchain`: nonempty list of `{command: [argv...], version: "exact.version"}` probes. The primary tool must be represented. Node also requires its package-manager version probe. Tools must already be installed from pinned, verified sources before execution; this module verifies versions and never downloads an implicit latest tool.
- `inputs`: explicit source/config/test paths or directories, relative to the repository root. Include all relevant build and check configuration. Selected paths must exist and contain files. Their path/hash map and selection are retained for evidence reuse.
- `checks`: unique names, `kind`, and argument-array `command`. Supported kinds: `format`, `lint`, `typecheck`, `test`, `build`, `validate`. Every declared command runs; each nonzero exit or deadline fails the job.
- `required_checks`: nonempty list of check names. Missing names fail. Caller workflows decide applicability before this module; an applicable required check is never silently skipped.

Optional fields: `working_directory` (default `.`), `lockfiles`, `install`, `immutable_paths` (defaults to `inputs`), `coverage`, `environment_names`, `receipt`, and `timeout` (1–3600 seconds; default 900). Per-check `timeout` has the same range. Unknown fields fail. Paths are repository-relative, including report paths; command-generated paths are relative to `working_directory`, so configure both consistently.

## Locked installation

Node requires `npm ci`, `pnpm install --frozen-lockfile`, or `yarn install --immutable`/`--frozen-lockfile`, and the corresponding declared lockfile. Python supports `uv sync --frozen`/`--locked` with `uv.lock`, or hash-locked pip requirements with `--require-hashes`. Deno uses `deno install --frozen` and `deno.lock`. Flutter uses `flutter pub get --enforce-lockfile` and `pubspec.lock`. Declare lock paths relative to the repository root and use the matching working directory. Unsupported installation modes fail.

Swift/PHP/shell/PowerShell are preinstalled tool profiles; package installation is outside this credential-free lane. Pin plugin/analyzer/test-framework dependencies in the caller's tool provisioning and declared input files. For PowerShell, a product-owned `.ps1` runs the pinned analyzer/Pester configuration; inline shell commands are refused. Linux PowerShell contract tests do not prove a Windows installation works. Swift compilation, native unit tests and signing stay in the Apple-hosted lane.

Installation and checks may not change declared locks or immutable inputs. Formatting commands must check, not rewrite. Intentionally generated build output belongs outside `immutable_paths`; narrowing that selection is an explicit product decision. Dependency-install lifecycle scripts are product code, so this job receives no deployment or provider secrets.

## Real test evidence

A `kind: test` check must declare `test_report: {path, format}`. Supported formats are `junit` and `flutter-machine`. The prior file is removed before execution so a failed report producer cannot reuse stale results. A successful command must produce at least one executed test; all-skipped suites, malformed/empty/missing reports, failed cases and incomplete Flutter runs fail. Flutter machine stdout is captured into the declared report; stderr is not treated as JSON. JUnit must include actual `testcase` elements. Suite totals alone cannot establish executed tests.

Node projects must provide a real test script that writes JUnit to the configured path. Pytest accepts `--junitxml`; Deno provides `--junit-path` in its [CLI](https://docs.deno.com/runtime/reference/cli/test/). Product shell/PowerShell tests may use a test framework or a product reporter producing JUnit. Do not add an echo-only test script to satisfy this contract. Known placeholder commands, failure-masking package scripts and `--passWithNoTests` fail validation. No generic library can prove an assertion is meaningful from a report; product review still verifies test content.

`coverage` names existing, nonempty files created by the checks. Their hashes are recorded. Receipts include source selection and hashes, tool/lock identity, check results, real-test summaries and GitHub repository/run/source metadata. That metadata is a claim until an independent run verifier checks the artifact origin; it is never authentication by itself.

## Workflow and environment contract

Root workflows install the pinned runtime and package manager before calling this module, with immutable action pins and verified downloads. The native self-repository composite action loads the same library revision as its workflow. No secrets are required. Explicit `environment_names` can pass non-secret test configuration; names suggesting credentials are rejected, and ambient provider/cloud/GitHub credentials are stripped by the common runner. Use isolated databases and fixture accounts; commands that contact production business APIs are not suitable quality checks.

Sonar analysis belongs in its separate credentialed workflow, after trusted coverage evidence is available. This module does not invoke it and does not duplicate tests for analysis.

## Signed Android builds

The separate trusted Android build workflow accepts explicit `build_credentials` environment names and `allowed_refs`. These credentials reach only `kind: build` commands; installation, format, lint and tests still receive the non-secret environment. The product Gradle configuration reads the existing keystore path and passwords, and its retained candidate must pass upload-certificate inspection. A PR cannot use this signing path.
