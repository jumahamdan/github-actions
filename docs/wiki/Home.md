# Shared workflow library

This Wiki explains the shared CI/CD library used to build product pipelines. Each product chooses the checks, build tools, hosting and release steps it needs.

**The long Actions list is a library of available pieces. It does not mean every workflow runs on every change.**

## Find what you need

- [Getting started](Getting-started.md): how a product adopts the library and avoids duplicate checks.
- [Workflow index](Workflow-index.md): a page for each workflow, grouped by purpose.
- [Release notes](Release-Notes.md) and [release publication](Release-Publish.md).
- [Security scans](Security-Scans.md), [CodeQL](Security-CodeQL.md) and [dependency updates](Dependabot-Updates.md).
- [Documentation checks](Checks-Documentation.md) and [manual Wiki publication](Docs-Publish-Wiki.md).
- [Sentry and Crashlytics](Monitor-Symbols-and-releases.md).

## How products use it

A product normally has a small number of visible entrypoints such as CI, promotion checks, a staging build/deployment and release. Those entrypoints call the selected shared modules. A mobile app, a WordPress website and a Docker service will choose different pieces.

The intended branch route is **feature → develop → staging → main**. Matching verified quality results can be reused during promotion. Current security, native/device verification and deployed health still have their own checks. Juma decides when the products in this rollout promote.

See how one product combines shared Linux checks, website delivery and a separate iOS release. [AliveDoctor CI/CD](Products-AliveDoctor.md)

[RiseUp CI/CD](Products-RiseUp.md) maps the website, forms, Telegram bot and monitoring. Its blueprint is published before implementation and checked against the finished setup afterward.

## When these pages change

Pages are written or updated when they are needed, reviewed with the repository changes, and published manually. Ordinary app releases do not automatically rewrite or publish the Wiki. The reviewed originals live in [the repository’s Wiki source folder](https://github.com/jumahamdan/github-actions/tree/main/docs/wiki).

These pages explain available capabilities. Actual product adoption must verify its existing credentials, targets, signing, monitoring and recovery before delivery is enabled.
