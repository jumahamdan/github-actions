# Deploy · Web hosting

Runs a configured website or application deployment/promotion through the supported Cloudflare, Vercel or Netlify adapter.

## When to use it

Use it for the provider actually hosting that product, after its candidate and destination have been approved. This reusable workflow runs only when a product caller invokes it.

## What it produces

A deployment receipt identifying the candidate, provider, target and result.

## Setup and limits

Each provider receives only its explicitly named existing token. Review any provider-side automatic deployment triggers to avoid duplicate or ungated deployment. A successful provider operation still needs the product’s deployed health checks.

## Details

- [Configuration and operating guide](../deployment.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-web-deploy)
- [Workflow source](../../.github/workflows/v2-web-deploy.yml)
- [All workflows](Workflow-index.md)
