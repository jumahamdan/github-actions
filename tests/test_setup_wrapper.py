import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError

LIBRARY = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('setup_wrapper', LIBRARY / 'scripts/setup-tools.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class SetupWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'GITHUB_WORKSPACE': str(self.root), 'RUNNER_TEMP': str(self.root),
                    'GITHUB_PATH': str(self.root / 'path-output'), 'GITHUB_ENV': str(self.root / 'env-output'),
                    'TOOL_BUNDLES': '', 'BINARY_MANIFEST': '', 'PYTHON_TOOLS': '', 'NPM_TOOLS': '',
                    'INSTALL_AI_TOOLS': ''}

    def test_locked_installers_disable_build_and_lifecycle_code(self):
        (self.root / 'requirements.txt').write_text('fixture==1 --hash=sha256:' + 'a' * 64)
        (self.root / 'tools').mkdir()
        (self.root / 'tools/package-lock.json').write_text('{}')
        env = {**self.env, 'PYTHON_TOOLS': 'requirements.txt', 'NPM_TOOLS': 'tools'}
        with patch.dict(os.environ, env), patch.object(wrapper.subprocess, 'run') as execute:
            wrapper.main()
        python_args = execute.call_args_list[0].args[0]
        npm_args = execute.call_args_list[1].args[0]
        self.assertIn('--only-binary=:all:', python_args)
        self.assertIn('--require-hashes', python_args)
        self.assertIn('--ignore-scripts', npm_args)
        self.assertIn(str(self.root / 'tools/node_modules/.bin'), (self.root / 'path-output').read_text())

    def test_escape_symlink_and_missing_lock_fail_before_installer(self):
        (self.root / 'escape').symlink_to('/tmp')
        (self.root / 'tools').mkdir()
        for setting, path in [('BINARY_MANIFEST', '../outside'), ('PYTHON_TOOLS', 'escape/file'),
                              ('NPM_TOOLS', 'tools')]:
            with self.subTest(setting=setting), patch.dict(os.environ, {**self.env, setting: path}), \
                    patch.object(wrapper.subprocess, 'run') as execute, patch.object(wrapper, 'install') as install:
                with self.assertRaises(PipelineError):
                    wrapper.main()
                execute.assert_not_called()
                install.assert_not_called()

    def test_bundletool_uses_library_manifest_and_publishes_absolute_verified_path(self):
        with patch.dict(os.environ, {**self.env, 'TOOL_BUNDLES': 'bundletool'}), \
                patch.object(wrapper, 'install') as install:
            wrapper.main()
        expected = json.loads((LIBRARY / 'examples/tools-bundletool.json').read_text())
        self.assertEqual(install.call_args.args[0], expected)
        self.assertEqual((self.root / 'env-output').read_text(),
                         'SHARED_BUNDLETOOL_JAR=' + str(self.root / 'shared-ci-tools/bundletool.jar') + '\n')

    def test_unknown_or_duplicate_bundle_refuses_download(self):
        for value in ('unknown', 'sonar,sonar'):
            with self.subTest(value=value), patch.dict(os.environ, {**self.env, 'TOOL_BUNDLES': value}), \
                    patch.object(wrapper, 'install') as install:
                with self.assertRaises(PipelineError):
                    wrapper.main()
                install.assert_not_called()
