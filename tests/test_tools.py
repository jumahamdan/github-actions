import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from shared_ci.common import PipelineError
from shared_ci.tools import install


class ToolTests(unittest.TestCase):
    def test_checksum_and_member_validation(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            data = b"#!/bin/sh\nexit 0\n"
            member = tarfile.TarInfo("tool")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        blob = buf.getvalue()
        item = {"name": "example", "version": "1.0", "url": "https://example.invalid/v1/tool.tar.gz",
                "sha256": hashlib.sha256(blob).hexdigest(), "member": "tool"}
        with tempfile.TemporaryDirectory() as d:
            install({"schema": 1, "tools": [item]}, d, fetch=lambda _: blob)
            self.assertTrue((Path(d) / "example").stat().st_mode & 0o100)
            item["sha256"] = "0" * 64
            with self.assertRaises(PipelineError):
                install({"schema": 1, "tools": [item]}, d, fetch=lambda _: blob)


class RuntimeToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
    def tar(self, entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for name, content, kind in entries:
                member = tarfile.TarInfo(name); member.mode = 0o755 if name.endswith('/tool') else 0o644
                if kind in ('symlink', 'hardlink'):
                    member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE; member.linkname = content; archive.addfile(member)
                elif kind == 'directory': member.type = tarfile.DIRTYPE; archive.addfile(member)
                elif kind == 'device': member.type = tarfile.CHRTYPE; archive.addfile(member)
                else:
                    data = content.encode(); member.size = len(data); archive.addfile(member, io.BytesIO(data))
        return buffer.getvalue()
    def item(self, blob, **extra):
        return {'name':'runtime','version':'1.2.3','url':'https://example.invalid/runtime-1.2.3.tar.gz','sha256':hashlib.sha256(blob).hexdigest(),'format':'tar','strip_prefix':'runtime','entrypoints':{'fixture':'bin/tool'},**extra}
    def install(self, blob, **extra):
        return install({'schema':1,'tools':[self.item(blob,**extra)]},self.root,fetch=lambda _:blob)
    def runtime(self, extras=()):
        return self.tar([('runtime/bin/tool','#!/bin/sh\ncat "$(dirname "$0")/../lib/value"\n','file'),('runtime/lib/value','version-1.2.3\n','file'),*extras])
    def test_multifile_launcher_preserves_relative_layout_and_is_idempotent(self):
        import subprocess
        blob=self.runtime();result=self.install(blob)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(subprocess.check_output([str(self.root/'fixture')],text=True),'version-1.2.3\n')
        self.install(blob)
        self.assertFalse(any(path.is_symlink() for path in self.root.rglob('*')))
    def test_tampered_runtime_and_destination_refused(self):
        blob=self.runtime();self.install(blob)
        target=next(self.root.glob('.shared-tools/runtime-*/lib/value'));target.write_text('tampered')
        with self.assertRaisesRegex(PipelineError,'changed'):self.install(blob)
    def test_traversal_absolute_duplicate_special_and_privileged_entries_rejected(self):
        cases=[('runtime/../escape','x','file'),('/absolute','x','file'),('runtime/lib/value','duplicate','file'),('runtime/device','','device'),('runtime/back\\slash','x','file')]
        for bad in cases:
            blob=self.runtime([bad])
            with self.subTest(bad=bad),self.assertRaises(PipelineError):self.install(blob)
            self.assertFalse((self.root/'fixture').exists())
    def test_links_rejected_by_default_and_materialized_only_with_optin(self):
        blob=self.runtime([('runtime/lib/alias','value','symlink')])
        with self.assertRaisesRegex(PipelineError,'opt-in'):self.install(blob)
        self.install(blob,materialize_internal_links=True)
        target=next(self.root.glob('.shared-tools/runtime-*/lib/alias'))
        self.assertTrue(target.is_file());self.assertFalse(target.is_symlink());self.assertEqual(target.read_text(),'version-1.2.3\n')
    def test_links_cannot_escape_cycle_or_have_missing_or_directory_targets(self):
        cases=[ [('runtime/lib/a','../../escape','symlink')], [('runtime/lib/a','missing','symlink')], [('runtime/lib/a','b','symlink'),('runtime/lib/b','a','symlink')], [('runtime/lib/a','../bin','symlink')] ]
        for extras in cases:
            with self.subTest(extras=extras),self.assertRaises(PipelineError):self.install(self.runtime(extras),materialize_internal_links=True)
    def test_hardlink_materialization_is_archive_root_relative(self):
        self.install(self.runtime([('runtime/lib/copy','runtime/lib/value','hardlink')]),materialize_internal_links=True)
        self.assertEqual(next(self.root.glob('.shared-tools/runtime-*/lib/copy')).read_text(),'version-1.2.3\n')
    def test_reviewed_test_fixture_exclusion_does_not_allow_traversal(self):
        self.install(self.runtime([('runtime/tests/broken','absent','symlink')]),exclude_paths=['tests'])
        self.assertFalse(any(p.name=='tests' for p in self.root.rglob('*')))
        with self.assertRaises(PipelineError):self.install(self.runtime([('runtime/tests/../../escape','x','file')]),exclude_paths=['tests'])
    def test_overlay_must_not_replace_existing_runtime_file(self):
        blob=self.runtime();overlay=self.tar([('lib/value','replacement','file')])
        item=self.item(blob,overlays=[{'url':'https://example.invalid/component-v1.tar.gz','sha256':hashlib.sha256(overlay).hexdigest(),'format':'tar'}])
        with self.assertRaisesRegex(PipelineError,'duplicate'):install({'schema':1,'tools':[item]},self.root,fetch=lambda url:overlay if 'component' in url else blob)
    def test_zip_runtime_and_symlink_materialization(self):
        import stat, zipfile
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            for name,text,mode in [('runtime/bin/tool','#!/bin/sh\nexit 0\n',stat.S_IFREG|0o755),('runtime/lib/value','fixture',stat.S_IFREG|0o644),('runtime/lib/alias','value',stat.S_IFLNK|0o777)]:
                info=zipfile.ZipInfo(name);info.create_system=3;info.external_attr=mode<<16;z.writestr(info,text)
        blob=buf.getvalue();self.install(blob,format='zip',materialize_internal_links=True)
        self.assertEqual(next(self.root.glob('.shared-tools/runtime-*/lib/alias')).read_text(),'fixture')
    def test_raw_file_can_be_non_executable(self):
        blob=b'synthetic jar bytes'
        item={'name':'bundletool.jar','version':'1.18.3','url':'https://example.invalid/bundletool-1.18.3.jar','sha256':hashlib.sha256(blob).hexdigest(),'format':'raw','executable':False}
        install({'schema':1,'tools':[item]},self.root,fetch=lambda _:blob)
        self.assertEqual((self.root/'bundletool.jar').read_bytes(),blob);self.assertFalse((self.root/'bundletool.jar').stat().st_mode&0o111)
    def test_download_expansion_and_entry_bounds(self):
        blob=self.runtime()
        for bounds in [{'max_download_bytes':2},{'max_expanded_bytes':2},{'max_files':1}]:
            with self.subTest(bounds=bounds),self.assertRaises(PipelineError):install({'schema':1,'bounds':bounds,'tools':[self.item(blob)]},self.root,fetch=lambda _:blob)
    def test_bad_url_checksum_secret_environment_and_linked_destination(self):
        blob=self.runtime()
        for extra in [{'url':'http://example.invalid/v1.tar.gz'},{'url':'https://example.invalid/latest/tool.tar.gz'},{'sha256':'0'*64},{'environment':{'API_TOKEN':'secret'}},{'entrypoints':{'../escape':'bin/tool'}}]:
            with self.subTest(extra=extra),self.assertRaises(PipelineError):self.install(blob,**extra)
        real=self.root/'real';real.mkdir();alias=self.root/'alias';alias.symlink_to(real,target_is_directory=True)
        with self.assertRaises(PipelineError):install({'schema':1,'tools':[self.item(blob)]},alias,fetch=lambda _:blob)
    def test_executable_permission_changes_are_detected(self):
        blob=self.runtime();self.install(blob)
        next(self.root.glob('.shared-tools/runtime-*/lib/value')).chmod(0o777)
        with self.assertRaisesRegex(PipelineError,'permissions'):self.install(blob)
    def test_metadata_is_checksummed_confined_and_cannot_overwrite_runtime(self):
        content='fixture component metadata\n'
        declaration={'path':'.install/component.json','content':content,'sha256':hashlib.sha256(content.encode()).hexdigest()}
        blob=self.runtime();self.install(blob,metadata_files=[declaration])
        self.assertEqual(next(self.root.glob('.shared-tools/runtime-*/.install/component.json')).read_text(),content)
        for bad in [{**declaration,'sha256':'0'*64},{**declaration,'path':'lib/value'},{**declaration,'path':'../outside'}]:
            with self.subTest(bad=bad),self.assertRaises(PipelineError):self.install(blob,metadata_files=[bad])


if __name__ == "__main__":
    unittest.main()
