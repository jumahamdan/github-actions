import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from shared_ci.common import PipelineError
from shared_ci.docs import anchors, execute


class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        (self.root / "README.md").write_text('# Example\n\n[Install](docs/setup.md#install-it)\n[External](https://example.invalid)\n')
        (self.root / "docs/setup.md").write_text('# Install it\n\n[Home](../README.md#example)\n\n## Same\n## Same\n\n[Second](#same-1)\n')
        self.config = {"version": 1, "documents": ["README.md", "docs/**/*.md"], "required_files": ["docs/setup.md"], "receipt": ".ci-results/docs.json"}

    def test_local_links_and_duplicate_heading_anchors(self):
        result = execute(self.config, self.root)
        self.assertEqual(result["local_links_checked"], 3)
        self.assertEqual(result["external_links_not_fetched"], 1)
        self.assertEqual(result["documents"], 2)

    def test_reference_html_and_escaped_filename_links(self):
        (self.root / "docs/My Guide.md").write_text('# Guide\n')
        (self.root / "README.md").write_text('# Example\n[reference][guide]\n\n[guide]: <docs/My Guide.md#guide>\n<a href="docs/setup.md#install-it">setup</a>\n[spaces](docs/My%20Guide.md#guide)\n')
        self.assertEqual(execute(self.config, self.root)["local_links_checked"], 5)

    def test_code_fences_and_inline_examples_are_not_links(self):
        (self.root / "README.md").write_text('# Example\n```md\n[not real](missing.md)\n```\n`[example](missing.md)`\n')
        execute(self.config, self.root)

    def test_missing_link_anchor_and_escape_fail(self):
        for target in ['missing.md', 'docs/setup.md#missing', '../outside', 'javascript:alert']:
            (self.root / "README.md").write_text(f'# Example\n[bad]({target})\n')
            with self.subTest(target=target), self.assertRaises(PipelineError):
                execute(self.config, self.root)

    def test_broken_symlink_escape_fails(self):
        (self.root / "docs/external.md").symlink_to(self.root.parent / "unrelated-file")
        (self.root / "README.md").write_text('[bad](docs/external.md)')
        with self.assertRaises(PipelineError):
            execute(self.config, self.root)

    def test_declared_generator_drift_and_failure(self):
        self.config["generated"] = [{"command": [sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('stale output')"], "outputs": ["README.md"]}]
        with self.assertRaisesRegex(PipelineError, "stale"):
            execute(self.config, self.root)
        self.config["generated"][0]["command"] = [sys.executable, "-c", "raise SystemExit(9)"]
        with self.assertRaisesRegex(PipelineError, "failed"):
            execute(self.config, self.root)

    def test_current_generator_passes(self):
        self.config["generated"] = [{"command": [sys.executable, "-c", "from pathlib import Path; p=Path('README.md'); p.write_text(p.read_text())"], "outputs": ["README.md"]}]
        self.assertEqual(execute(self.config, self.root)["generated"][0]["status"], "current")

    def test_no_match_unknown_fields_missing_required_and_output_collision(self):
        for field, value in [("documents", ["absent/*.md"]), ("required_files", ["no.md"]), ("typo", True), ("receipt", "README.md")]:
            config = copy.deepcopy(self.config); config[field] = value
            with self.subTest(field=field), self.assertRaises(PipelineError):
                execute(config, self.root)

    def test_unicode_anchor_and_heading_link(self):
        path = self.root / "heading.md"
        path.write_text('# Café & Tea!\n## Use [this](setup.md)\n')
        self.assertEqual(anchors(path), {'café--tea', 'use-this'})


if __name__ == '__main__':
    unittest.main()
