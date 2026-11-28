# Product caller starters

These templates give each product a small set of entrypoints: **CI**, **Promotion checks**, **Build**, **Staging**, and **Release**, as applicable. They call the shared reusable workflows at one immutable revision. They do not provision credentials, change branch settings, deploy a product during rendering, or certify a live account.

## Render and review

Choose a selection file: [web/AWS](example.json), [Android](android.json), [Apple](apple.json), [containers](containers.json), or [WordPress](wordpress.json). Replace `example/product` with the actual repository, select the relevant recipes, and adapt the committed configuration paths. Web delivery can select `aws`, `firebase`, `cloudflare`, `vercel`, or `netlify`.

From the library checkout at the version you intend to adopt:

```sh
python3 scripts/render-product-workflows.py \
  --config templates/callers/example.json \
  --library-sha FULL_REVIEWED_LIBRARY_COMMIT \
  --output /tmp/product-workflows
```

`--library-sha` must be the full 40-character compatible library commit; tags, branches, and the placeholder above are refused. Rendering creates a fresh directory containing `.github/workflows/*.yml` and generated `.ci/callers/` gate/reuse policies. Inspect those files, then copy them into the product through its normal reviewed PR. Existing output directories are never overwritten. The renderer does not copy provider example configurations or pretend their synthetic values are real.

Validate all checked-in recipes without credentials or network calls:

```sh
python3 scripts/render-product-workflows.py --check
PATH=/path/to/verified/tools:$PATH python3 scripts/render-product-workflows.py --check --actionlint
```

The validator checks actual reusable inputs, secrets, output names, permissions, dependencies, branch routes and required gates. `--actionlint` also checks the expanded GitHub YAML. It does not validate provider accounts or execute product commands. Source recipes use the same strict `$ref`/`$merge` engine as the [library workflow sources](../../workflow-templates/README.md); GitHub receives expanded ordinary YAML.

## Recipes

| Recipe | Default file | Trigger and behavior |
| --- | --- | --- |
| `ci` | `ci.yml` | PR into `develop`: enforce the feature route, run quality/security/docs, then the required gate. Mock AI is opt-in. |
| `promotion-checks` | `promotion.yml` | PR into `staging` or `main`: enforce `develop → staging → main`, discover matching original quality or run fresh, always run fresh security, then require both results. |
| `candidate-build` | `build-candidate.yml` | Manual dispatch on `staging`: run the product's build/quality configuration and retain its candidate and typed checks. |
| `container-build` | `build-candidate.yml` | Manual dispatch on `staging`: use the container build/test adapter and retain the exact Docker archive and candidate. |
| `web-delivery` | `staging.yml` | Manual dispatch on the fixed staging or main branch: import a selected staging candidate and call the chosen hosting adapter. |
| `container-delivery` | `staging.yml` | Manual dispatch: deploy or roll back an already published candidate using explicit current server release identity. |
| `android-staging` | `staging.yml` | Manual staging dispatch: build signed AAB, derive/verify Android artifacts, then distribute through the configured Play operation. |
| `apple-staging` | `staging.yml` | Manual staging dispatch: import a successful Xcode Cloud build/evidence from the private bridge, then operate on the exact TestFlight build. No GitHub Mac build. |
| `mobile-release` | `release.yml` | Manual dispatch on `main`: promote/select/submit the existing verified Apple or Android build according to committed store configuration. |
| `wordpress-package` | `staging.yml` | Manual staging dispatch: validate/package WordPress files and retain evidence for a separate dashboard publication handoff. |

These are common product pipeline patterns, not an automatic job for every optional service. Add CodeQL, Sonar, Sentry/Crashlytics, device tests, live AI evaluations, health checks or GitHub release publication only when the product policy selects them, using the [workflow catalog](../../docs/workflow-catalog.md) and exact caller contracts. They remain independently reusable modules.

Use one staging/release recipe per product lane or give each an explicit unique `filename`. Duplicate filenames are refused. All delivery recipes are manual initially; enabling staging pushes is a separate product-adoption decision after actual gates and recovery are verified. Production release authority stays with the product owner. Delivery requires the checked-out tree to match the candidate; selecting an arbitrary older candidate does not change the workflow's checkout.

