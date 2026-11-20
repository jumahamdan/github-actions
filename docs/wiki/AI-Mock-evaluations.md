# AI · Mock evaluations

Checks AI-related application logic with a fake provider so routine tests can be repeatable and run without paid model calls.

## When to use it

Use it in ordinary CI when the product has a configured mock evaluation suite. It is optional in the product CI starter. This reusable workflow runs only when a product caller invokes it.

## What it produces

An aggregate evaluation receipt showing that the configured assertions actually ran and met the required result policy.

## Setup and limits

The supplied direct mock profile uses an echo provider without provider credentials. Product-owned test commands remain trusted code. Mock results do not prove the quality or availability of a real model.

## Details

- [Configuration and operating guide](../ai-evaluations.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-ai-mock)
- [Workflow source](../../.github/workflows/v2-ai-mock.yml)
- [All workflows](Workflow-index.md)
