# Publishing a Wiki when needed

Write and review pages in `docs/wiki/`. **Docs · Publish Wiki** publishes only when someone manually requests it from the repository's default branch. PRs, pushes, schedules, app releases and deployments do not publish a Wiki. The same workflow can be called by another repository's explicitly manual workflow.

## First publication

Enable the repository's Wiki and create its initial `Home` page in GitHub. GitHub creates the separate Wiki Git repository after that first page exists; the publisher cannot initialize it through the Git contents API. See [GitHub's Wiki instructions](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages).

Commit the reviewed source pages through a PR, merge it, then open **Actions → Docs · Publish Wiki → Run workflow** on the default branch. Keep the source directory as `docs/wiki`. On the first publication, select **Replace the initial Home page** after checking that Home contains only the intended bootstrap content. Later publications leave that option off.

The publisher uses the same repository's built-in `GITHUB_TOKEN`, with `contents: write` only on its publication job. It does not need a new key, a saved personal token or an external Wiki account. The token is supplied only to the Git operation through a temporary askpass helper, never a remote URL or Git command argument. Wiki availability and account restrictions still depend on the repository's GitHub plan and settings.

## Source pages and conflicts

The source directory contains flat Markdown files such as `Home.md`, `Getting-started.md` and `_Sidebar.md`. Names use letters, digits, hyphens and underscores. `README.md` explains how to maintain the sources and is not published. Other file types, nested directories and symlinks are refused. Pages must be tracked in the exact clean source checkout.

Local links and heading anchors are validated. Links between published pages become Wiki links; links to files outside the Wiki sources point to that exact reviewed repository commit. Links inside code examples are unchanged. External URLs are retained, not checked for availability.

A small `.shared-wiki.json` ownership record tracks published pages and their hashes. Only those pages can be updated or removed; unrelated Wiki pages are preserved. If someone edits or removes a managed page through GitHub, publication stops. Copy and review the intended change in the source repository, then reconcile the Wiki baseline deliberately; do not silently overwrite it. An existing unrelated page with the same name also stops publication. The only explicit first-publication exception is the initial `Home.md` page.

The publisher captures the Wiki's current default-branch commit when preparing its changes, fetches it again before committing, and refuses drift. A concurrent update after that check is rejected by a normal fast-forward push. It never force-pushes, rebases remote edits or replaces a published page blindly. An unchanged source publication is a no-op.

## Reuse from another product

Create a small manual caller, replacing the placeholder with a full reviewed library commit SHA:

```yaml
name: Docs · Publish Wiki
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  publish:
    uses: jumahamdan/github-actions/.github/workflows/wiki-publish.yml@FULL_REVIEWED_LIBRARY_SHA
```

The shared workflow validates the actual caller event and default branch, even if a caller incorrectly attempts to invoke it on a PR or push. It publishes only to the caller repository's Wiki. A product chooses and reviews its own pages before its first publication.

## Local checks and deliberate publication

Validate draft sources without publishing:

```sh
python3 -m shared_ci.wiki --repository jumahamdan/github-actions --check
```

Export the exact clean committed source to a new directory for inspection:

```sh
python3 -m shared_ci.wiki --repository jumahamdan/github-actions --output /tmp/reviewed-wiki
```

An authorized maintainer can publish locally using existing Git authentication:

```sh
python3 -m shared_ci.wiki --repository jumahamdan/github-actions --publish
```

Use `--adopt-existing-home` only for the reviewed initial Home page. These commands create no credentials and do not modify local/global Git authentication. Actions publication additionally enforces the manual default-branch context. The retained repository source and Wiki commit history record what changed.
