#!/usr/bin/env python3
"""Generate a grouped workflow index without duplicating the caller contracts."""

import argparse
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
GROUPS = ("Control", "Checks", "Security", "AI", "Build", "Mobile", "Deploy", "Monitor", "Verify", "Release")
# Human explanations belong here; names, source links and implementation links
# come from the workflows so changing a display name cannot leave the index stale.
CATALOG = {
    "v2-plan": ("Control", "Select affected products and validate the branch route."),
    "v2-required": ("Control", "Require every applicable job to finish successfully."),
    "v2-checks": ("Checks", "Run the product's configured format, lint, types, tests and build commands."),
    "v2-reuse": ("Checks", "Reuse original passing checks after authenticating their run and matching inputs."),
    "v2-docs": ("Checks", "Check documentation links, anchors and generated facts."),
    "v2-sonar": ("Checks", "Analyze code with authenticated matching coverage and the selected Sonar project."),
    "v2-security": ("Security", "Run configured security scanners and enforce their policy."),
    "v2-codeql": ("Security", "Build and analyze selected languages with CodeQL; upload only where allowed."),
    "v2-ai-mock": ("AI", "Run deterministic AI tests with a fake provider and no paid model calls."),
    "v2-ai-live": ("AI", "Run explicitly enabled provider evaluations with product assertions and limits."),
    "v2-container-build": ("Build", "Build and test container images for an immutable candidate."),
    "v2-wordpress-package": ("Build", "Check and package WordPress code for an explicit publishing handoff."),
    "v2-android-build": ("Mobile", "Run the configured Android build and retain its candidate evidence."),
    "v2-mobile-verify": ("Mobile", "Derive and verify Android test artifacts from the selected retained bundle."),
    "v2-apple-import": ("Mobile", "Import private evidence from an authenticated Apple build; no compilation here."),
    "v2-device-tests": ("Mobile", "Run explicitly enabled Google device tests on verified mobile artifacts."),
    "v2-mobile-stores": ("Mobile", "Operate on the selected Play or Apple build and preserve processing/review state."),
    "v2-aws": ("Deploy", "Execute the selected AWS infrastructure, site or backend operation."),
    "v2-firebase": ("Deploy", "Execute the selected Firebase hosting, functions, rules or configuration operation."),
    "v2-web-deploy": ("Deploy", "Execute the configured web-hosting deployment or promotion operation."),
    "v2-container-delivery": ("Deploy", "Publish, promote, deploy or recover the selected immutable container release."),
    "v2-monitoring": ("Monitor", "Verify/upload symbols and record candidate-bound monitoring releases or deployments."),
    "v2-health": ("Verify", "Check the deployed target's health and expected revision."),
    "v2-evidence": ("Release", "Create or verify the selected candidate and its required evidence."),
    "v2-release-notes": ("Release", "Prepare notes for the configured public, tester and internal audiences."),
    "v2-release-publish": ("Release", "Publish the approved GitHub tag, notes and exact release assets."),
}
LEGACY = ("ai-code-review", "code-quality-typescript", "security-audit")


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def read_workflow(path):
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("jobs"), dict):
        raise ValueError(f"Workflow metadata is incomplete: {path.name}")
    events = data.get("on", data.get(True, {}))
    if not isinstance(events, dict):
        raise ValueError(f"Workflow triggers must be explicit: {path.name}")
    return data, events


def implementations(data, root):
    modules = []
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            module = step.get("with", {}).get("module")
            if module and module not in modules:
                if not isinstance(module, str) or not module.isidentifier() or not (root / "shared_ci" / f"{module}.py").is_file():
                    raise ValueError(f"Unknown implementation module: {module}")
                modules.append(module)
    if not modules:
        raise ValueError("Reusable workflow must identify its implementation")
    return ", ".join(f"[{module}](../shared_ci/{module}.py)" for module in modules)


def validate_wiki_pages(files, root):
    folder = root / "docs/wiki"
    pages = {path: path.read_text() for path in folder.glob("*.md")}
    index = (folder / "Workflow-index.md").read_text()
    sidebar = (folder / "_Sidebar.md").read_text()
    for key, workflow in files.items():
        data, _ = read_workflow(workflow)
        title = "# " + data["name"]
        matching = [path for path, text in pages.items() if text.splitlines() and text.splitlines()[0] == title]
        if len(matching) != 1:
            raise ValueError(f"Expected one Wiki page for {key}")
        page = matching[0]
        if f"../../.github/workflows/{workflow.name}" not in pages[page]:
            raise ValueError(f"Wiki page must link its actual workflow: {page.name}")
        if f"({page.name})" not in index or f"({page.name})" not in sidebar:
            raise ValueError(f"Wiki page is absent from navigation: {page.name}")


