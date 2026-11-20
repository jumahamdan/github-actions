# Mobile · Android verification

Checks the selected Android bundle and derives the configured device-test artifacts from those retained bytes.

## When to use it

Use it after a successful Android build, before the product distributes or device-tests that candidate. This reusable workflow runs only when a product caller invokes it.

## What it produces

An inspection receipt with app/build identity and retained artifact information; configured candidate composition can bind the inspection to the build evidence.

## Setup and limits

The caller authenticates the original build and supplies the required tools and signing configuration. This does not run the app on a device or publish it to Google Play.

## Details

- [Configuration and operating guide](../mobile.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-mobile-verify)
- [Workflow source](../../.github/workflows/v2-mobile-verify.yml)
- [All workflows](Workflow-index.md)
