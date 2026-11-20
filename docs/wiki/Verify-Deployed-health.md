# Verify · Deployed health

Checks that the selected deployed target is reachable and reports the expected behavior or release identity.

## When to use it

Use it after a deployment, or when a release policy needs a current health check of an authorized target. This reusable workflow runs only when a product caller invokes it.

## What it produces

A bounded health receipt for the configured HTTP, DNS or TLS checks, without retaining response bodies or secret header values.

## Setup and limits

The product selects meaningful assertions and exact targets. A basic successful HTTP response alone does not prove every business flow. Private authenticated probes require the explicit existing credential and trusted branch context.

## Details

- [Configuration and operating guide](../deployment.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-health)
- [Workflow source](../../.github/workflows/v2-health.yml)
- [All workflows](Workflow-index.md)
