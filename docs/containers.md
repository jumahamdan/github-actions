# Containers and verified-host Compose delivery

`python -m shared_ci.containers --config CONFIG.json --root CALLER_CHECKOUT`
implements six operations. Examples in `examples/containers/` are generic configuration templates: replace synthetic hashes, registry names, paths and product commands with the actual reviewed inputs. A caller must not publish these illustrative configurations unchanged.

| Operation | Behavior and receipt |
|---|---|
| `build` | Checks the declared source SHA against a clean tracked checkout, builds once, runs every supplied test against the image ID, saves that image to a Docker archive and records its checksum and successful test identities. |
| `publish` | Requires trusted context, validates the candidate/archive, loads without rebuilding, rejects a conflicting existing release tag, pushes the tested image and verifies the remote image configuration digest. It records immutable registry references. |
| `manifest` | Collects every active service image by registry digest, using publication receipts or explicitly pinned third-party components; verifies each reference against the registry. |
| `promote` | Requires trusted context and the expected previous destination digest, copies the existing manifest with `--prefer-index=false`, and verifies that the destination digest is identical. A retry of the same promotion is idempotent. |
| `deploy` | Uses a verified SSH host, host lock, fresh backup, immutable Compose overrides, migration commands and health/image identity checks; preserves the successful deployment configuration and previous manifest. |
| `rollback` | Restores a selected preserved configuration without rebuilding or removing volumes. Requires expected current/target release IDs and a backward-compatible migration policy. |

## Build and publication

Caller workflows install Docker Engine, Buildx and Compose at reviewed versions. Tests are product-owned argument arrays, never shell strings. A whole argument `{image}` is replaced by the built image ID; `CANDIDATE_IMAGE` is also set for Compose-based smoke tests. These tests must prove actual application behavior. A successful placeholder command is not application assurance. The build module never inherits cloud or registry credentials and rejects credential-like Docker build argument names; secrets also must be excluded from the build context by the product's scanning and `.dockerignore` policy.

The source checkout must match `source_sha` with no tracked edits. Generated/untracked inputs remain the caller's responsibility: include any legitimate generated inputs in release evidence and do not smuggle new source into a build context. Candidate receipts and Docker archives must be obtained from trusted workflow artifacts with verified run/source identity; their checksums alone do not authenticate their origin.

For `v2-container-build.yml` or the shared action wrapper, a committed build configuration can use `"source_sha": "workflow"`. The wrapper resolves this marker only for the container `build` operation, in a GitHub Actions push/manual branch context. It requires an exact lowercase `GITHUB_SHA` matching the actual clean Git checkout before calling Docker, and leaves the committed configuration unchanged. The caller still chooses which branches may build; the marker grants no publication or deployment authority. PR and other event contexts cannot use this marker. An explicit source SHA retains the existing strict comparison, and direct `python -m shared_ci.containers` invocation still requires the literal SHA.

Build and publish run in separate jobs. Upload the candidate archive and receipt, preserving their relative paths, then retrieve them from that trusted build run. The publication job has narrowly scoped package-write permission. Public images need no pull credential. For private registry access, configure `registry.server`, `username_env` and `password_env`; the module creates an empty temporary Docker configuration, sends the password via stdin, and removes the configuration on success or failure. Ambient `~/.docker/config.json` is never used. Authenticated manifest collection also requires `allowed_refs` and trusted context.

The build operation intentionally creates a **single-platform image**, whose registry config digest must match its tested local image ID. Multi-platform index assembly is not implied. Promotion can preserve a pre-existing single-platform manifest or index, but does not certify untested architectures. Every dependent image—including databases, optimizers, routing data and sidecars—belongs in the component manifest. Changes to a mutable alias do not alter a preserved digest.

Serialize tag promotions in the caller workflow. The registry does not offer a universal atomic compare-and-swap for mutable tags; the helper checks before and after the write, but another independent publisher must not race it. A conflicting release tag is refused rather than silently replaced.

## Server preparation and configuration

