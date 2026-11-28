# Reusing checks during promotion

Quality checks can run once for a set of product inputs. A later promotion can reuse that evidence after proving those inputs, the quality configuration, the expected tools, and the shared implementation are unchanged. A new merge commit alone does not require repeating the checks.

This module implements that proof. It never starts tests or deploys anything. Missing, failed, expired, altered, or stale evidence fails the job and requires a fresh quality run. The caller must not turn this failure into an automatic success or skip an applicable required check.

| Stage | Product workflow responsibility |
| --- | --- |
| Feature PR into `develop` | Run applicable quality checks once; retain the receipt and its allowlisted coverage bundle. |
| PR into `staging` | Select the original successful run explicitly. Reuse matching quality evidence; run checks afresh if relevant inputs changed or evidence is unavailable. |
| Push to `staging` | Build and test the release candidate as required by its platform; retain artifact identities. |
| PR into `main` | Verify candidate identity and current promotion policy. Reuse the original quality run if still fresh. |
| Push to `main` | Publish the verified candidate through its product release workflow. |

Quality reuse does not replace current security intelligence, live AI evaluations, device tests, signing, store validation, deployment health checks, or release authorization. Those lanes have separate evidence and freshness rules. Product migration must wire this policy into its caller and required gate; merely adding this library file does not change existing product triggers.

## Automatic discovery with fresh fallback

The existing `v2-checks.yml` workflow accepts an optional `reuse-config` path. Its default is empty, so existing callers continue to run fresh quality. An [explicit discovery policy](../examples/discover-quality.json) selects one product repository, producer workflow, exact displayed producer job, artifact namespace and integration branches. The quality configuration in that policy must be the same file this job executes. The job retains its existing language/tool setup, outputs and quality receipt contract.

Discovery inspects at most `max_runs` recent successful runs (20 by default, 1–100 allowed). It authenticates each selected run and its exact attempt artifact through the existing verifier below. The artifact name must be `<artifact_prefix>-run-<run_id>-attempt-<attempt>`. The producer's exact head must be an ancestor of the current checkout. For PR producers, authenticated run metadata must identify one same-repository PR into an allowed integration branch and the matching head; this proves source ancestry, not a separate PR merge-state lookup. Push/dispatch producers must use an allowed branch and the same ancestry proof. Squashed or unavailable source history, ambiguous PR metadata, unrelated release lines and current-run evidence cannot qualify. Use complete checkout history; uncertainty runs fresh quality.

All existing receipt checks still apply: original success only, original freshness, matching selected inputs/configuration/locks/toolchain/library, executed tests and no chained reuse. A bounded search miss, stale or mismatched evidence, missing read permission or unavailable API runs the ordinary quality runner. Fresh check failures remain failures. Invalid discovery policy fails rather than silently changing the configured scope. Library and quality results continue to preserve original timestamps when reused.

The [promotion caller starter](../templates/callers/recipes/promotion-checks.yml) enables discovery and independently runs a mandatory fresh security job. Its required gate rejects missing, skipped, cancelled or failed security even when quality is reused. Set the profile, scanner installation manifests and product security policy before adoption. Multiple products sharing a repository should select distinct producer workflows/jobs and artifact namespaces, with each quality configuration covering that product's complete inputs. There is no mutable shared evidence-selection variable and no store, deployment or publication operation in this starter.

## Configuration and credentials

Start with [the example](../examples/reuse-quality.json). Replace the repository, trusted workflow path and quality configuration path. Pass the original workflow outputs through the explicit runtime selectors; artifact names include their producing run and attempt. The trusted workflow path is the **product caller** recorded in the Actions run API, not the shared reusable workflow path.

`run_id`, `run_head_sha`, `source_sha`, `artifact` and optional `producer_job` accept literal strings or `{ "env": "VARIABLE_NAME" }`. Required `run_attempt` accepts a positive integer/decimal string or an environment selector. Required `allowed_branches` and `allowed_events` accept explicit nonempty arrays or an environment selector containing a JSON array. Missing, malformed, wildcard or duplicate policy entries fail; nothing searches for a loosely matching “latest successful” run. A product can record these selections with its candidate or obtain them from a successful producer job in the current workflow.

| Field / environment | Meaning |
| --- | --- |
| `source.run_id` / example `QUALITY_RUN_ID` | Original producing Actions run ID. |
| `source.run_attempt` / `QUALITY_RUN_ATTEMPT` | Exact producing attempt, normally its `run-attempt` workflow output. |
| `source.artifact` / `QUALITY_ARTIFACT` | Exact `receipt-artifact-name` output, including run/attempt suffix. |
| `source.producer_job` / `QUALITY_PRODUCER_JOB` | Exact displayed successful job name, such as `quality / run`; required for a producer in the current active run. |
| `source.allowed_branches` / `QUALITY_ALLOWED_BRANCHES` | Approved producer head names, such as `["develop"]` or the explicitly selected PR head branch; no wildcard or `refs/heads/` prefix. |
| `source.allowed_events` / `QUALITY_ALLOWED_EVENTS` | Explicit subset of `push`, `pull_request`, `workflow_dispatch`; no `pull_request_target`. |
| `source.run_head_sha` / example `QUALITY_RUN_HEAD_SHA` | Exact `head_sha` returned by the Actions workflow-run API. |
| `source.source_sha` / example `QUALITY_TESTED_SHA` | Actual tested checkout SHA from the original quality receipt's provenance and assurance. |
| `token_env` / default `GH_TOKEN` | Existing GitHub token with Actions read permission for the same product repository. Map the job's `github.token`; no new provider key is needed. |
| `max_age_seconds` | Required age ceiling, between 1 and 604800 seconds; example: one day. |
| `quality_config` | Current product quality configuration, containing explicit inputs, lockfiles, toolchain, checks, and required-check names. |

