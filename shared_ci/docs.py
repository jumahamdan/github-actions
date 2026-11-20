"""Check repository documentation links, anchors and generated-fact freshness."""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file, strict_keys
from .quality import argv


_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s()]*(?:\([^()]*\)[^\s()]*)*)\s*(?:[\"'][^\n]*?[\"']\s*)?\)")
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)", re.M)
_HTML_LINK = re.compile(r"<(?:a|img)\b[^>]*\b(?:href|src)=[\"']([^\"']+)[\"']", re.I)


def prose_only(text):
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            lines.append("")
        else:
            lines.append(line if fence is None else "")
    return "\n".join(lines)


def anchors(path):
    text = prose_only(Path(path).read_text(encoding="utf-8"))
    result = set(re.findall(r"<(?:a|h[1-6])\b[^>]*\b(?:id|name)=[\"']([^\"']+)[\"']", text, re.I))
    counts = {}
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(match.group(1))
        elif index and re.match(r"^\s{0,3}(?:=+|-+)\s*$", line) and lines[index - 1].strip():
            headings.append(lines[index - 1].strip())
    for heading in headings:
        heading = re.sub(r"<[^>]*>", "", heading)
        heading = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
        heading = heading.replace("`", "").lower()
        slug = "".join(char for char in heading if char in "_- " or char.isspace() or unicodedata.category(char)[0] in "LNM")
        slug = re.sub(r"\s", "-", slug)
        suffix = counts.get(slug, 0)
        counts[slug] = suffix + 1
        result.add(slug if suffix == 0 else f"{slug}-{suffix}")
    return result


def links(path):
    text = prose_only(Path(path).read_text(encoding="utf-8"))
    # Inline code examples are not hyperlinks.
    text = re.sub(r"`+[^`\n]*`+", "", text)
    return [match.group(1).strip("<>") for pattern in (_LINK, _REFERENCE, _HTML_LINK) for match in pattern.finditer(text)]


def validate_link(root, document, target):
    if not target:
        raise PipelineError(f"empty link in {document.relative_to(root)}")
    parsed = urlsplit(target)
    if parsed.scheme in {"https", "http", "mailto", "tel", "data"} or parsed.netloc:
        return "external"
    if parsed.scheme:
        raise PipelineError(f"unsupported link scheme in {document.relative_to(root)}")
    path_part = unquote(parsed.path)
    if path_part.startswith("/"):
        relative = Path(path_part.lstrip("/"))
    else:
        absolute = document.parent / path_part if path_part else document
        # Relative Markdown ../ links are legitimate if their resolved target stays in the repo.
        try:
            relative = absolute.resolve().relative_to(root)
        except ValueError as exc:
            raise PipelineError(f"documentation link escapes repository: {target}") from exc
    path = confined_path(root, relative)
    if parsed.fragment and path.is_file() and path.suffix.lower() in {".md", ".markdown"}:
        fragment = unquote(parsed.fragment)
        if fragment not in anchors(path):
            raise PipelineError(f"missing documentation anchor: {target}")
    return "local"


def execute(config, root):
    strict_keys(config, {"version", "documents", "required_files", "generated", "receipt", "timeout"}, required=("version", "documents"))
    if type(config["version"]) is not int or config["version"] != 1:
        raise PipelineError("unsupported documentation configuration version")
    root = Path(root).resolve()
    timeout = config.get("timeout", 300)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise PipelineError("invalid documentation command timeout")
    patterns = config["documents"]
    if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
        raise PipelineError("documents must contain path patterns")
    paths = set()
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise PipelineError("document patterns must stay in repository")
        matched = [p for p in root.glob(pattern) if p.is_file()]
        if not matched:
            raise PipelineError(f"document selection has no files: {pattern}")
        for path in matched:
            paths.add(confined_path(root, path.relative_to(root)))
    required = config.get("required_files", [])
    if not isinstance(required, list):
        raise PipelineError("required_files must be a list")
    for relative in required:
        if not confined_path(root, relative).is_file():
            raise PipelineError("required documentation must be a file")
    generated = config.get("generated", [])
    if not isinstance(generated, list):
        raise PipelineError("generated must be a list")
    generated_results = []
    for generator in generated:
        strict_keys(generator, {"command", "outputs", "working_directory"}, required=("command", "outputs"))
        outputs = generator["outputs"]
        if not isinstance(outputs, list) or not outputs:
            raise PipelineError("generator must declare existing output files")
        before = {p: sha256_file(confined_path(root, p)) for p in outputs}
        cwd = confined_path(root, generator.get("working_directory", "."))
        run(argv(generator["command"]), cwd=cwd, timeout=timeout)
        after = {p: sha256_file(confined_path(root, p)) for p in outputs}
        if before != after:
            raise PipelineError("generated documentation is stale; regenerate and commit the changed files")
        generated_results.append({"outputs": after, "status": "current"})
    local_count = external_count = 0
    for path in sorted(paths):
        if path.suffix.lower() not in {".md", ".markdown"}:
            raise PipelineError("documentation link checking supports Markdown files")
        for target in links(path):
            result = validate_link(root, path, target)
            local_count += result == "local"
            external_count += result == "external"
    receipt = {"schema_version": 1, "kind": "documentation", "status": "passed", "documents": len(paths),
               "local_links_checked": local_count, "external_links_not_fetched": external_count,
               "generated": generated_results,
               "files": {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(paths)}}
    if config.get("receipt"):
        path = confined_path(root, config["receipt"], must_exist=False)
        if path in paths:
            raise PipelineError("receipt cannot overwrite a document")
        atomic_json(path, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        result = execute(load_json(confined_path(Path(args.root).resolve(), args.config)), args.root)
        print(json.dumps({k: result[k] for k in ("status", "documents", "local_links_checked", "external_links_not_fetched")}))
    except (PipelineError, OSError, ValueError) as exc:
        print(f"documentation: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
