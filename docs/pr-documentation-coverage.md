# PR documentation and release-note coverage

The versioned [v2-pr-docs action](../actions/v2-pr-docs/action.yml) checks a pull request's documentation coverage and explicit behavior declaration. A consumer supplies one small [settings file](../examples/pr-docs.json); product paths stay in that repository.

The action is a coverage check. It cannot infer whether behavior really changed, judge whether an explanation is true, or prove documentation is accurate. Reviewers still own those decisions. Keep the existing [generated-fact and link checks](documentation.md) and [release validation](releases.md).

## Rules and PR template

For a nonexempt code change, update a configured documentation file or supply this line in the PR description:

```text
Docs updated (which) / none needed (why): none needed — Existing documentation already describes the unchanged behavior.
```

The reason must contain at least four words and 20 letters, with no template placeholders such as `TODO`, `TBD` or `<your reason>`. A filename written in the description does not replace an actual documentation change. When documentation changed, the ordinary template line can name it; the file diff supplies coverage.

Every nonexempt code change also needs exactly one explicit behavior declaration:

```text
Behavior changed: yes
```

or:

```text
Behavior changed: no — Internal refactoring preserves the existing public behavior.
```

A declared `yes` requires a nonempty added release-note line, even for otherwise exempt changes. The check reads the complete release-note document at the exact PR head and verifies the diff's added line positions. Blank lines, headings (including underlined headings), comments, fenced or indented examples, blockquotes, placeholders, removed files and missing text diffs do not qualify. Use ordinary prose or a list item for the release note. A declared `no` needs a specific reason with the same length rules. Ordinary Markdown bullets, checkboxes and bold labels are accepted; declarations inside comments or fenced examples are ignored. Duplicate declarations fail rather than selecting a convenient answer.

## Repository settings

```json
{
  "version": 1,
  "code_paths": ["src/**", "api/**", "ui/**"],
  "doc_paths": ["README.md", "docs/**"],
  "release_note_paths": ["CHANGELOG.md"],
  "exemptions": {
    "tests": ["spec/**"],
    "additional": ["examples/evidence/**"]
  }
}
```

Selections are case-sensitive repository-relative glob patterns. `*` matches across directories; a leading `**/` also matches zero directories. Each required selection has 1–100 patterns. Unknown fields, absolute paths and parent traversal fail. The module never executes a command from the settings or PR contents. Use a reviewed settings file and preserve the repository's normal review protection for configuration/workflow changes.

Built-in exemption groups cover:

- Tests: `tests/**`, `test/**`, nested `tests/` or `test/`, `*.test.*`, `*_test.*` and `test_*.py`.
- CI: `.github/**` and `.ci/**`.
- Evidence: `docs/evidence/**`.

Optional `exemptions.tests`, `exemptions.ci`, `exemptions.evidence` and `exemptions.additional` extend those selections. Exemptions apply per path: a PR mixing tests and application code still requires code coverage. Evidence files do not count as product documentation. Renames classify both the old and new path. Documentation-only changes need no behavior declaration, but any supplied declaration must be valid.

## Workflow integration

Use a normal `pull_request` workflow with `edited` included, so changing the description reevaluates its declarations. Keep this job independent of path-filtered build/test jobs; otherwise a body-only edit could leave an old result in place. Replace the action placeholder below with the reviewed **full 40-character library commit SHA** containing the action. This is a template, not a runnable pin.

```yaml
name: PR documentation coverage
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, edited, ready_for_review]
permissions:
  contents: read
  pull-requests: read
concurrency:
  group: pr-documentation-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  coverage:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803
        with:
          persist-credentials: false
      - uses: jumahamdan/github-actions/actions/v2-pr-docs@REVIEWED_40_CHARACTER_SHA
        with:
          config: .ci/pr-docs.json
          github-token: ${{ github.token }}
```

The action runs Python 3.11 or later (available on the shown runner) and needs no third-party Python package. The token only reads PR metadata/files and configured release-note contents. It makes no comments, pushes, deployments or provider calls. `pull_request_target` is deliberately unsupported. No product-specific path, private source text or credential belongs in the public library example.

The PR file list is paginated, counted and deduplicated. More than GitHub's 3,000-file limit, incomplete pages, unavailable diffs/documents, or a head/base/body change during the read fail with an actionable message. At most 20 changed release-note files of 1 MiB each are read; PR descriptions are limited to 256 KiB. A stale run for a replaced head cannot pass. Current description text is fetched from GitHub, including on an `edited` event.

The command prints only sanitized result counts, fixed explanatory errors and repository/PR/commit identity; it does not print PR prose, source paths, patch contents or credentials. A policy failure exits nonzero. A GitHub read failure also blocks rather than assuming the missing files are exempt. Retry the latest run after correcting the description or resolving API access; do not bypass the required check.

## Verification

The [regression tests](../tests/test_pr_docs.py) cover valid coverage and reasons, placeholders, explicit behavior changes, release-note prose versus comments/fences/headings, renamed paths, mixed exemptions, complete pagination, concurrent PR edits and invalid metadata. Library tests use synthetic repository names and local API fixtures, with no live credentials or publishing.
