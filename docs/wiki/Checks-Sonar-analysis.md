# Checks · Sonar analysis

Sends selected source and verified matching test coverage to the product’s Sonar project, then waits for its quality gate.

## When to use it

Use it when the product has chosen Sonar analysis and its account supports the required branch or PR mode. This reusable workflow runs only when a product caller invokes it.

## What it produces

An analysis receipt and the Sonar quality-gate result. It uses coverage from the original test job without rerunning the tests.

## Setup and limits

The caller supplies the existing SONAR_TOKEN, project settings and authenticated quality artifact selection. Fork PRs cannot enter this credentialed analysis path. Account and plan setup are verified during product adoption.

## Details

- [Configuration and operating guide](../sonar.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-sonar)
- [Workflow source](../../.github/workflows/v2-sonar.yml)
- [All workflows](Workflow-index.md)