A manual dispatch on the wrong branch cannot silently pass: provider/build jobs are branch-bound and the always-running required gate refuses missing/skipped/failed applicable jobs. Android staging has one explicit submission policy: a successful `distribute` job with exactly `status: pending` and `state: submitted` satisfies the staging gate. Its store receipt keeps `availability_verified: false`; the gate records `submitted`. This lets a successful staging run provide its verified candidate to a later release without claiming that Play has made it available. Drafts, deferred Console review, processing and advisory outcomes fail. The exception cannot run outside the trusted `staging` branch.

Apple staging still requires TestFlight `testing_ready`; its processing/review states remain pending and fail the gate. Retry the state-aware operation after Apple finishes processing or review to obtain actual readiness. Production release gates accept only `passed`. Play upload/promotion always records pending, including after restoring completed state, so a production Play submission alone does not turn the release gate green. Product adoption must provide separate current availability verification or an explicit human verification handoff; this library has no Play availability-verification operation. Apple can separately check availability with `apple-status`. Store operation state uses the same-run `previous` selector for failed-jobs retries; missing uncertain state must be reconciled, never discarded for a blind retry.

## Product configuration

Selection files accept only explicit documented keys. Every workflow needs a `recipe`; `filename` is optional. CI profiles are `node`, `python`, `deno`, `flutter`, `swift`, `php`, `shell`, and `powershell`. `swift` is the Linux-compatible lint/check profile; Xcode compilation and tests stay in the Apple lane.

All recipes accept `binary_manifest`, `npm_tools`, and `python_tools` as literal relative paths, plus `tool_bundles` as an explicit comma-separated subset of `bundletool,android-build-tools,google-sdk,sonar`. Omit an option to preserve the called workflow's tool defaults. Provider CLIs that are not supplied by those defaults require product-owned pinned tools/locks before authentication. `npm_tools` names a directory containing a reviewed `package-lock.json`; `python_tools` names hash-locked requirements. Runtime versions default to the reviewed shared workflow; change version inputs deliberately during adoption when a product needs different supported versions.

CI enables `security` and `docs` by default; `mock_ai` defaults to false. Each boolean changes both the job list and its required-check policy. Supply real `.ci/quality.json`, `.ci/products.json`, `.ci/security.json`, `.ci/docs.json`, and optional mock-AI config. Their commands, tool versions, locks, assertions and source input selection are product responsibilities. The synthetic CI selection disables security/docs only for its isolated contract fixture; it is not a recommended product security policy.

Default configuration paths are visible in each selection example and generated caller. Adapt them to the [existing configuration examples](../../examples) and [exact reusable contracts](../../docs/workflow-contracts.md). Include whole relevant source/config directories, lockfiles and local scripts in quality inputs. Do not include generated result directories in immutable source. Generated `.ci/callers/` policies must be committed before the original quality run so promotion compares unchanged inputs.

Container build configuration uses `"source_sha": "workflow"` to bind the build to the actual GitHub checkout without committing a future SHA. Configure real container tests, the archive/build-record paths and a matching candidate definition. Container delivery expects retained publication and manifest evidence from the product's reviewed registry publication/manifest composition; the host-deploy starter does not implicitly publish images or invent a digest. Use the documented [container operations](../../docs/containers.md) and [runtime selections](../../docs/runtime-workflows.md) for that composition.

## Promotion without duplicate checks

The promotion starter discovers original successful CI evidence automatically and runs fresh quality when none qualifies. It uses `v2-checks.yml` with an optional discovery policy, preserving the ordinary runner and receipt contract. No repository variable selects a mutable "latest" run.

The generated policy fixes the product repository and `ci_workflow`, exact `quality_producer_job` (default `quality / run`), `quality_artifact_prefix` (default `ci-quality`) and `quality_branches` (default `["develop"]`). For PR producers those branches name the destination; the authenticated run's exact source head must also be in the promotion candidate's ancestry. Squashed or unavailable source history falls back to fresh checks. Multiple products should use their own workflow/job/artifact namespace and complete quality input selection. The default freshness limit is 24 hours from original creation; stale, changed, chained or unavailable evidence runs fresh. Current quality failures still block.

