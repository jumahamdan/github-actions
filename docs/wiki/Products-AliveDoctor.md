# AliveDoctor CI/CD

AliveDoctor is an iOS app with a public website, an admin page, and AWS services. Its product repository owns its workflows and release decisions. It calls the shared library's composite actions for Node quality checks, promotion evidence and secret scanning. Those calls are pinned to the reviewed library commit `cc25699e7f0e189910a1a7d7b8ebb713a9f2ee9b` (v2.2.1).

## Shared library calls

| Shared action | AliveDoctor use |
|---|---|
| `actions/v2-run`, module `quality` | Runs the six configured Node check groups and records their test evidence. |
| `actions/v2-run`, module `reuse` | Authenticates original evidence for eligible unchanged promotion checks. |
| `actions/v2-run`, module `security` | Runs a fresh secret scan with the product's reviewed policy. |
| `actions/v2-setup` | Installs the checksum-verified secret scanner. |

The local wrapper runs `quality` and `reuse` inside the existing Node jobs. The CI secret-scan job calls `v2-setup` and `security` directly. Website delivery, Apple builds, Sentry symbol uploads and release publication use the product's own workflows and scripts described below.

## Product workflows

| Workflow | When it runs | What it does |
|---|---|---|
| **CI** (`ci.yml`) | Pull requests into `develop`, `staging` and `main` | Checks Swift style, repository secrets, app and widget versions, server and site quality, documentation, and offline AI checks. The live evaluation job requires separate activation. |
| **Admin** (`admin.yml`) | Pull requests that affect the admin page, its API or their check configuration | Checks the admin API and builds and checks the admin export. |
| **CD** (`cd.yml`) | Pushes to `staging` and `main`, plus manual runs | Delivers the matching website. Manual runs default to a production dry run and offer an identity-only check on the matching branch. |
| **Delete merged branch** (`cleanup-branches.yml`) | Merged pull requests from the same repository | Removes finished branches while keeping `develop`, `staging`, `main` and designated record branches. |

GitHub Actions uses Linux runners. The merge helper checks the current PR head, verifies that target-branch changes are accounted for, and requires the applicable jobs to pass. On the two same-repository promotion routes, previous promotion merge commits are allowed when they add no content. Keeping the existing job names preserves that gate while a local wrapper calls the shared actions.

## What gets checked again

The six Node jobs cover the backend platform, observability, the early-access API, the public site, the admin API and the admin page. Fresh runs use the pinned Node and npm versions, install from lockfiles, run the required checks and write JUnit results. Their receipts record executed tests and hashes of the declared source and configuration files. [Code quality](Checks-Code-quality.md)

Feature PRs run fresh. On the same repository's `develop → staging` and `staging → main` promotion PRs, five jobs can reuse an original passing receipt. The admin page always builds fresh when required because it downloads fonts from an external service. The secret scan also always runs fresh and uses the product's reviewed rules. [Security scans](Security-Scans.md)

The wrapper looks for evidence from a successful same-repository PR run within seven days. The run must match the final head of a PR merged into `develop` or `staging`, with that merge in the promotion's history. The shared action authenticates the original run attempt and artifact, then requires matching hashes for the declared inputs, including the workflow, check configuration and lockfiles. Missing, expired or changed evidence triggers fresh checks in the same job. Reused receipts cannot be used as the source for another reuse. [Verified check reuse](Checks-Reuse-verified-checks.md)

## From a feature to TestFlight and the App Store

1. A feature PR goes into `develop`. The release owner decides when to promote `develop` to `staging` for TestFlight and `staging` to `main` for release.
2. The repository defines Xcode Cloud workflows for app unit tests on applicable PRs into `develop` and `staging`, and for signing and archiving to TestFlight on applicable staging pushes. Activation is pending. An authorized Mac handles the TestFlight build and upload in the meantime.
3. Testers use the TestFlight build that will go to App Store review. Debug symbols for the app and widget go to Sentry so crash reports can be read. [Symbols and releases](Monitor-Symbols-and-releases.md)
4. The product's `scripts/release/promote.sh` checks the tested build record and compares its app inputs with the promotion. It merges the approved PR, tags the resulting release and publishes the GitHub Release with the build artifacts. A maintainer submits that same build in App Store Connect.

If an app change needs new server behavior, the team releases the compatible backend change first so the TestFlight build can use it. The backend has a separate deployment procedure.

The website deploy is independent of the app release record. Production CD checks the matching three-part `CHANGELOG.md` entry and publishes the website, including its release notes page. The promotion helper uses the full entry for the GitHub Release. CD includes a manual AWS login check on the exact target branch. That job ends after checking identity, and hosted verification on both branches is still pending.

## Keeping this guide current

Update the Wiki source through a reviewed library PR when this integration changes. After the PR merges, a maintainer publishes the Wiki manually from the library's default branch. [Manual Wiki publication](Docs-Publish-Wiki.md)
