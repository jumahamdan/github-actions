# Mobile · Device tests

Runs the configured Google device tests against selected verified mobile artifacts.

## When to use it

Use it when a product explicitly requires device testing and has configured the real test project, device selection and assertions. This reusable workflow runs only when a product caller invokes it.

## What it produces

A device-test receipt tied to the selected candidate and provider result.

## Setup and limits

Device tests can incur charges and require the product’s existing Google identity and permissions. Unit tests or a passing upload do not prove installation and behavior on a real device; product adoption verifies that coverage.

## Details

- [Configuration and operating guide](../mobile.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-device-tests)
- [Workflow source](../../.github/workflows/v2-device-tests.yml)
- [All workflows](Workflow-index.md)