The adapter works with an existing Hetzner VPS or another SSH-accessible Linux Docker host. It does not provision machines or require a Hetzner API token. The remote host needs Python 3.11+, Docker Engine, Compose with `--wait` support, and permission for the chosen deployment user to run Docker. The project directory must already exist without symlinks. SSH accepts an explicit host/user/port, a named private-key environment variable and exactly one pre-verified known-hosts source (`known_hosts_env` or a workspace-relative `known_hosts_file`). Host keys are never learned using `ssh-keyscan` during deployment; agent forwarding and ambient SSH configuration are disabled.

Create the server's `runtime_env_file` inside the project directory with mode `0600`. It stays on the server and is referenced by path, not copied into release artifacts. Existing runtime values are preserved during deployments and rollback; restoring earlier non-secret configuration or secrets, if necessary, is a separately reviewed product recovery step. The environment file's identity influences the deployment ID without retaining its contents.

`compose_files` are uploaded as preserved snapshots; `support_files` optionally maps relative destination names to non-secret tracked assets such as a Caddyfile or backup script. Paths cannot escape the snapshot. Relative bind-mounted assets must be included; runtime data volumes remain existing named volumes or explicit host paths. `profiles` selects any required Compose profiles. Every **active** service needs a digest-pinned manifest entry; missing/extra entries, any active `build:` declaration and mismatched image references fail before deployment. A Docker volume must never be deleted to simulate a successful update.

The SSH remote command is a fixed Python bootstrap. The validated request and optional narrowly scoped existing registry password travel over stdin; no user-controlled values are interpolated into shell code. Remote registry auth uses a temporary Docker configuration and is removed when execution ends. Expanded Compose output and subprocess stderr are captured without printing, because either can contain runtime secrets.

## Backup, migration and recovery

State lives in a private `.shared-ci-PROJECT/` directory. A kernel file lock serializes operations on the host. All subprocesses have bounded deadlines. `expected_current_release` is the last healthy release ID from the server receipt, or `absent` for initial adoption, and prevents overwriting a changed release decision.

Stateful deployments require a backup producer command. Its stdout is written directly to a temporary file; producer failure, empty output or checksum failure stops execution **before pull, migration or startup**. There is no `pg_dump | gzip` pipeline that can conceal a failing producer. The backup is renamed and retained with size/SHA-256 evidence. A first stateful installation therefore needs an initialized backing database or an explicit bootstrap procedure outside this deployment operation. A nonempty checksummed dump proves capture, not restorability: isolated restore exercises and retention policies remain product requirements.

Optional migration and mandatory health checks are argument arrays. `@compose` as the first argument expands to the exact selected Compose command. The helper pulls digest-pinned images, executes migrations, runs `compose up --detach --no-build --wait`, verifies each running container's image identity, then runs product health assertions. Products with self-migrating images can leave `migrations` empty, but still need backup and an honest migration recovery policy.

On failure, the last healthy record remains intact and the failed attempt is recorded; the helper does not guess whether reverting an image is database-safe. `migration_policy: manual-recovery` blocks image rollback until the product's database recovery procedure is performed. For compatible migrations, `rollback_target: previous` restores the preceding successful release. After a failed upgrade, `rollback_target: last-healthy` restores the current last-healthy snapshot; both require the exact expected target ID and a fresh backup for stateful services. Preserved file tampering blocks rollback. No path invokes `compose down`, deletes a volume, or rebuilds on the server.

Successful local receipts use `status: passed`; server delivery also records `health_status: healthy`. Publication, stable-tag promotion and actual client/server installation remain distinct facts. Existing desktop installers need their own Windows integration proof and must not be replaced by this server adapter.

## Verification

`python -m unittest discover -s tests -p 'test_containers*.py' -v` runs synthetic Docker/registry/SSH contracts and a fake-Docker remote executor. It covers exact-image/archive identity, conflicting tags, immutable promotion, credential cleanup, failed/empty backups, host-lock contention, health failure, preserved snapshots and rollback. It makes no real registry writes, Docker deployments or SSH connections.

CLI options were checked against the installed Buildx/Compose help and official [imagetools inspect](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/), [imagetools create](https://docs.docker.com/reference/cli/docker/buildx/imagetools/create/) and [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/) documentation. Fixture proof is distinct from a live registry round trip, host onboarding, real restore test or product smoke test.
