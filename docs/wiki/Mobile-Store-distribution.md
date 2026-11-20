# Mobile · Store distribution

Performs the configured Google Play or Apple store operation on an existing verified build.

## When to use it

Use it for an explicitly selected tester distribution, store selection, submission or supported status operation. Production release authority remains with the product owner. This reusable workflow runs only when a product caller invokes it.

## What it produces

A store-operation receipt and durable retry state. The result distinguishes a completed submission from processing, review and actual availability.

## Setup and limits

Reuse the existing signed candidate and project credentials. Never discard uncertain operation state and retry blindly. A release must import a candidate from its successful producing attempt; a failed-jobs-only retry may require a fresh staging run. Play submission alone remains pending availability.

## Details

- [Configuration and operating guide](../stores.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-mobile-stores)
- [Workflow source](../../.github/workflows/v2-mobile-stores.yml)
- [All workflows](Workflow-index.md)
