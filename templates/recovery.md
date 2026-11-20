# Product recovery runbook

Complete and test this runbook with the product's actual infrastructure before enabling delivery.

1. Identify the incident owner, affected environment, currently deployed candidate and observed failure. Pause further releases using the existing product control.
2. Select the last known healthy candidate from authenticated retained evidence. Verify its artifact hashes, provider revision and compatibility with the current database and installed mobile clients.
3. Record the intended recovery operation and required authority. Redeploy/promote that exact retained output where supported. Managed source platforms may rebuild; record the resulting revision.
4. Verify required HTTP/API, DNS/TLS, association files and product behavior checks. Confirm monitoring reports the intended release/environment.
5. If a migration changed persistent data, use the separately verified backup/restore procedure and recovery objective. An app/image rollback is not a database restore. Do not delete volumes to recover an application.
6. Retain the recovery receipt, candidate/provider identity, verification results and any manual steps. Reconcile uncertain provider operations before retrying them.

Record a real rehearsal with synthetic data, the backup location and access role, retention, restore duration, recovery-point objective and escalation contacts. Store credentials separately; this document contains their names and owners only.
