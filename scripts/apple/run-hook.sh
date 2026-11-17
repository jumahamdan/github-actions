#!/bin/bash
# Vendor this small loader into ci_scripts; review its SHA when adopting a library revision.
set -euo pipefail
: "${SHARED_PYTHON:?Absolute path to the preprovisioned Python interpreter required}"
: "${SHARED_PYTHON_VERSION:=3.12.3}"
[[ "$SHARED_PYTHON" == /* && -x "$SHARED_PYTHON" ]] || {
  printf '%s\n' 'SHARED_PYTHON must name an executable at an absolute path.' >&2
  exit 1
}
[[ "$SHARED_PYTHON_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  printf '%s\n' 'SHARED_PYTHON_VERSION must be an exact major.minor.patch version.' >&2
  exit 1
}
# Check before creating a checkout or making a network request. -I ignores ambient
# PYTHONPATH/user packages; -B avoids writing bytecode into the verified library.
"$SHARED_PYTHON" -I -B -c 'import sys
expected = tuple(int(part) for part in sys.argv[1].split("."))
if sys.version_info < (3, 11):
    sys.exit("Shared Apple hooks require Python 3.11 or newer; provision the pinned interpreter before this hook.")
if sys.version_info[:3] != expected:
    sys.exit("Shared Apple hook Python version differs from SHARED_PYTHON_VERSION.")' "$SHARED_PYTHON_VERSION"
: "${CI_PRIMARY_REPOSITORY_PATH:?Xcode Cloud repository required}"
: "${CI_XCODE_CLOUD:?Xcode Cloud required}"
: "${SHARED_LIBRARY_REPOSITORY:?Exact HTTPS GitHub repository required}"
: "${SHARED_LIBRARY_REVISION:?Immutable forty-character commit required}"
: "${SHARED_APPLE_CONFIG:=${SHARED_MOBILE_CONFIG:-}}"
: "${SHARED_APPLE_OUTPUT:=${SHARED_MOBILE_OUTPUT:-}}"
: "${SHARED_APPLE_CONFIG:?Product JSON path required}"
: "${SHARED_APPLE_OUTPUT:?Product receipt path required}"
export SHARED_APPLE_CONFIG SHARED_APPLE_OUTPUT
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
# The dispatcher inserts only its own verified checkout into the isolated path.
"$SHARED_PYTHON" -I -B "$shared_checkout/scripts/apple/run-hook.py"
