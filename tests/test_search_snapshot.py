"""Local snapshot contracts; no remote stores or site changes."""
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import search_snapshot as snapshot


def packed(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)


def fixture(root, surface):
    root.mkdir(parents=True)
    corpus = {'v': 2, 'rp': ['VoiceOfML/Test'], 'fd': [[]], 'rc': [[0, 'Book', 'pdf', 0, 123, 0]]}
    (root / 'search_data.json.gz').write_bytes(packed(corpus))
    (root / 'folder_tree.json.gz').write_bytes(packed({}))
    (root / 'folder_browser.json.gz').write_bytes(packed({}))
    field_digest = hashlib.sha256(b'book\0voiceofml/test\0\0').digest()
    if surface == 'space':
        label = b'jieba-0.42.1-cjk-char-v1'
        (root / 'search_words.bin.gz').write_bytes(gzip.compress(b'VWI2' + bytes([len(label)]) + label + field_digest + b'\1', mtime=0))
        for width in (2, 3):
            (root / f'search_ngrams_{width}.bin.gz').write_bytes(gzip.compress(b'VNG3' + bytes([width]) + field_digest + b'\1', mtime=0))
    prefix = '/search/data/' if surface == 'pages' else '/data/'
    for family in ('initial', 'sidebar'):
        (root / family / 'repos').mkdir(parents=True)
        (root / family / 'global.json').write_text('{}')
        (root / family / 'repos' / 'Test.json').write_text('{}')
        (root / family / 'manifest.json').write_text(json.dumps({'version': 2, 'urls': [
            prefix + family + '/global.json', prefix + family + '/repos/Test.json']}))


class SearchSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pages, self.space = self.root / 'pages', self.root / 'space'
        fixture(self.pages, 'pages'); fixture(self.space, 'space')
        self.source = self.root / 'search_data.json'
        self.source.write_bytes(gzip.decompress((self.pages / 'search_data.json.gz').read_bytes()))
        self.revisions = {'VoiceOfML/Test': 'a' * 40}

    def tearDown(self):
        self.temp.cleanup()

    def test_reproducible_identity_exact_verification_and_no_publication(self):
        manifest = snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)
        self.assertEqual(snapshot.verify_manifest(manifest, self.pages, self.space, self.source), manifest)
        self.assertEqual(snapshot.build_manifest(self.pages, self.space, self.revisions, self.source), manifest)
        self.assertFalse(manifest['publication_ready'])
        self.assertEqual(manifest['remote_writes'], 0)
        self.assertEqual(manifest['record_count'], 1)
        self.assertIn('not verified', manifest['index_assurance'])
        self.assertIn('provenance', manifest['capture_assurance'])
        changed = {**self.revisions, 'VoiceOfML/Test': 'b' * 40}
        self.assertNotEqual(snapshot.build_manifest(self.pages, self.space, changed, self.source)['generation'], manifest['generation'])

    def test_corruption_surface_drift_unlisted_files_and_symlinks_rejected(self):
        manifest = snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)
        member = self.pages / 'initial/global.json'
        member.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            snapshot.verify_manifest(manifest, self.pages, self.space, self.source)
        member.write_text('{}')
        (self.space / 'folder_tree.json.gz').write_bytes(packed({'changed': True}))
        with self.assertRaisesRegex(ValueError, 'do not match'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)
        (self.space / 'folder_tree.json.gz').write_bytes((self.pages / 'folder_tree.json.gz').read_bytes())
        (self.pages / 'extra.txt').write_text('unmanaged')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)
        (self.pages / 'extra.txt').unlink()
        (self.pages / 'initial' / 'link').symlink_to(member)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)

    def test_missing_or_mismatched_revisions_and_indexes_fail_closed(self):
        for revisions in ({}, {'VoiceOfML/Test': 'b'}, {'VoiceOfML/Other': 'a' * 40}):
            with self.subTest(revisions=revisions), self.assertRaises(ValueError):
                snapshot.build_manifest(self.pages, self.space, revisions, self.source)
        (self.space / 'search_ngrams_3.bin.gz').write_bytes(gzip.compress(b'VNG3\3' + b'x' * 32))
        with self.assertRaisesRegex(ValueError, 'does not bind'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)
        (self.space / 'search_words.bin.gz').unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)

    def test_manifest_paths_must_match_existing_payloads_and_surface(self):
        path = self.pages / 'initial/manifest.json'
        for urls in (['/data/initial/global.json'], ['/search/data/initial/../x'],
                     ['/search/data/initial/global.json', '/search/data/initial/global.json'],
                     ['/search/data/initial/global.json', '/search/data/initial/repos/missing.json']):
            path.write_text(json.dumps({'version': 2, 'urls': urls}))
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)
        path.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'bootstrap manifest'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)

    def test_captured_source_must_match_both_surfaces(self):
        corpus = json.loads(self.source.read_bytes())
        corpus['rc'][0][1] = 'different'
        self.source.write_text(json.dumps(corpus))
        with self.assertRaisesRegex(ValueError, 'captured source payload'):
            snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)

    def test_invalid_compact_records_are_rejected_even_when_surfaces_agree(self):
        for corpus in ({'v': 2, 'rp': ['VoiceOfML/Test'], 'fd': [[]], 'rc': [[1, 'bad', 'pdf', 0, 1, 0]]},
                       {'v': 2, 'rp': ['VoiceOfML/Test'], 'fd': [[]], 'rc': []}):
            for root in (self.pages, self.space):
                (root / 'search_data.json.gz').write_bytes(packed(corpus))
            with self.assertRaises(ValueError):
                snapshot.build_manifest(self.pages, self.space, self.revisions, self.source)


if __name__ == '__main__':
    unittest.main()
