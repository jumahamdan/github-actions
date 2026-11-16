# Store delivery

`python -m shared_ci.stores --config product-store.json --root retained-build --output receipt.json`

This module selects and distributes an existing candidate. It does not build, sign, or upload an IPA. Xcode Cloud performs the Apple upload. Apple API build IDs are different from Xcode Cloud's CI_BUILD_ID.

## Required evidence and credentials

Every operation needs schema 1, an explicit operation, candidate and inspection paths, expected_identity and expected_source_sha from the trusted caller's selection, allowed_refs, token_env and state_file. Candidate identity includes source/tree, toolchain, library revision, all input/artifact hashes, typed check requirements and check hashes. The mobile inspection must be one of those checks and match the source, app identifier, version and build. Retained files are rehashed. This works in an archive directory without `.git`; authenticate the downloaded workflow/run/artifact before calling it. A locally supplied JSON hash does not authenticate its origin.

The inspection contract is `kind: apple|android`, `source_sha`, `application: {id, version, build}` and `artifacts: {relative_path: {sha256, bytes}}`. Apple additionally needs distribution_ready:true from signed archive verification. Android needs upload_signer_sha256. All paths use the same retained root. Keep native archive contents, all inspection artifacts, candidate inputs and typed check configurations/receipts together.

The trusted GitHub push/manual job passes a short-lived Google OAuth access token or App Store Connect JWT through the named environment variable. Authorization is an HTTP header. Tokens never appear in argv, state or receipts. HTTP redirects with credentials are refused. App permissions and store legal/privacy/review metadata are caller account setup; this module does not assert they are complete. Apple-hosted build hooks produce evidence; credentialed distribution jobs run after authenticated retrieval.

## Google Play

Operations are `play-inspect`, `play-upload`, and `play-promote`. Use `play-inspect` to retain the target track digest, then supply it as expected_track_digest for the approved upload/promotion request. Each operation uses its own state_file. Drift fails before changing the track.

An upload verifies the retained AAB, matching package/versionCode from bundletool inspection, its cryptographic JAR signature using jarsigner, and the exact upload certificate SHA256 using keytool. Install the pinned JDK before execution. Android upload certificates are normally self-signed: jarsigner strict certificate-trust bit 4 is accepted only alongside the explicit signer pin; unsigned-entry/signature failures are rejected. The Play upload response's versionCode and sha256 must equal the selected candidate. An already uploaded identical version is reused; a conflicting hash fails. Promotion reuses a version already on its explicit source track and verifies the Google bundle hash without uploading again.

A track update replaces its release list with the selected candidate. This intentionally does not silently retain unrelated older releases or multiple form-factor releases. Such a product requires an explicit expanded track policy before adoption. Staged rollouts accept a fraction strictly between zero and one; completed/draft releases cannot carry a fraction.

Commits explicitly use `changesInReviewBehavior=ERROR_IF_IN_REVIEW`; the API default can cancel an existing review. review_policy is `submit` or `defer`. Optional allow_review_defer_fallback applies only to Google's specific rejection that changes cannot be sent automatically; other errors fail. Drafts, deferred Console review and submitted changes return status:pending, with availability_verified:false. Successful API submission is not proof of availability to users.

State is persisted before upload/commit. Completed requests reuse the saved receipt; uploads can resume by reconciling version+hash. A lost commit response stops subsequent retries for explicit Console/API reconciliation. Do not delete that state and retry blindly. Persist it as a private job artifact and serialize operations per package/track in the caller; concurrent Play edits are account-side conflicts.

## Apple

Operations: `apple-build` waits for processing; `testflight` assigns explicit groups and optionally submits beta review; `apple-select` selects the exact build for an existing App Store version; `apple-submit` submits that version through reviewSubmissions/reviewSubmissionItems; `apple-status` checks actual downloadable distribution state.

Every operation verifies the selected ASC build's owning app, bundle ID, marketing version, platform and build number against the inspection. Reject invalid/failed/expired builds. Processing timeout returns pending. This proves the selected unique store identity; the upstream Xcode Cloud upload receipt must establish which archive was uploaded. ASC does not offer a generic downloadable IPA hash comparison here.

