# Security · Scans

Runs the product’s selected security scanners and applies a clear severity and exception policy.

## When to use it

Use it for the relevant source, dependencies or retained candidate. A release may need fresh security information even when its original build is unchanged. This reusable workflow runs only when a product caller invokes it.

## What it produces

A sanitized security receipt with finding counts, scanner/rule/database identities and the policy verdict.

## Setup and limits

Supported profiles include Gitleaks, npm audit, pip-audit, Trivy and reviewed Semgrep rules. Each product selects applicable profiles. Missing tools, malformed results and expired exceptions fail. This workflow does not rotate secrets or enable paid GitHub features.

## Details

- [Configuration and operating guide](../security.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-security)
- [Workflow source](../../.github/workflows/v2-security.yml)
- [All workflows](Workflow-index.md)
