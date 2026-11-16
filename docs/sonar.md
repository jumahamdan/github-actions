# Sonar analysis with trusted coverage

Use this separate credentialed command after the workflow has authenticated the quality artifact's origin:

```bash
python3 -m shared_ci.sonar --config .ci/sonar.json --root . \
  --verified-repository example/product --verified-run-id 123 \
  --verified-source-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The three `--verified-*` values must come from a successful GitHub-run/artifact verification step, not from the downloaded receipt itself or an untrusted caller JSON file. The module checks that the receipt agrees with those values, is fresh, contains executed passing tests, and matches the current full selected source set and exact coverage bytes. It cannot authenticate a GitHub artifact using JSON alone. Root reusable workflows must perform that external origin verification first.

Configuration version 1 requires `mode` (`pull_request` or `branch`), `project_key`, HTTPS `host_url`, `source_paths`, `quality_receipt`, nonempty `coverage` entries `{path, property}`, pinned `scanner`, and explicit `allowed_refs`. Branch mode requires `branch`; PR mode requires `pull_request: {key, branch, base}`. Modes cannot be mixed. The scanner is a separately installed and checksum-verified SonarScanner CLI executable with an exact numeric version. Its version is checked before a credential is passed. Coverage properties must be Sonar report-path properties. See `examples/sonar-branch.json`.

`SONAR_TOKEN` is the only credential passed to the scanner, through its environment rather than command arguments. Credentialed execution requires the common trusted GitHub context check; allowed refs are explicit branch names. PR analysis only permits explicitly allowed branches from the same repository. Fork or untrusted-event analysis is unavailable and fails if required; it is never marked successful. Confirm Sonar organization/plan support before activation. Do not inherit all caller secrets.

The scanner waits for its quality gate, and failure blocks the job. The module does not run tests, install dependencies or execute a caller-supplied scanner script. Optional `organization`, `receipt`, `timeout` (default 900 seconds), and `max_receipt_age_seconds` (default one day; maximum seven days) control the supported contract. No caller token is retained in receipts.

Run trusted PR analysis plus a separate target-branch analysis after merge to maintain Sonar's comparison baseline. Reuse coverage only when the external trusted-run check and input hashes match; a changed source set invalidates it. An identical tree under a different commit needs independently established equivalence, not a blind rewrite of receipt metadata. [Sonar documents its target-branch comparison](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/pull-request-analysis.md).

Unit tests use simulated scanner responses and synthetic credentials. They prove argument construction, token isolation, gate error propagation and evidence rejection. They do not claim a successful live Sonar analysis or a verified account entitlement.
