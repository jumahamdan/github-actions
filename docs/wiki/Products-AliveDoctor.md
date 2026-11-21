# AliveDoctor workflow map

| Component | Workflow | Open to fix it |
|---|---|---|
| Website | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) checks; [CD](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cd.yml) publishes | [Checks](https://github.com/alivedoctor/alivedoctor/blob/develop/.ci/quality/site.json) · [Deploy](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/aws/site/scripts/site-deploy.sh) |
| Website signup API | CI → Early-access API tests | [API code and tests](https://github.com/alivedoctor/alivedoctor/tree/develop/backend/aws/site/) |
| Backend platform | [CD](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cd.yml) → [Deploy · AWS](Deploy-AWS.md); automatic on `main` after activation | [Deploy settings](https://github.com/alivedoctor/alivedoctor/blob/develop/.ci/deploy/platform-production.json) · [Setup guide](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/aws/platform/README.md#activate-automatic-production-deployment-250) |
| Admin page and API | [Admin](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/admin.yml) checks; deploy manually | [Admin deployment guide](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/aws/admin/README.md) |
| iPhone app and TestFlight | CI style checks; Xcode Cloud **setup pending** | [Apple workflow settings](https://github.com/alivedoctor/alivedoctor/tree/develop/scripts/xcode-cloud/workflows/) |
| AWS login | [Verify AWS login](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/aws-login.yml), manual; publishes nothing | [Run instructions](https://github.com/alivedoctor/alivedoctor/blob/develop/docs/aws-login.md) |
| AWS alarms | CI → Observability stack; deploy manually | [Alarm configuration](https://github.com/alivedoctor/alivedoctor/tree/develop/backend/aws/observability/) |
| Sentry | Upload crash symbols; label TestFlight/App Store reports (`unknown` until verified). | [Symbols](https://github.com/alivedoctor/alivedoctor/blob/develop/ci_scripts/ci_post_xcodebuild.sh) · [Labels](https://github.com/alivedoctor/alivedoctor/blob/develop/AliveDoctor/App/CrashReporting.swift) |
| Secret scanning | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) → Secret scan | [Scan rules](https://github.com/alivedoctor/alivedoctor/blob/develop/.gitleaks.toml) |
| AI tests | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) → offline checks; real-model checks when AI changes, full checks before production. | [Runner](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/evals/ci/run.mjs) · [Guide](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/evals/README.md) |
| Version tags and release notes | Manual release command | [Release script](https://github.com/alivedoctor/alivedoctor/blob/develop/scripts/release/promote.sh) · [Release notes](https://github.com/alivedoctor/alivedoctor/blob/develop/CHANGELOG.md) |
| Old branch cleanup | [Delete merged branch](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cleanup-branches.yml) | [Cleanup rules](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cleanup-branches.yml) |

**Shared checks:** [Check settings](https://github.com/alivedoctor/alivedoctor/tree/develop/.ci/quality/) · [Reuse settings](https://github.com/alivedoctor/alivedoctor/tree/develop/.ci/reuse/) · [Shared-action wrapper](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/actions/shared-quality/action.yml). Unchanged promotion checks can reuse a verified result; secret scans and admin page builds run fresh.

**Release route:** feature → develop → staging → main. You decide when to promote. The App Store uses the tested TestFlight build.

**Backend activation pending ([#250](https://github.com/alivedoctor/alivedoctor/issues/250)):** Claude applies the AWS role and sets the repository variable before the workflow reaches `main`. Until then, use the manual deploy script. Once active, CD prepares the approved commit, deploys it separately from the website, then checks both AI revision tags. It does not repeat the PR's AI tests.

**AWS login verified:** [Staging](https://github.com/alivedoctor/alivedoctor/actions/runs/44863081366) and [production](https://github.com/alivedoctor/alivedoctor/actions/runs/44863084406) passed without publishing website content.
