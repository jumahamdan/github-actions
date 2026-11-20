# Release · Publish

Publishes the selected GitHub release with its approved notes and exact retained assets.

## When to use it

Use it after the approved tag exists and the product has deliberately selected the candidate and publication operation. This reusable workflow runs only when a product caller invokes it.

## What it produces

A draft or published GitHub release and a receipt verifying the uploaded asset inventory and bytes.

## Setup and limits

It checks the expected commit, library revision and toolchain. Conflicting assets fail rather than silently replacing published files. A GitHub release does not prove TestFlight readiness or public App Store/Play availability.

## Details

- [Configuration and operating guide](../releases.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-release-publish)
- [Workflow source](../../.github/workflows/v2-release-publish.yml)
- [All workflows](Workflow-index.md)
