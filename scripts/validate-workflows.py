#!/usr/bin/env python3
"""Validate v2 files, including GitHub.com self references unknown to actionlint 1.7.12."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def validate():
    workflows = sorted((ROOT / ".github/workflows").glob("v2-*.yml"))
    workflows.append(ROOT / ".github/workflows/github-actions-ci.yml")
    commands = []
    with tempfile.TemporaryDirectory(prefix="shared-ci-workflow-lint-") as d:
        for path in workflows:
            parsed = yaml.safe_load(path.read_text())
            if not isinstance(parsed, dict) or "jobs" not in parsed:
                raise ValueError(f"Invalid workflow: {path.name}")
            for job in parsed["jobs"].values():
                for step in job.get("steps", []):
                    uses = step.get("uses", "")
                    if not uses:
                        continue
                    if uses.startswith("$/"):
                        if "@" in uses or ".." in Path(uses[2:]).parts:
                            raise ValueError("Invalid self-repository reference")
                        metadata_path = ROOT / uses[2:] / "action.yml"
                        metadata = yaml.safe_load(metadata_path.read_text())
                        if metadata.get("runs", {}).get("using") != "composite":
                            raise ValueError("Expected reviewed composite action")
                        supplied = set(step.get("with", {}))
                        declared = metadata.get("inputs", {})
                        if supplied - set(declared):
                            raise ValueError(f"Unknown inputs to {uses}")
                        for name, spec in declared.items():
                            if spec.get("required") and name not in supplied and "default" not in spec:
                                raise ValueError(f"Missing input {name} to {uses}")
                        # This is only a parser compatibility translation in a temporary copy.
                        # GitHub receives the original $/ reference and resolves the exact revision.
                        step["uses"] = "./" + uses[2:]
                    elif not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", uses):
                        raise ValueError(f"Unpinned action: {uses}")
            temporary = Path(d) / path.name
            temporary.write_text(yaml.safe_dump(parsed, sort_keys=False))
            commands.append(str(temporary))
        subprocess.run(["actionlint", *commands], cwd=ROOT, check=True)
    for path in (ROOT / "actions").glob("v2-*/action.yml"):
        metadata = yaml.safe_load(path.read_text())
        if not all(key in metadata for key in ("name", "description", "runs")):
            raise ValueError("Invalid action metadata")
        for step in metadata["runs"]["steps"]:
            if "run" in step:
                if "${{" in step["run"]:
                    raise ValueError("Action inputs must use environment bindings, not shell interpolation")
                with tempfile.NamedTemporaryFile(mode="w", suffix=".sh") as script:
                    script.write("#!/usr/bin/env bash\n" + step["run"])
                    script.flush()
                    subprocess.run(["shellcheck", script.name], check=True)
    print(json.dumps({"workflows": len(workflows), "self_reference_metadata": "validated",
                      "actionlint_compatibility": "$/ translated to ./ only for local parser"}))


if __name__ == "__main__":
    validate()
