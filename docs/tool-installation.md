# Checksum-locked tool installation

`shared_ci.tools` installs reviewed HTTPS downloads before a job receives credentials. It does not execute vendor installers or package-manager update commands. Use a fresh job-specific destination, put that directory on PATH, and keep it for the duration of the job:

```sh
python3 -m shared_ci.tools --manifest examples/tools-sonar-linux-x64.json --destination "$RUNNER_TEMP/shared-tools"
python3 -m shared_ci.tools --manifest examples/tools-google-sdk-linux-x64.json --destination "$RUNNER_TEMP/shared-tools"
python3 -m shared_ci.tools --manifest examples/tools-bundletool.json --destination "$RUNNER_TEMP/shared-tools"
printf '%s\n' "$RUNNER_TEMP/shared-tools" >> "$GITHUB_PATH"
```

These are concrete Linux x64 recipes, with downloaded bytes and local version/help checks verified. Install only the tools a job needs. Package-manager language dependencies remain separately locked through the setup action. No service login, project deployment, Sonar analysis or device test is performed by installation.

| Recipe | Pinned release | Local verification |
| --- | --- | --- |
| [SonarScanner](../examples/tools-sonar-linux-x64.json) | 8.3.1.16491, Linux x64, bundled JRE | `sonar-scanner --version` |
| [Google SDK](../examples/tools-google-sdk-linux-x64.json) | 582.0.0, beta component 2026.08.21, Linux x64, bundled Python | `gcloud version --format=json`; `gcloud beta device-run sessions submit instrumentation --help` |
| [bundletool](../examples/tools-bundletool.json) | 1.18.3 JAR | `java -jar "$RUNNER_TEMP/shared-tools/bundletool.jar" version` |

The JAR is intentionally non-executable and needs a separately pinned Java runtime. The Sonar recipe retains its bundled JRE; the Android build job should install its declared JDK instead of relying on an incidental scanner dependency. Keep `scanner.version` and mobile tool versions aligned with these manifests. Android SDK/build-tools installations are a separate declared toolchain.

## Manifest contract

A schema 1 manifest declares a nonempty `tools` list and optional `bounds`. Every tool has a unique safe `name`, exact numeric `version`, pinned HTTPS `url`, and lowercase SHA-256. Redirects must remain HTTPS; initial URLs cannot contain credentials, queries, fragments, or a `latest` path segment. Downloaded bytes must match the recorded digest before any archive content is installed. Download limits and timeouts apply. Hashes here were recorded from the official release downloads over HTTPS; they are not a claim of independent signature verification.

Choose one layout:

- A tar/zip `member` installs one regular file at `<destination>/<name>`, preserving the old single-binary contract. The entire archive's names/types are validated, and links are rejected.
- `format: raw` installs the exact downloaded bytes at `<destination>/<name>`. `executable` defaults to true; JARs use false.
- A runtime tree uses `format: tar` or `zip`, optional `strip_prefix`, and `entrypoints` mapping launcher names to relative files. It preserves directories, libraries, configuration and bundled runtimes under `.shared-tools/<name>-<version>-<manifest-hash>`. Shell launchers execute those real entrypoint paths, preserving tools that locate resources relative to their own location. No filesystem symlinks are created. Runtime mode cannot also use `member` or `executable`.

Optional `overlays` are additional checksum-locked tar/zip sources merged into the staged tree. Existing paths cannot be replaced. `environment` adds literal non-secret launcher variables, such as disabling telemetry and automatic update checks; secret-looking variable names are rejected. Provider credentials belong only to the later operation's explicit environment allowlist.

Optional `metadata_files` declares bounded UTF-8 content and its SHA-256 at a new relative path. This supports offline component registration without an installer script. It cannot replace an archive member or the library's own installation receipt. The Google recipe installs the beta command file from its official component archive and registers its manifest/snapshot from Google's versioned 582 component catalog. `gcloud version --format=json` then reports the exact beta version required by the device-test helper. The catalog URL is `https://dl.google.com/dl/cloudsdk/channels/rapid/components-v582.0.0.json`; installing an unpinned current beta component later would invalidate this contract.

## Archive validation

Tar and ZIP are decoded explicitly; `extractall` is never used. Absolute paths, traversal, non-canonical separators, duplicate entries, file-as-directory collisions, encrypted ZIPs, devices, sparse tar entries, and privileged permission bits are rejected. Files receive only 0644 or 0755. Reinstallation checks the complete stored file map and modes; a changed runtime or conflicting launcher fails. A completed tree is renamed into place before launchers are created, so partial extraction never becomes an executable entrypoint. Use a new destination when changing pins; the installer never replaces an existing different launcher silently.

Links are rejected by default. The reviewed `materialize_internal_links: true` option resolves only existing archive regular-file targets inside the selected runtime. Cycles, excessive depth, absolute targets, escaping targets and missing/directory targets fail. Each resolved link becomes an ordinary copy, counted toward expansion limits. This option is needed because the real Sonar Linux archive uses internal JRE license links and Google bundles Python links. It preserves their content without creating filesystem links.

Google SDK 582 also ships two dangling links in vendored `requests` test-certificate fixtures. Its recipe explicitly excludes `platform/gsutil/third_party/requests/tests`, which is outside the gcloud runtime and commands exposed here. Exclusions apply to exact reviewed subtrees after archive path/type validation; they do not permit traversal. Missing runtime link targets still fail. Do not use broad exclusions to conceal an incomplete required runtime.

Default limits are 128 MiB compressed download per source, 1 GiB expanded tree including materialized copies, and 50,000 entries. `bounds` can lower these or raise them up to 512 MiB, 2 GiB, and 100,000 entries respectively. Limits include overlays and metadata in the resulting tree. Archive runtime directories and launcher paths are checked for existing filesystem symlinks.

## Updating a recipe

Select an exact official release, inspect its archive layout and links, record the downloaded SHA-256, and update its version, URL, prefix, metadata and entrypoints together. Run local version/help checks in an isolated HOME with no provider credentials. Add failure tests for a newly supported archive shape and rerun installer tests. Updating a manifest does not automatically upgrade consumer repositories; their adoption pins and config versions must change explicitly.

References: [SonarScanner releases](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/scanners/sonarscanner), [Google versioned archives](https://cloud.google.com/sdk/docs/downloads-versioned-archives), and [bundletool 1.18.3](https://github.com/google/bundletool/releases/tag/1.18.3).