Configure `profile` and tool manifests for the same quality runner used by CI. A fresh `security_config` (default `.ci/security.json`) is mandatory on every promotion. Its job never reuses quality receipts and cannot be disabled through recipe settings. Missing, skipped, failed or cancelled security blocks the generated required gate. Native/device verification, live checks and current environment health remain product-owned. See [the discovery and reuse contract](../../docs/quality-reuse.md).

## Credential and variable ownership

The renderer never accepts secret values and never emits `secrets: inherit`. It maps existing named secrets only to the selected provider job. Values referenced through `vars.STAGING_*` or `vars.PRODUCTION_*` must be **repository or organization variables**: environment variables are not available while the caller evaluates `with:`. The reusable provider job separately declares its selected GitHub environment.

| Provider/lane | Existing public variables | Explicit existing secrets |
| --- | --- | --- |
| AWS | `<PREFIX>_AWS_ROLE_ARN`, `<PREFIX>_AWS_REGION` | None; job uses OIDC |
| Firebase / Android stores | `<PREFIX>_GOOGLE_WORKLOAD_IDENTITY_PROVIDER`, `<PREFIX>_GOOGLE_SERVICE_ACCOUNT` | None; job uses Google federation |
| Cloudflare | Product/account identifiers in committed config | `CLOUDFLARE_API_TOKEN` |
| Vercel | Product/team identifiers in committed config | `VERCEL_TOKEN` |
| Netlify | Site identifiers in committed config | `NETLIFY_AUTH_TOKEN` |
| Docker/SSH | Hosts, pinned host keys and target policy in product config | `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS`, and registry username/password when needed |
| Android build/inspection | Signing alias/fingerprint in committed config | `ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_PASSWORD` |
| Apple | `<PREFIX>_ASC_KEY_ID`, `<PREFIX>_ASC_ISSUER_ID`; AWS variables for evidence import | `ASC_PRIVATE_KEY_BASE64` |

`<PREFIX>` is `STAGING` on staging and `PRODUCTION` on main. Map the names to existing project secrets during adoption; do not create replacement keys automatically. Read-only Actions evidence uses `github.token` within the same repository. Apple configuration must match the fixed Cloud workflow/product, private storage account/role and immutable exported evidence. Configure its Xcode Cloud hooks using the [Apple bridge guide](../../docs/apple-bridge.md) and [native hook instructions](../../docs/mobile.md).

## Manual evidence selections and native build numbers

Delivery's `candidate` input is JSON containing string fields `run_id`, `run_attempt`, `run_head_sha`, `source_sha`, `identity`, and `artifact`, obtained from the selected successful staging producer. The producing workflow path and branch allowlist are fixed in the caller. `security_refresh`, `prior_deployment`, `store_selection`, `container_selection`, and `apple_selection` follow the [runtime contracts](../../docs/runtime-workflows.md). They are authenticated selectors, not passing receipts pasted into a form.

`build_number` records the number selected by the product's existing durable allocation process. **Passing `candidate-build` alone does not set an Android versionCode or compile an app.** The Android recipe emits `.ci/callers/read-android-build-number.py` so the actual product build script can read and validate that same dispatch selection:

```sh
build_number=$(python3 .ci/callers/read-android-build-number.py)
exec flutter build appbundle --release --build-number="$build_number"
```

Use that snippet in a committed product-owned build script called by the quality configuration. Its `environment_names` must explicitly include `GITHUB_ACTIONS`, `GITHUB_EVENT_NAME`, `GITHUB_REF` and `GITHUB_EVENT_PATH`, and its `build_credentials` must declare only the Android signing variables needed by that build. Include the helper and build script in `inputs`. The helper checks an approved staging dispatch and accepts positive integers up to 2,000,000,000; it does not allocate or persist numbers. Native inspection and store identity verification still reject mismatched artifacts. Keep credentialed build quality separate from promotion's deterministic CI quality configuration.

The Android two-stage candidate contract uses `checks/build.json` for original quality and `checks/android.json` for native inspection. The verification job authenticates the original quality receipt before creating the final candidate. Adapt candidate inputs/artifacts/config paths to match those exact records. If a product requires separate security, device or symbol evidence, add those actual jobs and authenticated receipt mappings to its reviewed caller; do not remove the required candidate checks to make an incomplete pipeline pass. Apple imports retain the checks produced by the reviewed native build. WordPress packaging produces a handoff artifact; publishing through its dashboard remains an explicit separate operation.
