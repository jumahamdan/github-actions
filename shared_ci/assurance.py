"""Bind a check receipt to its reviewed configuration and source tree."""

from pathlib import Path

from .common import PipelineError, fingerprint, run, sha256_file


def library_identity(library_root):
    """Fingerprint executable library policy; documentation-only commits may reuse it.

    Include nested helpers and installation locks as well as the module itself:
    unchanged quality.py alone cannot prove unchanged workflow behavior.
    """
    root = Path(library_root).resolve()
    patterns = ("shared_ci/**/*.py", "scripts/**/*.py", "scripts/**/*.sh", "scripts/**/*.ps1",
                "actions/v2-*/action.yml", "actions/v2-*/action.yaml",
                ".github/workflows/v2-*.yml", ".github/workflows/v2-*.yaml",
                "tooling/ai/package-lock.json", "tooling/ai/package.json",
                "tooling/binaries.json", "requirements-ci.txt")
    selected = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root)
            if path.is_symlink() or any((root / parent).is_symlink() for parent in relative.parents):
                raise PipelineError("Library implementation cannot contain symlinks")
            if not path.is_file():
                raise PipelineError("Library implementation must contain regular files")
            selected[relative.as_posix()] = sha256_file(path)
    if not any(path.startswith("shared_ci/") for path in selected):
        raise PipelineError("Shared library implementation is missing")
    return fingerprint(selected)


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
