# AI evaluations and advisory review

`python -m shared_ci.ai --config <file.json> --root <source-checkout> --output <receipt.json>` runs configured evaluations and exports only an aggregate receipt. `--source-revision <full-sha>` requires the checked-out revision to match. AI evaluation, AI code review and scheduled content generation are separate operations.

## Modes and trust

| Mode | Credentials | Meaning |
|---|---|---|
| `mock` | None inherited; isolated home and empty credential/config files | Direct Promptfoo uses only the echo provider and deterministic assertions. Custom product mock commands are trusted product code, not an OS/network sandbox. |
| `live` | Explicit selected-provider mappings only | Requires `live_enabled: true`, an allowed GitHub branch/event and product-side spending controls. A failed, missing, partial or malformed evaluation blocks. |
| `review` | Same trust and credential requirements as live | Outputs an advisory aggregate receipt. It never posts comments, files issues, approves, merges or provides release certification. Provider execution errors still fail. |

Model IDs are always explicit. Claude through Anthropic and Claude through Bedrock are different authentication routes. Existing product secrets are mapped by name; the module does not provision credentials or read a vault. Bedrock requires temporary AWS credentials and an invoke-only role established by the caller. An AWS deployment role is not an implicit substitute.

The public examples contain synthetic input only. The [mock example](../examples/ai-mock.json) is runnable without provider keys. The [live template](../examples/ai-live.json) is intentionally disabled and requires the caller's model, policies, input files and credential mapping before activation.

## Tool installation and workflow contract

Install Promptfoo **0.125.1** using the library's integrity-locked npm tool package, in an uncredentialed setup step. Use Node compatible with that package's engine declaration. Put its installed executable on `PATH`, or set `promptfoo.command` to its absolute executable path. Do not use an unpinned download command. The module calls `--version` without credentials and rejects a mismatch before evaluation.

Workflow command:

```sh
python -m shared_ci.ai --config "$AI_CONFIG" --root "$CALLER_ROOT" --output "$AI_RECEIPT"
```

Only the aggregate `AI_RECEIPT` belongs in workflow artifacts. Never upload the workspace wholesale or preserve Promptfoo raw JSON, logs, configuration exports or cache directories. Each run creates a private temporary directory, empty home, empty dotenv/AWS/Google credential files and private report/log locations. They are deleted on success, failure and process timeout. The installed tool itself remains outside that temporary directory.

No automatic `.env` loading is permitted by the shared direct runner: it supplies an empty explicit env file. Telemetry, update checks, hosted generation, sharing, tracing, result database writes and response caching are disabled. This is execution configuration, not a claim that custom trusted code is network-sandboxed. A product runner must honor the same contract and must not independently load credentials or export raw data.

## Configuration

The strict schema is implemented in `shared_ci/ai.py`. Unknown fields and invalid types fail. Required sections:

- `schema_version: 1`, `mode`, `promptfoo.command/version` and `identity.candidate`.
- `evaluation.config`, a workspace-confined path; `inputs`, all relevant prompt/dataset/grader files; `expected_results`, the exact expected result count; and `minimum_pass_rate`, between zero and one. Declare generated case counts through the product adapter rather than accepting partial reports.
- `providers`, each containing `kind`, `id`, `model` and `env`. `env` maps destination names to existing source environment names, for example `{"OPENAI_API_KEY":"AI_OPENAI_API_KEY"}`. Only the selected providers' mappings enter execution. Missing values fail by name only.
- `limits`: `timeout_seconds`, `call_timeout_seconds`, `max_concurrency`, `max_retries`, `max_results`, `max_output_tokens` and `upstream_budget` (`not-applicable` for mocks or `product-enforced` for live/review).
- Live/review additionally requires `live_enabled: true` and explicit `trusted_refs` such as `refs/heads/staging`. Pull requests are denied by default. `allow_pull_requests: true` only admits same-repository heads whose exact branch is allowlisted; `pull_request_target` remains rejected.

Optional `variables` must use `AI_VAR_` names and contain nonsecret values. Optional `evaluation.gate` is an argument-array command for product-specific assertions. It receives the private report through `AI_RESULTS_PATH`, and must exit successfully. Its output is not published. Medical policies, accuracy thresholds, model families and review rules remain product-owned.