Keep the two SHA fields separate. GitHub documents `GITHUB_SHA` for `pull_request` as the synthetic merge commit. This module compares `run_head_sha` with the API and `source_sha` with the receipt; it never guesses that they are interchangeable. [GitHub event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request), [workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run).

The invoking job needs `contents: read` for checkout and `actions: read` for retrieval. No provider secrets, write permission, or OIDC permission is required. Use the library's pinned `actions/v2-run` action with `module: reuse`, the reuse configuration path, and an optional `receipt-path`. The root wrapper preserves the module's verified **quality** assurance; the candidate check entry is `{ "path": "reused-quality.json", "kind": "quality", "config": "ci/quality.json" }`, pointing to the original quality configuration, not the reuse configuration. Upload the wrapper's allowlisted bundle as usual.

The reusable `v2-reuse.yml` workflow binds these selectors through `quality-run-id`, `quality-run-attempt`, `quality-artifact`, `quality-run-head-sha`, `quality-tested-sha`, `quality-allowed-branches`, `quality-allowed-events` and optional `quality-producer-job`. Its example configuration reads the matching `QUALITY_*` environment variables. For a same-run dependency, use the quality job's `run-id`, `run-attempt` and `receipt-artifact-name` outputs; choose the exact caller workflow path and displayed quality job name. Keep the run head separate from the tested merge SHA for PRs. The reuse job executes no tests, so a passing reuse receipt does not duplicate the original quality run.

The equivalent local command is:

```sh
python3 -m shared_ci.reuse --config ci/reuse-quality.json --root . --output reused-quality.json
```

This command requires the existing read token in the selected environment variable. It rejects an output that would replace tracked source, either configuration, a selected input, or a lockfile. It removes an old output before attempting retrieval so a failed attempt leaves no previous success receipt.

## What must match

The module always downloads through the strict mode of `evidence.fetch_receipt`, supplying the selected attempt, branch/event policy and optional producer job. It authenticates the expected repository, workflow and run head, selected attempt/job outcome and time window, artifact expiry/uniqueness, and GitHub's artifact SHA-256 digest. Its bounded archive parser accepts the receipt and declared coverage files only. GitHub exposes artifact digests and expiration in its [artifact API](https://docs.github.com/en/rest/actions/artifacts).

A foreign run requires its selected attempt to have completed successfully. Within the current active run, one exact successful producer job can supply the receipt while downstream jobs are still running. A failed-jobs rerun can reuse that same producer from an earlier completed attempt, even if another job caused the earlier attempt to fail. The imported receipt records the original selected attempt, branch and event, not the latest rerun metadata. Failed, absent, duplicate or running producers, wrong branches/events, and artifacts outside the selected execution window are refused. See [artifact transport](artifacts.md).

The authenticated receipt must then satisfy all of these requirements:

- Original schema-1 quality receipt with `status: passed`, matching product/profile/run/tested SHA, and matching source/configuration assurance.
- Original creation timestamp within the explicit age limit, with a timezone and no future date. Reuse never refreshes this timestamp and cannot chain another reused receipt.
- Exact current quality configuration checksum and the same selected input paths. Every selected file's content hash, including files newly added inside a selected directory, must match. Every declared dependency lock is checked separately.
- Every file selected by `immutable_paths` must also be covered by recorded inputs or lockfiles. Quality does not separately retain immutable-file hashes, so an extra immutable file outside that record requires fresh evidence.
- Exact expected toolchain commands and versions from the current quality configuration. The configured tools are not rerun merely to compare expectations.
- Every configured check present exactly once, with the correct kind and a passing result; every required name exists. At least one test check must prove a positive executed count, zero failures, a nonnegative skipped count, and a valid retained test-report hash.
- Matching `library.identity`. This fingerprints Python modules and nested helpers, script helpers, v2 composite actions/workflows, and installation locks. Implementation changes invalidate reuse; documentation-only library commits do not.
- No modified tracked files in the current checkout. Only after these comparisons does the module bind the result to its current commit and tree.

The product owns a complete `quality.inputs` selection: include source, tests, local check scripts, manifests, compiler/linter/test configuration, and relevant product workflow configuration. A file omitted from that selection is outside this proof. Prefer whole source/configuration directories where possible so a newly added file cannot be overlooked. A changed unrelated documentation file can therefore preserve quality evidence while changing the full current source tree.

Quality currently records configured environment **names**, not their values. Reuse consequently refuses nonempty `environment_names` and `build_credentials`. Checks that depend on a mutable remote service, machine-local state, or other undeclared inputs also require a fresh run; do not route those checks through this module. Credentialed builds and their artifacts have their own candidate verification.

## Retained evidence

The result preserves the original check results, input hashes, provenance, and creation time. `reused_from` records the original API artifact digest, run and attempt, producer branch/event, run head, tested SHA/tree, workflow, original assurance/library record, freshness policy, and verification time. The top-level assurance refers to the current source and the same quality configuration, making it consumable by candidate verification.

Coverage bytes stay in the original artifact. The reused receipt has an empty top-level `coverage` map and retains the original coverage metadata under `reused_from.coverage`; it does not claim to package reports it never regenerated. Sonar should fetch the original run's authenticated coverage through its separate adapter. Retain the original artifact long enough for the intended promotion window, within the freshness ceiling.
