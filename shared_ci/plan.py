"""Select product work and enforce the feature/develop/staging/main branch route."""

import fnmatch
import os
import re
from pathlib import Path

from .common import PipelineError, load_json, run, strict_keys


def decide(config, changed, event, *, event_name, ref):
    strict_keys(config, {"schema", "products", "shared_paths"}, {"schema", "products", "shared_paths"})
    if config["schema"] != 1 or not isinstance(config["products"], dict) or not config["products"]:
        raise PipelineError("Product selection policy is empty")
    if event_name == "pull_request":
        pr = event["pull_request"]
        head, base = pr["head"]["ref"], pr["base"]["ref"]
        if base == "develop" and head not in {"develop", "staging", "main"}:
            phase = "check"
        elif (head, base) in (("develop", "staging"), ("staging", "main")):
            phase = "verify"
        else:
            raise PipelineError("Pull request does not follow the configured branch route")
    elif event_name in ("push", "workflow_dispatch") and ref in ("refs/heads/develop", "refs/heads/staging", "refs/heads/main"):
        phase = "integrate" if ref == "refs/heads/develop" else "deliver"
    else:
        raise PipelineError("Unsupported event or branch for product selection")
    shared = config["shared_paths"]
    if not isinstance(shared,list) or not all(isinstance(p,str) and p for p in shared):
        raise PipelineError("Shared path selection is invalid")
    decisions = {}
    for name, product in config["products"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise PipelineError("Invalid product identifier")
        strict_keys(product, {"paths"}, {"paths"})
        patterns = product["paths"]
        if not isinstance(patterns,list) or not patterns or not all(isinstance(p,str) and p for p in patterns):
            raise PipelineError("Product paths cannot be empty")
        applicable = any(fnmatch.fnmatchcase(path, pattern) for path in changed for pattern in patterns+shared)
        decisions[name] = {"changed": applicable, "check": applicable and phase == "check",
                           "verify": applicable and phase == "verify", "deliver": applicable and phase == "deliver"}
    return {"schema":1,"status":"passed","phase":phase,"products":decisions,"changed_file_count":len(changed)}


def execute(config, root):
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or os.environ.get("GITHUB_ACTIONS") != "true":
        raise PipelineError("Product selection requires a GitHub event")
    event = load_json(Path(event_path))
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    if name == "pull_request":
        base, head = event["pull_request"]["base"]["sha"], event["pull_request"]["head"]["sha"]
        range_mode = "..."
    else:
        base, head = event.get("before", ""), os.environ.get("GITHUB_SHA", "")
        range_mode = ".."
    if not re.fullmatch(r"[0-9a-f]{40}",head):
        raise PipelineError("Selection requires an exact source commit")
    if name == "workflow_dispatch" or base == "0"*40:
        changed = run(["git","ls-tree","-r","--name-only","-z",head],cwd=root).stdout.split("\0")
    elif re.fullmatch(r"[0-9a-f]{40}",base):
        changed = run(["git","diff","--name-only","-z",base+range_mode+head],cwd=root).stdout.split("\0")
    else:
        raise PipelineError("Selection requires an exact comparison base")
    return decide(config,[p for p in changed if p],event,event_name=name,ref=os.environ.get("GITHUB_REF", ""))
