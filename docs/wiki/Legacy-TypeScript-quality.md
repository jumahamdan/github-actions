# Legacy · TypeScript quality

The earlier Node/TypeScript quality caller, retained for existing integrations.

## When it runs

Only when an existing caller invokes it. It is not selected for new product adoption.

## What to know

It has historical failure-tolerant lint/test behavior and older action references. Do not assume its green result provides the strict quality evidence required by the newer release flow.

Keep it until its consumers have migrated through their own reviewed changes. Removing a legacy file is not part of creating this Wiki.

## Details

- [Current replacement guidance](../quality.md)
- [Compatibility policy](../workflow-catalog.md#legacy-compatibility)
- [Original callable workflow](../../.github/workflows/code-quality-typescript.yml)
- [All workflows](Workflow-index.md)
