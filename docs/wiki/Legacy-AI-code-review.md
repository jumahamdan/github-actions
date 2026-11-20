# Legacy · AI code review

The earlier AI review caller, retained so existing integrations can keep their original callable path.

## When it runs

Only when an existing caller invokes it. It is not selected for new product adoption.

## What to know

It selects the older extraction/review actions and requires existing provider credentials. Its permissions include PR/issue writes. This is not the same contract as the newer aggregate-only AI evaluation and advisory-review module.

Keep it until its consumers have migrated through their own reviewed changes. Removing a legacy file is not part of creating this Wiki.

## Details

- [Current replacement guidance](../ai-evaluations.md)
- [Compatibility policy](../workflow-catalog.md#legacy-compatibility)
- [Original callable workflow](../../.github/workflows/ai-code-review.yml)
- [All workflows](Workflow-index.md)
