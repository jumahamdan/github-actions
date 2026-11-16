"""Bind a check receipt to its reviewed configuration and source tree."""

from pathlib import Path

from .common import PipelineError, run, sha256_file


def stamp(receipt, kind, config_path, root):
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout.strip():
        raise PipelineError("Cannot retain release evidence from modified tracked source")
    receipt["assurance"] = {
        "kind": kind,
        "source_sha": run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip(),
        "source_tree": run(["git", "rev-parse", "HEAD^{tree}"], cwd=root).stdout.strip(),
        "config_sha256": sha256_file(Path(config_path)),
    }
    return receipt
