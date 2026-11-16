"""Audience-safe notes and idempotent publication of an already-approved candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .common import PipelineError, atomic_json, cli_error, confined_path, load_json, sha256_file, strict_keys, trusted_context
from .evidence import verify


class APIError(PipelineError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub release request failed (HTTP {status})")


def github_api(method, path, token, body=None, binary=None):
    if not path.startswith("/repos/") or not token:
        raise PipelineError("GitHub repository path and credential are required")
    host = "https://uploads.github.com" if binary is not None else "https://api.github.com"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    data = binary if binary is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = "application/octet-stream" if binary is not None else "application/json"
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            urllib.request.Request(host + path, method=method, headers=headers, data=data), timeout=120
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise APIError(exc.code) from None
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise PipelineError("GitHub release response unavailable or invalid") from exc


def render(notes, audience):
    strict_keys(notes, {"schema", "product", "version", "build", "sections", "locales"},
                {"schema", "product", "version", "build", "sections"})
    if notes["schema"] != 1 or audience not in ("public", "testers", "internal"):
        raise PipelineError("Unsupported notes schema or audience")
    if not isinstance(notes["sections"], list) or not notes["sections"]:
        raise PipelineError("Release notes require authored sections")
    rendered = []
    for section in notes["sections"]:
        strict_keys(section, {"title", "body", "audiences"}, {"title", "body", "audiences"})
        if not isinstance(section["audiences"], list) or not section["audiences"] or any(
            a not in ("public", "testers", "internal") for a in section["audiences"]
        ):
            raise PipelineError("Each section needs an explicit audience allowlist")
        if not all(isinstance(section[k], str) and section[k].strip() for k in ("title", "body")):
            raise PipelineError("Release section is empty")
        if re.search(r"\b(TODO|TBD)\b|\[\[", section["body"]):
            raise PipelineError("Release notes contain an unfinished placeholder")
        if audience in section["audiences"]:
            rendered.append(f"## {section['title']}\n\n{section['body'].strip()}")
    if not rendered:
        raise PipelineError("Release notes have no content for the selected audience")
    return "\n\n".join(rendered) + "\n"


def draft_from_prs(changes, *, previous_tag, candidate_sha):
    if not previous_tag or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise PipelineError("Draft generation requires explicit release boundaries")
    seen, groups = set(), {}
    for change in changes:
        strict_keys(change, {"number", "title", "category", "promotion"}, {"number", "title", "category"})
        number = change["number"]
        if not isinstance(number, int) or number < 1:
            raise PipelineError("Invalid pull request identity")
        if change.get("promotion") or number in seen:
            continue
        seen.add(number)
        groups.setdefault(change["category"], []).append(f"- {change['title']} (#{number})")
    if not groups:
        raise PipelineError("No underlying changes for draft notes")
    return {"schema": 1, "status": "draft", "previous_tag": previous_tag,
            "candidate_sha": candidate_sha, "categories": groups}


def publish(config, root, *, api=github_api):
    strict_keys(config, {"schema", "repository", "tag", "candidate", "notes", "audience", "allowed_refs",
                        "token_env", "prerelease", "publish", "expected_commit"},
                {"schema", "repository", "tag", "candidate", "notes", "audience", "allowed_refs",
                 "token_env", "prerelease", "publish", "expected_commit"})
    if config["schema"] != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]):
        raise PipelineError("Invalid release schema/repository")
    if not re.fullmatch(r"v[A-Za-z0-9][A-Za-z0-9._+-]*", config["tag"]):
        raise PipelineError("Invalid release tag")
    if not isinstance(config["publish"], bool) or not isinstance(config["prerelease"], bool):
        raise PipelineError("Publication state must be explicit booleans")
    trusted_context(config["allowed_refs"])
    if os.environ.get("GITHUB_REPOSITORY") != config["repository"]:
        raise PipelineError("Cross-repository publication is not permitted")
    candidate = load_json(confined_path(root, config["candidate"]))
    verify(candidate, root)
    notes = load_json(confined_path(root, config["notes"]))
    if any(notes.get(k) != candidate.get(k) for k in ("product", "version", "build")):
        raise PipelineError("Release notes do not describe the selected candidate")
    body = render(notes, config["audience"])
    token = os.environ.get(config["token_env"], "")
    prefix = "/repos/" + config["repository"]
    obj = api("GET", prefix + "/git/ref/tags/" + urllib.parse.quote(config["tag"], safe=""), token)["object"]
    for _ in range(5):
        if obj.get("type") != "tag":
            break
        obj = api("GET", prefix + "/git/tags/" + obj["sha"], token)["object"]
    if obj.get("type") != "commit" or obj.get("sha") != config["expected_commit"] or config["expected_commit"] != os.environ.get("GITHUB_SHA"):
        raise PipelineError("Existing release tag is not the approved current commit")
    try:
        release = api("GET", prefix + "/releases/tags/" + urllib.parse.quote(config["tag"], safe=""), token)
    except APIError as exc:
        if exc.status != 404:
            raise
        release = api("POST", prefix + "/releases", token, {"tag_name": config["tag"],
                      "name": config["tag"], "body": body, "draft": True, "prerelease": config["prerelease"]})
    if release.get("body") != body or release.get("prerelease") != config["prerelease"]:
        raise PipelineError("Existing release differs; replacement requires a new explicit release decision")
    existing = {a["name"]: a for a in release.get("assets", [])}
    artifact_records = dict(candidate["artifacts"])
    manifest_path = confined_path(root, config["candidate"])
    artifact_records[config["candidate"]] = {"sha256": sha256_file(manifest_path), "bytes": manifest_path.stat().st_size}
    expected_names = [Path(p).name for p in artifact_records]
    if len(expected_names) != len(set(expected_names)):
        raise PipelineError("Release artifact basenames collide")
    if set(existing) - set(expected_names):
        raise PipelineError("Existing release contains unexpected assets")
    for relative, record in artifact_records.items():
        name = Path(relative).name
        digest = "sha256:" + record["sha256"]
        if name in existing:
            if existing[name].get("digest") != digest or existing[name].get("state") != "uploaded":
                raise PipelineError("Existing release asset has different bytes or is incomplete")
        else:
            if not release.get("draft"):
                raise PipelineError("Published release is missing a required asset; refusing mutation")
            asset = api("POST", prefix + f"/releases/{int(release['id'])}/assets?name=" + urllib.parse.quote(name),
                        token, binary=confined_path(root, relative).read_bytes())
            if asset.get("digest") != digest or asset.get("state") != "uploaded":
                raise PipelineError("Uploaded release asset verification failed")
    if config["publish"] and release.get("draft"):
        release = api("PATCH", prefix + f"/releases/{int(release['id'])}", token, {"draft": False})
    return {"schema": 1, "status": "passed", "candidate_identity": candidate["identity"],
            "release_id": release["id"], "tag": config["tag"], "state": "draft" if release.get("draft") else "published",
            "store_status": "not-established-by-github-publication"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("render", "draft", "publish"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--audience", choices=("public", "testers", "internal"), default="public")
    args = parser.parse_args()
    config = load_json(args.config)
    if args.operation == "render":
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(config, args.audience))
    elif args.operation == "draft":
        atomic_json(args.output, draft_from_prs(config["changes"], previous_tag=config["previous_tag"], candidate_sha=config["candidate_sha"]))
    else:
        atomic_json(args.output, publish(config, args.root))


if __name__ == "__main__":
    cli_error(main)
