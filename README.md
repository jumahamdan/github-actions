# Shared CI/CD workflows

Reusable checks, release evidence and delivery adapters for independent products. Each repository chooses its language, platforms and providers; shared code supplies consistent execution, credential boundaries and failure handling.

The v2 implementation is under review. Existing v1 action and reusable-workflow files retain their original behavior. Adopt v2 explicitly using a reviewed full commit SHA; do not move an existing consumer to `main` or silently change its release process.

The preserved v1 entrypoints are deprecated and unsupported for new adoption. Known defects include old-owner moving references and checks that tolerate failure. They remain unchanged so existing callers are not silently altered; v2 is the supported replacement under review.

| Area | Documentation |
| --- | --- |
| Lint, format, types, tests, builds | [Quality profiles](docs/quality.md) |
| Branches, job selection and adoption | [Workflow integration](docs/integration.md) |
| Keys, secrets, variables and identity | [Credential contracts](docs/credentials.md) |
| Toolchain integrity | [Verified tool installation](docs/tool-installation.md) |
| Candidate retention and transfer | [Artifacts](docs/artifacts.md) |
| Reusing verified checks | [Quality reuse](docs/quality-reuse.md) |
| Release notes and publication | [Release evidence](docs/releases.md) |
| Apple, Android and device tests | [Mobile helpers](docs/mobile.md), [stores](docs/stores.md) |
| Store retries and Apple evidence storage | [Operation state](docs/state.md), [Apple bridge](docs/apple-bridge.md) |
| AWS, Firebase, hosting and WordPress | [Deployment adapters](docs/deployment.md) |
| Docker, registries, Compose and SSH | [Containers](docs/containers.md) |
| Sentry and Crashlytics | [Monitoring](docs/monitoring.md) |
| Promptfoo, provider evals and advisory AI review | [AI evaluations](docs/ai-evaluations.md) |
| Security scans and reporting | [Security](docs/security.md) |
| Coverage analysis | [Sonar](docs/sonar.md) |
| Documentation and optional Wiki navigation | [Documentation checks](docs/documentation.md) |
| Complete known service scope | [Capability ledger](docs/capabilities.md) |

Reusable workflows are `.github/workflows/v2-*.yml`; composable actions are `actions/v2-*`; provider-independent modules are `shared_ci/`. Examples use synthetic identifiers. Replace their project paths, tool versions, fingerprints and explicit selections during adoption; they do not provision accounts or certify a real product.

Default library validation uses local fixtures and mock AI providers. Paid model/device jobs, live deployments, store submissions and changes to consumer repositories require their own explicitly selected product workflow. Apple compilation and signing remain in Xcode Cloud or an existing authorized Mac lane; there is no GitHub macOS runner.

Run `python3 scripts/run-tests.py` for the test suite. Self-CI additionally checks workflow contracts, shell syntax, documentation, secrets and locked AI dependencies, and exercises reusable quality and mock-evaluation workflows. Local validation uses the checked-in hash-locked Python requirements and checksum-locked binary manifest under `tooling/`.

Canonical Actions references use `jumahamdan/github-actions`. GitHub repository redirects do not repair an old-owner `uses:` reference. Preserve each consumer's existing pin until its separate adoption review.
