# RiseUp workflow blueprint

**Status: planned, November 21, 2026.** This page is written before implementation. [Track the work](https://github.com/jumahamdan/rise-up-for-autism/issues/200). After testing, each row will describe the verified setup.

| Component | Planned workflow | Open to fix it |
|---|---|---|
| Website and HubSpot forms | Shared code checks, unit tests, browser/accessibility tests and a preview build | [Website](https://github.com/jumahamdan/rise-up-for-autism/tree/develop/website) · [CI](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/ci.yml) |
| Website delivery | Separate staging and production delivery; require checked artifacts, bundle-secret scanning and health checks | [Deploy workflows](https://github.com/jumahamdan/rise-up-for-autism/tree/develop/.github/workflows) |
| Telegram request agent | Keep request → preview → client approval; use the same required checks | [Agent workflow](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/bot-agent.yml) |
| Telegram/board bridge | Separate tests and deployment for this Worker | [Bridge code and guide](https://github.com/jumahamdan/rise-up-for-autism/tree/develop/tools/board-bridge) |
| Google review data | Keep scheduled update PRs and offline validation | [Refresh workflow](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/google-reviews.yml) |
| Monitoring | Keep uptime checks, incident tracking and Cloudflare alerts | [Uptime workflow](https://github.com/jumahamdan/rise-up-for-autism/blob/develop/.github/workflows/uptime.yml) |
| Repository maintenance | Shared security and documentation checks; release notes tied to the released revision | [Shared security](Security-Scans.md) · [Documentation](Checks-Documentation.md) · [Release notes](Release-Notes.md) |

**Branch plan:** feature → develop → staging → main. Today, develop deploys staging and releases go straight to main. The bot must adopt the new route while retaining the client's release approval.

**Avoid repeat work:** reuse verified checks only when all inputs match. Staging and production currently have different search-indexing build settings, so their build outputs cannot be treated as identical.

**Credentials:** deployment credentials stay in GitHub secrets; HubSpot runtime credentials stay in Worker secrets. Document names and locations in the private repo, never values. Keep live form/calendar writes out of ordinary CI.

**Finish:** verify every row, record remaining manual steps and rollback instructions, update this page, and add its shortcut to the RiseUp README. Sentry remains off under the existing project decision.
