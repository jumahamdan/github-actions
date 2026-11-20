# Shared CI/CD workflows

Reusable checks, builds, release evidence and delivery adapters for independent products. Each product chooses its language, providers and release process, then calls the shared pieces it needs.

**Start with the [workflow catalog](docs/workflow-catalog.md).** It groups the workflows by purpose and explains when they run. The [Wiki source pages](docs/wiki/Home.md) provide a plain-language overview and a page for each workflow. The long GitHub Actions list includes callable building blocks; it does not mean every workflow runs on each change. `Library · Validation` checks this repository, and GitHub manages the separate Dependabot update runs.

## Start here

1. Choose the relevant [product caller starters](templates/callers/README.md) for CI, a staging build/deployment and a deliberate release.
2. Follow the [integration guide](docs/integration.md), preserving the product's branch route and promotion authority.
3. Supply the exact [workflow inputs, named secrets and outputs](docs/workflow-contracts.md). Use existing project credentials and a reviewed full library commit SHA.
4. Check the [capability ledger](docs/capabilities.md) for implementation/test coverage and what must still be proved against the real product.

Adopt one product at a time. Templates do not configure accounts or establish that a product is ready to deploy. Keep its current working pipeline until the selected replacement is verified.

## How the library is organized

| Location | Purpose |
| --- | --- |
| `workflow-templates/` | Editable workflow definitions and shared named fragments |
| `.github/workflows/v2-*.yml` | Generated callable workflows with names grouped by purpose |
| `actions/v2-*` | Shared actions for setup, execution, artifact transport and Wiki publishing |
| `shared_ci/` | Validated implementation modules, split by function |
| [templates/callers](templates/callers/README.md) | Product workflow starters and their renderer configuration |
| `examples/` | Product configuration examples with synthetic identifiers |
| `tests/` and `fixtures/` | Unit/contract tests and isolated verification inputs |
| `docs/` | Integration, provider contracts, recovery and generated references |
| `docs/wiki/` | Reviewed source pages for the shared-library Wiki |

For library changes, edit the source definitions and regenerate; see [Maintaining the library](docs/maintaining-workflows.md). Common execution and source fragments avoid copying provider behavior into every product. Each generated workflow remains readable and independently callable.

## Guides

| Area | Documentation |
| --- | --- |
| Format, lint, types, tests and build commands | [Quality profiles](docs/quality.md) |
| Branches, applicability and promotion | [Integration](docs/integration.md), [workflow selections](docs/runtime-workflows.md) |
| Keys, secrets, variables and identity | [Credential contracts](docs/credentials.md), [caller reference](docs/workflow-contracts.md) |
| Toolchain integrity | [Verified tool installation](docs/tool-installation.md) |
| Build retention and verified check reuse | [Artifacts](docs/artifacts.md), [quality reuse](docs/quality-reuse.md), [runtime](docs/runtime.md) |
| Release notes and publication | [Release evidence](docs/releases.md) |
| Apple, Android and device testing | [Mobile](docs/mobile.md), [stores](docs/stores.md), [Apple bridge](docs/apple-bridge.md) |
| Stateful operation retries | [Operation state](docs/state.md) |
| AWS, Firebase, hosting and WordPress | [Deployment adapters](docs/deployment.md) |
| Docker, registries, Compose and SSH | [Containers](docs/containers.md) |
| Sentry and Crashlytics | [Monitoring](docs/monitoring.md) |
| Mock/live AI evaluations and advisory AI review | [AI evaluations](docs/ai-evaluations.md) |
| Security and coverage analysis | [Security](docs/security.md), [Sonar](docs/sonar.md) |
| Documentation and manual Wiki publication | [Documentation checks](docs/documentation.md), [Wiki publishing](docs/wiki-publishing.md) |

Default library validation uses local fixtures and mock AI providers. Live model/device jobs, provider delivery and store submissions require explicit product configuration and verification. Apple compilation and signing stay in Xcode Cloud or an existing authorized Mac lane; there is no GitHub macOS runner.

Run `python3 scripts/run-tests.py` for the test suite. Library CI also verifies generated workflows/caller templates, workflow contracts, shell syntax, documentation, source secrets and locked AI dependencies, and exercises reusable quality and mock-evaluation jobs. See the [maintenance checks](docs/maintaining-workflows.md#check-generated-files-and-callers) for local commands.

## Compatibility

New consumers use the canonical `jumahamdan/github-actions` repository and a full reviewed commit SHA. Preserve each existing consumer's pin until its own adoption review; an owner redirect does not repair an old-owner Actions reference.

The three [legacy callable workflows](docs/workflow-catalog.md#legacy-compatibility) remain for compatibility and are labelled as legacy. Their callable paths and execution contracts are preserved. Known limitations include old-owner moving references and checks that tolerate failure; they are deprecated for new adoption. Do not disable or remove them until their consumers have migrated.
