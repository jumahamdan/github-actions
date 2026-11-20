# Getting started

Adopt one product at a time. Keep its current working pipeline while the selected replacement is configured and verified.

## Choose the product’s entrypoints

1. Inventory the existing CI, build output, hosting/store targets and release process.
2. Select the relevant [product caller starters](../../templates/callers/README.md).
3. Pin the shared workflows to a reviewed full commit and supply the actual product configuration.
4. Run the selected checks and verify a real staging candidate before enabling delivery.
5. Keep production promotion as the product owner’s deliberate decision.

The templates do not create accounts, signing identities or secrets. Use the project’s existing credentials and pass only the named credentials needed by each job. Keep existing Sentry monitoring enabled where it is used. Apple compilation and signing stay in Xcode Cloud or an explicitly authorized Mac lane; there is no GitHub macOS runner.

## Avoid duplicate work

Feature PRs run their applicable quality checks. Promotion checks can [reuse the original verified results](Checks-Reuse-verified-checks.md) when source inputs, configuration, tools and freshness still match. A new commit with the same relevant content does not automatically require another test run.

Reuse is not a shortcut around current security information, live AI/device checks, signing, store state or deployed health. If the original proof is missing or no longer applies, fresh evidence is required.

## Keep release outcomes clear

A successful upload or store submission can still be waiting for processing or review. Read the actual receipt and provider state before describing a build as available. For a release in another run, select a candidate created in the successful staging attempt; retrying only failed jobs may require a fresh staging run and Android build number.

## Reference

- [Integration guide](../integration.md)
- [Existing credentials, variables and identity](../credentials.md)
- [Exact caller inputs and outputs](../workflow-contracts.md)
- [Capability coverage and adoption limits](../capabilities.md)
- [All workflows](Workflow-index.md)