| Provider kind | Direct profile and credential destination |
|---|---|
| `openai` | `openai:responses:<model>` or `openai:chat:<model>`; `OPENAI_API_KEY` |
| `anthropic` | `anthropic:messages:<model>`; `ANTHROPIC_API_KEY` |
| `bedrock` | `bedrock:converse:<model-or-inference-profile>`; `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` |
| `gemini` | `google:<model>`; `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| `http` | Explicit HTTP provider configuration; only explicitly mapped `AI_TARGET_` names |
| `mock` | `echo`; no credentials |

## Direct Promptfoo and existing product runners

The default `engine: promptfoo` accepts a **JSON Promptfoo configuration**. The module writes a private runtime copy, checks the provider inventory, overrides output-token limits for direct model providers, sets per-provider `maxRetries`, and controls concurrency/timeouts. Inline provider credential fields and overrides of dotenv/output/tracing/command-line settings are rejected. Product prompts, assertions, files and transforms stay product-owned. Mock mode forbids executable transforms, file imports and model-backed assertions; an echo target with an LLM grader would not be credential-free.

The executed command is real Promptfoo:

```text
promptfoo eval --config <private-runtime.json> --env-file <empty.env>
  --output <private-results.json> --no-cache --no-write
  --no-progress-bar --no-table --max-concurrency <limit>
```

The direct profile rejects model-graded assertions and per-test/grader provider overrides rather than letting Promptfoo select a default paid model. Product-owned JavaScript/Python assertions remain trusted code and must not make undeclared model calls.

For model-graded assertions, existing YAML suites or multi-stage generated red teams, use `engine: product` and `evaluation.command` as an argument array. The adapter receives `AI_EVAL_CONFIG`, `AI_RESULTS_PATH`, `AI_EXPECTED_REVISION`/`EXPECTED_REVISION`, `AI_MAX_OUTPUT_TOKENS`, `AI_MAX_CONCURRENCY`, `AI_MAX_RETRIES` and the pinned `PROMPTFOO_VERSION`. It must execute its actual evaluation/generation/gate sequence, honor the deadline and fresh-result policy, and write Promptfoo-compatible JSON to `AI_RESULTS_PATH`. `EVALS_ENV` points to an empty file. The module still validates version, trust, credentials, complete result counts and the final result structure. A callback that only writes a made-up passing report is not an evaluation implementation.

Promptfoo's assertion-failure exit `100` is distinguished from execution failure: complete valid results may meet a product's configured pass threshold despite individual assertion failures. Provider errors always block. Other nonzero exits fail. Passing rows in mock/live mode need assertion evidence; an empty/no-test run cannot pass. Known Promptfoo JSON variants with rows under `results.results`, `results.outputs` or top-level `results` are validated against their statistics.

## Candidate identity, privacy and limits

Receipts contain candidate/source revision, configured provider/model IDs, hashes of declared input files and configuration, the pinned tool version, aggregate counts, optional reported usage, gate status and duration. They contain no prompts, raw responses, error payloads, credential values or model-authored prose. Review mode intentionally does not publish free-form model feedback; a separately authorized product UI can provide its own private review delivery.

`release: true` is permitted only for live mode. HTTP release targets additionally require `revision_probe` with `url_env`, response `header`, optional `token_env` and GET/POST settings. The probe uses HTTPS, refuses redirects and requires the target's revision header to match the checked-out candidate. Failed probes block. Direct model evaluations bind the model/prompt/input identities in the receipt rather than claiming a hosted application revision. Response caching is disabled for all modes in this initial implementation; this is conservative, and does not disable a provider's internal prompt caching.

The shared process deadline is enforced, including version checks and product gates. Direct Promptfoo receives concurrency, per-call/total-timeout, provider retry and direct-provider output-token limits. A result-count maximum is also checked before/after execution against the declared expected count. That declaration is **not** a global upstream call budget: retries, generated attacks, judges, custom code and backend model chains can make additional requests. Product/account controls must bound those requests and spending. The receipt records that boundary as a declaration rather than proof.

Optional `max_reported_tokens` and `max_reported_cost_usd` are **post-run accounting gates**. Missing accounting fails when a limit is required. These cannot reverse incurred charges and are never described as hard spending caps. Direct HTTP targets and custom product runners own their token/retry implementation; the receipt does not claim the library applied a direct model cap there.

## Verification and source contracts

`tests/test_ai.py` exercises real subprocess fixtures for clean and failing runs, credential isolation, version mismatch, malformed/empty/partial reports, provider errors, wrong revisions, post-run accounting, deadline cleanup, required product gates, unsupported config, direct provider cap construction and advisory behavior. Actual Promptfoo 0.125.1 echo success and deliberate failure are checked separately without provider credentials or model calls.

Primary contracts checked November 15, 2026: [CLI/options and dotenv behavior](https://www.promptfoo.dev/docs/usage/command-line/), [configuration](https://www.promptfoo.dev/docs/configuration/reference/), [output format/privacy](https://www.promptfoo.dev/docs/configuration/outputs/), [retry layers](https://www.promptfoo.dev/docs/configuration/rate-limits/), [echo caveats](https://www.promptfoo.dev/docs/providers/echo/), and [Bedrock Converse](https://www.promptfoo.dev/docs/providers/aws-bedrock/). Recheck these contracts when the pinned tool changes. No live provider/account integration is claimed by fixture or echo evidence.