def render(root=ROOT):
    folder = root / ".github/workflows"
    paths = sorted(path for path in folder.iterdir() if path.suffix in {".yml", ".yaml"})
    files = {path.stem: path for path in paths}
    if len(files) != len(paths):
        raise ValueError("Duplicate workflow names across YAML extensions")
    expected = set(CATALOG) | set(LEGACY) | {"github-actions-ci", "wiki-publish"}
    if set(files) != expected:
        raise ValueError(f"Catalog coverage differs from workflow files: {sorted(set(files) ^ expected)}")
    if any(category not in GROUPS for category, _ in CATALOG.values()):
        raise ValueError("Unknown catalog category")
    validate_wiki_pages(files, root)
    rows = []
    for key, (category, purpose) in CATALOG.items():
        path = files[key]
        data, events = read_workflow(path)
        if set(events) != {"workflow_call"}:
            raise ValueError(f"Reusable workflow unexpectedly runs automatically: {path.name}")
        rows.append((category, data["name"], key, purpose, implementations(data, root)))
    library, events = read_workflow(files["github-actions-ci"])
    if set(events) != {"pull_request", "push", "workflow_dispatch"}:
        raise ValueError("Update the catalog for changed library-validation triggers")
    if any(not isinstance(events[event], dict) or events[event].get("branches") != ["main"]
           for event in ("pull_request", "push")):
        raise ValueError("Update the catalog for changed library-validation branches")
    wiki, wiki_events = read_workflow(files["wiki-publish"])
    if set(wiki_events) != {"workflow_dispatch", "workflow_call"}:
        raise ValueError("Wiki publication must remain manually invoked")
    capability_count = len(json.loads((root / "docs/capabilities.json").read_text())["services"])
    lines = [
        "# Workflow catalog", "",
        "Generated by `python3 scripts/workflow-catalog.py`. The display names and implementation links below come from the checked-in workflow files.", "",
        "## What the Actions list means", "",
        f"This repository contains {len(CATALOG)} reusable v2 workflows, {len(LEGACY)} legacy callable workflows, a manually invoked Wiki publisher and one workflow that validates the library itself. GitHub also displays its managed Dependabot update runs. A long Actions list does not mean every item runs on each change.", "",
        "| Kind | When it runs |", "| --- | --- |",
        f"| [{cell(library['name'])}](../.github/workflows/github-actions-ci.yml) | PRs into `main`, pushes to `main`, or an explicit manual run. Its jobs call the reusable quality and mock-AI workflows. |",
        f"| [{cell(wiki['name'])}](../.github/workflows/wiki-publish.yml) | Explicit manual run from the repository's default branch, directly or through a reusable call. Publishes reviewed pages; never runs on a push, PR or schedule. |",
        "| Dependabot Updates | GitHub-managed dependency checks configured in [Dependabot settings](../.github/dependabot.yml). |",
        f"| {len(CATALOG)} reusable v2 workflows | Only when another workflow calls them with `workflow_call`. They do not independently trigger on pushes or PRs. |",
        f"| {len(LEGACY)} legacy workflows | Only when an existing caller invokes them. Deprecated for new adoption. |", "",
        "Use the [product caller starters](../templates/callers/README.md) to compose a few clear product entrypoints: CI, a staging build/deployment, and a deliberate release. Each product calls only the pieces it needs. Category prefixes organize the display names; they are not GitHub folders or a claim that the other items are hidden.", "",
        "## Reusable building blocks", "",
        "Each workflow link opens its executable YAML. The implementation column links shared modules; `evidence` is often the common optional candidate step. Exact inputs, secret names, outputs and permission requirements are in the generated [caller reference](workflow-contracts.md), not duplicated here.", "",
    ]
    for group in GROUPS:
        lines += [f"### {group}", "", "| Workflow | Use it for | Implementation |", "| --- | --- | --- |"]
        for category, name, key, purpose, modules in sorted(rows, key=lambda row: row[1]):
            if category == group:
                lines.append(f"| [{cell(name)}](../.github/workflows/{files[key].name}) | {purpose} | {modules} |")
        lines.append("")
    lines += ["## Legacy compatibility", "",
              "These callable paths and execution contracts remain available to existing consumers. Their display labels identify them as legacy; new callers use v2. Old-owner moving references and failure-tolerant checks are known legacy limitations. Do not remove or disable these workflows until their consumers have migrated.", "",
              "| Workflow | Status |", "| --- | --- |"]
    for key in LEGACY:
        data, events = read_workflow(files[key])
        if set(events) != {"workflow_call"}:
            raise ValueError(f"Update the legacy trigger description for {key}")
        lines.append(f"| [{cell(data['name'])}](../.github/workflows/{files[key].name}) | Compatibility only; do not adopt. |")
    lines += ["", "## Adoption and maintenance", "",
              "Templates do not supply real product targets, signing, credentials, release authority or passing evidence. Configure these explicitly and verify the actual product before enabling delivery. Live AI/device tests require opt-in. Apple compilation stays in Xcode Cloud or an existing authorized Mac lane; this library has no GitHub macOS runner.", "",
              f"Use the [{capability_count}-area capability ledger](capabilities.md) for implementation/test coverage and live-adoption limits, [integration guide](integration.md) for the branch route, and [maintenance guide](maintaining-workflows.md) for source templates and generation checks.", "",
              "The [Wiki source pages](wiki/Home.md) explain each workflow in plain language. Follow [manual Wiki publication](wiki-publishing.md) when those reviewed pages need publishing.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the checked-in catalog differs from current workflow metadata")
    args = parser.parse_args()
    target = ROOT / "docs/workflow-catalog.md"
    content = render()
    if args.check:
        if not target.is_file() or target.read_text() != content:
            raise SystemExit("Stale workflow catalog: run python3 scripts/workflow-catalog.py")
    else:
        target.write_text(content)


if __name__ == "__main__":
    main()
