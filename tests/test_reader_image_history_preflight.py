"""Run the read-only CLI and its real sidecar/Search/Reader helpers on fixtures."""
import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts import reader_image_history as history, reader_image_history_preflight as preview
from scripts import reader_route_plan as routes, search_adopt, search_artifact_store as artifacts, search_snapshot

REPO = 'VoiceOfML/images'
PATH = 'album/h.PNG'
KEY = REPO + '\0' + PATH
OLD, CURRENT, OP = 'a' * 40, 'b' * 40, 'c' * 32


def image_bytes(fmt, color, *, mode='RGB', alpha_pixel=None):
    data = io.BytesIO()
    with Image.new(mode, (16, 16), color) as image:
        if alpha_pixel is not None:
            image.putpixel(alpha_pixel, (255, 0, 0, 0))
        image.save(data, fmt)
    return data.getvalue()


class AlphaProofTests(unittest.TestCase):
    def test_opaque_alpha_channel_matches_rgb(self):
        original = image_bytes('PNG', (255, 0, 0, 255), mode='RGBA')
        encoded = image_bytes('AVIF', 'red')
        self.assertEqual(preview._decoded_image(original, 'PNG'), ((16, 16), None))
        self.assertEqual(preview._decoded_image(original, 'PNG'), preview._decoded_image(encoded, 'AVIF'))

    def test_missing_or_moved_transparent_pixel_does_not_match(self):
        def make(fmt, pixel):
            return image_bytes(fmt, (255, 0, 0, 255), mode='RGBA', alpha_pixel=pixel)

        original = preview._decoded_image(make('PNG', (1, 1)), 'PNG')
        self.assertIsNotNone(original[1])
        self.assertEqual(original, preview._decoded_image(make('AVIF', (1, 1)), 'AVIF'))
        self.assertNotEqual(original, preview._decoded_image(make('AVIF', None), 'AVIF'))
        self.assertNotEqual(original, preview._decoded_image(make('AVIF', (2, 2)), 'AVIF'))


