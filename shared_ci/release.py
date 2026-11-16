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

from .common import PipelineError, atomic_json, cli_error, confined_path, load_json, run, sha256_file, strict_keys, trusted_context
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
            data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise PipelineError("GitHub release response exceeds size limit")
            return json.loads(data) if data else None
    except urllib.error.HTTPError as exc:
        raise APIError(exc.code) from None
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise PipelineError("GitHub release response unavailable or invalid") from exc


def render(notes, audience, locale=None):
    strict_keys(notes, {"schema", "product", "version", "build", "sections", "locales", "required_sections", "max_characters"},
                {"schema", "product", "version", "build", "sections"})
    if notes["schema"] != 1 or audience not in ("public", "testers", "internal"):
        raise PipelineError("Unsupported notes schema or audience")
    if not isinstance(notes["sections"], list) or not notes["sections"]:
        raise PipelineError("Release notes require authored sections")
    locales = notes.get("locales", {})
    if not isinstance(locales, dict) or any(not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language) for language in locales):
        raise PipelineError("Invalid release-note locale map")
    for language, sections in locales.items():
        render({**notes, "sections": sections, "locales": {}}, audience)
    if locale is not None:
        if locale not in locales:
            raise PipelineError("Selected release-note locale is absent")
        return render({**notes,"sections":locales[locale],"locales":{}},audience)
    required = notes.get("required_sections", {})
    if not isinstance(required,dict) or set(required)-{"public","testers","internal"}:
        raise PipelineError("Invalid required release-note section policy")
    for titles in required.values():
        if not isinstance(titles,list) or not all(isinstance(title,str) and title for title in titles):
            raise PipelineError("Required section names must be nonempty strings")
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
    selected_titles=[section["title"] for section in notes["sections"] if audience in section["audiences"]]
    if len(selected_titles)!=len(set(selected_titles)) or set(required.get(audience,[]))-set(selected_titles):
        raise PipelineError("Release notes have duplicate or missing required sections")
    text="\n\n".join(rendered)+"\n"
    limit=notes.get("max_characters",50000)
    if type(limit) is not int or not 1<=limit<=50000 or len(text)>limit:
        raise PipelineError("Release notes exceed the explicit destination length limit")
    return text


def draft(config, root, *, api=github_api):
    strict_keys(config,{"schema","repository","previous_tag","candidate_sha","token_env","categories","max_commits"},
                {"schema","repository","previous_tag","candidate_sha","token_env","categories"})
    if config["schema"]!=1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",config["repository"]):
        raise PipelineError("Invalid release draft repository")
    tag=config["previous_tag"]; candidate=config["candidate_sha"]
    if not re.fullmatch(r"v[A-Za-z0-9][A-Za-z0-9._+-]*",tag) or not re.fullmatch(r"[0-9a-f]{40}",candidate):
        raise PipelineError("Release draft boundaries must be explicit tag and commit")
    if not isinstance(config["categories"],dict) or not all(isinstance(k,str) and isinstance(v,str) and v for k,v in config["categories"].items()):
        raise PipelineError("Release category mapping is invalid")
    base=run(["git","rev-parse","--verify","refs/tags/"+tag+"^{commit}"],cwd=root).stdout.strip()
    if run(["git","merge-base","--is-ancestor",base,candidate],cwd=root,check=False).returncode:
        raise PipelineError("Previous release is not an ancestor of the candidate")
    commits=run(["git","rev-list",base+".."+candidate],cwd=root).stdout.splitlines()
    maximum=config.get("max_commits",500)
    if type(maximum) is not int or not 1<=maximum<=1000 or not commits or len(commits)>maximum:
        raise PipelineError("Release comparison is empty or exceeds its configured bound")
    token=os.environ.get(config["token_env"],"")
    if not token:raise PipelineError("Release draft requires its named read-only GitHub credential")
    prefix="/repos/"+config["repository"]
    changes=[]; seen=set(); boundaries=set(commits)
    for sha in commits:
        for pr in _pages(api,prefix+"/commits/"+sha+"/pulls",token):
            if not pr.get("merged_at") or pr.get("merge_commit_sha") not in boundaries or pr.get("number") in seen:
                continue
            if pr.get("base",{}).get("repo",{}).get("full_name")!=config["repository"]:
                raise PipelineError("Release comparison returned a different repository")
            seen.add(pr["number"])
            labels={label["name"] for label in pr.get("labels",[])}
            category=next((value for label,value in config["categories"].items() if label in labels),"Changes")
            route=(pr.get("head",{}).get("ref"),pr.get("base",{}).get("ref"))
            changes.append({"number":pr["number"],"title":pr["title"],"category":category,"promotion":route in (("develop","staging"),("staging","main"))})
    result=draft_from_prs(changes,previous_tag=tag,candidate_sha=candidate)
    result["boundaries_verified"]=True
    result["previous_sha"]=base
    return result


