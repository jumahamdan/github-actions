# Sonar analysis with trusted coverage

Use this separate credentialed command after the workflow has authenticated the quality artifact's origin:

```bash
python3 -m shared_ci.sonar --config .ci/sonar.json --root . \
  --verified-repository example/product --verified-run-id 123 \
  --verified-source-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

The three `--verified-*` values must come from a successful GitHub-run/artifact verification step, not from the downloaded receipt itself or an untrusted caller JSON file. The module checks that the receipt agrees with those values, is fresh, contains executed passing tests, and matches the current full selected source set and exact coverage bytes. It cannot authenticate a GitHub artifact using JSON alone. `v2-sonar.yml` performs that origin verification using the selected attempt, producer job and explicit branch/event policy before importing coverage.

## Quality job handoff

The Sonar job can depend on a quality job in the same running workflow; it does not need to repeat tests or wait for the entire run to finish. Supply `quality-run-id`, `quality-run-attempt`, `quality-artifact` and `quality-workflow`, plus `quality-producer-job` with the exact completed job name shown by GitHub (for example `quality / run`). Select the artifact name returned by the quality workflow, which includes its run and attempt. A retry may select an earlier successful quality job's attempt and artifact; authenticated evidence retains that selected attempt's provenance.

`quality-allowed-branches` is a required JSON array of exact producer head-branch names without `refs/heads/` or wildcards. `quality-allowed-events` defaults to `["push","workflow_dispatch"]`; PR coverage needs explicit `"pull_request"` permission. The wrapper always uses the authenticated attempt-and-branch mode, including when the producer belongs to a completed run. A missing attempt, branch policy or same-run producer is a failure.

`quality-run-head-sha` selects GitHub's REST run head commit. `quality-source-sha` selects the commit actually tested by quality. They are normally the same on a push; on a PR, the former is `github.event.pull_request.head.sha` and the latter is the checked-out/tested `github.sha` merge commit. Coverage receipt provenance is compared against the tested commit, while the provider's run/job/artifact metadata is authenticated against the run head.

For a branch job, the caller handoff is:

```yaml
sonar:
  needs: quality
  uses: jumahamdan/github-actions/.github/workflows/v2-sonar.yml@REVIEWED_FULL_COMMIT_SHA
  with:
    config: .ci/sonar.json
    environment: development-analysis
    artifact-name: sonar
    quality-run-id: ${{ needs.quality.outputs.run-id }}
    quality-run-attempt: ${{ needs.quality.outputs.run-attempt }}
    quality-artifact: ${{ needs.quality.outputs.receipt-artifact-name }}
    quality-workflow: .github/workflows/ci.yml
    quality-producer-job: quality / run
    quality-run-head-sha: ${{ github.sha }}
    quality-source-sha: ${{ github.sha }}
    quality-allowed-branches: '["develop"]'
    quality-allowed-events: '["push","workflow_dispatch"]'
  secrets:
    SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

Replace the revision marker with the reviewed full library commit. The caller grants `contents: read` and `actions: read`. For PR analysis, also set `allow-pull-request: true`, allow the exact same-repository head branch and `pull_request` event, select the distinct run-head SHA, and use the PR-mode Sonar configuration below. The job guard and module both refuse forks. A branch/event allowlist never grants a fork access to Sonar credentials.

## Scanner configuration

Configuration version 1 requires `mode` (`pull_request` or `branch`), `project_key`, HTTPS `host_url`, `source_paths`, `quality_receipt`, nonempty `coverage` entries `{path, property}`, pinned `scanner`, and explicit `allowed_refs`. Branch mode requires `branch`; PR mode requires `pull_request: {key, branch, base}`. Modes cannot be mixed. The scanner is a separately installed and checksum-verified SonarScanner CLI executable with an exact numeric version. Its version is checked before a credential is passed. Coverage properties must be Sonar report-path properties. See `examples/sonar-branch.json`.

`SONAR_TOKEN` is the only credential passed to the scanner, through its environment rather than command arguments. Credentialed execution requires the common trusted GitHub context check; configuration `allowed_refs` contains exact `refs/heads/...` names. PR analysis only permits explicitly allowed branches from the same repository. Fork or untrusted-event analysis is unavailable and fails if required; it is never marked successful. Confirm Sonar organization/plan support before activation. Do not inherit all caller secrets.

The scanner waits for its quality gate, and failure blocks the job. The module does not run tests, install dependencies or execute a caller-supplied scanner script. Optional `organization`, `receipt`, `timeout` (default 900 seconds), and `max_receipt_age_seconds` (default one day; maximum seven days) control the supported contract. No caller token is retained in receipts.

Run trusted PR analysis plus a separate target-branch analysis after merge to maintain Sonar's comparison baseline. Reuse coverage only when the external trusted-run check and input hashes match; a changed source set invalidates it. An identical tree under a different commit needs independently established equivalence, not a blind rewrite of receipt metadata. [Sonar documents its target-branch comparison](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/pull-request-analysis.md).

Unit tests use simulated scanner responses and synthetic credentials. They prove argument construction, token isolation, gate error propagation and evidence rejection. They do not claim a successful live Sonar analysis or a verified account entitlement.
