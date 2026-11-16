import unittest
import copy
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from shared_ci.common import PipelineError
from shared_ci.release import APIError, draft, draft_from_prs, render, publish
from shared_ci.assurance import stamp
from shared_ci.evidence import create


class ReleaseTests(unittest.TestCase):
    def test_locales_required_sections_and_limits_are_enforced(self):
        notes={"schema":1,"product":"example","version":"1.0","build":"1","sections":[{"title":"Changes","body":"A useful fix.","audiences":["public"]}],"required_sections":{"public":["Changes"]},"locales":{"fr":[{"title":"Changes","body":"Une correction utile.","audiences":["public"]}]}}
        self.assertIn("correction",render(notes,"public","fr"))
        notes["required_sections"]["public"].append("Missing")
        with self.assertRaises(PipelineError):render(notes,"public")
        notes["required_sections"]={};notes["max_characters"]=2
        with self.assertRaises(PipelineError):render(notes,"public")

    def test_draft_uses_actual_git_boundaries_and_merged_pull_requests(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            def git(*args):return subprocess.run(["git",*args],cwd=root,check=True,capture_output=True,text=True).stdout.strip()
            git("init","-q");git("config","user.name","Fixture");git("config","user.email","fixture@example.invalid")
            (root/"code").write_text("old");git("add","code");git("commit","-qm","base");git("tag","v1")
            base=git("rev-parse","HEAD");(root/"code").write_text("new");git("commit","-qam","change");head=git("rev-parse","HEAD")
            valid={"number":3,"title":"A real change","merged_at":"2026-01-01T00:00:00Z","merge_commit_sha":head,"labels":[{"name":"feature"}],"head":{"ref":"feature/a"},"base":{"ref":"develop","repo":{"full_name":"owner/example"}}}
            def api(*args):return [valid,{**valid,"number":2,"merge_commit_sha":base},{**valid,"number":4,"head":{"ref":"develop"},"base":{"ref":"staging","repo":{"full_name":"owner/example"}}}]
            config={"schema":1,"repository":"owner/example","previous_tag":"v1","candidate_sha":head,"token_env":"GH_TOKEN","categories":{"feature":"Added"}}
            with patch.dict(os.environ,{"GH_TOKEN":"synthetic"}):
                result=draft(config,root,api=api)
                self.assertTrue(result["boundaries_verified"])
                self.assertEqual(result["categories"],{"Added":["- A real change (#3)"]})
                config["candidate_sha"]=base
                with self.assertRaises(PipelineError):draft(config,root,api=api)
    def test_public_notes_cannot_include_internal_section(self):
        notes = {"schema": 1, "product": "example", "version": "1.0.0", "build": "1", "sections": [
            {"title": "What's new", "body": "A clearer sign-in screen.", "audiences": ["public", "internal"]},
            {"title": "Operations", "body": "Internal recovery instructions.", "audiences": ["internal"]}]}
        self.assertNotIn("recovery", render(notes, "public"))
        self.assertIn("recovery", render(notes, "internal"))
        notes["sections"][0]["body"] = "TODO"
        with self.assertRaises(PipelineError):
            render(notes, "public")

    def test_draft_deduplicates_promotions_and_requires_changes(self):
        rows = [{"number": 1, "title": "Fix", "category": "Fixes"},
                {"number": 1, "title": "Fix", "category": "Fixes"},
                {"number": 2, "title": "Promote", "category": "Other", "promotion": True}]
        result = draft_from_prs(rows, previous_tag="v1", candidate_sha="a" * 40)
        self.assertEqual(result["categories"], {"Fixes": ["- Fix (#1)"]})
        with self.assertRaises(PipelineError):
            draft_from_prs([], previous_tag="v1", candidate_sha="a" * 40)


class FakeGitHub:
    """The by-tag endpoint exposes published releases, while List also exposes drafts."""
    def __init__(self, commit):
        self.commit = commit
        self.release = None
        self.assets = {}
        self.calls = []
        self.fail_upload = False
        self.bad_publish = False
        self.tag_changed = False
        self.concurrent_asset = False
        self.next_id = 1

    def __call__(self, method, path, token, body=None, binary=None):
        self.calls.append((method, path, body))
        if '/git/ref/tags/' in path:
            return {'object': {'type': 'commit', 'sha': ('f' * 40 if self.tag_changed else self.commit)}}
        if path.endswith('/releases/tags/v1.0.0'):
            if not self.release or self.release['draft']: raise APIError(404)
            return copy.deepcopy(self.release)
        if method == 'GET' and '/releases?' in path:
            return [copy.deepcopy(self.release)] if self.release else []
        if method == 'POST' and path.endswith('/releases'):
            self.release = {**body, 'id': 41}
            return copy.deepcopy(self.release)
        if method == 'GET' and '/releases/41/assets?' in path:
            if self.concurrent_asset and self.assets:
                self.assets['unexpected.bin'] = {'id': 99, 'name': 'unexpected.bin', 'state': 'uploaded', 'size': 1, 'digest': 'sha256:' + 'f' * 64}
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['page'][0])
            return copy.deepcopy(list(self.assets.values())[(page - 1) * 100:page * 100])
        if method == 'POST' and '/releases/41/assets?' in path:
            name = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['name'][0]
            self.next_id += 1
            asset = {'id': self.next_id, 'name': name, 'size': len(binary), 'state': 'uploaded',
                     'digest': 'sha256:' + hashlib.sha256(binary).hexdigest()}
            if self.fail_upload:
                self.fail_upload = False
                self.assets[name] = {**asset, 'size': 0, 'state': 'starter', 'digest': None}
                raise APIError(502)
            self.assets[name] = asset
            return copy.deepcopy(asset)
        if method == 'DELETE' and '/releases/assets/' in path:
            asset_id = int(path.rsplit('/', 1)[1])
            self.assets = {name: asset for name, asset in self.assets.items() if asset['id'] != asset_id}
            return None
        if method == 'GET' and path.endswith('/releases/41'):
            return copy.deepcopy(self.release)
        if method == 'PATCH' and path.endswith('/releases/41'):
            if not self.bad_publish: self.release.update(body)
            return copy.deepcopy(self.release)
        raise AssertionError('Unexpected fake request: ' + method + ' ' + path)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()
        self.git = git
        git('init', '-q'); git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture')
        (self.root / 'source.txt').write_text('tested source')
        (self.root / 'check-config.json').write_text('{"version":1}')
        git('add', 'source.txt', 'check-config.json'); git('commit', '-qm', 'fixture')
        self.sha = git('rev-parse', 'HEAD')
        (self.root / 'app.bin').write_bytes(b'tested artifact')
        check = stamp({'status': 'passed'}, 'quality', self.root / 'check-config.json', self.root)
        (self.root / 'checks.json').write_text(json.dumps(check))
        self.candidate_config = {'schema': 1, 'product': 'example', 'version': '1.0.0', 'build': '1',
                                 'library_sha': 'a' * 40, 'inputs': ['source.txt', 'check-config.json'],
                                 'artifacts': ['app.bin'], 'toolchain': {'compiler': '1.2.3'},
                                 'checks': [{'path': 'checks.json', 'kind': 'quality', 'config': 'check-config.json'}]}
        self.write_candidate()
        self.notes = {'schema': 1, 'product': 'example', 'version': '1.0.0', 'build': '1', 'sections': [
            {'title': "What's new", 'body': 'A clearer sign-in screen.', 'audiences': ['public']}]}
        (self.root / 'notes.json').write_text(json.dumps(self.notes))
        self.config = {'schema': 1, 'repository': 'owner/example', 'tag': 'v1.0.0', 'candidate': 'candidate.json',
                       'notes': 'notes.json', 'audience': 'public', 'allowed_refs': ['refs/heads/main'],
                       'token_env': 'RELEASE_TOKEN', 'prerelease': False, 'publish': False, 'expected_commit': self.sha,
                       'expected_library_sha': 'a' * 40, 'expected_toolchain': {'compiler': '1.2.3'}}
        self.env = patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push',
                                          'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': 'owner/example',
                                          'GITHUB_SHA': self.sha, 'RELEASE_TOKEN': 'synthetic-token'}, clear=True)
        self.env.start()
        self.api = FakeGitHub(self.sha)

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def write_candidate(self):
        self.candidate = create(self.candidate_config, self.root)
        (self.root / 'candidate.json').write_text(json.dumps(self.candidate))

    def publish(self): return publish(self.config, self.root, api=self.api)

    def test_draft_upload_resume_publish_and_rerun_are_idempotent(self):
        result = self.publish()
        self.assertEqual(result['state'], 'draft')
        self.assertEqual(set(self.api.assets), {'app.bin', 'candidate.json'})
        writes = sum(method in ('POST', 'PATCH', 'DELETE') for method, _, _ in self.api.calls)
        self.assertEqual(self.publish()['state'], 'draft')
        self.assertEqual(sum(method in ('POST', 'PATCH', 'DELETE') for method, _, _ in self.api.calls), writes)
        self.config['publish'] = True
        self.assertEqual(self.publish()['state'], 'published')
        writes += 1
        self.assertEqual(self.publish()['state'], 'published')
        self.assertEqual(sum(method in ('POST', 'PATCH', 'DELETE') for method, _, _ in self.api.calls), writes)
        self.assertEqual(sum(method == 'POST' and path.endswith('/releases') for method, path, _ in self.api.calls), 1)

    def test_interrupted_upload_stays_draft_and_zero_byte_starter_is_recovered(self):
        self.config['publish'] = True
        self.api.fail_upload = True
        with self.assertRaises(APIError): self.publish()
        self.assertTrue(self.api.release['draft'])
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.api.calls))
        self.assertEqual(self.publish()['state'], 'published')
        self.assertEqual(sum(method == 'DELETE' for method, _, _ in self.api.calls), 1)
        self.assertEqual(sum(method == 'POST' and path.endswith('/releases') for method, path, _ in self.api.calls), 1)

    def test_published_assets_are_never_replaced_or_repaired(self):
        self.config['publish'] = True; self.publish()
        self.api.assets.pop('app.bin')
        before = len(self.api.calls)
        with self.assertRaisesRegex(PipelineError, 'Published release is missing'): self.publish()
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))

    def test_corrupt_asset_unknown_asset_or_different_notes_fail_without_mutation(self):
        self.publish()
        original_assets = copy.deepcopy(self.api.assets)
        for scenario in ('digest', 'unexpected', 'notes', 'nonempty-starter'):
            self.api.assets = copy.deepcopy(original_assets)
            self.api.release['body'] = render(self.notes, 'public')
            if scenario == 'digest': self.api.assets['app.bin']['digest'] = 'sha256:' + '0' * 64
            if scenario == 'unexpected': self.api.assets['other.bin'] = {**self.api.assets['app.bin'], 'id': 98, 'name': 'other.bin'}
            if scenario == 'notes': self.api.release['body'] = 'different'
            if scenario == 'nonempty-starter': self.api.assets['app.bin']['state'] = 'starter'
            before = len(self.api.calls)
            with self.subTest(scenario=scenario), self.assertRaises(PipelineError): self.publish()
            self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))

    def test_tag_current_checkout_expected_library_and_toolchain_are_required(self):
        for field, wrong in (('expected_commit', 'f' * 40), ('expected_library_sha', 'b' * 40),
                             ('expected_toolchain', {'compiler': '9.9.9'})):
            original = self.config[field]; self.config[field] = wrong
            with self.subTest(field=field), self.assertRaises(PipelineError): self.publish()
            self.config[field] = original
        self.assertFalse(self.api.calls)
        self.api.commit = 'f' * 40
        with self.assertRaisesRegex(PipelineError, 'tag'): self.publish()
        self.assertFalse(any(method != 'GET' for method, _, _ in self.api.calls))

    def test_candidate_artifact_check_and_source_mutation_block_before_api(self):
        for name in ('app.bin', 'checks.json', 'source.txt'):
            path = self.root / name; content = path.read_bytes(); path.write_bytes(b'changed')
            with self.subTest(name=name), self.assertRaises(PipelineError): self.publish()
            path.write_bytes(content)
        self.assertFalse(self.api.calls)

    def test_renamed_or_duplicate_asset_names_fail_before_remote_writes(self):
        for names in (['a/app.bin', 'b/app.bin'], ['unsafe name.bin']):
            for name in names:
                target = self.root / name; target.parent.mkdir(exist_ok=True); target.write_bytes(b'fixture')
            self.candidate_config['artifacts'] = names; self.write_candidate()
            with self.subTest(names=names), self.assertRaisesRegex(PipelineError, 'asset names'): self.publish()
        self.assertFalse(self.api.calls)

    def test_remote_inventory_or_publish_state_cannot_claim_success(self):
        self.config['publish'] = True; self.api.bad_publish = True
        with self.assertRaisesRegex(PipelineError, 'did not publish'): self.publish()
        self.assertTrue(self.api.release['draft'])
        self.api.bad_publish = False; self.api.concurrent_asset = True
        with self.assertRaisesRegex(PipelineError, 'unexpected assets'): self.publish()
        self.assertTrue(self.api.release['draft'])

    def test_tag_move_during_upload_blocks_publication(self):
        api = self.api
        def moving(method, path, token, body=None, binary=None):
            result = api(method, path, token, body, binary)
            if method == 'POST' and '/assets?' in path: api.tag_changed = True
            return result
        self.config['publish'] = True
        with self.assertRaisesRegex(PipelineError, 'tag'): publish(self.config, self.root, api=moving)
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.api.calls))

    def test_asset_mutation_between_verification_and_upload_is_detected(self):
        api = self.api
        def mutate(method, path, token, body=None, binary=None):
            result = api(method, path, token, body, binary)
            if method == 'POST' and path.endswith('/releases'): (self.root / 'app.bin').write_bytes(b'changed')
            return result
        with self.assertRaisesRegex(PipelineError, 'changed before upload'): publish(self.config, self.root, api=mutate)
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.api.calls))

    def test_draft_lookup_and_asset_inventory_paginate(self):
        self.publish()
        api = self.api
        def paginated(method, path, token, body=None, binary=None):
            if '/releases?' in path:
                if urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['page'] == ['1']: return [{'id': i, 'tag_name': 'v-other'} for i in range(100)]
                return [copy.deepcopy(api.release)]
            return api(method, path, token, body, binary)
        self.assertEqual(publish(self.config, self.root, api=paginated)['state'], 'draft')

    def test_more_than_one_page_of_assets_is_verified_on_rerun(self):
        names = []
        for index in range(101):
            name = f'part-{index}.bin'; (self.root / name).write_bytes(f'part {index}'.encode()); names.append(name)
        self.candidate_config['artifacts'] = names; self.write_candidate()
        self.publish()
        self.assertEqual(len(self.api.assets), 102)
        before = len(self.api.calls)
        self.assertEqual(self.publish()['state'], 'draft')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls[before:]))
        self.assertTrue(any('/assets?per_page=100&page=2' in path for _, path, _ in self.api.calls))

    def test_annotated_tag_chain_and_malformed_remote_states(self):
        api = self.api
        def annotated(method, path, token, body=None, binary=None):
            if '/git/ref/tags/' in path: return {'object': {'type': 'tag', 'sha': 'c' * 40}}
            if '/git/tags/' in path: return {'object': {'type': 'commit', 'sha': self.sha}}
            return api(method, path, token, body, binary)
        self.assertEqual(publish(self.config, self.root, api=annotated)['state'], 'draft')
        for change in ({'draft': 'false'}, {'tag_name': 'v-other'}, {'id': None}):
            original = copy.deepcopy(api.release); api.release['draft'] = False; api.release.update(change)
            with self.subTest(change=change), self.assertRaises(PipelineError): self.publish()
            api.release = original

    def test_ambiguous_drafts_missing_token_and_unsafe_event_fail(self):
        self.publish(); api = self.api
        def duplicate(method, path, token, body=None, binary=None):
            if '/releases?' in path: return [copy.deepcopy(api.release), copy.deepcopy(api.release)]
            return api(method, path, token, body, binary)
        with self.assertRaisesRegex(PipelineError, 'Multiple releases'): publish(self.config, self.root, api=duplicate)
        del os.environ['RELEASE_TOKEN']
        with self.assertRaisesRegex(PipelineError, 'credential is missing'): self.publish()
        os.environ['RELEASE_TOKEN'] = 'synthetic'; os.environ['GITHUB_EVENT_NAME'] = 'pull_request_target'
        with self.assertRaises(PipelineError): self.publish()


if __name__ == "__main__":
    unittest.main()
