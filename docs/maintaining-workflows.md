# Maintaining the shared library

The library has three separate layers: editable definitions, generated reusable workflows, and product callers. Keep those roles separate so a common fix has one source and each product keeps its own release decisions.

| Layer | Where to edit | What it controls |
| --- | --- | --- |
| Shared workflow definitions | `workflow-templates/workflows/` and named fragments under `workflow-templates/` | Reusable inputs, setup, execution order and evidence handling |
| Generated executable YAML | `.github/workflows/v2-*.yml` | What GitHub executes; regenerate rather than hand-edit |
| Shared actions | `actions/v2-*` | Setup, module execution, retained-artifact transport and Wiki publication |
| Implementation modules | `shared_ci/` | Validated provider operations and common behavior |
| Product caller starters | [templates/callers](../templates/callers/README.md) | A starting CI/build/delivery composition with explicit product prerequisites |
| Product configuration | The adopting product's repository | Commands, paths, targets, existing credential names, evidence selection and promotion authority |

## Change a reusable workflow

1. Edit its source under `workflow-templates/workflows/`. Put definitions shared by several workflows in an appropriately named fragment. Keep provider-specific permissions, credentials and conditions explicit.
2. Generate the executable YAML and derived documentation:

   ```sh
   python3 scripts/generate-workflows.py
   python3 scripts/workflow-contracts.py
   python3 scripts/workflow-catalog.py
   ```

3. Review the generated diff as executable code. Confirm the intended change to triggers, inputs, defaults, secrets, permissions, job/step order and evidence handling. Display-name changes should not rename callable files or job IDs.
4. Run the required checks below and the tests affected by the change. Update the capability ledger when behavior or its test evidence changes.
5. Land the source, generated outputs, tests and describing documentation in the same PR. Publish a new immutable version after verification; never move an old tag.

The source format uses named references and explicit mapping composition, not arbitrary shell or Python execution. The generator rejects unresolved references, reference cycles and conflicting mapping entries. Refer to its [source format instructions](../workflow-templates/README.md) for the exact syntax. Generated headers point maintainers back to their source.

Workflow names use a category and a clear purpose, such as `Checks · Code quality` or `Deploy · AWS`. The executable `v2-*.yml` paths are compatibility interfaces; a clearer display name is not a reason to break existing callers. The [catalog](workflow-catalog.md) groups the names for browsing, while the [caller reference](workflow-contracts.md) lists the authoritative input/secret/output contracts.

`wiki-publish.yml` is a small manually invoked entrypoint that is also reusable by a product's manual caller. It uses the shared Wiki action and publisher. It is maintained directly because it supports `workflow_dispatch` as well as `workflow_call`; the 26 generated v2 workflows remain call-only. Wiki pages live in `docs/wiki/`, pass normal documentation checks, and publish only on an explicit manual run. See [Wiki publishing](wiki-publishing.md).

## Check generated files and callers

```sh
python3 scripts/generate-workflows.py --check
python3 scripts/workflow-contracts.py --check
python3 scripts/workflow-catalog.py --check
python3 scripts/render-product-workflows.py --check
python3 scripts/validate-workflows.py
python3 -m shared_ci.docs --config .ci/docs.json --root .
git diff --check
```

Install the hash-locked Python requirements and checksum-locked validation tools documented in [tool installation](tool-installation.md). Run `python3 scripts/run-tests.py` when library behavior or its generators change. Library CI also scans source, audits the locked AI tools and exercises the actual reusable quality and mock-evaluation jobs. Mock AI validation uses no provider key or paid model call.

Workflow validation also covers the retained legacy TypeScript caller and requires independent lint, type-check and test controls. Its type errors block the job; lint and test failures keep their existing advisory policy. The other legacy callers remain compatibility-only and are not included in this validator's syntax scan.

`--check` commands verify the checked-in representation. They fail on drift instead of silently accepting a different generated file. When adding a reusable workflow, also add its purpose/category to `scripts/workflow-catalog.py` and regenerate the catalog; an unclassified workflow fails that check.

## Dependency updates

Dependabot can propose action-pin updates directly in generated YAML. Treat that proposal as an input to review: apply the intended pin change to its shared source fragment or workflow source, regenerate, and inspect the complete generated diff. If a common fragment serves multiple workflows, those generated callers should receive the same reviewed update.

Compatible action updates may be completed together in one reviewed maintenance PR. Close the superseded bot PRs only after that replacement passes its checks and merges. Grouping bot updates does not synchronize the editable source templates; the generation check remains required.

Artifact uploads explicitly use ZIP archives because candidate import verifies the authenticated archive checksum before inspecting its contents. Keep `archive: true` when upgrading the uploader and `digest-mismatch: error` on CodeQL report downloads. Exercise an actual hosted upload, download and candidate import when changing these actions. Shared Node setup disables automatic package-manager caching; the legacy TypeScript caller retains its explicit npm lockfile cache. These action updates do not change the separately selected application Node version.

Do not import an arbitrary generated workflow back into the source, overwrite a fragment based on an unreviewed bot diff, or bypass the drift check. Review changes to downloaded-tool hashes and package locks through their existing integrity checks as well. The [legacy workflows](workflow-catalog.md#legacy-compatibility) are separate compatibility interfaces; migrating or upgrading their behavior requires its own consumer assessment.

## Adopt in a product

Use the [caller starter instructions](../templates/callers/README.md) and select only the recipes needed by that product. Render with a reviewed full library SHA into a fresh directory, inspect the result, then add the selected files through the product's normal PR process. Rendering does not deploy anything or configure provider accounts.

Product entrypoints should be easy to recognize: CI, a staging build/deployment, and an explicit release. Start delivery manually while proving real credentials, signing, targets, candidate provenance and recovery. Enable an automatic staging trigger only when the product's integration is verified. Production promotion remains the product owner's decision.

Configuration examples are contracts to adapt, not proof that a product has passed its gates. Keep exact candidate selections and original successful check receipts; never substitute a fresh build simply to make a promotion proceed. See [integration](integration.md), [workflow selections](runtime-workflows.md) and [credential ownership](credentials.md).
