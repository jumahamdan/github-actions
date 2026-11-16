#!/usr/bin/env python3
"""Install tool dependencies before any provider credentials are exposed."""
import os
import subprocess
import sys
from pathlib import Path

LIBRARY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LIBRARY))
from shared_ci.common import PipelineError, confined_path, load_json
from shared_ci.tools import install


def main():
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    paths = []
    bundles={"sonar":"tools-sonar-linux-x64.json","google-sdk":"tools-google-sdk-linux-x64.json",
             "bundletool":"tools-bundletool.json","android-build-tools":"tools-android-build-tools-linux-x64.json"}
    selected=[name.strip() for name in os.environ.get("TOOL_BUNDLES","").split(",") if name.strip()]
    if len(selected)!=len(set(selected)) or set(selected)-set(bundles):
        raise PipelineError("Unknown or duplicate built-in tool bundle")
    destination=Path(os.environ["RUNNER_TEMP"])/"shared-ci-tools"
    for name in selected:
        install(load_json(LIBRARY/"examples"/bundles[name]),destination)
    if selected:
        paths.append(str(destination))
    if "bundletool" in selected:
        with open(os.environ["GITHUB_ENV"],"a") as stream:
            stream.write("SHARED_BUNDLETOOL_JAR="+str(destination/"bundletool.jar")+"\n")
    if os.environ.get("BINARY_MANIFEST"):
        manifest = confined_path(workspace, os.environ["BINARY_MANIFEST"])
        destination = Path(os.environ["RUNNER_TEMP"]) / "shared-ci-tools"
        install(load_json(manifest), destination)
        paths.append(str(destination))
    if os.environ.get("PYTHON_TOOLS"):
        requirements = confined_path(workspace, os.environ["PYTHON_TOOLS"])
        subprocess.run([sys.executable,"-m","pip","install","--only-binary=:all:","--require-hashes","-r",str(requirements)],check=True)
    directories = []
    if os.environ.get("NPM_TOOLS"):
        directories.append(confined_path(workspace, os.environ["NPM_TOOLS"]))
    if os.environ.get("INSTALL_AI_TOOLS") == "true":
        directories.append(LIBRARY / "tooling" / "ai")
    for directory in directories:
        if not (directory/"package-lock.json").is_file():
            raise PipelineError("CLI installation requires a reviewed npm lockfile")
        subprocess.run(["npm","ci","--prefix",str(directory),"--ignore-scripts","--no-audit","--no-fund"],check=True)
        paths.append(str(directory/"node_modules"/".bin"))
    with open(os.environ["GITHUB_PATH"],"a") as stream:
        stream.write("\n".join(paths)+"\n")


if __name__ == "__main__":
    main()
