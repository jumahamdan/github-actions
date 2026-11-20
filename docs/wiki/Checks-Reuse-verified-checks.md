# Checks · Reuse verified checks

Proves that a previous successful quality run still applies, so a promotion can avoid repeating the same tests.

## When to use it

Use it for promotion checks when the original run, configuration, source inputs, tools and shared implementation can be authenticated and still match. This reusable workflow runs only when a product caller invokes it.

## What it produces

A reuse receipt preserving the original checks, creation time, run and attempt, plus the new verification record. It executes no tests.

## Setup and limits

Missing, changed, failed or expired evidence blocks reuse. The starter’s freshness limit is 24 hours. Current security information, live AI/device checks, signing, deployed health and release authority have their own requirements.

## Details

- [Configuration and operating guide](../quality-reuse.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-reuse)
- [Workflow source](../../.github/workflows/v2-reuse.yml)
- [All workflows](Workflow-index.md)
