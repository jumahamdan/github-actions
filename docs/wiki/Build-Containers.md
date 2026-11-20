# Build · Containers

Builds a Docker image once, runs the product’s image tests and saves the tested image for later delivery.

## When to use it

Use it when a product ships as a container. Its build configuration must identify the actual source and tests; the starter begins with a manual staging build. This reusable workflow runs only when a product caller invokes it.

## What it produces

A Docker archive, its checksum and the build/test receipt. Candidate evidence can retain those exact files for publication and deployment.

## Setup and limits

The current build creates a single-platform image. Registry publication and server deployment are separate operations. The build context must exclude secrets; this job does not inherit registry or cloud credentials.

## Details

- [Configuration and operating guide](../containers.md)
- [Exact inputs, outputs and secret names](../workflow-contracts.md#v2-container-build)
- [Workflow source](../../.github/workflows/v2-container-build.yml)
- [All workflows](Workflow-index.md)
