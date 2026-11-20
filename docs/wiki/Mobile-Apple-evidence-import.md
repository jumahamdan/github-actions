# Mobile · Apple evidence import

Retrieves retained Apple build evidence from the private bridge and verifies its connection to the selected successful native build.

## When to use it

Use it when an Apple pipeline needs to bring an Xcode Cloud candidate into later GitHub checks or store operations. This reusable workflow runs only when a product caller invokes it.

## What it produces

An authenticated imported candidate and its native build/check evidence.

## Setup and limits

The product must configure its exact Apple workflow, private storage location and allowed access role. Apple compilation and signing remain in Xcode Cloud or an explicitly authorized Mac lane. This job does not use a GitHub macOS runner.

## Details

- [Configuration and operating guide](../apple-bridge.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-apple-import)
- [Workflow source](../../.github/workflows/v2-apple-import.yml)
- [All workflows](Workflow-index.md)
