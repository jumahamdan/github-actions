import copy
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from shared_ci.common import PipelineError
from shared_ci.wordpress import execute


class WordPressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        (self.root / 'component').mkdir()
        (self.root / 'component/index.html').write_text('<section><h2>Fixture</h2><img src="logo.svg"></section>')
        self.config = {'schema': 1, 'source': 'component', 'package_name': 'synthetic-theme', 'files': ['index.html'], 'output': 'out/theme.zip', 'receipt': 'out/receipt.json'}
    def test_deterministic_zip_and_manual_handoff(self):
        first = execute(self.config, self.root); second = execute(self.config, self.root)
        self.assertEqual(first['sha256'], second['sha256']); self.assertEqual(first['delivery_status'], 'packaged_not_published')
        with zipfile.ZipFile(self.root / 'out/theme.zip') as archive:
            self.assertEqual(archive.namelist(), ['synthetic-theme/index.html'])
            self.assertEqual(archive.infolist()[0].date_time, (1980, 1, 1, 0, 0, 0))
    def test_php_exact_version_and_syntax_check(self):
        (self.root / 'component/plugin.php').write_text('<?php function fixture() { return 1; }')
        self.config['files'].append('plugin.php'); self.config['php'] = {'command': ['php'], 'version': '8.3.12'}
        with patch('shared_ci.wordpress.run', return_value=subprocess.CompletedProcess([], 0, 'PHP 8.3.12', '')) as runner:
            execute(self.config, self.root)
        self.assertEqual(runner.call_args_list[1].args[0][:2], ['php', '-l'])
        with patch('shared_ci.wordpress.run', side_effect=PipelineError('syntax')), self.assertRaises(PipelineError): execute(self.config, self.root)
        with patch('shared_ci.wordpress.run', return_value=subprocess.CompletedProcess([], 0, 'PHP 8.2.0', '')), self.assertRaises(PipelineError): execute(self.config, self.root)
    def test_missing_php_tool_rejected(self):
        (self.root / 'component/plugin.php').write_text('<?php broken')
        self.config['files'] = ['plugin.php']
        with self.assertRaises(PipelineError): execute(self.config, self.root)
    def test_invalid_html_rejected(self):
        for content in ['', '<div><p>x</div>', '<div>unfinished']:
            (self.root / 'component/index.html').write_text(content)
            with self.subTest(content=content), self.assertRaises(PipelineError): execute(self.config, self.root)
    def test_secret_traversal_symlink_and_empty_file_lists_rejected(self):
        (self.root / 'component/alias.html').symlink_to('index.html')
        for files in [['.env'], ['wp-config.php'], ['../outside.html'], ['alias.html'], []]:
            self.config['files'] = files
            with self.subTest(files=files), self.assertRaises(PipelineError): execute(self.config, self.root)
    def test_output_and_receipt_cannot_overwrite_sources(self):
        self.config['output'] = 'component/theme.zip'
        with self.assertRaises(PipelineError): execute(self.config, self.root)
        self.config['output'] = 'out/theme.zip'; self.config['receipt'] = 'component/index.html'
        with self.assertRaises(PipelineError): execute(self.config, self.root)
        self.assertTrue((self.root / 'component/index.html').read_text().startswith('<section>'))


if __name__ == '__main__': unittest.main()
