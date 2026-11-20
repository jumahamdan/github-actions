"""Execute legacy quality scripts and guard their independent workflow controls."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/code-quality-typescript.yml").read_text())
SPEC = importlib.util.spec_from_file_location("workflow_validation", ROOT / "scripts/validate-workflows.py")
VALIDATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATION)


class LegacyTypeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        for command in ("npm", "npx"):
            tool = self.bin / command
            tool.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "name = pathlib.Path(sys.argv[0]).name\n"
                "with open(os.environ['TEST_COMMAND_LOG'], 'a') as out:\n"
                "    out.write(json.dumps([name, *sys.argv[1:]]) + '\\n')\n"
                "raise SystemExit(int(os.environ.get('TEST_' + name.upper() + '_EXIT', '0')))\n"
            )
            tool.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TEST_COMMAND_LOG": str(self.log),
        }
        self.steps = WORKFLOW["jobs"]["quality"]["steps"]

    def tearDown(self):
        self.temp.cleanup()

    def run_step(self, name, **env):
        step = next(step for step in self.steps if step.get("name") == name)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=self.root, env={**self.env, **env}, text=True, capture_output=True,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_selected_checks(self, **inputs):
        """Select only real top-level steps according to their declared boolean flags."""
        results = {}
        for step in self.steps:
            condition = re.fullmatch(r"\$\{\{ inputs\.(run_\w+) \}\}", step.get("if", ""))
            if condition and inputs[condition[1]]:
                results[step["name"]] = self.run_step(step["name"])
        return results

    def test_disabling_lint_preserves_typecheck_and_test_execution(self):
        VALIDATION.validate_legacy_typescript(WORKFLOW)
        results = self.run_selected_checks(run_lint=False, run_typecheck=True, run_tests=True)
        self.assertEqual(set(results), {"TypeScript type check", "Run tests"})
        self.assertTrue(all(result.returncode == 0 for result in results.values()))
        self.assertEqual(self.commands(), [["npx", "tsc", "--noEmit"], ["npm", "test", "--", "--passWithNoTests"]])

    def test_tests_run_when_both_other_checks_are_disabled(self):
        results = self.run_selected_checks(run_lint=False, run_typecheck=False, run_tests=True)
        self.assertEqual(set(results), {"Run tests"})
        self.assertEqual(self.commands(), [["npm", "test", "--", "--passWithNoTests"]])

    def test_type_errors_fail_for_default_and_app_tsconfig(self):
        for app_config in (False, True):
            with self.subTest(app_config=app_config):
                if app_config:
                    (self.root / "tsconfig.app.json").write_text("{}")
                result = self.run_step("TypeScript type check", TEST_NPX_EXIT="2")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("BLOCKING PR", result.stdout)
                expected = ["npx", "tsc", "--noEmit"]
                if app_config:
                    expected += ["--project", "tsconfig.app.json"]
                self.assertEqual(self.commands()[-1], expected)
        step = next(step for step in self.steps if step.get("name") == "TypeScript type check")
        self.assertFalse(step["continue-on-error"])

    def test_lint_and_test_errors_keep_legacy_advisory_policy(self):
        for name in ("Run ESLint", "Run tests"):
            with self.subTest(step=name):
                result = self.run_step(name, TEST_NPM_EXIT="2")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                step = next(step for step in self.steps if step.get("name") == name)
                self.assertTrue(step["continue-on-error"])
        self.assertEqual(self.commands(), [["npm", "run", "lint"], ["npm", "test", "--", "--passWithNoTests"]])

    def test_validation_refuses_checks_swallowed_inside_lint_script(self):
        broken = copy.deepcopy(WORKFLOW)
        steps = broken["jobs"]["quality"]["steps"]
        lint = next(step for step in steps if step.get("name") == "Run ESLint")
        for name in ("TypeScript type check", "Run tests"):
            swallowed = next(step for step in steps if step.get("name") == name)
            steps.remove(swallowed)
            lint["run"] += "\n" + yaml.safe_dump([swallowed])
        with self.assertRaisesRegex(ValueError, "independent TypeScript type check step"):
            VALIDATION.validate_legacy_typescript(broken)

    def test_summary_treats_input_as_data(self):
        summary = self.root / "job summary.md"
        literal = '$(touch should-not-exist); `touch also-not-exist`'
        result = self.run_step(
            "Summary", GITHUB_STEP_SUMMARY=str(summary), QUALITY_DIRECTORY=literal,
            QUALITY_NODE_VERSION="20", QUALITY_RUN_LINT="false", QUALITY_RUN_TYPECHECK="true",
            QUALITY_RUN_TESTS="true",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(literal, summary.read_text())
        self.assertFalse((self.root / "should-not-exist").exists())
        self.assertFalse((self.root / "also-not-exist").exists())


if __name__ == "__main__":
    unittest.main()
