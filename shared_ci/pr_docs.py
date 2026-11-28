"""Check declared PR documentation and release-note coverage without executing PR code."""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .common import PipelineError, confined_path, load_json, strict_keys


SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_FILES = 3000  # GitHub's pull-request file endpoint cannot prove larger lists complete.
MAX_RESPONSE = 8 * 1024 * 1024
DEFAULT_EXEMPTIONS = {
    "tests": ["tests/**", "test/**", "**/tests/**", "**/test/**", "**/*.test.*", "**/*_test.*", "**/test_*.py"],
    "ci": [".github/**", ".ci/**"],
    "evidence": ["docs/evidence/**"],
    "additional": [],
}
DOCS_LABEL = "Docs updated (which) / none needed (why)"
BEHAVIOR_LABEL = "Behavior changed"


def path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise PipelineError("Paths must be normalized repository-relative paths")
    return value


def patterns(value):
    if not isinstance(value, list) or not value or len(value) > 100:
        raise PipelineError("Path selections must contain 1 to 100 glob patterns")
    return [path(item) for item in value]


def configuration(config):
    strict_keys(config, {"version", "code_paths", "doc_paths", "release_note_paths", "exemptions"},
                required=("version", "code_paths", "doc_paths", "release_note_paths"))
    if type(config["version"]) is not int or config["version"] != 1:
        raise PipelineError("Unsupported PR coverage configuration version")
    result = {name: patterns(config[name]) for name in ("code_paths", "doc_paths", "release_note_paths")}
    additional = config.get("exemptions", {})
    strict_keys(additional, DEFAULT_EXEMPTIONS)
    result["exemptions"] = {name: list(defaults) + (patterns(additional[name]) if name in additional else [])
                            for name, defaults in DEFAULT_EXEMPTIONS.items()}
    return result


def matches(filename, selections):
    # fnmatch treats '*' as including '/'. '**/' additionally matches zero directories.
    return any(fnmatch.fnmatchcase(filename, pattern)
               or (pattern.startswith("**/") and fnmatch.fnmatchcase(filename, pattern[3:]))
               for pattern in selections)


