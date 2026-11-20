"""Workflow source composition must be deterministic and fail on ambiguous edits."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("workflow_generation", ROOT / "scripts/generate-workflows.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class WorkflowGenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = self.root / "workflow-templates"
        (self.sources / "workflows").mkdir(parents=True)
        self.output = self.root / ".github/workflows"
        self.output.mkdir(parents=True)
        self.fragments = {
            "inputs": {"config": {"type": "string", "required": True}},
            "step": {"name": "Run checked command", "run": "printf '%s' \"$VALUE\"",
                     "env": {"VALUE": "${{ inputs.config }}"}},
        }
        self.write(self.sources / "common.yml", self.fragments)
        for name in ("first", "second"):
            self.write(self.sources / "workflows" / f"v2-{name}.yml", {
                "name": f"Checks · {name}", "on": {"workflow_call": {"inputs": {"$ref": "common.inputs"}}},
                "permissions": {"contents": "read"},
                "jobs": {"run": {"runs-on": "ubuntu-24.04", "steps": [{"$ref": "common.step"}]}},
            })

    def write(self, path, value):
        path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))

    def test_repository_generated_files_match_editable_sources(self):
        count, changed = generator.generate(ROOT, check=True)
        self.assertEqual(count, len(list((ROOT / "workflow-templates/workflows").glob("*.yml"))))
        self.assertGreater(count, 0)
        self.assertEqual(changed, 0)

    def test_generation_is_repeatable_and_only_writes_changed_outputs(self):
        self.assertEqual(generator.generate(self.root), (2, 2))
        original = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.glob("*.yml")}
        self.assertEqual(generator.generate(self.root), (2, 0))
        self.assertEqual(original, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                    for p in self.output.glob("*.yml")})
        self.assertEqual(generator.generate(self.root, check=True), (2, 0))

    def test_one_shared_edit_reaches_each_consumer_without_expression_evaluation(self):
        self.fragments["step"]["env"]["VALUE"] = "${{ inputs.config }}:keep literal $HOME and $(command)"
        self.fragments["step"]["continue-on-error"] = False
        self.write(self.sources / "common.yml", self.fragments)
        outputs = generator.render_workflows(self.root)
        for content in outputs.values():
            data = yaml.safe_load(content)
            step = data["jobs"]["run"]["steps"][0]
            self.assertEqual(step, self.fragments["step"])
            self.assertIs(step["continue-on-error"], False)
            self.assertNotIn("$ref:", content)
            self.assertTrue(content.startswith(generator.HEADER))

    def test_expansions_are_independent_and_do_not_mutate_source_fragments(self):
        fragments = {"common.shared": {"nested": ["original"]}}
        actual = generator.expand([{"$ref": "common.shared"}, {"$ref": "common.shared"}], fragments)
        actual[0]["nested"].append("changed")
        self.assertEqual(actual[1], {"nested": ["original"]})
        self.assertEqual(fragments["common.shared"], {"nested": ["original"]})

    def test_check_refuses_modified_output_without_rewriting_it(self):
        generator.generate(self.root)
        path = self.output / "v2-first.yml"
        path.write_text(path.read_text().replace("contents: read", "contents: write"))
        modified = path.read_bytes()
        with self.assertRaisesRegex(generator.GenerationError, "Stale generated workflows"):
            generator.generate(self.root, check=True)
        self.assertEqual(path.read_bytes(), modified)

    def test_check_refuses_missing_output_and_generate_refuses_unsourced_files(self):
        with self.assertRaisesRegex(generator.GenerationError, "Stale generated workflows"):
            generator.generate(self.root, check=True)
        self.assertFalse(list(self.output.glob("*.yml")))
        extra = self.output / "v2-unowned.yml"
        extra.write_text("Do not delete me")
        with self.assertRaisesRegex(generator.GenerationError, "lack sources"):
            generator.generate(self.root)
        self.assertEqual(extra.read_text(), "Do not delete me")

    def test_unknown_and_recursive_references_are_rejected(self):
        for node, fragments in [
            ({"$ref": "missing.name"}, {}),
            ({"$ref": "../../outside"}, {}),
            ({"$ref": "common.a"}, {"common.a": {"$ref": "common.a"}}),
            ({"$ref": "common.a"}, {"common.a": {"$ref": "common.b"},
                                      "common.b": {"nested": {"$ref": "common.a"}}}),
        ]:
            with self.subTest(node=node, fragments=fragments), self.assertRaises(generator.GenerationError):
                generator.expand(node, fragments)

    def test_mapping_composition_never_silently_overrides_keys(self):
        fragments = {"common.permissions": {"contents": "read"}}
        with self.assertRaisesRegex(generator.GenerationError, "Duplicate merged keys: contents"):
            generator.expand({"$merge": [{"$ref": "common.permissions"}, {"contents": "write"}]}, fragments)
        actual = generator.expand({"$merge": [{"$ref": "common.permissions"}, {"actions": "read"}]}, fragments)
        self.assertEqual(actual, {"contents": "read", "actions": "read"})

    def test_directive_typos_siblings_and_wrong_merge_types_fail(self):
        for node in [{"$include": "common.step"}, {"$ref": "common.step", "if": "false"},
                     {"$merge": {}}, {"$merge": []}, {"$merge": [False]},
                     {"$ref": ["common.step"]}, {False: "bad key"}]:
            with self.subTest(node=node), self.assertRaises(generator.GenerationError):
                generator.expand(node, {"common.step": self.fragments["step"]})

    def test_duplicate_yaml_keys_aliases_merges_and_nonstring_keys_fail(self):
        path = self.sources / "invalid.yml"
        for text in ("value: 1\nvalue: 2\n", "value: &item [one]\ncopy: *item\n",
                     "value: {<<: {one: two}}\n", "1: value\n"):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(generator.GenerationError):
                generator.load_yaml(path)

    def test_unquoted_github_event_key_and_real_booleans_keep_correct_types(self):
        path = self.sources / "boolean.yml"
        path.write_text("on: {workflow_call: {}}\nenabled: true\ndisabled: false\nword: off\n")
        value = generator.load_yaml(path)
        self.assertIn("on", value)
        self.assertIs(value["enabled"], True)
        self.assertIs(value["disabled"], False)
        self.assertEqual(value["word"], "off")

    def test_invalid_unused_fragment_fails_before_any_output_is_written(self):
        self.fragments["unused"] = {"$ref": "unknown.value"}
        self.write(self.sources / "common.yml", self.fragments)
        with self.assertRaisesRegex(generator.GenerationError, "Unknown fragment"):
            generator.generate(self.root)
        self.assertFalse(list(self.output.glob("*.yml")))

    def test_auto_trigger_and_duplicate_display_name_are_rejected(self):
        path = self.sources / "workflows/v2-first.yml"
        source = generator.load_yaml(path)
        source["on"]["push"] = None
        self.write(path, source)
        with self.assertRaisesRegex(generator.GenerationError, "callable workflow"):
            generator.render_workflows(self.root)
        del source["on"]["push"]
        source["name"] = "Checks · second"
        self.write(path, source)
        with self.assertRaisesRegex(generator.GenerationError, "Duplicate workflow display name"):
            generator.render_workflows(self.root)

    def test_source_names_and_fragment_names_cannot_hide_unsupported_files(self):
        (self.sources / "workflows/v2-first.yml").rename(self.sources / "workflows/legacy.yml")
        with self.assertRaisesRegex(generator.GenerationError, "Invalid generated workflow filename"):
            generator.render_workflows(self.root)
        self.write(self.sources / "bad.yml", {"bad.name": {}})
        with self.assertRaisesRegex(generator.GenerationError, "Invalid fragment name"):
            generator.load_fragments(self.sources)


if __name__ == "__main__":
    unittest.main()
