# Checks · Documentation

Checks that required documentation exists, its local links and anchors work, and declared generated content is current.

## When to use it

Use it in CI when documentation changes or when code changes can make documented facts stale. This reusable workflow runs only when a product caller invokes it.

## What it produces

A documentation receipt with file identities and check counts when configured.

## Setup and limits

External web links are counted but not fetched. This check does not write prose or publish a Wiki. Page writing and manual Wiki publication are separate actions.

## Details

- [Configuration and operating guide](../documentation.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-docs)
- [Workflow source](../../.github/workflows/v2-docs.yml)
- [All workflows](Workflow-index.md)
