# AI · Live evaluations

Checks the real behavior of a selected AI provider against the product’s prompts, examples and assertions.

## When to use it

Use it when a product deliberately needs live model or endpoint evaluation. It runs only when a caller selects it and the live configuration allows the branch and event. This reusable workflow runs only when a product caller invokes it.

## What it produces

A result receipt with the selected provider/model, input identities and aggregate evaluation results. Raw prompts and responses are not published in the receipt.

## Setup and limits

Live calls can incur charges. The product supplies its existing provider credentials, explicit model, assertions and spending controls. OpenAI, Anthropic and AWS Bedrock use separate credential mappings. A successful mock evaluation does not replace this check.

## Details

- [Configuration and operating guide](../ai-evaluations.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-ai-live)
- [Workflow source](../../.github/workflows/v2-ai-live.yml)
- [All workflows](Workflow-index.md)
