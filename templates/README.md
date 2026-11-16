# Product documentation index

Adapt this index to the product before adoption. Keep facts generated from code where possible; do not maintain a second manual copy in a Wiki.

| Document | Required content |
| --- | --- |
| Overview | Product purpose, owner, supported clients and links |
| Setup | Exact toolchain, lockfiles, local commands, non-secret configuration |
| Architecture | Components, data stores, external interfaces and ownership |
| Environments | Development/staging/production targets and credential **names** |
| CI/CD | Triggers, required checks, candidate evidence and promotion authority |
| Release notes | Customer changes, tester instructions, developer/migration notes |
| Recovery | Previous candidate selection, health checks, data restore and escalation |
| Security | Supported versions and verified private reporting route |

Each behavior-changing PR updates its corresponding document or explains why no documentation change is needed. Use relative links to versioned documents. A Wiki may link to this material if its availability and audience fit the repository; enabling one is a separate product decision.
