# Dependabot Updates

GitHub manages these dependency-update runs. They are not one of the library’s reusable product jobs.

## When it runs

According to each repository’s Dependabot configuration. The shared library currently checks GitHub Actions and its AI-tool npm dependencies weekly.

## What it produces

Dependency update PRs when GitHub finds eligible changes. Those PRs still need their normal checks and review before merging.

## Product setup

Start with the template and retain only the package ecosystems and directories that the product actually uses. Dependabot alerts and security updates also need the appropriate repository settings. This does not automatically merge updates or promote a product to staging or production.

## Details

- [This library’s configuration](../../.github/dependabot.yml)
- [Product configuration template](../../templates/dependabot.yml)
- [Security setup and limits](../security.md#dependency-updates-and-reporting)
- [All workflows](Workflow-index.md)
