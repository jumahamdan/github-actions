# Deploy · Containers

Publishes, promotes, deploys or recovers selected immutable container releases using the configured operation.

## When to use it

Use it after the image has been built and tested, or for a deliberate recovery using a retained release. This reusable workflow runs only when a product caller invokes it.

## What it produces

Registry, manifest or server-operation evidence. A successful server deployment records health and the selected release identity.

## Setup and limits

An existing Hetzner or other Linux Docker host can use the SSH/Compose path. The product supplies verified host keys, existing credentials, backup and migration/recovery policy. This adapter does not provision a server, rebuild on it or delete data volumes.

## Details

- [Configuration and operating guide](../containers.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-container-delivery)
- [Workflow source](../../.github/workflows/v2-container-delivery.yml)
- [All workflows](Workflow-index.md)
