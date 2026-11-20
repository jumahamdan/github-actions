# Deploy · Firebase

Runs a selected Firebase operation for the product’s configured hosting, functions, rules or other supported configuration.

## When to use it

Use it when the product uses Firebase and the exact project, environment, candidate and operation have been selected. This reusable workflow runs only when a product caller invokes it.

## What it produces

A provider-operation receipt tied to the selected source/candidate and Firebase target.

## Setup and limits

Use the product’s existing Google federation and narrowly scoped permissions. Some managed services build again from the verified source; the receipt must not call those bytes identical to a local build. This does not enable Crashlytics in an app.

## Details

- [Configuration and operating guide](../deployment.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-firebase)
- [Workflow source](../../.github/workflows/v2-firebase.yml)
- [All workflows](Workflow-index.md)
