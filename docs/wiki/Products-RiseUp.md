# RiseUp workflow guide

[Repository](https://github.com/jumahamdan/rise-up-for-autism) · [Rollout evidence](https://github.com/jumahamdan/rise-up-for-autism/issues/200) · [Detailed guide](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/docs/ci-cd.md)

**Scope:** the workflows below are installed on `develop`, `staging` and `main`. The [rollout record](https://github.com/jumahamdan/rise-up-for-autism/issues/200) contains the test and deployment results. Production website publication is excluded from this infrastructure migration.

| Component | Workflow to open | What it does |
|---|---|---|
| Website + HubSpot handlers | [CI](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/ci.yml) | Lint, types, unit/browser/accessibility tests, secrets, dependencies and docs |
| Website previews | [Preview](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/deploy-preview.yml) | Publishes the checked PR build; does not rebuild it |
| Staging + production | [Delivery](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/site-delivery.yml) | Builds the selected environment, verifies approval, publishes checked files and tests health |
| Telegram requests | [Agent](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/bot-agent.yml) · [Release](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/bot-release.yml) | Request → checked preview → one client approval → staged release |
| GitHub `@claude` requests | [Claude](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/claude.yml) | Responds when explicitly called on a GitHub issue or PR |
| Approval + alerts Worker | [Bridge delivery](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/bridge-deploy.yml) | Separate manual deployment; starts with a dry run |
| Google review data | [Weekly refresh](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/google-reviews.yml) | Opens a data-update PR; approve its CI run if GitHub prompts |
| Monitoring | [Uptime](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/uptime.yml) | Checks the live site every five minutes; Cloudflare alerts also create incidents |
| Release notes | [Notes draft](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/release-notes.yml) | Creates notes when requested; does not publish automatically |

**Release path:** feature → develop → staging → main. A release finishes only after deployment health passes. Human promotions require Juma's `release:approved` label; Telegram releases require the signed client approval.

**Shared library:** v2.2.1 supplies quality checks, verified reuse, documentation, candidate evidence, artifact transport, deployment, health and release-note formatting. RiseUp keeps its provider settings and bot rules locally.

**Avoiding repeats:** unchanged promotion checks reuse original successful evidence. Security stays fresh. Preview and staging are hidden from search engines; production needs a separate indexable build.

**Fixing a bad release:** revert the change through a checked PR and the same release path. No automatic data rollback.

Credentials stay in GitHub or Worker secrets. Ordinary tests do not submit real forms, book calls or message clients. Sentry remains off. GitHub dependency alerts and security-fix PRs are enabled.
