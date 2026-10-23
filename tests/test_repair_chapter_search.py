import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import repair_chapter_search as repair
from scripts.epub_chapters import bundle_version
from scripts.reader_assets import canonical_json


class Store:
    def __init__(self, values):
        self.values = values

    def read_bytes(self, bucket, path):
        return self.values[path]

    def list_files(self, bucket, prefixes):
        return {path for path in self.values if path.startswith(prefixes)}


class ChapterSearchRepairTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        old = root / 'old';(old / 'chapters').mkdir(parents=True)
        body = b'<html><body><p>literal &amp;nbsp; and &amp;#0 tail</p></body></html>'
        (old / 'chapters/chapter-0001.xhtml').write_bytes(body)
        chapters = [dict(index=1, title='literal', path='chapters/chapter-0001.xhtml',
                         bytes=len(body), sha256=repair.digest(body))]
        search = gzip.compress(canonical_json(dict(version=1, kind='epub-search-index',
            chapters=[dict(index=1, title='literal', path=chapters[0]['path'], text='literal and � tail')])), mtime=0)
        (old / 'epub-search-index.json.gz').write_bytes(search)
        manifest = dict(version=1, kind='epub-chapters', chapters=chapters,
                        toc=[dict(title='literal', chapter=1, depth=0, fragment='')],
                        search_index=dict(path='epub-search-index.json.gz', bytes=len(search), sha256=repair.digest(search)))
        raw = canonical_json(manifest, pretty=True)
        (old / 'chapter-manifest.json').write_bytes(raw)
        (old / 'resources').mkdir();(old / 'resources/image.png').write_bytes(b'unchanged image')
        prefix = 'chapters/ebook/chm/' + 'a' * 64 + '/' + bundle_version(old)
        entry = dict(key='VoiceOfML/Test\0book.chm', source_sha256='a' * 64,
            source_revision='revision', source_extension='chm', extension='chm',
            mode='chapter-stream', bucket=repair.BUCKET, profile='chm-epub-chapters-v9-source-text',
            chapter_count=1, root=prefix, manifest=prefix+'/chapter-manifest.json',
            search_index=prefix+'/epub-search-index.json.gz', processing_roots=['old-processing-root'])
        selection = dict(key=entry['key'], source_sha256='a'*64, manifest=entry['manifest'], manifest_sha256=repair.digest(raw))
        values = {prefix+'/'+p.relative_to(old).as_posix():p.read_bytes() for p in old.rglob('*') if p.is_file()}
        values['chapters/ebook/chm/index.json'] = canonical_json(dict(files=[entry, dict(key='other', root='unchanged')], failures=[]))
        return root, entry, selection, Store(values)

    def test_repair_preserves_every_body_toc_resource_and_old_remote_bytes(self):
        root, entry, selection, store = self.fixture()
        before = dict(store.values)
        fixed, uploads, receipt = repair.rebuild(entry, selection, store, root/'repair')
        self.assertEqual(store.values, before)
        self.assertNotEqual(fixed['root'], entry['root'])
        self.assertEqual(fixed['source_sha256'], entry['source_sha256'])
        self.assertNotIn('processing_roots', fixed)
        self.assertEqual(receipt['changed_text_chapters'], [1])
        self.assertEqual(Path(uploads[fixed['root']+'/chapters/chapter-0001.xhtml']).read_bytes(),
                         before[entry['root']+'/chapters/chapter-0001.xhtml'])
        self.assertEqual(Path(uploads[fixed['root']+'/resources/image.png']).read_bytes(), b'unchanged image')
        manifest = json.loads(Path(uploads[fixed['manifest']]).read_bytes())
        self.assertEqual(manifest['toc'], json.loads(before[entry['manifest']])['toc'])
        search = json.loads(gzip.decompress(Path(uploads[fixed['search_index']]).read_bytes()))
        self.assertEqual(search['chapters'][0]['text'], 'literal &nbsp; and &#0 tail')
        self.assertEqual(manifest['search_index']['text_profile'], search['text_profile'])

    def test_corrupt_middle_resource_and_stale_selection_fail_before_publish(self):
        for corrupt in ('manifest', 'resource'):
            root, entry, selection, store = self.fixture()
            if corrupt == 'manifest':
                selection['manifest_sha256'] = 'b' * 64
            else:
                store.values[entry['root']+'/resources/image.png'] = b'corrupted image'
            with self.subTest(corrupt=corrupt), patch.object(repair, 'publish_checkpoint') as publish:
                with self.assertRaises(ValueError):
                    repair.run([selection], store, root/'repair')
                publish.assert_not_called()

    def test_exact_selection_validation_and_central_writer_guard(self):
        root, entry, selection, store = self.fixture()
        for invalid in ([], [selection, selection], [{**selection, 'manifest':'../outside'}],
                        [{**selection, 'source_sha256':'b'*64}]):
            with self.assertRaises(ValueError):
                repair.targets(invalid)
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'serialized central'):
                repair.run([selection], store, root/'repair', apply=True)

    def test_apply_uses_protected_checkpoint_and_preserves_unselected_entries(self):
        root, entry, selection, store = self.fixture()
        previous = json.loads(store.values['chapters/ebook/chm/index.json'])
        with patch.object(repair, 'assert_writer'), patch.object(repair, 'read_index', return_value=previous), \
                patch.object(repair, 'publish_checkpoint', return_value=previous) as publish:
            result = repair.run([selection], store, root/'repair', apply=True)
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(publish.call_args.args[2], previous)
        self.assertEqual(publish.call_args.kwargs['apply'], True)
        self.assertEqual(publish.call_args.args[3][0]['key'], entry['key'])
        self.assertEqual(publish.call_args.args[4], [])

    def test_resume_reuses_exact_success_without_republishing(self):
        root, entry, selection, store = self.fixture()
        fixed, uploads, receipt = repair.rebuild(entry, selection, store, root/'built')
        for remote, local in uploads.items():
            store.values[remote] = Path(local).read_bytes()
        store.values['chapters/ebook/chm/index.json'] = canonical_json(dict(files=[fixed], failures=[]))
        with patch.object(repair, 'assert_writer'), patch.object(repair, 'publish_checkpoint') as publish:
            result = repair.run([selection], store, root/'resumed', apply=True)
        self.assertTrue(result['files'][0]['already_repaired'])
        publish.assert_not_called()

    def test_workflow_keeps_real_writer_lock_and_default_plan(self):
        path = Path(__file__).parents[1]/'.github/workflows/reader-chapter-search-repair.yml'
        data = yaml.safe_load(path.read_text())
        self.assertEqual(data['concurrency']['group'], 'reader-sidecar')
        self.assertFalse(data['concurrency']['cancel-in-progress'])
        self.assertFalse(data[True]['workflow_dispatch']['inputs']['apply']['default'])
        text = path.read_text()
        self.assertIn('reader-v3-publish.yml', text)
        self.assertIn('build_reader_assets_v2_sidecar.py --apply', text)
        self.assertNotIn('ebook-convert', text)
        self.assertNotIn('sudo apt-get', text)
