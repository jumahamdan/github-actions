#!/bin/bash
# Vendor this small loader into ci_scripts; review its SHA when adopting a library revision.
set -euo pipefail
: "${CI_PRIMARY_REPOSITORY_PATH:?Xcode Cloud repository required}"
: "${CI_XCODE_CLOUD:?Xcode Cloud required}"
: "${SHARED_LIBRARY_REPOSITORY:?Exact HTTPS GitHub repository required}"
: "${SHARED_LIBRARY_REVISION:?Immutable forty-character commit required}"
: "${SHARED_MOBILE_CONFIG:?Product JSON path required}"
: "${SHARED_MOBILE_OUTPUT:?Product receipt path required}"
[[ "$CI_XCODE_CLOUD" == "TRUE" || "$CI_XCODE_CLOUD" == "true" ]]
[[ "$SHARED_LIBRARY_REPOSITORY" =~ ^https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]]
[[ "$SHARED_LIBRARY_REVISION" =~ ^[0-9a-f]{40}$ ]]
shared_checkout="$(mktemp -d "${TMPDIR:-/tmp}/shared-mobile.XXXXXX")"
trap 'rm -rf -- "$shared_checkout"' EXIT
# Disable user/system Git config: credential helpers and url rewrites are unnecessary here.
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_TERMINAL_PROMPT=0
git -C "$shared_checkout" init -q
git -C "$shared_checkout" remote add origin "$SHARED_LIBRARY_REPOSITORY"
git -C "$shared_checkout" -c protocol.file.allow=never fetch --depth=1 origin "$SHARED_LIBRARY_REVISION"
git -C "$shared_checkout" checkout --detach -q FETCH_HEAD
[[ "$(git -C "$shared_checkout" rev-parse HEAD)" == "$SHARED_LIBRARY_REVISION" ]]
# -P prevents source checkout shadowing the shared_ci module. Python 3.11+ is required.
PYTHONPATH="$shared_checkout" python3 -P -m shared_ci.mobile \
  --root "$CI_PRIMARY_REPOSITORY_PATH" --config "$SHARED_MOBILE_CONFIG" --output "$SHARED_MOBILE_OUTPUT"
