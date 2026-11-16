# Durable store operation state

An isolated runner must restore the previous store operation's state before retrying it. A lost Play commit response or Apple submission response can mean the provider accepted the write. Starting again with an empty state file would bypass that uncertainty check.

The wrapper calls `state.snapshot(effective_store_config, root)` after execution, including failure, when `state_file` exists. Save the returned envelope as `operation-state.json` in a fresh private artifact directory. The optional matching `receipt.json` is the only other permitted file. Use a unique artifact name for the run and attempt, and upload that directory even after a failed operation. The envelope contains the known operation state, exact configuration fingerprint, candidate/source identity and payload checksum; credentials and configuration values are not copied. A cancelled job can only resume from state that reached durable storage. If no artifact survived, reconcile the provider manually before permitting another mutation.

To restore, call `state.restore(selection, root, store_config=effective_store_config)` after applying runtime candidate/configuration overrides. The CLI equivalent is:

```sh
python -m shared_ci.state --config restore.json --store-config effective-store.json --root workspace --output restore-receipt.json
```

The [selection example](../examples/state-restore.json) names the exact repository, run, attempt, workflow path, displayed producer job name, artifact name, allowed branches/events/conclusions and token environment name. `GH_STATE_TOKEN` needs Actions read access to the current repository. The state producer must be a completed push/manual attempt and completed exact job; `failure`, `cancelled` and `timed_out` are accepted only when explicitly listed. The selected same-run attempt must precede the currently executing attempt. Foreign repositories, pull requests and running jobs are refused.

The adapter authenticates the selected attempt through GitHub's attempt endpoint, then its exact job, branch, source and artifact creation window. Exactly one unexpired artifact must match. Its SHA256 from authenticated GitHub metadata must match the bounded download. The download token is never forwarded to signed storage redirects. The ZIP may contain only the state envelope and optional matching receipt: no source files, traversal, links, duplicate entries or arbitrary additional files are imported.

The state must match the exact operation, candidate identity, source and effective configuration fingerprint. Its phase and fields must belong to that operation's known schema. An existing local state file is accepted only when byte-identical; conflicting files, symbolic links and candidate evidence destinations are refused. New state uses exclusive creation and private permissions. Successful restoration means only that the saved state is authentic and restored. Store execution still refuses uncertain phases, reuses completed receipts and preserves pending review/processing states. A release gate must inspect that operation's status and intended availability state.

Mutating reruns without an explicit state selection should fail before execution, even when the earlier attempt may have failed before its first write. This conservative rule prevents a missing artifact from being mistaken for a clean operation. Select the latest relevant saved state and reconcile any missing intervening attempt. Do not delete or hand-edit uncertain state to force another upload/commit. Keep store operations serialized and retain state long enough for reviews and reconciliation.

Some operations only inspect provider state or perform an idempotent selection and never create a state file. The wrapper skips snapshotting when none exists; it must not invent a completed state. Test fixtures cover failed/cancelled attempt restoration, lost-commit replay refusal, pending receipt reuse and corrupted/conflicting state without making real provider calls.

Primary contracts: GitHub's [workflow run attempt endpoint](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt), [jobs for an attempt](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt), and [artifact metadata/download API](https://docs.github.com/en/rest/actions/artifacts).
