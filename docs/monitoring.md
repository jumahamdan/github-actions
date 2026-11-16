# Required crash symbol uploads

`python -m shared_ci.monitoring --config product-monitoring.json --root retained-build --output receipt.json`

Sentry and Crashlytics remain enabled by the product's SDK configuration. This module does not change consent, collection settings, SDKs, event contents or provider accounts. It uploads the files needed to read crashes from the selected build. Required upload failure blocks the job; there is no allow-failure option. A passing upload receipt explicitly says symbolication_verified:false: a controlled crash in the real app is still required during adoption.

## Evidence and tool setup

Use the candidate/inspection/expected_identity/expected_source_sha/allowed_refs contract described in [stores](stores.md). The candidate must retain every required binary, dSYM file, Dart symbol file, R8 mapping and JavaScript/source map pair. Hashes are checked before and after upload, and all typed check/source/config bindings are verified. Original files stay intact. Upload jobs never regenerate or modify the tested candidate.

Install tools before credentials enter the job. `tool` is a command prefix plus the exact stdout of `--version`; it is verified without provider credentials. Executables must be on PATH or absolute paths because Sentry and Firebase run in temporary clean directories. Pin the binary/package and its integrity in the tool-provisioning workflow as well: a version string alone is not binary authenticity. The standard build tools (JDK, binutils readelf and Gradle dependencies) belong in the candidate toolchain and pinned setup. macOS-only Firebase upload-symbols runs on the Apple host, not a GitHub macOS runner.

Credentials use explicitly named environment roles. Sentry maps only the selected token into SENTRY_AUTH_TOKEN. Dotenv loading, update checks and ambient home configuration are disabled, with a temporary HOME/current directory. Firebase CLI uses a caller-created ephemeral ADC file named by credentials_env; retain neither this file nor credentials in candidate artifacts. Temporary upload directories are removed even when commands fail. Full CLI output and secret values are not included in receipts.

## Supported operations

| Operation | Binding and supported upload |
| --- | --- |
| sentry-native | Requires mobile's matching binary/dSYM UUID+architecture entries, retained file hashes and every inspected target; uses debug-files upload with explicit IDs, --require-all and --wait. |
| sentry-dart | Matches the GNU build ID of each retained Dart ELF symbol file to libapp.so inside the selected AAB, obtains Sentry debug IDs through debug-files check --json, then requires every ID on upload. |
| sentry-r8 | Byte-compares mapping.txt with the AAB's embedded ProGuard mapping, computes its Sentry UUID and requires it in packaged sentry-debug-meta.properties before proguard upload --require-one. |
| sentry-sourcemaps | Requires nonempty v3 maps with unique debug IDs already injected into the corresponding retained JavaScript; uploads with --validate --strict --wait --no-rewrite. No post-build injection. |
| sentry-release | Creates the matching app-id@version+build release, optionally finalizes it. It does not create a deployment marker. |
| sentry-deploy | Creates a release/environment deploy marker only with a supplied hash-bound successful deployment receipt matching candidate_identity and environment. |
| crashlytics-apple | Requires every inspected dSYM UUID and all bundle files, validates GoogleService-Info.plist app/bundle identifiers, hash-pins the Firebase SDK upload-symbols executable, then uploads the retained dSYM bundles. |
| crashlytics-dart | Matches retained Dart ELF build IDs to AAB libraries and validates google-services.json app/package identity; Firebase CLI crashlytics:symbols:upload receives only copied selected files. |
| crashlytics-r8 | Byte-compares mapping with the AAB, validates Firebase app/package configuration, then runs the specific Crashlytics Gradle mapping-upload task with its original build outputs and pinned inputs. A dry run must show only that task; actual skipped/NO-SOURCE tasks or other tasks fail. |

Native UUID proof is provided by mobile inspection, which actually compares dwarfdump outputs. File hashes alone never establish native symbol compatibility. `symbols` lists individual DWARF files for native Apple uploads, not unverified directory globs. `dart_libraries` maps each retained symbol path to its exact `base/lib/<abi>/libapp.so` entry. R8 is linked to the mapping embedded by AGP, and Sentry additionally verifies its packaged runtime UUID. Source map debug IDs must be generated in the tested build.

Sentry configuration names explicit org/project/environment/release/dist. Release must equal app ID + `@` + version + `+` + build; dist must equal build. Self-hosted Sentry requires an explicit HTTPS URL with no embedded credentials. Native uploads themselves are associated by debug ID; release/environment metadata is recorded alongside them. SDK event release/environment values remain the product's responsibility.

Firebase app configuration must be a retained candidate input/artifact. Apple upload-symbols is pinned by SDK file SHA256 rather than an invented CLI version flag. Its public app configuration is supplied via -gsp; it does not use a global interactive Firebase login.

Crashlytics R8 uses the product's existing pinned Gradle plugin. Keep the complete original upload metadata and generated build outputs alongside the mapping; retain their hashes in candidate artifacts. Pin wrapper JAR/properties, dependency verification metadata, plugin versions and reviewed build configuration in candidate inputs and list them as gradle.build_inputs. Set cache_env to an explicitly provided Gradle cache directory. Exclude all dependencies that would regenerate mapping IDs or rebuild using exclude_tasks; dry-run refuses anything except the exact upload task. This is deliberately strict: if the project's plugin cannot upload its retained outputs alone, establish that product's supported build-time upload path before adoption. The library does not invent a Firebase CLI R8 mapping command.

Caller artifact retention must preserve the candidate and all symbol files for retry and incident investigation. Receipts report upload completion, not a live crash test, Sentry issue resolution, store availability or observability account readiness.

Primary contracts: [Sentry debug files](https://docs.sentry.io/cli/dif/), [Sentry CLI source](https://github.com/getsentry/sentry-cli/tree/master/src/commands), [Sentry configuration](https://docs.sentry.io/cli/configuration/), [Firebase Flutter symbols](https://firebase.google.com/docs/crashlytics/flutter/get-deobfuscated-reports), [Apple symbols](https://firebase.google.com/docs/crashlytics/ios/get-deobfuscated-reports), and [Android mappings](https://firebase.google.com/docs/crashlytics/android/get-deobfuscated-reports). Unit tests simulate tools; no real monitoring upload has been performed by library validation.
