# Deploy · AWS

Runs the selected supported AWS infrastructure, static-site or backend deployment operation against an explicit target.

## When to use it

Use it after the product has selected its candidate, AWS account/region, environment and approved deployment operation. This reusable workflow runs only when a product caller invokes it.

## What it produces

An operation receipt with the verified candidate and affected provider identities, such as a stack or static-site target.

## Setup and limits

Use the product’s existing AWS role through temporary credentials. Protected-resource checks and product recovery rules still apply. AWS deployment does not implicitly invoke Bedrock models; live AI evaluation has its own workflow and permissions.

## Details

- [Configuration and operating guide](../deployment.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-aws)
- [Workflow source](../../.github/workflows/v2-aws.yml)
- [All workflows](Workflow-index.md)
