"""Publish reviewed Markdown pages to the same repository's Wiki on request."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .common import PipelineError, confined_path
from .docs import _HTML_LINK, _LINK, _REFERENCE, validate_link

MANIFEST = ".shared-wiki.json"
PAGE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_-]*|_Sidebar|_Footer)\.md")
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def git(root, *args, env=None, raw=False):
    result = subprocess.run(["git", *args], cwd=root, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode:
        # Git/provider diagnostics may echo an authentication URL or helper output.
        raise PipelineError("Wiki Git operation failed; check access, initialization or concurrent updates")
    return result.stdout if raw else result.stdout.strip()


def verify_context(repository, revision, env=None):
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS") != "true":
        if env.get("GITHUB_ACTIONS") or env.get("GITHUB_EVENT_NAME"):
            raise PipelineError("Wiki publication requires a genuine manual Actions context")
        return
    try:
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
        repo = event["repository"]
        branch = repo["default_branch"]
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise PipelineError("Missing Wiki caller repository context") from exc
    if (env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or repo.get("full_name") != repository or env.get("GITHUB_REPOSITORY") != repository
            or not isinstance(branch, str) or not branch
            or env.get("GITHUB_REF") != "refs/heads/" + branch
            or env.get("GITHUB_SHA") != revision):
        raise PipelineError("Wiki publication is manual and restricted to the caller's default branch")


def prose_mask(text):
    """Mask code without moving link offsets in the original Markdown."""
    result, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        in_code = fence is not None or marker is not None
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
        result.append(re.sub(r"[^\r\n]", " ", line) if in_code else line)
    return re.sub(r"`+[^`\n]*`+", lambda m: " " * len(m.group()), "".join(result))


def without_symlinks(root, path):
    current = root
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise PipelineError("Wiki path escapes the repository") from exc
    for part in parts:
        current = current.parent if part == ".." else current / part
        if not current.is_relative_to(root) or current.is_symlink():
            raise PipelineError("Wiki paths must stay inside the repository without symlinks")
    return current


def rewrite(root, document, text, pages, repository, revision, tracked_targets=None):
    replacements = {}
    for pattern in (_LINK, _REFERENCE, _HTML_LINK):
        for match in pattern.finditer(prose_mask(text)):
            original = match.group(1)
            target = original.strip("<>")
            if validate_link(root, document, target) == "external":
                continue
            parsed = urlsplit(target)
            part = unquote(parsed.path)
            path = (root / part.lstrip("/")) if part.startswith("/") else (document.parent / part if part else document)
            path = without_symlinks(root, path).resolve()
            if tracked_targets is not None and path not in tracked_targets:
                relative = path.relative_to(root).as_posix()
                git(root, "cat-file", "-e", revision + (":" + relative if relative != "." else "^{tree}"))
                tracked_targets.add(path)
            if path in pages:
                url = "https://github.com/" + repository + "/wiki/" + quote(path.stem, safe="-_")
            else:
                kind = "tree" if path.is_dir() else "blob"
                url = ("https://github.com/" + repository + "/" + kind + "/" + revision + "/"
                       + quote(path.relative_to(root).as_posix(), safe="/"))
            url = urlunsplit((*urlsplit(url)[:3], parsed.query, parsed.fragment))
            replacements[match.span(1)] = "<" + url + ">" if original.startswith("<") else url
    for (start, end), value in sorted(replacements.items(), reverse=True):
        text = text[:start] + value + text[end:]
    return text


def render(root, source, repository, revision, *, tracked=True):
    root = Path(root).resolve()
    if not REPOSITORY.fullmatch(repository) or not SHA.fullmatch(revision):
        raise PipelineError("Wiki needs an owner/repository and exact source commit")
    if Path(source).is_absolute() or ".." in Path(source).parts:
        raise PipelineError("Wiki source must stay inside the repository")
    folder = confined_path(root, source)
    without_symlinks(root, root / source)
    if not folder.is_dir():
        raise PipelineError("Wiki source must be a directory")
    pages = []
    for path in sorted(folder.iterdir()):
        if path.name == "README.md":
            continue
        if path.is_symlink() or not path.is_file() or not PAGE.fullmatch(path.name):
            raise PipelineError("Wiki source may contain only flat Markdown pages with safe names")
        pages.append(confined_path(root, path.relative_to(root)))
    if not any(p.name == "Home.md" for p in pages) or not pages:
        raise PipelineError("Wiki source must contain Home.md")
    if len({p.name.casefold() for p in pages}) != len(pages):
        raise PipelineError("Wiki page names collide")
    if tracked:
        if git(root, "rev-parse", "HEAD") != revision or git(root, "status", "--porcelain", "--untracked-files=no"):
            raise PipelineError("Wiki publication requires the exact clean reviewed checkout")
        for path in pages:
            git(root, "ls-files", "--error-unmatch", "--", path.relative_to(root).as_posix())
    page_set = set(pages)
    tracked_targets = set() if tracked else None
    return {p.name: rewrite(root, p, p.read_text(encoding="utf-8"), page_set, repository, revision, tracked_targets) for p in pages}


def read_manifest(wiki, repository):
    path = wiki / MANIFEST
    if path.is_symlink():
        raise PipelineError("Unsafe Wiki ownership manifest")
    if not path.exists():
        return {}
    if not path.is_file():
        raise PipelineError("Unsafe Wiki ownership manifest")
    try:
        data = json.loads(path.read_text())
        valid = (set(data) == {"schema", "repository", "source_sha", "pages"}
                 and type(data["schema"]) is int and data["schema"] == 1 and data["repository"] == repository
                 and SHA.fullmatch(data["source_sha"]) and isinstance(data["pages"], dict)
                 and all(PAGE.fullmatch(name) and re.fullmatch(r"[0-9a-f]{64}", value)
                         for name, value in data["pages"].items())
                 and len({name.casefold() for name in data["pages"]}) == len(data["pages"]))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise PipelineError("Invalid Wiki ownership manifest")
    return data["pages"]


def prepare(wiki, pages, repository, revision, *, adopt_home=False):
    """Validate every destination before writing; preserve unrelated Wiki pages."""
    wiki = Path(wiki)
    if git(wiki, "status", "--porcelain"):
        raise PipelineError("Wiki checkout is not clean")
    before = git(wiki, "rev-parse", "HEAD")
    previous = read_manifest(wiki, repository)
    for name, expected in previous.items():
        path = wiki / name
        if path.is_symlink() or not path.is_file() or digest(path.read_text()) != expected:
            raise PipelineError("A managed Wiki page changed outside publication; reconcile it into source first")
    for name, content in pages.items():
        if not PAGE.fullmatch(name):
            raise PipelineError("Invalid exported Wiki page")
        path = wiki / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise PipelineError("Unsafe Wiki destination")
        if path.exists() and name not in previous and path.read_text() != content:
            if not (name == "Home.md" and adopt_home):
                raise PipelineError("An unmanaged Wiki page would be overwritten")
    manifest = {"schema": 1, "repository": repository, "source_sha": revision,
                "pages": {name: digest(content) for name, content in sorted(pages.items())}}
    for name in previous.keys() - pages.keys():
        (wiki / name).unlink()
    for name, content in pages.items():
        (wiki / name).write_text(content, encoding="utf-8")
    (wiki / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return before


def push_prepared(wiki, expected_head, *, env=None):
    """Push only to the selected current default branch; never force or rebase."""
    branch = git(wiki, "symbolic-ref", "--short", "HEAD")
    if not branch or branch.startswith("-"):
        raise PipelineError("Wiki default branch is invalid")
    git(wiki, "fetch", "origin", branch, env=env)
    if git(wiki, "rev-parse", "FETCH_HEAD") != expected_head:
        raise PipelineError("Wiki changed after preparation; review the newer pages before publishing")
    git(wiki, "add", "--all")
    # An unrelated Wiki .gitignore or Git filter must not silently omit/change pages.
    manifest = json.loads((wiki / MANIFEST).read_text())
    for name in [MANIFEST, *manifest["pages"]]:
        git(wiki, "ls-files", "--error-unmatch", "--", name)
        if git(wiki, "show", ":" + name, raw=True) != (wiki / name).read_text():
            raise PipelineError("Wiki index differs from prepared page bytes")
    changed = bool(git(wiki, "diff", "--cached", "--name-only"))
    if not changed:
        return {"status": "unchanged", "wiki_commit": expected_head}
    git(wiki, "-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "Publish reviewed workflow documentation")
    # A race after fetch is rejected by the ordinary fast-forward push.
    git(wiki, "push", "origin", "HEAD:refs/heads/" + branch, env=env)
    return {"status": "published", "wiki_commit": git(wiki, "rev-parse", "HEAD")}


def publish(pages, repository, revision, *, adopt_home=False):
    verify_context(repository, revision)
    with tempfile.TemporaryDirectory(prefix="shared-wiki-") as directory:
        temporary = Path(directory)
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = env.get("WIKI_GITHUB_TOKEN")
        if env.get("GITHUB_ACTIONS") == "true" and not token:
            raise PipelineError("Wiki publication requires the same-repository GitHub token")
        if token:
            # The secret stays in the step environment, never Git argv, URLs or config.
            askpass = temporary / "askpass.py"
            askpass.write_text("#!/usr/bin/env python3\nimport os,sys\nprint('x-access-token' if 'Username' in sys.argv[1] else os.environ['WIKI_GITHUB_TOKEN'])\n")
            askpass.chmod(0o700)
            env["GIT_ASKPASS"] = str(askpass)
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = ""
        wiki = temporary / "wiki"
        git(temporary, "clone", "--quiet", "--", "https://github.com/" + repository + ".wiki.git", str(wiki), env=env)
        before = prepare(wiki, pages, repository, revision, adopt_home=adopt_home)
        result = push_prepared(wiki, before, env=env)
        result.update(repository=repository, source_sha=revision, pages=len(pages))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--source", default="docs/wiki")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", help="Export into a new directory without publishing")
    parser.add_argument("--check", action="store_true", help="Validate draft sources without publication")
    parser.add_argument("--publish", action="store_true", help="Explicitly publish the clean reviewed source")
    parser.add_argument("--adopt-existing-home", action="store_true", help="Replace only the initial unmanaged Home page")
    args = parser.parse_args()
    try:
        if sum(bool(x) for x in (args.check, args.publish, args.output)) != 1:
            raise PipelineError("Choose exactly one of --check, --output or --publish")
        root = Path(args.root).resolve()
        revision = git(root, "rev-parse", "HEAD")
        pages = render(root, args.source, args.repository, revision, tracked=not args.check)
        if args.publish:
            result = publish(pages, args.repository, revision, adopt_home=args.adopt_existing_home)
        elif args.output:
            output = Path(args.output)
            if output.exists():
                raise PipelineError("Wiki export directory must be new")
            output.mkdir(parents=True)
            for name, content in pages.items():
                (output / name).write_text(content, encoding="utf-8")
            result = {"status": "exported", "pages": len(pages), "source_sha": revision}
        else:
            result = {"status": "passed", "pages": len(pages)}
        print(json.dumps(result))
        return 0
    except (PipelineError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("Wiki publication: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
