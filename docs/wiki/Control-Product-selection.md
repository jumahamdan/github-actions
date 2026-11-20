# Control · Product selection

Decides which products are affected by a change and checks that the PR follows the agreed branch route.

## When to use it

Use it at the start of a product CI or promotion caller. The supported route is feature → develop → staging → main. This reusable workflow runs only when a product caller invokes it.

## What it produces

A phase and per-product decisions about which checks, verification and delivery work apply.

## Setup and limits

Product and shared paths come from the product configuration. This workflow does not merge a PR or decide to promote production. Juma retains that decision for the products in this rollout.

## Details

- [Configuration and operating guide](../integration.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-plan)
- [Workflow source](../../.github/workflows/v2-plan.yml)
- [All workflows](Workflow-index.md)
