# Editable reusable-workflow sources

Edit these sources, then run `python3 scripts/generate-workflows.py` from the repository root. Commit both the source change and generated `.github/workflows/v2-*.yml` files. `python3 scripts/generate-workflows.py --check` refuses missing or stale output. GitHub reads the generated files; these source fragments are never executed or interpreted by GitHub.

| Source | What belongs here |
| --- | --- |
| [workflows/](workflows/) | One readable source for each callable workflow: display name, provider-specific steps, conditions, permissions and concurrency |
| [inputs.yml](inputs.yml) | Shared input contracts, including exact defaults and descriptions |
| [steps.yml](steps.yml) | Shared checkout, tool installation, candidate import/creation and artifact retention steps |
| [outputs.yml](outputs.yml) | Common workflow outputs and job output expressions |
| [bindings.yml](bindings.yml) | Repeated argument mappings passed to the shared execution action |
| [permissions.yml](permissions.yml) | Explicit common read-only and federated-authentication permissions |
| [secrets.yml](secrets.yml) | Shared named secret contracts; never secret values |

The workflow filenames and job IDs remain stable caller contracts. Display names group the Actions list by purpose. Common fragments centralize repeated definitions while provider-specific behavior stays visible in its workflow source. The three legacy callable workflows and the library's own validation workflow are maintained separately.

## Two explicit composition directives

A reference expands one named fragment. `namespace.name` means the top-level `name` entry in `namespace.yml`, loaded from this directory. References cannot name filesystem paths or remote resources.

```yaml
steps:
  - $ref: steps.checkout
  - $ref: steps.setup-python
```

Mapping composition combines disjoint mappings:

```yaml
inputs:
  $merge:
    - $ref: inputs.core
    - operation:
        description: Explicit operation chosen by this workflow
        required: true
        type: string
```

`$ref` and `$merge` must each be the only key in their mapping. `$merge` never overrides an existing key; a collision is an error even when the two values are equal. For a genuinely different contract, keep that field explicit or use a separately named fragment. A workflow's step list stays an ordered list; reference expansion does not reorder or concatenate steps.

Unknown references, recursive references, invalid fragment names, duplicate YAML keys, implicit YAML merges and aliases are refused. Unused fragments are also validated. Boolean `true`/`false` values stay booleans; `on` remains the GitHub event key. Strings, including `${{ ... }}` expressions, remain literal strings throughout generation. No shell evaluation, environment lookup, template language or arbitrary code execution occurs during expansion.

## Reviewing a change

Review the shared source change and every affected generated workflow. Pay particular attention to credential scope, event conditions, permissions, job IDs, artifact selectors and the order of setup versus authentication. Generation preserves explicit values; it does not decide which access is safe.

The initial extraction was compared with `v2.0.0`: all 26 parsed workflows were identical except their top-level display names. Future behavior changes still need their own review and relevant tests. The generator tests cover shared-edit propagation, deterministic output, stale-file detection and refusal of ambiguous source edits without requiring Git history or tags.

Dependency update tools may propose changes to generated action pins. Apply the intended pin change to the matching shared step or explicit workflow source, regenerate, and review the affected output. A generated-only edit intentionally fails the consistency check.

Product repositories should start from the [caller templates](../templates/), rather than copying these internal source fragments. See the [integration guide](../docs/integration.md) for product ownership and release authority.
