import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from shared_ci.common import PipelineError
from shared_ci.wiki import MANIFEST, git, prepare, push_prepared, render, verify_context


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"; self.root.mkdir()
        self.pages = self.root / "docs/wiki"; self.pages.mkdir(parents=True)
        (self.pages / "Home.md").write_text('# Home\n\n[Guide](Guide.md#setup)\n[Source](../../README.md#product)\n')
        (self.pages / "Guide.md").write_text('# Guide\n\n## Setup\n\n[Home](Home.md)\n')
        (self.pages / "README.md").write_text('# Maintainer source\n')
        (self.root / "README.md").write_text('# Product\n')
        self.init(self.root)
        self.commit(self.root)
        self.revision = git(self.root, "rev-parse", "HEAD")

    def init(self, root):
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Wiki test")
        git(root, "config", "user.email", "wiki@example.invalid")

    def commit(self, root):
        git(root, "add", "--all")
        git(root, "commit", "--allow-empty", "-m", "Test fixture")

    def rendered(self, **kwargs):
        return render(self.root, "docs/wiki", "example/product", self.revision, **kwargs)

    def wiki(self):
        path = Path(self.temp.name) / "wiki"; path.mkdir()
        self.init(path)
        (path / "Home.md").write_text('Initial Home\n')
        (path / "Unrelated.md").write_text('Keep this manually maintained page.\n')
        self.commit(path)
        return path

    def test_links_are_rewritten_but_code_and_external_links_are_preserved(self):
        extra = '\n[ref][guide]\n\n[guide]: <Guide.md#setup>\n<a href="Guide.md#setup">Guide</a>\n[External](https://example.invalid/page)\n`[example](missing.md)`\n```md\n[example](missing.md)\n```\n'
        with (self.pages / "Home.md").open("a") as out:
            out.write(extra)
        result = self.rendered(tracked=False)
        home = result["Home.md"]
        self.assertIn('https://github.com/example/product/wiki/Guide#setup', home)
        self.assertIn('https://github.com/example/product/blob/' + self.revision + '/README.md#product', home)
        self.assertIn('[External](https://example.invalid/page)', home)
        self.assertIn('`[example](missing.md)`', home)
        self.assertIn('```md\n[example](missing.md)\n```', home)
        self.assertNotIn('README.md', result)

    def test_missing_and_escaping_links_or_anchors_fail(self):
        for link in ['missing.md', 'Guide.md#missing', '../../../../outside.md', 'javascript:alert']:
            with self.subTest(link=link):
                (self.pages / "Home.md").write_text('# Home\n[bad](' + link + ')\n')
                with self.assertRaises(PipelineError):
                    self.rendered(tracked=False)

    def test_symlink_pages_source_and_link_targets_fail(self):
        (self.pages / "Alias.md").symlink_to(self.pages / "Guide.md")
        with self.assertRaises(PipelineError):
            self.rendered(tracked=False)
        (self.pages / "Alias.md").unlink()
        (self.root / "alias").symlink_to(self.pages, target_is_directory=True)
        with self.assertRaises(PipelineError):
            render(self.root, 'alias', 'example/product', self.revision, tracked=False)
        (self.root / "linked.md").symlink_to(self.root / "README.md")
        (self.pages / "Home.md").write_text('# Home\n[linked](../../linked.md)\n')
        with self.assertRaises(PipelineError):
            self.rendered(tracked=False)

    def test_unsafe_or_case_colliding_source_files_fail(self):
        for name in ['secret.env', 'folder', 'home.md']:
            path = self.pages / name
            path.mkdir() if name == 'folder' else path.write_text('content')
            with self.subTest(name=name), self.assertRaises(PipelineError):
                self.rendered(tracked=False)
            path.rmdir() if path.is_dir() else path.unlink()

    def test_publish_source_must_be_clean_tracked_and_exact(self):
        self.rendered()
        (self.pages / "New.md").write_text('# New\n')
        with self.assertRaises(PipelineError):
            self.rendered()
        (self.pages / "New.md").unlink()
        (self.pages / "Guide.md").write_text('# Edited\n')
        with self.assertRaises(PipelineError):
            self.rendered()
        with self.assertRaises(PipelineError):
            render(self.root, 'docs/wiki', 'example/product', 'f' * 40)

    def test_relative_targets_must_exist_in_the_selected_commit(self):
        (self.pages / "Home.md").write_text('# Home\n[local](../../local.md)\n')
        self.commit(self.root); self.revision = git(self.root, 'rev-parse', 'HEAD')
        (self.root / 'local.md').write_text('# Local only\n')
        self.rendered(tracked=False)
        with self.assertRaises(PipelineError):
            self.rendered()

    def test_actual_action_cannot_import_product_shadow_package(self):
        library = Path(__file__).resolve().parent.parent
        shadow = self.root / 'shared_ci'; shadow.mkdir()
        marker = self.root / 'shadow-executed'
        (shadow / '__init__.py').write_text("from pathlib import Path\nPath('shadow-executed').write_text('unsafe import')\nraise RuntimeError('shadow imported')\n")
        action = yaml.safe_load((library / 'actions/v2-wiki/action.yml').read_text())
        event = Path(self.temp.name) / 'event.json'
        event.write_text(json.dumps({'repository': {'full_name':'example/product','default_branch':'main'}}))
        env = dict(os.environ, WIKI_ACTION_PATH=str(library / 'actions/v2-wiki'), WIKI_SOURCE='docs/wiki',
                   WIKI_ADOPT_HOME='false', WIKI_GITHUB_TOKEN='', GITHUB_WORKSPACE=str(self.root),
                   GITHUB_REPOSITORY='example/product', GITHUB_ACTIONS='true', GITHUB_EVENT_NAME='workflow_dispatch',
                   GITHUB_REF='refs/heads/main', GITHUB_SHA=self.revision, GITHUB_EVENT_PATH=str(event))
        result = subprocess.run(['bash','-e','-c',action['runs']['steps'][0]['run']],cwd=self.root,env=env,
                                text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('same-repository GitHub token', result.stderr)
        self.assertFalse(marker.exists())

    def test_actions_context_requires_manual_default_branch_and_exact_source(self):
        event = Path(self.temp.name) / 'event.json'
        event.write_text(json.dumps({'repository': {'full_name': 'example/product', 'default_branch': 'develop'}}))
        env = {'GITHUB_ACTIONS':'true', 'GITHUB_EVENT_NAME':'workflow_dispatch', 'GITHUB_EVENT_PATH':str(event),
               'GITHUB_REPOSITORY':'example/product', 'GITHUB_REF':'refs/heads/develop', 'GITHUB_SHA':self.revision}
        verify_context('example/product', self.revision, env)
        for key,value in [('GITHUB_EVENT_NAME','push'),('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REF','refs/heads/feature/wiki'),
                          ('GITHUB_SHA','f'*40),('GITHUB_REPOSITORY','another/repo'),('GITHUB_ACTIONS','false')]:
            with self.subTest(key=key,value=value), self.assertRaises(PipelineError):
                verify_context('example/product', self.revision, dict(env, **{key:value}))
        verify_context('example/product', self.revision, {})

    def test_initial_home_adoption_is_explicit_and_unrelated_pages_survive(self):
        wiki = self.wiki(); pages = self.rendered()
        with self.assertRaisesRegex(PipelineError, 'unmanaged'):
            prepare(wiki, pages, 'example/product', self.revision)
        prepare(wiki, pages, 'example/product', self.revision, adopt_home=True)
        self.assertEqual((wiki / 'Home.md').read_text(), pages['Home.md'])
        self.assertEqual((wiki / 'Unrelated.md').read_text(), 'Keep this manually maintained page.\n')
        self.commit(wiki)
        prepare(wiki, pages, 'example/product', self.revision)
        self.assertEqual(git(wiki, 'status', '--porcelain'), '')

    def test_conflicting_unmanaged_page_is_never_overwritten(self):
        wiki = self.wiki()
        (wiki / 'Guide.md').write_text('Unrelated Guide\n'); self.commit(wiki)
        with self.assertRaisesRegex(PipelineError, 'unmanaged'):
            prepare(wiki, self.rendered(), 'example/product', self.revision, adopt_home=True)
        self.assertEqual((wiki / 'Home.md').read_text(), 'Initial Home\n')
        self.assertFalse((wiki / MANIFEST).exists())

    def test_managed_manual_edits_or_missing_pages_refuse_publication(self):
        wiki = self.wiki(); pages = self.rendered()
        prepare(wiki, pages, 'example/product', self.revision, adopt_home=True); self.commit(wiki)
        (wiki / 'Guide.md').write_text('A human edit\n'); self.commit(wiki)
        with self.assertRaisesRegex(PipelineError, 'changed outside'):
            prepare(wiki, pages, 'example/product', self.revision)
        self.assertEqual((wiki / 'Guide.md').read_text(), 'A human edit\n')

    def test_removed_managed_pages_are_deleted_without_touching_unrelated_pages(self):
        wiki = self.wiki(); pages = self.rendered()
        prepare(wiki, pages, 'example/product', self.revision, adopt_home=True); self.commit(wiki)
        pages.pop('Guide.md')
        prepare(wiki, pages, 'example/product', self.revision)
        self.assertFalse((wiki / 'Guide.md').exists())
        self.assertTrue((wiki / 'Unrelated.md').exists())

    def test_invalid_manifest_and_destination_symlink_are_refused(self):
        wiki = self.wiki()
        (wiki / MANIFEST).write_text(json.dumps({'schema':1,'repository':'example/product','source_sha':self.revision,
                                             'pages':{'../outside.md':'a'*64}})); self.commit(wiki)
        with self.assertRaisesRegex(PipelineError, 'ownership manifest'):
            prepare(wiki, self.rendered(), 'example/product', self.revision, adopt_home=True)
        (wiki / MANIFEST).unlink(); (wiki / 'Guide.md').symlink_to(wiki / 'Home.md'); self.commit(wiki)
        with self.assertRaisesRegex(PipelineError, 'Unsafe'):
            prepare(wiki, self.rendered(), 'example/product', self.revision, adopt_home=True)

    def test_dangling_manifest_symlink_cannot_write_outside_checkout(self):
        wiki = self.wiki()
        outside = Path(self.temp.name) / 'outside-manifest.json'
        (wiki / MANIFEST).symlink_to(outside); self.commit(wiki)
        with self.assertRaisesRegex(PipelineError, 'Unsafe Wiki ownership manifest'):
            prepare(wiki, self.rendered(), 'example/product', self.revision, adopt_home=True)
        self.assertFalse(outside.exists())
        self.assertEqual((wiki / 'Home.md').read_text(), 'Initial Home\n')

    def test_normal_git_push_noop_and_concurrent_update_refusal(self):
        seed = self.wiki()
        remote = Path(self.temp.name) / 'remote.git'
        git(Path(self.temp.name), 'clone', '--bare', str(seed), str(remote))
        checkout = Path(self.temp.name) / 'checkout'
        git(Path(self.temp.name), 'clone', str(remote), str(checkout))
        before = prepare(checkout, self.rendered(), 'example/product', self.revision, adopt_home=True)
        result = push_prepared(checkout, before)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(git(remote, 'rev-parse', 'HEAD'), result['wiki_commit'])
        before = prepare(checkout, self.rendered(), 'example/product', self.revision)
        self.assertEqual(push_prepared(checkout, before)['status'], 'unchanged')
        other = Path(self.temp.name) / 'other'
        git(Path(self.temp.name), 'clone', str(remote), str(other))
        git(other, 'config', 'user.name', 'Another editor'); git(other, 'config', 'user.email', 'other@example.invalid')
        (other / 'Another.md').write_text('Concurrent page\n'); self.commit(other); git(other, 'push', 'origin', 'main')
        pages = self.rendered(); pages['Guide.md'] += '\nApproved update\n'
        prepare(checkout, pages, 'example/product', self.revision)
        with self.assertRaisesRegex(PipelineError, 'changed after preparation'):
            push_prepared(checkout, before)
        self.assertEqual(git(remote, 'rev-parse', 'HEAD'), git(other, 'rev-parse', 'HEAD'))

    def test_gitignore_cannot_silently_omit_new_pages(self):
        seed = self.wiki()
        (seed / '.gitignore').write_text('Guide.md\n'); self.commit(seed)
        remote = Path(self.temp.name) / 'ignored.git'
        git(Path(self.temp.name), 'clone', '--bare', str(seed), str(remote))
        checkout = Path(self.temp.name) / 'ignored-checkout'
        git(Path(self.temp.name), 'clone', str(remote), str(checkout))
        before = prepare(checkout, self.rendered(), 'example/product', self.revision, adopt_home=True)
        with self.assertRaises(PipelineError):
            push_prepared(checkout, before)
        self.assertEqual(git(remote, 'rev-parse', 'HEAD'), before)


if __name__ == '__main__':
    unittest.main()
