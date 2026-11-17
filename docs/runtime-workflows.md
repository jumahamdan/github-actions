# Runtime workflow selections

Committed product configuration describes the account, allowed branches, tools and policy. A reviewed caller selects the particular build, prior deployment or interrupted operation at runtime. Reusable workflows pass those selections as action inputs, authenticate downloaded evidence and check every retained file before a provider operation. Candidate digests do not need to be committed before a build exists.

## Build and transfer

Give a build workflow `candidate-config`, `candidate-manifest` and a retained check receipt path. Candidate creation receives the current check's digest directly from the trusted action output. `check-digests` can supply other trusted step-output digests. For checks in other jobs, `check-receipts` maps each declared candidate check path to its authenticated receipt selector; the helper verifies the producer before writing that receipt. A configuration value declaring that a check passed is insufficient.

Receipt artifact names are `<artifact-name>-run-<run-id>-attempt-<attempt>`. Candidate artifact names insert `-candidate` before the run suffix. The workflow exports the actual names through `receipt-artifact-name` and `candidate-artifact-name`, plus `run-id`, `run-attempt`, `candidate-identity`, `source-sha` and `candidate-manifest`. Use those outputs in a dependent job. Different attempts keep separate artifacts so retry state remains attributable to its producer.

To import a candidate, select `artifact-run-id`, `artifact-run-attempt`, `artifact-source-sha`, `artifact-identity`, `artifact-workflow`, `artifact-input-name` and `artifact-allowed-branches`. Branches are an explicit JSON array of producer head names, such as `["staging"]`; no wildcard or implicit branch is accepted. For another job in the same running workflow, `artifact-producer-job` must exactly match its displayed completed job name, for example `build / run`.

`artifact-source-sha` identifies the tested candidate. `artifact-run-head-sha` separately identifies the GitHub REST run head. These normally match for pushes. A PR's tested merge commit can differ from its branch head: select both explicitly. Only uncredentialed verification workflows allow `artifact-allowed-events` to include `pull_request`; provider workflows keep push/manual producers. The downloaded candidate and current checkout must still have identical source trees and matching retained bytes.

Workflows check out the current `github.sha`. They do not silently change a credentialed job to arbitrary downloaded source. Recovery of an older build with a different source tree needs an explicitly reviewed caller/release run at that tree. Copying its files onto current changed source is rejected.

## Receipt selectors

An authenticated receipt selector uses these fields:

```json
{
  "repository": "owner/product",
  "run_id": "123",
  "run_attempt": 1,
  "run_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_workflow": ".github/workflows/release.yml",
  "artifact_name": "security-run-123-attempt-1",
  "producer_job": "security / run",
  "allowed_branches": ["staging"],
  "allowed_events": ["push"]
}
```

The producer job is required for a same-run handoff. Provider proofs come from trusted push/manual runs in the current repository. The Actions read credential is supplied only to the helper step. `GH_EVIDENCE_TOKEN` uses the explicitly mapped `ARTIFACT_READ_TOKEN` or the caller's GitHub token; release publication can keep a separate contents-write token. Existing project credentials are reused; these workflows create no keys.

For `check-receipts`, wrap selectors under their retained paths, for example `{"checks/security.json": <selector>}`. For `prior-deployment`, supply a single selector for the prior authenticated provider deployment. Promotion also checks candidate, environment, provider/account/project and the remote version selected by that proof. It does not accept an arbitrary local JSON receipt as proof of a deployment.

`security-refresh` maps each original candidate security-check path to an object containing `selector`, `policy`, `policy_sha256` and `source_sha`. The selector identifies a successful fresh scan of that exact retained candidate. The remaining fields explicitly select the current policy and scan source. Original candidate identity and artifact bytes remain unchanged. See [security refresh](security.md) for freshness and policy checks.

## Store retries and outcomes

`v2-mobile-stores.yml` accepts `state-restore` containing the exact prior operation-state selection described in [state](state.md). It restores state after applying the selected candidate and before calling the store. `STATE_READ_TOKEN` optionally supplies separate Actions read access; otherwise the helper uses the configured evidence token or GitHub token. Missing state on a stateful rerun is a refusal, not evidence that the earlier request never reached the provider.

The state envelope is retained even if a provider call fails, when state exists. A forced cancellation can prevent upload; reconcile the provider before another mutation if no durable state survived. Preserve artifacts through the full review/retry window using `receipt-retention-days`.

A successful request awaiting store processing or review returns `status: pending` and its `state`, with a preserved receipt. The job can finish successfully, but the required release gate refuses pending/advisory status. Pending submission and public availability are different outcomes.

## Containers and Apple

Container delivery accepts `current-release` for the exact current server release ID or `absent`, plus `container-selection` for the selected publication/tag or rollback target. Static account/host/SSH policy remains in the committed configuration. The runtime helper validates retained container records against the authenticated generic candidate before calling the container adapter.

`v2-apple-import.yml` imports an already successful Xcode Cloud run through the [private evidence bridge](apple-bridge.md). `apple-bridge-selection` selects candidate identity/source, the Cloud run/build and immutable S3 version; configured Cloud workflow/product and AWS storage identities remain fixed. The workflow uses existing AWS federation and an existing ASC P8 key supplied through `credentials-config`. It exports the verified candidate and packages it for subsequent GitHub jobs. It does not compile or sign Apple applications on a GitHub runner.

The workflow, wrapper and artifact tests use synthetic data and mocked provider responses. Native Apple builds, actual signing, store permissions and live deployment/recovery evidence remain required during each product's adoption.
