# Monitor · Symbols and releases

Uploads matching symbols or source maps and records the configured Sentry or Crashlytics operation for a verified candidate.

## When to use it

Use the operations required by the product’s existing monitoring setup. Keep Sentry enabled where it is already used. This reusable workflow runs only when a product caller invokes it.

## What it produces

A receipt connecting the uploaded files or release/deployment marker to the verified build and selected monitoring target.

## Setup and limits

Use existing project credentials and retain the original symbol files. Apple tools that require macOS run in the authorized Apple lane. Upload completion does not prove a readable real crash; that test belongs in product adoption.

## Details

- [Configuration and operating guide](../monitoring.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-monitoring)
- [Workflow source](../../.github/workflows/v2-monitoring.yml)
- [All workflows](Workflow-index.md)
