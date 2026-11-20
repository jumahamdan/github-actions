# Mobile · Android build

Runs the product’s Android build configuration and retains the resulting build with its evidence.

## When to use it

Use it to produce an Android staging candidate. The product owns the actual build commands and durable build-number allocation. This reusable workflow runs only when a product caller invokes it.

## What it produces

Build/quality results and, when configured, a candidate bundle containing the retained Android output.

## Setup and limits

Signed builds need the product’s existing signing secrets. Selecting a build number in a form does not change the app unless the actual build command consumes it. Native inspection and store distribution are separate checks.

## Details

- [Configuration and operating guide](../mobile.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-android-build)
- [Workflow source](../../.github/workflows/v2-android-build.yml)
- [All workflows](Workflow-index.md)
