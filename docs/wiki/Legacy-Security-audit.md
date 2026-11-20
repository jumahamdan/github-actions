# Legacy · Security audit

The earlier npm/Python dependency-audit caller, retained for existing integrations.

## When it runs

Only when an existing caller invokes it. It is not selected for new product adoption.

## What to know

Its configurable failure handling and older tool-installation behavior differ from the newer strict scanner policy. Unsupported ecosystem selection does not provide the newer policy’s coverage guarantees.

Keep it until its consumers have migrated through their own reviewed changes. Removing a legacy file is not part of creating this Wiki.

## Details

- [Current replacement guidance](../security.md)
- [Compatibility policy](../workflow-catalog.md#legacy-compatibility)
- [Original callable workflow](../../.github/workflows/security-audit.yml)
- [All workflows](Workflow-index.md)
