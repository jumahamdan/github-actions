# Checks · Code quality

Runs the product’s configured formatting, lint, type, test and optional build commands.

## When to use it

Use it for applicable feature PR checks, or for a separate build configuration where the product needs one. Only the commands selected by that product run. This reusable workflow runs only when a product caller invokes it.

## What it produces

A quality receipt recording actual checks, test counts, tool versions and source/configuration identities. Selected coverage files can be retained for later analysis.

## Setup and limits

The product supplies real commands, dependency locks and complete source inputs. A cache is not a passing test result. Deterministic checks can later be reused only after their evidence is verified.

## Details

- [Configuration and operating guide](../quality.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-checks)
- [Workflow source](../../.github/workflows/v2-checks.yml)
- [All workflows](Workflow-index.md)
