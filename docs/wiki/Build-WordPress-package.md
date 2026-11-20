# Build · WordPress package

Checks selected WordPress code files and packages them for the existing website publishing process.

## When to use it

Use it when a theme, plugin or other tracked WordPress component needs a release package. Dashboard-only content is outside this workflow. This reusable workflow runs only when a product caller invokes it.

## What it produces

A deterministic ZIP, file hashes and a receipt marked as packaged but not published.

## Setup and limits

It does not update a live WordPress site or operate the Hostinger dashboard. Installation, backup, cache clearing and a website check remain part of the explicit publishing handoff.

## Details

- [Configuration and operating guide](../deployment.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-wordpress-package)
- [Workflow source](../../.github/workflows/v2-wordpress-package.yml)
- [All workflows](Workflow-index.md)
