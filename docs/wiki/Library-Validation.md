# Library · Validation

Checks that the shared library itself is consistent and working before its changes are adopted by products.

## When it runs

On PRs into the library’s `main` branch, pushes to `main`, or a deliberate manual run. This is library maintenance; products run their own selected callers.

## What it checks

Generated workflow sources and contracts, caller templates, syntax, documentation links, source secrets, locked AI dependencies, the test suite and a mock AI evaluation. The quality and mock jobs exercise the library’s own reusable modules.

## What a passing run means

The library’s automated checks passed for that revision. It does not certify real product signing, provider accounts, store availability or deployment recovery. Those remain product adoption checks.

## Details

- [Validation workflow](../../.github/workflows/github-actions-ci.yml)
- [Library maintenance guide](../maintaining-workflows.md)
- [Capability and verification ledger](../capabilities.md)
- [All workflows](Workflow-index.md)
