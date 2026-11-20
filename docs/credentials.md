# Credentials, variables and environments

Reuse existing project secrets. This library creates no provider account or API key. Callers map each named secret explicitly; do not use `secrets: inherit`. Select a GitHub environment for credentialed jobs and configure its allowed branches and approval policy in the product repository. Environment secrets belong to the job's environment; they cannot be passed as environment secrets through the caller's `workflow_call` syntax. Check organization/repository access and environment precedence during adoption.

| Capability | Non-secret configuration | Existing credential role |
| --- | --- | --- |
| AWS delivery | Account, region, role ARN, environment | GitHub OIDC to scoped deployment role |
| Bedrock evaluation | Region, model/inference profile, invoke-only role ARN | Separate GitHub OIDC evaluation role |
| Firebase and Google devices | Project, WIF provider, service account, app/bucket | Google Workload Identity Federation; scoped access token |
| Google Play | Package, track, rollout, WIF provider/service account | Android Publisher OAuth scope; Play Console app access also required |
| Apple | Issuer ID, key ID, app/build/group IDs | Existing ASC P-256 private key; generated ten-minute JWT, or supplied short-lived JWT |
| Android signing | Upload certificate fingerprint, key alias | Existing keystore and passwords |
| Sentry | Organization, project, release, environment | Project-scoped `SENTRY_AUTH_TOKEN` |
| Crashlytics | Firebase app ID, symbol type, pinned tool | Google federation plus retained native/Gradle configuration |
| Cloudflare | Account, Worker, environment | Scoped `CLOUDFLARE_API_TOKEN` |
| Vercel / Netlify | Organization/project/site ID, target | `VERCEL_TOKEN` / `NETLIFY_AUTH_TOKEN` |
| Docker registry | Registry, repository, digest | Read/write role for publication; separate read-only server role |
| SSH / Hetzner | Host, user, deployment directory | Existing SSH key and independently verified known-hosts file |
| AI | Explicit provider, model, dataset, limits | Selected project's existing OpenAI, Anthropic or Gemini key; Bedrock uses AWS |
| GitHub receipts / releases | Repository, workflow, run, candidate identity | Read-only Actions token for evidence; contents-write only for publication |
| Sonar | Server/project, branch, authenticated coverage selection | `SONAR_TOKEN`; read-only Actions token |

`credentials-config` points to a JSON contract with `schema: 1` and `variables`, `secrets`, `files`. Every declaration names an uppercase environment variable and a boolean `required`. Public variables may have `allowed` values or a regular-expression `pattern`. Secret files declare `encoding: base64|text`, `format: binary|pem|json`, and an `output_env` file-path variable. Required missing/invalid values fail before the operation. Values never appear in the preflight report.

Use [Apple](../examples/credentials-apple.json) or [Android](../examples/credentials-android.json) contracts. Decoded files live outside the source/artifact directory with private permissions. Cleanup runs on success or exception; derived secrets are masked. Never put keys in `.ci` JSON, Git variables, build arguments or release notes. GitHub's Google auth action cleans its own temporary federation file.

For AWS, restrict the OIDC trust to the repository and intended GitHub environment/ref and audience `sts.amazonaws.com`. For Google, restrict federation by repository identity and environment/ref, then grant the service account only the selected product resources. An OAuth scope alone is not authorization. No long-lived cloud access key is required by these workflows.

Record the owner, purpose, account/environment, expiry, rotation procedure and emergency revocation method for each credential **name**, never its value. Confirm replacement credentials before removing old signing/provider access. Logs and receipts are not a secret vault. Environment setup and a real permission check are adoption evidence, separate from this library's synthetic tests.