def visible_prose(body, maximum=256 * 1024):
    if body is None:
        return ""
    if not isinstance(body, str) or len(body) > maximum:
        raise PipelineError("Markdown text is invalid or exceeds the coverage-check limit")
    text = re.sub(r"<!--.*?(?:-->|$)", lambda match: "\n" * match.group(0).count("\n"), body, flags=re.S)
    lines = text.splitlines()
    visible = []
    fence = None
    for line in lines:
        if fence:
            marker, length = fence
            if re.fullmatch(r" {0,3}" + re.escape(marker) + "{" + str(length) + r",}[ \t]*", line):
                fence = None
            visible.append("")
            continue
        opening = re.match(r"^ {0,3}(?:(?:[-*+]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            visible.append("")
        elif line.startswith(("    ", "\t")) or re.match(r"^ {0,3}>", line):
            visible.append("")
        elif re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            # The preceding paragraph is a setext heading, not release prose.
            previous = len(visible) - 1
            while previous >= 0 and visible[previous].strip():
                visible[previous] = ""
                previous -= 1
            visible.append("")
        else:
            visible.append(line)
    return "\n".join(visible)


def declaration(body, label):
    expression = re.compile(r"^\s*(?:[-*]\s+(?:\[[ xX]\]\s*)?)?" + re.escape(label) + r"\s*:\s*(.*?)\s*$", re.I)
    values = []
    for line in visible_prose(body).splitlines():
        match = expression.fullmatch(line.replace("**", ""))
        if match:
            values.append(match.group(1))
    if len(values) > 1:
        raise PipelineError(f"Keep exactly one {label} declaration")
    return values[0] if values else None


def meaningful(reason):
    text = reason.strip().strip("()` ")
    if re.search(r"\b(?:todo|tbd|placeholder|fill in|explain here|your reason)\b|<[^>]*>", text, re.I):
        return False
    words = re.findall(r"[^\W\d_]+", text, re.UNICODE)
    return len(words) >= 4 and sum(map(len, words)) >= 20


def explanation(value, prefix):
    if value is None:
        return False
    match = re.fullmatch(re.escape(prefix) + r"\s*(?:[:\-–—]\s*|\(\s*)(.+?)\)?", value, re.I)
    return bool(match and meaningful(match.group(1)))


def added_release_lines(files, selections):
    count = 0
    for changed in files:
        if changed["status"] == "removed" or not matches(changed["filename"], selections):
            continue
        patch = changed.get("patch", "")
        if not isinstance(patch, str) or len(patch) > MAX_RESPONSE:
            raise PipelineError("Release-note diff is invalid or exceeds the coverage-check limit")
        added = {}
        position = None
        for line in patch.splitlines():
            hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                position = int(hunk.group(1))
            elif line.startswith("+"):
                if position is None or position < 1:
                    raise PipelineError("Release-note diff has no valid line positions")
                added[position] = line[1:]
                position += 1
            elif line.startswith(" ") and position is not None:
                position += 1
            elif not line.startswith(("-", "\\ No newline")):
                raise PipelineError("Release-note diff is incomplete or malformed")
        content = changed.get("new_content", "")
        if not isinstance(content, str) or len(content) > 1024 * 1024:
            raise PipelineError("Release-note content is invalid or exceeds the coverage-check limit")
        content_lines = content.splitlines()
        if added and (max(added) > len(content_lines)
                      or any(content_lines[number - 1] != text for number, text in added.items())):
            raise PipelineError("Release-note content does not match its diff positions")
        # Parse the full new document so an added line inside an existing comment
        # or code fence cannot masquerade as a release note.
        for number, line in enumerate(visible_prose(content, maximum=1024 * 1024).splitlines(), start=1):
            if number not in added:
                continue
            text = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?", "", line).strip()
            if text and not text.startswith("#") and len(re.findall(r"[^\W\d_]+", text)) >= 2:
                if not re.search(r"\b(?:todo|tbd|placeholder)\b", text, re.I):
                    count += 1
    return count


def validate_files(files):
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise PipelineError("Cannot prove the complete changed-file list")
    seen = set()
    names = set()
    for changed in files:
        if not isinstance(changed, dict) or changed.get("status") not in {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}:
            raise PipelineError("Unsupported changed-file metadata")
        filename = path(changed.get("filename"))
        if filename in seen:
            raise PipelineError("Duplicate changed-file metadata")
        seen.add(filename)
        names.add(filename)
        if changed["status"] == "renamed":
            names.add(path(changed.get("previous_filename")))
    return names


def evaluate(config, body, files):
    policy = configuration(config)
    names = validate_files(files)
    exempt_patterns = [pattern for group in policy["exemptions"].values() for pattern in group]
    code = {name for name in names if matches(name, policy["code_paths"])
            and not matches(name, exempt_patterns + policy["doc_paths"] + policy["release_note_paths"])}
    docs = {name for name in names if matches(name, policy["doc_paths"]) and not matches(name, exempt_patterns)}
    docs_value = declaration(body, DOCS_LABEL)
    behavior = declaration(body, BEHAVIOR_LABEL)
    behavior_changed = bool(behavior is not None and re.fullmatch(r"yes(?:\s*[:\-–—]\s*\S.*)?", behavior, re.I))
    no_behavior = explanation(behavior, "no")
    release_lines = added_release_lines(files, policy["release_note_paths"])
    errors = []
    if code and not docs and not explanation(docs_value, "none needed"):
        errors.append(f"Code changed: update configured docs or add '{DOCS_LABEL}: none needed — <specific reason>' (at least four words and 20 letters).")
    if code and not (behavior_changed or no_behavior):
        errors.append(f"Code changed: declare '{BEHAVIOR_LABEL}: yes' or '{BEHAVIOR_LABEL}: no — <specific reason>' (at least four words and 20 letters).")
    elif behavior is not None and not (behavior_changed or no_behavior):
        errors.append(f"Use a valid '{BEHAVIOR_LABEL}: yes' or 'no — <specific reason>' declaration.")
    if behavior_changed and not release_lines:
        errors.append("Behavior changed: add a nonempty release-note line in a configured file; headings, comments, placeholders and missing text diffs do not qualify.")
    exemption = None
    if names and all(matches(name, exempt_patterns) for name in names):
        exemption = "configured-exempt-files-only"
    elif not code:
        exemption = "no-configured-code-change"
    return {"version": 1, "status": "failed" if errors else "passed", "changed_files": len(files),
            "code_paths_changed": len(code), "doc_paths_changed": len(docs),
            "release_note_lines_added": release_lines, "behavior_changed": behavior_changed,
            "exemption": exemption, "errors": errors}


class GitHub:
    def __init__(self, token, api_url="https://api.github.com"):
        parsed = urlsplit(api_url)
        if (not token or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise PipelineError("A read-only GitHub token and HTTPS API endpoint are required")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def __call__(self, endpoint):
        request = Request(self.api_url + endpoint, headers={"Authorization": f"Bearer {self.token}",
                          "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise PipelineError("GitHub response exceeds the coverage-check limit")
            return json.loads(raw)
        except (HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
            raise PipelineError("Could not read complete PR metadata from GitHub; check read permissions and retry") from exc


def snapshot(pr, repository, number, head):
    if (not isinstance(pr, dict) or not isinstance(pr.get("base"), dict)
            or not isinstance(pr.get("head"), dict) or not isinstance(pr["base"].get("repo"), dict)
            or pr.get("number") != number
            or pr.get("base", {}).get("repo", {}).get("full_name") != repository
            or pr.get("head", {}).get("sha") != head
            or not isinstance(pr.get("base", {}).get("sha"), str)
            or not SHA.fullmatch(pr["base"]["sha"])
            or type(pr.get("changed_files")) is not int or not 0 <= pr["changed_files"] <= MAX_FILES):
        raise PipelineError("PR identity changed or its complete file list is unavailable; rerun on the current head")
    visible_prose(pr.get("body"))
    return (head, pr["base"]["sha"], pr.get("body"), pr["changed_files"])


def collect(event, repository, api, release_note_paths=()):
    if not isinstance(event, dict) or not isinstance(event.get("pull_request"), dict):
        raise PipelineError("This check requires a pull_request event")
    pr = event.get("pull_request", {}) if isinstance(event, dict) else {}
    number = event.get("number") if isinstance(event, dict) else None
    head = pr.get("head", {}).get("sha") if isinstance(pr.get("head"), dict) else None
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or not isinstance(event.get("repository"), dict)
            or event.get("repository", {}).get("full_name") != repository
            or type(number) is not int or number < 1 or not isinstance(head, str) or not SHA.fullmatch(head)):
        raise PipelineError("This check requires a pull_request event with an exact head and repository")
    endpoint = f"/repos/{repository}/pulls/{number}"
    current = api(endpoint)
    before = snapshot(current, repository, number, head)
    files = []
    for page in range(1, (before[3] + 99) // 100 + 1):
        batch = api(f"{endpoint}/files?per_page=100&page={page}")
        expected = min(100, before[3] - len(files))
        if not isinstance(batch, list) or len(batch) != expected:
            raise PipelineError("GitHub did not return the complete PR file list; retry the check")
        files.extend(batch)
    validate_files(files)
    release_files = [changed for changed in files if changed["status"] != "removed"
                     and matches(changed["filename"], release_note_paths) and changed.get("patch")]
    if len(release_files) > 20:
        raise PipelineError("Select at most 20 changed release-note files")
    for changed in release_files:
        content = api(f"/repos/{repository}/contents/{quote(changed['filename'], safe='/')}?ref={head}")
        if (not isinstance(content, dict) or content.get("type") != "file"
                or content.get("path") != changed["filename"] or content.get("encoding") != "base64"
                or type(content.get("size")) is not int or not 0 <= content["size"] <= 1024 * 1024
                or not isinstance(content.get("content"), str)):
            raise PipelineError("Could not verify the release-note file at the exact PR head")
        try:
            raw = base64.b64decode("".join(content["content"].split()), validate=True)
            if len(raw) != content["size"]:
                raise ValueError("size mismatch")
            changed["new_content"] = raw.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise PipelineError("Release notes must be a complete UTF-8 text file") from exc
    if snapshot(api(endpoint), repository, number, head) != before:
        raise PipelineError("PR head, base or description changed during the check; rerun it")
    return current.get("body"), files, {"repository": repository, "pull_request": number, "head_sha": head, "base_sha": before[1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
            raise PipelineError("Use pull_request, including edited events; pull_request_target is not supported")
        event = load_json(os.environ.get("GITHUB_EVENT_PATH", ""))
        config = load_json(confined_path(args.root, args.config))
        policy = configuration(config)
        api = GitHub(os.environ.get("PR_DOCS_GITHUB_TOKEN", ""), os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        body, files, identity = collect(event, os.environ.get("GITHUB_REPOSITORY", ""), api, policy["release_note_paths"])
        result = {**evaluate(config, body, files), **identity}
        print(json.dumps(result, sort_keys=True))
        return 1 if result["status"] != "passed" else 0
    except PipelineError as error:
        print(f"PR coverage check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
