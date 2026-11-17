# Workflow integration

Use one product at a time. Inventory its existing triggers, toolchain, build outputs, deployment target, credentials and recovery process before changing its caller. A website and mobile application can use separate workflows in one repository or independent repositories. Shared workflows contain generic execution; product configuration and release authority remain in the product repository.

## Branch route and avoiding duplicate work

The branch route is `feature → develop → staging → main`. `v2-plan.yml` enforces the PR route and selects products from their changed paths. [Product selection](../examples/products.json) declares product paths and shared paths. It emits a phase and per-product `check`, `verify`, `deliver` booleans. Invalid promotion routes and `pull_request_target` fail. Changes to shared configuration select all affected products.

- Feature PR into `develop`: run applicable format/lint/types/tests and credential-free AI checks. Cancel superseded PR runs.
- Promotion PR: verify authenticated prior evidence and candidate identity; do not unconditionally repeat all checks. Missing, changed or expired evidence requires a new check before promotion.
- Push to `staging`: build an approved candidate where necessary and deliver to the explicitly configured test environment. Required device and symbol checks precede tester distribution.
- Push to `main`: deliver or publish the selected tested candidate. Apple/Play promotion reuses the existing store build. Managed source deployments explicitly record their provider rebuild.

Use PR events for ordinary CI and accepted pushes/manual events for delivery. Do not trigger identical CI on both a feature push and its PR. Avoid parallel provider auto-deploy triggers bypassing required checks. Never treat a dependency cache as successful test evidence. Full source-tree verification is deliberately conservative; inputs must match before reusing test evidence across merge commits.

## Caller contract

Pin every reusable `uses:` to a full immutable commit. GitHub.com `$/actions/...` loads helpers from that exact workflow revision, including nested calls; the caller does not provide a second library pin. This syntax is not supported on GitHub Enterprise Server. Local actionlint compatibility translates it only in temporary validation copies after checking each referenced action and input. GitHub receives the original syntax.

Common inputs are `config`, `root`, `artifact-name`, explicit runtime/tool manifests, and optional `credentials-config` and `receipt-path`. Credentialed workflows require `environment`; map only their named secrets. Install tools before authentication. `actions/v2-run` allows product-owned job composition when a reusable job's fixed setup does not fit, including Apple-hosted hooks.

To retain a new build, supply `candidate-config` and `candidate-manifest`. The module's check receipt must be retained at the path declared by the candidate's typed check contract. Candidate creation validates trusted check digests and files, and the workflow uploads only its prepared allowlisted bundle. Use the exported `candidate-artifact-name`, `candidate-identity` and `source-sha` in downstream jobs; names include the producing run and attempt. Choose artifact retention long enough for promotion, symbol recovery and rollback. [Runtime selections](runtime-workflows.md) explains cross-job check imports, security refresh and retry state.

To consume it, provide `artifact-run-id`, `artifact-run-attempt`, `artifact-source-sha`, `artifact-identity`, `artifact-workflow`, `artifact-input-name` and explicit `artifact-allowed-branches`. When a PR's tested merge SHA differs from its branch head, also supply `artifact-run-head-sha` and the permitted PR event in an uncredentialed verifier. For an earlier job in the same running workflow, provide its exact completed `artifact-producer-job` name. Authentication checks the producing run/job and artifact digest before import. Existing source files must be identical; conflicting files are never overwritten. See [transport](artifacts.md) for trust and size limits.

`v2-required.yml` evaluates `toJSON(needs)` against an explicit boolean applicability map. Always run the gate, including after failures; every applicable job must report success. Unexpected skips, cancellations, missing results and undeclared jobs fail. A pending store review is not a passing distribution gate; preserve its receipt and operation state, reconcile the provider state, then resume deliberately.

## Completion for each adoption

Record exact pins, real workflow results, credential presence/permissions, target/environment separation, artifact retention, a readable crash report where applicable, and recovery evidence. For mobile, verify signing, installed upgrades, tester access and actual store state. For hosting, verify HTTP/DNS/TLS and association files. A product with missing release prerequisites can adopt checks without enabling delivery. Repo moves require reviewing caller paths, environments, token permissions and provider bindings as well as copying YAML.

Do not enable a paid GitHub feature, new provider credential, Wiki, production trigger or additional telemetry merely to make every repository look alike.
