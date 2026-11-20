# Release · Candidate evidence

Creates or verifies the record connecting a release candidate to its source, retained files, tools and required checks.

## When to use it

Use it when a product needs a candidate that can be selected, verified and reused across staging and production. This reusable workflow runs only when a product caller invokes it.

## What it produces

A candidate manifest whose identity includes the recorded inputs, artifacts and typed check evidence.

## Setup and limits

File hashes alone do not establish who produced an artifact. Later jobs must also authenticate the original GitHub or Apple producer. Candidate verification does not itself deploy, submit a store build or authorize promotion.

## Details

- [Configuration and operating guide](../releases.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-evidence)
- [Workflow source](../../.github/workflows/v2-evidence.yml)
- [All workflows](Workflow-index.md)