class MemoryReads:
    def __init__(self):
        self.objects = {}
        self.read_etags = {}
        self.calls = []
        self.read = lambda bucket, key, raw: raw

    def add(self, bucket, key, data):
        self.objects[bucket, key] = data if isinstance(data, bytes) else history._encode(data)

    def read_bytes(self, bucket, path):
        self.calls.append((bucket, path))
        try:
            raw = self.objects[bucket, path]
        except KeyError:
            raise FileNotFoundError(path) from None
        self.read_etags[bucket, path] = '"' + history._sha(raw) + '"'
        return self.read(bucket, path, raw)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryReads()
        self.sources = {OLD: image_bytes('PNG', 'red')}
        self.sources[CURRENT] = self.sources[OLD]
        self.source_reads = []
        webp = image_bytes('WEBP', 'red')
        digest = history._sha(self.sources[OLD])
        self.old_path = f'pages/image/png/{digest}/document.webp'
        self.old = {'key': KEY, 'repo': REPO, 'source_path': PATH, 'source_revision': OLD, 'source_sha256': digest,
                    'source_bytes': len(self.sources[OLD]), 'extension': 'png', 'mode': 'image', 'page_count': 1,
                    'bucket': preview.BUCKET, 'path': self.old_path, 'sha256': history._sha(webp), 'bytes': len(webp),
                    'processing_roots': ['reader-index/processing/historical.json']}
        self.other = {'key': REPO + '\0other.jpg', 'mode': 'image', 'path': 'pages/image/other.webp'}
        self.canonical = {'version': 1, 'kind': 'image-stream-index', 'files': [self.old, self.other],
                          'failures': [{'key': REPO + '\0missing.png'}], 'metadata': 'untouched'}
        self.shard_path = f'pages/image/index-v2-{preview.shard_for(KEY, 8):02d}.json'
        self.shard = {**self.canonical, 'files': [self.old], 'failures': []}
        for key, data in ((history.CANONICAL, self.canonical), (self.shard_path, self.shard), (self.old_path, webp)):
            self.store.add(preview.BUCKET, key, data)
        for bucket, key, required in routes.INDEXES:
            if required and (bucket, key) not in self.store.objects:
                self.store.add(bucket, key, {'files': []})
        self.base = preview._build_sidecar(preview.Inputs(self.store))
        base_raw = gzip.compress(preview.reader.encode(self.base), mtime=0)
        self.store.add(preview.BUCKET, routes.SIDECAR, base_raw)
        _, ref = preview.control.prepare_base({'files': {}}, base_raw)
        self.store.add(preview.BUCKET, ref['path'], base_raw)
        reader = preview.reader
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': ref}
        self.reader = catalog['generation'] = reader.text.digest(catalog)
        catalog_path = f'reader-index/v3/generations/{self.reader}/catalog.json'
        catalog_raw = reader.encode(catalog)
        self.store.add(preview.BUCKET, catalog_path, catalog_raw)
        self.store.add(preview.BUCKET, reader.POINTER, {'version': 1, 'kind': 'reader-reading-pointer',
                       'generation': self.reader, 'catalog': reader.metadata(preview.BUCKET, catalog_path, catalog_raw)})
        corpus = {'v': 2, 'rp': [REPO], 'fd': [['album']], 'rc': [[0, 'h', 'PNG', 0, len(self.sources[OLD]), 0]]}
        corpus_raw = gzip.compress(search_snapshot.canonical(corpus), mtime=0)
        member = {'sha256': history._sha(corpus_raw), 'bytes': len(corpus_raw)}
        identity = {'version': 1, 'kind': 'search-snapshot-local', 'record_count': 1,
                    'source_revisions': {REPO: CURRENT},
                    'files': {site: {'search_data.json.gz': member} for site in ('pages', 'space')}}
        self.search = history._sha(search_snapshot.canonical(identity))
        manifest = artifacts.make_manifest({**identity, 'generation': self.search,
                                           'remote_writes': 0, 'publication_ready': False})
        self.store.add(preview.search.BUCKET, f'generations/{self.search}/manifest.json', manifest)
        self.store.add(preview.search.BUCKET, artifacts.object_key(member['sha256']), corpus_raw)
        self.store.add(preview.search.BUCKET, preview.search.KEY, preview.search.make_pointer(self.search, None, OP))
        self.http = {search_adopt.SPACE + '/api/search-data-generation': history._encode(
                        {'generation': self.search, 'surface': 'space', 'verified': True, 'records': 1,
                         'api_generation': 'fixture'}),
                     search_adopt.PAGES + '/data/generation-manifest.json': history._encode(manifest),
                     search_adopt.PAGES + '/data/search_data.json.gz': corpus_raw,
                     search_adopt.PAGES + '/data/initial/global.json': b'{"total":1}'}

    def read_source(self, pin, size):
        self.assertEqual(pin[:2], (REPO, PATH))
        self.assertEqual(size, len(self.sources[OLD]))
        self.source_reads.append(pin[2])
        return self.sources[pin[2]]

    def build(self, encode=None, **changes):
        return preview.plan(**({'store': self.store, 'read_source': self.read_source,
            'source_head': lambda _: CURRENT, 'encode_avif': encode or (lambda _: image_bytes('AVIF', 'red')),
            'repo': REPO, 'path': PATH, 'historical_revision': OLD, 'current_revision': CURRENT,
            'search_generation': self.search, 'reader_generation': self.reader, 'operation': OP,
            'search_get': lambda url, limit: self.http[url]} | changes))

    def test_cli_outputs_only_local_one_row_and_projection(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            output = Path(tmp) / 'report'
            argv = ['--repo', REPO, '--path', PATH, '--historical-revision', OLD, '--current-revision', CURRENT,
                    '--search-generation', self.search, '--reader-generation', self.reader, '--operation', OP,
                    '--producer-checkout', str(Path(tmp) / 'producer'), '--producer-revision', 'd' * 40,
                    '--output', str(output)]
            def encode(raw):
                self.assertEqual(raw, self.sources[OLD])
                return image_bytes('AVIF', 'red')

            self.assertEqual(preview.main(argv, build=lambda _, encoder: self.build(encoder),
                                          load_encoder=lambda *_: encode), 0)
            report = json.loads((output / 'report.json').read_bytes())
            candidate = json.loads((output / 'candidate.json').read_bytes())
            self.assertEqual(candidate, {**self.canonical, 'files': [report['target_row'], self.other],
                                         'checkpoint_operation': OP})
            self.assertEqual(report['changed_route_keys'], [KEY])
            proposed = routes.decode_sidecar((output / 'pinned-base.json.gz').read_bytes())
            self.assertEqual(proposed, {**self.base, 'f': {**self.base['f'], KEY: report['after_route']}})
            self.assertEqual(history._sha((output / 'document.avif').read_bytes()), report['target_row']['sha256'])
            self.assertEqual(report['source']['sha256'], report['current_source']['sha256'])
            self.assertEqual(report['shard']['path'], self.shard_path)
            self.assertTrue(report['canonical']['etag'].startswith('"'))
            self.assertEqual(report['old_row'], self.old)
            self.assertNotIn('processing_roots', report['target_row'])
            self.assertEqual(report['remote_writes'], 0)
            self.assertFalse(report['publication_ready'])
            self.assertFalse(report['deletion_authorized'])
        self.assertEqual(self.source_reads, [OLD, CURRENT])

    def test_preview_target_uses_the_exact_producer_profile_and_content_root(self):
        report, _, _, _, avif_raw = self.build()
        avif = history.Asset(report['target_row']['root'], report['after_route']['p'],
                             history._sha(avif_raw), len(avif_raw))
        webp = history.Asset(self.old_path.rsplit('/', 1)[0], self.old_path, self.old['sha256'], self.old['bytes'])
        fields = {'source': history.Source(**report['source']), 'current': history.Source(**report['current_source']),
                  'webp': webp, 'avif': avif, 'operation': OP}
        for row, replacement in (({**report['target_row'], 'profile': 'foreign'}, avif),
                                 ({**report['target_row'], 'root': 'pages/image/png/' + 'f' * 64,
                                   'path': 'pages/image/png/' + 'f' * 64 + '/document.avif'},
                                  history.Asset('pages/image/png/' + 'f' * 64,
                                                'pages/image/png/' + 'f' * 64 + '/document.avif',
                                                avif.sha256, avif.size))):
            with self.assertRaisesRegex(ValueError, 'profile or content address'):
                history._replacement(self.canonical, KEY, report['old_row_sha256'], row,
                                     **(fields | {'avif': replacement}))

    def test_different_source_bytes_and_wrong_stored_webp_fail(self):
        self.sources[CURRENT] = self.sources[OLD][:-1] + bytes([self.sources[OLD][-1] ^ 1])
        with self.assertRaisesRegex(ValueError, 'source identities differ'):
            self.build()
        self.sources[CURRENT] = self.sources[OLD]
        self.store.add(preview.BUCKET, self.old_path, b'other')
        with self.assertRaisesRegex(ValueError, 'stored WebP differs'):
            self.build()

    def test_rejects_unmerged_shard_and_unrelated_sidecar_drift(self):
        self.store.add(preview.BUCKET, self.shard_path, {**self.shard, 'files': [
            {**self.old, 'checkpointed_at': '2026-11-20T00:00:00Z'}]})
        with self.assertRaisesRegex(ValueError, 'unmerged checkpoint'):
            self.build()
        self.store.add(preview.BUCKET, self.shard_path, self.shard)
        self.store.add(preview.BUCKET, routes.SIDECAR, gzip.compress(history._encode({'v': 1, 'f': {KEY: self.base['f'][KEY]}})))
        with self.assertRaisesRegex(ValueError, 'exact published sidecar'):
            self.build()

    def test_rejects_changed_search_pin_and_shard_validator(self):
        with self.assertRaisesRegex(ValueError, 'dataset head'):
            self.build(source_head=lambda _: OLD)
        self.store.calls.clear()

        def change_validator(bucket, path, raw):
            if path == self.shard_path and self.store.calls.count((bucket, path)) > 1:
                self.store.read_etags[bucket, path] = '"rotated"'
            return raw

        self.store.read = change_validator
        with self.assertRaisesRegex(ValueError, 'CAS validator'):
            self.build()


if __name__ == '__main__':
    unittest.main()
