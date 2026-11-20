# Release · Notes

Prepares release notes for the product’s selected public, tester and internal audiences.

## When to use it

Use it when preparing a specific approved candidate for tester or customer communication. This reusable workflow runs only when a product caller invokes it.

## What it produces

Notes bound to that candidate and the configured audience. PR-derived categories are a starting draft; authored summaries and product-specific fields remain reviewed content.

## Setup and limits

Public notes must exclude internal operational details. This workflow does not publish a GitHub release, send messages or submit store notes by itself. Destination-specific publication remains separate.

## Details

- [Configuration and operating guide](../releases.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-release-notes)
- [Workflow source](../../.github/workflows/v2-release-notes.yml)
- [All workflows](Workflow-index.md)
