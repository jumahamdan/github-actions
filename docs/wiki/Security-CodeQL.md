# Security · CodeQL

Analyzes supported source languages with CodeQL and applies the shared security policy to the resulting findings.

## When to use it

Use it when the product selects CodeQL, has the necessary entitlement, and its code can be analyzed by the supported Linux toolchain. This reusable workflow runs only when a product caller invokes it.

## What it produces

A security receipt. Uploading analysis to GitHub’s Security tab is a separate explicit option requiring the appropriate permissions and plan.

## Setup and limits

Confirm licensing and avoid running both GitHub default setup and this custom setup. This workflow does not supply Swift/macOS, Dart or PHP CodeQL analysis; those products need their applicable native or language checks.

## Details

- [Configuration and operating guide](../security.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-codeql)
- [Workflow source](../../.github/workflows/v2-codeql.yml)
- [All workflows](Workflow-index.md)
