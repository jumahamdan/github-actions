# Legacy · TypeScript quality

The earlier Node/TypeScript quality caller, retained for existing integrations.

## When it runs

Only when an existing caller invokes it. It is not selected for new product adoption.

## What to know

Lint, TypeScript type checking and tests are separate steps, each controlled by its own input. Disabling lint does not disable type checking or tests. Type errors fail the job; lint and test failures retain their historical advisory behavior. Do not assume its green result provides the strict quality evidence required by the newer release flow.

Its checkout and Node setup actions are pinned to reviewed commits. Library validation checks the workflow syntax and the independent check controls so misplaced steps cannot silently remove these checks again.

Keep it until its consumers have migrated through their own reviewed changes. Removing a legacy file is not part of creating this Wiki.

## Details

- [Current replacement guidance](../quality.md)
- [Compatibility policy](../workflow-catalog.md#legacy-compatibility)
- [Original callable workflow](../../.github/workflows/code-quality-typescript.yml)
- [All workflows](Workflow-index.md)