def draft_from_prs(changes, *, previous_tag, candidate_sha):
    if not previous_tag or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise PipelineError("Draft generation requires explicit release boundaries")
    if not isinstance(changes, list):
        raise PipelineError("Draft changes must be a list")
    seen, groups = set(), {}
    for change in changes:
        strict_keys(change, {"number", "title", "category", "promotion"}, {"number", "title", "category"})
        number = change["number"]
        if type(number) is not int or number < 1:
            raise PipelineError("Invalid pull request identity")
        if any(not isinstance(change[key], str) or not change[key].strip() or "\n" in change[key] or "\r" in change[key]
               for key in ("title", "category")) or type(change.get("promotion", False)) is not bool:
            raise PipelineError("Draft changes require nonempty single-line title/category and boolean promotion")
        if change.get("promotion") or number in seen:
            continue
        seen.add(number)
        groups.setdefault(change["category"], []).append(f"- {change['title']} (#{number})")
    if not groups:
        raise PipelineError("No underlying changes for draft notes")
    return {"schema": 1, "status": "draft", "previous_tag": previous_tag,
            "candidate_sha": candidate_sha, "categories": groups}


def _pages(api, path, token):
    values = []
    for page in range(1, 21):
        batch = api("GET", path + f"?per_page=100&page={page}", token)
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            raise PipelineError("GitHub release list response is malformed")
        values.extend(batch)
        if len(batch) < 100:
            return values
    raise PipelineError("GitHub release inventory exceeds bounded pagination")


def _tag_commit(api, prefix, tag, token, expected):
    response = api("GET", prefix + "/git/ref/tags/" + urllib.parse.quote(tag, safe=""), token)
    obj = response.get("object", {}) if isinstance(response, dict) else {}
    seen = set()
    for _ in range(5):
        if obj.get("type") != "tag":
            break
        sha = obj.get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or sha in seen:
            raise PipelineError("Invalid or cyclic annotated tag")
        seen.add(sha)
        response = api("GET", prefix + "/git/tags/" + sha, token)
        obj = response.get("object", {}) if isinstance(response, dict) else {}
    if obj.get("type") != "commit" or obj.get("sha") != expected:
        raise PipelineError("Existing release tag is not the approved current commit")


def _release_shape(release, tag, body, prerelease):
    if (not isinstance(release, dict) or type(release.get("id")) is not int or release["id"] < 1 or
            release.get("tag_name") != tag or type(release.get("draft")) is not bool or
            type(release.get("prerelease")) is not bool):
        raise PipelineError("GitHub returned an invalid release identity/state")
    if release.get("body") != body or release["prerelease"] != prerelease:
        raise PipelineError("Existing release differs; replacement requires a new explicit release decision")
    return release


def _find_release(api, prefix, tag, token):
    try:
        return api("GET", prefix + "/releases/tags/" + urllib.parse.quote(tag, safe=""), token)
    except APIError as exc:
        if exc.status != 404:
            raise
    # The by-tag endpoint guarantees published releases. List releases with push auth to find drafts.
    matches = [item for item in _pages(api, prefix + "/releases", token) if item.get("tag_name") == tag]
    if len(matches) > 1:
        raise PipelineError("Multiple releases refer to the selected tag")
    return matches[0] if matches else None


def _assets(api, prefix, release_id, token):
    values = _pages(api, prefix + f"/releases/{release_id}/assets", token)
    result = {}
    for item in values:
        name = item.get("name")
        if not isinstance(name, str) or not name or name in result or type(item.get("id")) is not int or item["id"] < 1:
            raise PipelineError("GitHub returned invalid/duplicate asset identities")
        result[name] = item
    return result


def _asset_matches(asset, name, record):
    return (asset.get("name") == name and asset.get("state") == "uploaded" and
            asset.get("digest") == "sha256:" + record["sha256"] and asset.get("size") == record["bytes"])


