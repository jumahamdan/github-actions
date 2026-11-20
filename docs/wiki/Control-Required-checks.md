# Control · Required checks

Combines the selected job results into a clear required check, so missing or failed work cannot disappear from the final verdict.

## When to use it

Use it at the end of CI, promotion or delivery callers. It must run even when an earlier job fails. This reusable workflow runs only when a product caller invokes it.

## What it produces

A receipt identifying passed, explicitly inapplicable or permitted submitted checks. Unexpected skips, failures and missing results fail the gate.

## Setup and limits

Production stays strict. The Android staging starter can explicitly accept a successful pending/submitted store operation while preserving unverified availability. Apple staging still waits for actual TestFlight readiness.

## Details

- [Configuration and operating guide](../runtime-workflows.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-required)
- [Workflow source](../../.github/workflows/v2-required.yml)
- [All workflows](Workflow-index.md)
