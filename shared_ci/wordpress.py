"""Validate and reproducibly package selected WordPress components; never publish."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from .common import PipelineError, atomic_json, confined_path, load_json, run, sha256_file, strict_keys


class FragmentParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    def __init__(self):
        super().__init__(convert_charrefs=True); self.stack = []; self.tags = 0
    def handle_starttag(self, tag, attrs):
        self.tags += 1
        if tag not in self.VOID: self.stack.append(tag)
    def handle_startendtag(self, tag, attrs): self.tags += 1
    def handle_endtag(self, tag):
        if tag in self.VOID or not self.stack or self.stack.pop() != tag:
            raise PipelineError("HTML fragment has mismatched closing tags")
    def finish(self):
        self.close()
        if not self.tags or self.stack:
            raise PipelineError("HTML fragment is empty or has unclosed tags")


def execute(config, root):
    strict_keys(config, {"schema", "source", "package_name", "files", "php", "output", "receipt", "timeout"},
                required=("schema", "source", "package_name", "files", "output"))
    if type(config["schema"]) is not int or config["schema"] != 1:
        raise PipelineError("unsupported WordPress package schema")
    root = Path(root).resolve()
    source = confined_path(root, config["source"])
    if not source.is_dir() or not isinstance(config["package_name"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", config["package_name"]):
        raise PipelineError("invalid WordPress source or package name")
    files = config["files"]
    if not isinstance(files, list) or not files or not all(isinstance(p, str) and p for p in files) or len(files) != len(set(files)):
        raise PipelineError("WordPress package needs an explicit unique file list")
    timeout = config.get("timeout", 60)
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise PipelineError("invalid WordPress lint timeout")
    selected = []
    for relative in sorted(files):
        parts = Path(relative).parts
        if any(p in {".git", ".github", "node_modules", ".aws"} for p in parts) or Path(relative).name.startswith(".env") or Path(relative).name == "wp-config.php" or Path(relative).suffix.lower() in {".pem", ".key", ".p8", ".p12", ".pfx"}:
            raise PipelineError("credential/configuration material cannot enter a WordPress package")
        path = confined_path(source, relative)
        if any((source / Path(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)) or not path.is_file():
            raise PipelineError("WordPress package files must be regular non-symlink files")
        selected.append((relative, path))
    selected_bytes = {relative: path.read_bytes() for relative, path in selected}
    php_files = [p for _, p in selected if p.suffix.lower() == ".php"]
    if php_files:
        php = config.get("php")
        strict_keys(php, {"command", "version"}, required=("command", "version"))
        if not isinstance(php["command"], list) or len(php["command"]) != 1 or Path(php["command"][0]).name not in {"php", "php.exe"}:
            raise PipelineError("PHP must name the pinned PHP executable")
        if not isinstance(php["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", php["version"]):
            raise PipelineError("PHP version must be pinned")
        result = run(php["command"] + ["--version"], cwd=source, timeout=timeout)
        if not re.search(r"(?<![\w.])" + re.escape(php["version"]) + r"(?![\w.])", result.stdout + result.stderr):
            raise PipelineError("installed PHP version differs from configuration")
        for path in php_files:
            run(php["command"] + ["-l", str(path)], cwd=source, timeout=timeout)
    for _, path in selected:
        if path.suffix.lower() in {".html", ".htm"}:
            parser = FragmentParser(); parser.feed(path.read_text(encoding="utf-8")); parser.finish()
    output = confined_path(root, config["output"], must_exist=False)
    if output.suffix.lower() != ".zip" or output.is_relative_to(source):
        raise PipelineError("ZIP output must be outside the source component directory")
    if (root / config["output"]).is_symlink():
        raise PipelineError("ZIP output cannot be a symlink")
    receipt_path = None
    if config.get("receipt"):
        receipt_path = confined_path(root, config["receipt"], must_exist=False)
        if receipt_path == output or receipt_path.is_relative_to(source):
            raise PipelineError("receipt cannot overwrite package/source files")
    if any(path.read_bytes() != selected_bytes[relative] for relative, path in selected):
        raise PipelineError("validation changed the selected WordPress files")
    output.parent.mkdir(parents=True, exist_ok=True)
    # ZIP_STORED avoids zlib-version differences, with fixed timestamps/modes/order.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, path in selected:
            info = zipfile.ZipInfo(config["package_name"] + "/" + Path(relative).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3; info.external_attr = 0o100644 << 16
            archive.writestr(info, selected_bytes[relative])
    result = {"schema": 1, "kind": "wordpress-package", "status": "passed", "delivery_status": "packaged_not_published",
              "package": config["output"], "sha256": sha256_file(output), "files": {rel: hashlib.sha256(content).hexdigest() for rel, content in selected_bytes.items()},
              "handoff": ["Confirm a usable site backup and retain the previous component ZIP.",
                          "Install the selected ZIP through the existing WordPress publishing process.",
                          "Purge the relevant cache and check the changed pages and app association files.",
                          "Record installation evidence; restore the previous component if verification fails."]}
    if receipt_path:
        atomic_json(receipt_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True); parser.add_argument("--root", default=".")
    args = parser.parse_args()
    try:
        result = execute(load_json(confined_path(args.root, args.config)), args.root)
        print(json.dumps({k: result[k] for k in ("status", "delivery_status", "sha256")}))
    except (PipelineError, OSError, ValueError) as exc:
        print(f"wordpress: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