def publish(config, root, *, api=github_api):
    strict_keys(config, {"schema", "repository", "tag", "candidate", "notes", "audience", "allowed_refs",
                        "token_env", "prerelease", "publish", "expected_commit", "expected_library_sha", "expected_toolchain"},
                {"schema", "repository", "tag", "candidate", "notes", "audience", "allowed_refs",
                 "token_env", "prerelease", "publish", "expected_commit", "expected_library_sha", "expected_toolchain"})
    if config["schema"] != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]):
        raise PipelineError("Invalid release schema/repository")
    if not re.fullmatch(r"v[A-Za-z0-9][A-Za-z0-9._+-]*", config["tag"]):
        raise PipelineError("Invalid release tag")
    if not isinstance(config["publish"], bool) or not isinstance(config["prerelease"], bool):
        raise PipelineError("Publication state must be explicit booleans")
    context = trusted_context(config["allowed_refs"])
    if context["repository"] != config["repository"]:
        raise PipelineError("Cross-repository publication is not permitted")
    expected = config["expected_commit"]
    if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected) or
            expected != context["sha"] or run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip() != expected):
        raise PipelineError("Release commit does not match the current checked-out workflow commit")
    candidate_path = confined_path(root, config["candidate"])
    candidate = load_json(candidate_path)
    if (not isinstance(config["expected_library_sha"], str) or
            not re.fullmatch(r"[0-9a-f]{40}", config["expected_library_sha"]) or
            not isinstance(config["expected_toolchain"], dict) or not config["expected_toolchain"]):
        raise PipelineError("Publication requires explicit expected library SHA and toolchain")
    verify(candidate, root, expected_library_sha=config["expected_library_sha"],
           expected_toolchain=config["expected_toolchain"])
    notes = load_json(confined_path(root, config["notes"]))
    if any(notes.get(k) != candidate.get(k) for k in ("product", "version", "build")):
        raise PipelineError("Release notes do not describe the selected candidate")
    body = render(notes, config["audience"])
    if not isinstance(config["token_env"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", config["token_env"]):
        raise PipelineError("Invalid release credential environment name")
    token = os.environ.get(config["token_env"], "")
    if not token:
        raise PipelineError("Release credential is missing")
    # Complete local checks before creating a remote draft. GitHub normalizes special asset filenames.
    artifact_records = dict(candidate["artifacts"])
    if config["candidate"] in artifact_records:
        raise PipelineError("Candidate manifest cannot also be a candidate payload")
    artifact_records[config["candidate"]] = {"sha256": sha256_file(candidate_path), "bytes": candidate_path.stat().st_size}
    expected_assets = {}
    for relative, record in artifact_records.items():
        name = Path(relative).name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name) or name.endswith(".") or name in expected_assets:
            raise PipelineError("Release asset names must be unique and GitHub-safe")
        expected_assets[name] = (relative, record)
    prefix = "/repos/" + config["repository"]
    _tag_commit(api, prefix, config["tag"], token, expected)
    release = _find_release(api, prefix, config["tag"], token)
    if release is None:
        release = api("POST", prefix + "/releases", token, {"tag_name": config["tag"],
                      "target_commitish": expected, "name": config["tag"], "body": body,
                      "draft": True, "prerelease": config["prerelease"]})
        if not isinstance(release, dict) or release.get("draft") is not True:
            raise PipelineError("GitHub did not create the requested draft")
    _release_shape(release, config["tag"], body, config["prerelease"])
    release_id = release["id"]
    existing = _assets(api, prefix, release_id, token)
    if set(existing) - set(expected_assets):
        raise PipelineError("Existing release contains unexpected assets")
    for name, (relative, record) in expected_assets.items():
        if name in existing:
            asset = existing[name]
            if _asset_matches(asset, name, record):
                continue
            # GitHub documents zero-byte 'starter' assets after interrupted uploads as safe to delete.
            if release["draft"] and asset.get("state") == "starter" and asset.get("size") == 0:
                api("DELETE", prefix + f"/releases/assets/{asset['id']}", token)
            else:
                raise PipelineError("Existing release asset has different bytes or is incomplete")
        if not release["draft"]:
            raise PipelineError("Published release is missing a required asset; refusing mutation")
        payload = confined_path(root, relative).read_bytes()
        if hashlib.sha256(payload).hexdigest() != record["sha256"] or len(payload) != record["bytes"]:
            raise PipelineError("Release artifact changed before upload")
        asset = api("POST", prefix + f"/releases/{release_id}/assets?name=" + urllib.parse.quote(name, safe=""),
                    token, binary=payload)
        if not isinstance(asset, dict) or not _asset_matches(asset, name, record):
            raise PipelineError("Uploaded release asset verification failed")
    # Re-read the complete remote inventory; upload success alone is not a publication gate.
    verified_assets = _assets(api, prefix, release_id, token)
    if set(verified_assets) != set(expected_assets) or any(
        not _asset_matches(verified_assets[name], name, record) for name, (_, record) in expected_assets.items()
    ):
        raise PipelineError("Final release asset inventory differs from the candidate")
    observed = api("GET", prefix + f"/releases/{release_id}", token)
    _release_shape(observed, config["tag"], body, config["prerelease"])
    if observed["id"] != release_id or observed["draft"] != release["draft"]:
        raise PipelineError("Release state changed concurrently")
    _tag_commit(api, prefix, config["tag"], token, expected)
    if config["publish"] and observed["draft"]:
        observed = api("PATCH", prefix + f"/releases/{release_id}", token, {"draft": False})
        _release_shape(observed, config["tag"], body, config["prerelease"])
        if observed["id"] != release_id or observed["draft"] is not False:
            raise PipelineError("GitHub did not publish the selected release")
    return {"schema": 1, "status": "passed", "candidate_identity": candidate["identity"],
            "release_id": release_id, "tag": config["tag"], "commit": expected,
            "state": "draft" if observed["draft"] else "published",
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
        atomic_json(args.output, draft(config,args.root))
    else:
        atomic_json(args.output, publish(config, args.root))


if __name__ == "__main__":
    cli_error(main)
