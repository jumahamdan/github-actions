# Documentation checks

`python3 -m shared_ci.docs --config .ci/docs.json --root .` validates local Markdown links and generated-fact freshness. See `examples/docs.json`.

Configuration version 1 requires `documents`, a nonempty list of repository-relative patterns. Every pattern must match a file; missing required documentation cannot produce a green empty check. `required_files` names mandatory files. Optional `generated` entries contain a command argument array, existing `outputs`, and optional `working_directory`. The module runs each generator and verifies that its declared outputs remain byte-identical. A difference requires regeneration and commitment in the product PR. Command failure also blocks.

Inline/reference Markdown links and HTML `href`/`src` links are checked. Local files, directories, Markdown heading anchors, duplicate heading suffixes and explicit HTML anchors are supported. URL-escaped paths and paths with spaces work. Relative `../` links may traverse within the repository, but external filesystem links and escaping symlinks fail. Fenced/inline code examples are ignored. HTTP(S), mail, telephone and data links are classified as external and are **not fetched**. External link availability needs a separate network-enabled check; this module never describes it as verified.

`receipt` optionally writes a JSON summary with document hashes, local-link counts, unfetched external-link counts and generator results. `timeout` bounds generator commands (default 300 seconds, maximum 3600). Commands receive no deployment/provider secrets. Unknown configuration fields fail.

Keep README, architecture, setup, release, environments and recovery documentation beside the code. Review behavior and documentation in the same PR. A Wiki may link to these documents, but this module does not enable a Wiki or publish private documents. No paid tools or account credentials are needed.
