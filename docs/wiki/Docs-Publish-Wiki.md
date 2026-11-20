# Docs · Publish Wiki

Publishes the reviewed documentation pages to this repository’s GitHub Wiki when someone deliberately requests it.

## When to use it

Use it after a documentation change has been reviewed and merged, when those pages are ready for readers. It runs manually from the repository’s default branch. Product pushes, pull requests, schedules and app releases do not trigger publication.

## What it produces

The Wiki receives the selected source pages and navigation. Links to other Wiki pages and detailed repository guides are adjusted for the published location. Unrelated Wiki pages are preserved. A concurrent update or an unexpected edit to a managed page stops publication for review.

## Setup and limits

Create the first Home page through GitHub once to initialize the Wiki. The publisher then uses the repository’s built-in GitHub token with permission to write its contents; it does not require a new API key. Existing Wiki pages are never a reason to bypass review of their source documentation.

Each product can reuse this workflow with its own reviewed documentation. Publishing does not write new prose, change a product deployment or publish pages to another repository.

## Details

- [Publishing instructions](../wiki-publishing.md)
- [Workflow and permissions](../../.github/workflows/wiki-publish.yml)
- [Exact caller contract](../workflow-contracts.md#wiki-publish)
- [Workflow index](Workflow-index.md)
