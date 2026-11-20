# AliveDoctor workflow map

Use this table to find the workflow and the file to fix. Links open the AliveDoctor repository.

| Component | Workflow | Open to fix it |
|---|---|---|
| Website | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) checks; [CD](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cd.yml) publishes | [Checks](https://github.com/alivedoctor/alivedoctor/blob/develop/.ci/quality/site.json) · [Deploy](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/aws/site/scripts/site-deploy.sh) |
| Website signup API | CI → Early-access API tests | [API code and tests](https://github.com/alivedoctor/alivedoctor/tree/develop/backend/aws/site/) |
| Backend platform | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) checks; deploy manually | [Backend deploy script](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/aws/platform/scripts/deploy-prod.sh) |
| Admin page and API | [Admin](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/admin.yml) checks; deploy manually | [Admin deployment guide](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/aws/admin/README.md) |
| iPhone app and TestFlight | CI style checks; Xcode Cloud **setup pending** | [Apple workflow settings](https://github.com/alivedoctor/alivedoctor/tree/develop/scripts/xcode-cloud/workflows/) |
| AWS alarms | CI → Observability stack; deploy manually | [Alarm configuration](https://github.com/alivedoctor/alivedoctor/tree/develop/backend/aws/observability/) |
| Sentry crash symbols | TestFlight archive hook | [Symbol upload script](https://github.com/alivedoctor/alivedoctor/blob/develop/ci_scripts/ci_post_xcodebuild.sh) |
| Secret scanning | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) → Secret scan | [Scan rules](https://github.com/alivedoctor/alivedoctor/blob/develop/.gitleaks.toml) |
| AI tests | [CI](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/ci.yml) → fake-provider tests; live tests pending | [AI test guide](https://github.com/alivedoctor/alivedoctor/blob/develop/backend/evals/README.md) |
| Version tags and release notes | Manual release command | [Release script](https://github.com/alivedoctor/alivedoctor/blob/develop/scripts/release/promote.sh) · [Release notes](https://github.com/alivedoctor/alivedoctor/blob/develop/CHANGELOG.md) |
| Old branch cleanup | [Delete merged branch](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cleanup-branches.yml) | [Cleanup rules](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/workflows/cleanup-branches.yml) |

**Shared checks:** [Check settings](https://github.com/alivedoctor/alivedoctor/tree/develop/.ci/quality/) · [Reuse settings](https://github.com/alivedoctor/alivedoctor/tree/develop/.ci/reuse/) · [Shared-action wrapper](https://github.com/alivedoctor/alivedoctor/blob/develop/.github/actions/shared-quality/action.yml). Unchanged promotion checks can reuse a verified result; secret scans and admin page builds run fresh.

**Release route:** feature → develop → staging → main. You decide when to promote. The App Store uses the tested TestFlight build.

**Still to verify:** AWS login from GitHub after the next authorized promotions.
