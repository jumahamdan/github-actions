# Shared workflows — instructions for Claude and Codex

`AGENTS.md` links to this file. Keep one set of instructions for both tools.

- Start with [README.md](README.md), the [workflow catalog](docs/workflow-catalog.md) and [maintenance guide](docs/maintaining-workflows.md).
- Edit reusable workflow sources in `workflow-templates/`, then regenerate the published YAML and references. Do not hand-edit generated workflows. Keep callable paths, inputs and job IDs compatible with existing consumers.
- Shared behavior belongs in `shared_ci/` or shared actions. Product commands, targets and release decisions belong in the product repository. Adopt one product at a time; use reviewed full commit pins and existing project credentials.
- Open changes as PRs into `main`. Describe the change in plain language, attach validation evidence, and merge only when required checks pass. A library merge does not authorize a product promotion or deployment.
- For documentation-only edits, run `python3 -m shared_ci.docs --config .ci/docs.json --root .` and `git diff --check`. For implementation or generator changes, also run the maintenance guide's checks and `python3 scripts/run-tests.py`.
- Wiki source lives in `docs/wiki/`. Keep product pages short: component, workflow, where to fix it, and any remaining verification. Distinguish code merged, provider setup completed and a successful real deployment.
- Publish reviewed Wiki changes only when requested, after merging, using the manual `wiki-publish.yml` workflow on `main`. Follow [Wiki publishing](docs/wiki-publishing.md); never overwrite conflicting website edits.
- Use local fixtures and mock providers for default library validation. Live AI calls, store submissions and provider deployments need the adopting product's authorization. Never commit secrets or introduce GitHub macOS runners.
- Work in an isolated branch/worktree when another session uses the repository. After merging, remove your clean completed worktree and local branch; leave other sessions' work alone.
