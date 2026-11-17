# Runtime evidence selections

Committed configuration describes the product, provider target and policy. Values produced by a build or earlier workflow arrive through explicit action inputs. The wrapper applies those selections in memory; it does not rewrite the checked-out configuration to commit a future build hash.

## Candidate selection

`selected-manifest`, `selected-identity` and `selected-source-sha` are an all-or-none selection returned by authenticated artifact import. They bind the exact manifest, original build source, executing library revision, complete checked-out source tree and retained bytes. The wrapper rechecks them after execution. Dirty tracked source, changed artifacts, a different tree or library revision fail. A promotion merge may have another commit SHA when its complete tree is identical.

Stores, symbol uploads, mutating web/cloud deployments and container delivery require this selection. Read-only deployment validation/snapshots/emulators and store inventories can run without a candidate. The provider modules still check their own signatures, account identities, current provider state and operation-specific evidence. Selection is trusted workflow data; a product command cannot manufacture authority merely by writing a JSON file.

Release publication also requires a selected candidate. Set `expected_library_sha: "workflow"` to resolve the executing library revision and `expected_commit: "workflow"` to select the current GitHub workflow commit. An explicit library value must equal the executing helper. The module still proves the checked-out commit and release tag, expected toolchain, notes identity and uploaded asset bytes. Release drafting and rendering do not use deployment evidence.

For release-note drafting, `candidate_sha: "workflow"` resolves to the selected candidate's original source when available, otherwise the current GitHub commit. The draft module still checks the previous-tag ancestry and actual associated merged PRs.

## Authenticated receipts from other jobs

`check-receipts` is an object mapping each declared candidate check path to a selector:

```json
{
  "evidence/quality.json": {
    "repository": "example/app",
    "run_id": "123456",
    "run_attempt": 1,
    "run_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "artifact_name": "quality-run-123456-attempt-1",
    "expected_workflow": ".github/workflows/ci.yml",
    "allowed_branches": ["feature/example"],
    "allowed_events": ["pull_request"],
    "producer_job": "quality / run"
  }
}
```

`run_head_sha` is the producing run's authenticated branch commit; a check receipt may describe a distinct tested PR merge commit. Candidate creation validates the receipt's source/tree/configuration binding against the candidate separately. For a producer in the current running workflow, `producer_job` must identify exactly one completed successful job. The selector includes an exact run attempt, workflow, branch/event allowlists and artifact name.

Only the current repository is accepted. Fetch requires authenticated GitHub metadata and artifact digest, and retains that origin inside the check as `authenticated_origin`. The wrapper hashes the final retained receipt and supplies its digest directly to candidate creation. Destinations must be declared candidate checks and cannot replace tracked source or conflicting existing files. `check-digests` can additionally select hashes from trusted prior action outputs; conflicting hashes fail. `current-receipt-path` and `current-receipt-sha` must be supplied together for a check produced in the current job.

Evidence fetch defaults to `GH_EVIDENCE_TOKEN` when present, otherwise `GH_TOKEN`. Explicit `token_env` permits only those names or `GH_ARTIFACT_TOKEN`. A selector cannot use an unrelated provider credential as a GitHub token. Map an existing read-only Actions token; keep it separate from a release-publication write token.

## Current security and prior deployments

`security-refresh` maps an original candidate security-check path to an object with `selector`, `policy`, `policy_sha256` and `source_sha`. `selector` uses the authenticated receipt shape above, restricted to approved push/manual producers. `policy` is the current local policy path, its expected SHA256 is explicit, and `source_sha` identifies the source tested by that fresh scan. The shared security module verifies exact candidate identity/files and current policy/freshness. The scoped proof applies only during this invocation; it does not mutate the original candidate or permanently relax freshness. Store, monitoring, deployment, container, evidence-verification and release-publication operations accept this input.

`prior-deployment` accepts the same receipt selector for a deployment promotion. The runtime passes an in-memory authenticated proof to the deployment module. The module then validates the original deployment's candidate, provider/account/project, environment and approved producer branch before promoting its exact version or deployment ID. A saved JSON file alone is not this proof. See [deployment](deployment.md) and [security](security.md).

## Store retries and outcomes

`store-selection` supplies moving provider state without changing the committed target. Apple accepts `{"apple":{"build_id":"200","app_store_version_id":"300","expected_selected_build":null}}`; optional `submission_id` selects an existing review submission. Play accepts `{"play":{"expected_track_digest":"<64-character SHA256>"}}`. Every supplied value is explicit; omitted fields retain the committed configuration. App/package/platform/track/groups, review policy and rollout authority cannot be changed through this input. The store module verifies the selected Apple build/version against the candidate or the current Play track against the selected digest before mutation.

`state-restore` contains the exact prior-attempt selector described in [store state](state.md). Its `token_env` must be `GH_STATE_TOKEN`. For an earlier attempt in the same current run, omitted `run_head_sha` resolves to the trusted current `GITHUB_SHA`; a different run must specify its own publishing-run SHA. Runtime candidate overrides are applied before restoring, so the state request fingerprint matches the effective operation.

Play upload/promotion, Apple submission and Firebase distribution reruns (`run_attempt > 1`) require explicit prior state. Missing retained state requires provider reconciliation; an empty workspace is not evidence that the earlier attempt did nothing. The wrapper saves a validated `operation-state.json` envelope on pending, success or failure when the operation produced a state file. It never invents state for read-only/idempotent operations. Uncertain commit state is retained and still refuses another provider mutation after restoration.

`status: pending` is a successful execution outcome with a receipt and a `state` output, but it cannot satisfy a release gate. A failed operation keeps a minimal sanitized failure receipt rather than an empty artifact or a passing receipt. Provider response bodies and exception text are excluded from that failure record.

## Containers

Container `publish`, `manifest`, `promote` and `deploy` require a selected generic candidate. Container build/publication records, archives, component manifests, Compose files and support files must be retained candidate inputs/artifacts. Source identities must agree. `manifest` derives its source from the selected candidate. Remote rollback uses its existing explicit release identities and does not require rebuilding or creating a new candidate.

`current-release` selects the exact current server release ID (24 lowercase hexadecimal characters) or `absent` before deploy/rollback. `container-selection` supports:

- Promotion: `{"publication":"artifacts/publication.json","tag":"registry.example/team/app:tested","expected_current_digest":"absent"}`. The immutable source image comes from that retained publication, not a future digest hardcoded in committed configuration. The provider checks the destination digest before changing the tag. Use its observed digest instead of `absent` when already deployed.
- Rollback: `{"expected_target_release":"dddddddddddddddddddddddd","rollback_target":"previous"}`. The remote helper checks the exact expected current and target release IDs and existing health/rollback policy before acting.

See [container behavior and recovery](containers.md) for backups, migrations, SSH and registry permissions.

## Apple import

`apple-bridge-selection` contains `expected_identity`, `expected_source_sha`, `version_id` and `cloud` (`workflow_id`, `product_id`, `build_run_id`, `build_number`). Only build/run/version selection changes at runtime; workflow and product IDs must equal the committed target. The importer authenticates the completed Cloud build and exact retained S3 version, then verifies candidate/library/source identity before exporting its manifest/identity/source outputs. Existing materialized P8 credentials supply the short-lived ASC token alias. Native export and account prerequisites are described in [Apple evidence storage](apple-bridge.md).
