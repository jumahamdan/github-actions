"""Read store build-number inventories without committing a release."""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from pathlib import Path

from .common import PipelineError, atomic_json, confined_path, sha256_file, strict_keys, trusted_context
from .stores import API, collection, data, env_secret, identifier


def build_number(value):
    if not re.fullmatch(r"[0-9]+", str(value)) or not 0 <= int(value) <= 2100000000:
        raise PipelineError("Store build number is not compatible with the product's positive integer allocation policy")
    return int(value)


def execute(config, root, *, api=None):
    required = {"schema", "operation", "product", "allowed_refs", "token_env", "remote_highest_output"}
    strict_keys(config, required | {"play", "apple"}, required)
    operation = config["operation"]
    if config["schema"] != 1 or operation not in ("play-inventory", "apple-inventory"):
        raise PipelineError("Unsupported store inventory operation")
    identifier(config["product"], "product")
    trusted_context(config["allowed_refs"])
    provider = "play" if operation == "play-inventory" else "apple"
    if provider not in config or ({"play", "apple"} - {provider}) & set(config):
        raise PipelineError("Exactly one matching store inventory configuration is required")
    request = api or API(provider, env_secret(config["token_env"]), 60)
    if api is not None:
        env_secret(config["token_env"])
    values, scope = [0], {}
    if provider == "play":
        settings = config["play"]
        strict_keys(settings, {"package"}, {"package"})
        package = identifier(settings["package"], "Play package")
        base = f"/androidpublisher/v3/applications/{package}/edits"
        edit = request("POST", base, {})
        edit_id = identifier(edit.get("id"), "Play inventory edit")
        base += "/" + edit_id
        counts = {}
        try:
            for group in ("bundles", "apks", "tracks"):
                items = request("GET", base + "/" + group).get(group, [])
                if not isinstance(items, list):
                    raise PipelineError("Malformed Play build inventory")
                counts[group] = len(items)
                for item in items:
                    if not isinstance(item, dict):
                        raise PipelineError("Malformed Play inventory resource")
                    if group == "tracks":
                        releases = item.get("releases", [])
                        if not isinstance(releases, list):
                            raise PipelineError("Malformed Play track releases")
                        for release in releases:
                            codes = release.get("versionCodes", [])
                            if not isinstance(codes, list):
                                raise PipelineError("Malformed Play release versions")
                            values.extend(build_number(code) for code in codes)
                    else:
                        values.append(build_number(item.get("versionCode")))
        finally:
            request("DELETE", base)
        scope = {"package": package, "inventory": counts, "coverage": "all_api_visible_tracks_bundles_apks"}
    else:
        settings = config["apple"]
        strict_keys(settings, {"app_id", "bundle_id", "platform", "version"}, {"app_id", "bundle_id", "platform", "version"})
        app = identifier(settings["app_id"], "Apple app")
        bundle = identifier(settings["bundle_id"], "bundle ID")
        version = identifier(settings["version"], "marketing version")
        if settings["platform"] not in ("IOS", "MAC_OS", "TV_OS", "VISION_OS"):
            raise PipelineError("Unsupported Apple platform")
        app_data = data(request, "GET", "/v1/apps/" + app)
        if app_data["id"] != app or app_data.get("attributes", {}).get("bundleId") != bundle:
            raise PipelineError("Apple inventory app/bundle identity mismatch")
        query = urllib.parse.urlencode({"filter[app]": app, "filter[preReleaseVersion.platform]": settings["platform"],
                                       "filter[preReleaseVersion.version]": version, "limit": "200"})
        builds = collection(request, "/v1/builds?" + query)
        ids = [build.get("id") for build in builds]
        if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
            raise PipelineError("Apple build inventory has absent or duplicate build IDs")
        for build in builds:
            values.append(build_number(build.get("attributes", {}).get("version")))
        scope = {"app_id": app, "bundle_id": bundle, "platform": settings["platform"], "version": version,
                 "builds": len(builds), "coverage": "all_build_states_for_app_platform_marketing_version"}
    inventory = {"product": config["product"], "highest": max(values), "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    output = confined_path(root, config["remote_highest_output"], must_exist=False)
    if output.exists():
        raise PipelineError("Remote inventory output must be fresh; retain previous allocation evidence separately")
    atomic_json(output, inventory)
    return {"schema": 1, "status": "passed", "operation": operation, "product": config["product"], "highest": inventory["highest"],
            "checked_at": inventory["checked_at"], "remote_highest_output": config["remote_highest_output"],
            "inventory_sha256": sha256_file(output), "scope": scope}