TestFlight checks group ownership and type, avoids duplicate assignment, and passes only when each group's actual internal/external beta state is ready. Missing export compliance, processing and beta review remain pending. Store selection requires expected_selected_build (null for an empty selection) or an already matching build. Existing active submissions require an explicit submission_id; unrelated/duplicate submission items fail. A lost submission-creation response requires reconciliation rather than a second creation. Review approval, contracts, developer release and availability remain separate states. `apple-status` passes availability only for a downloadable READY_FOR_DISTRIBUTION/legacy READY_FOR_SALE version selecting the same build.

CLI exit 0 means the requested operation completed; exit 2 writes a pending receipt; failures exit nonzero. `apple-select` and `apple-build` can pass while availability_verified remains false. A publishing gate must require its intended distribution state, never only process success. No auto-release settings, tester notification settings or app SDKs are changed.

## Validation and primary contracts

Tests use synthetic candidates and mocked API/CLI calls; no real token, upload, review submission or distribution was performed. Adoption requires account-specific permission and build/track proofs.

Contracts checked against [Google bundles upload](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.bundles/upload), [tracks](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks), [commit](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/commit), and Apple's [App Store Connect OpenAPI specification](https://developer.apple.com/sample-code/app-store-connect/app-store-connect-openapi-specification.zip) version 4.6. Examples contain synthetic placeholders; replace selections and pins from authenticated evidence.

## Optional Firebase App Distribution

`operation: firebase-distribution` supports the product's existing Firebase internal-testing route. It is separate from Play tracks. It uploads the exact inspected APK, then validates Firebase's app/version/build/release-note identity before distributing to explicit existing group aliases. It requires a successful device-test receipt for that APK hash and successful monitoring receipts covering the required symbol hashes, all bound into candidate checks. A failed or absent required check blocks upload.

The Firebase settings include app_id, candidate-bound app_config (google-services.json), artifact (APK), tool (pinned Firebase CLI prefix/version), credentials_env (ephemeral ADC file outside the retained root), group_aliases, release_notes (candidate-bound UTF-8 file), device_check and symbol_checks (receipt path to required symbol file paths). Top-level token_env supplies a short-lived Google OAuth token for supported release inventory/distribution REST calls. Both can come from the same existing federated service identity; no credential is created by the adapter.

The current Firebase CLI returns JSON success without a Release object. The adapter validates that JSON and uses Firebase's supported release-list API for the actual app/version/build/notes. The public release schema does not return a binary SHA256; the receipt therefore records the exact selected local upload hash and provider_binary_sha256_available:false. It does not pretend to compare a provider hash. APKs avoid the separate AAB internal-app-sharing re-signing path.

The upload CLI receives no tester/group option. After release verification, an explicit release:distribute call grants the configured groups access. That provider operation sends tester release/invitation emails; invoking this route in a product is a distribution decision. No email or upload was sent while validating this library. Tester addresses and signed binary-download URLs are never retained in receipts.

A pre-existing matching version/build without this request's completed state fails for reconciliation. Saved successful state avoids uploading or notifying testers again. A lost upload/distribution response also stops subsequent automatic retries because release metadata alone cannot prove which bytes/groups completed. Processing timeout writes pending state and can resume metadata polling without another upload. A passed distribution receipt means the provider accepted distribution to the selected groups; tester installation remains unverified.

Create the final candidate after required device and symbol receipts exist. Monitoring receipts include artifact_records so symbol bytes are bound independently of the earlier candidate identity, avoiding a circular identity dependency. Retain the input config for each check alongside its typed assurance/source binding.

Primary contracts: [Firebase CLI Android distribution](https://firebase.google.com/docs/app-distribution/android/distribute-cli), [CLI implementation](https://github.com/firebase/firebase-tools/blob/master/src/commands/appdistribution-distribute.ts), [release resource](https://firebase.google.com/docs/reference/app-distribution/rest/v1/projects.apps.releases) and [group distribution](https://firebase.google.com/docs/reference/app-distribution/rest/v1/projects.apps.releases/distribute).
