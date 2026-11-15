"""Offline refresh tests exercise real projection collection and single-root CAS."""
import base64
import copy
import gzip
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import reader_page_consumer_refresh as refresh
from scripts import publish_reader_v3 as publication, reader_gc_collect as collector
from scripts import reader_lifecycle as lifecycle
from tests.test_reader_gc_ccrd import build_store

ASSETS = refresh.ASSETS
PDF = publication.shared.PDF_PAGES_BUCKET
INPUT = publication.shared.PDF_OCR_INPUT_BUCKET
CCRD = collector.graph.ccrd_lifecycle.BUCKET
ROOT = (ASSETS, refresh.CONSUMERS)
HF = 'https://voiceofml-search.hf.space/api/reader-assets'
PAGES = 'https://vomebook.github.io/search/data/reader_assets.json.gz'
CCRD_URL = 'https://vomebook-search.hf.space/api/index-status'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Store:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.immutable_writes = []
        self.conditional_reads = []
        self.allowed = True
        self.mode = 'success'
        self.on_read_for_update = None

    def read_bytes(self, bucket, path):
        try:
            return self.objects[bucket, path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def assert_serialized_writer(self, protocol):
        if not self.allowed or protocol != publication.PROTOCOL:
            raise RuntimeError('no serialized writer')

    def read_for_update(self, bucket, path):
        assert (bucket, path) == ROOT
        self.conditional_reads.append((bucket, path))
        if self.writes and self.mode == 'readback-failure':
            raise OSError('readback unavailable')
        if self.on_read_for_update:
            self.on_read_for_update()
        raw = self.read_bytes(bucket, path)
        return raw, sha(raw)

    def immutable_put(self, bucket, path, raw):
        assert bucket == ASSETS
        assert path == refresh.INTENTS + sha(raw) + '.json'
        prior = self.objects.get((bucket, path))
        if prior is not None and prior != raw:
            raise ValueError('immutable conflict')
        self.immutable_writes.append((bucket, path))
        self.objects[bucket, path] = raw

    def put_if_match(self, bucket, path, raw, token):
        assert (bucket, path) == ROOT
        self.writes.append((bucket, path, raw, token))
        if self.mode == 'conflict':
            self.objects[ROOT] = b'foreign writer'
        if sha(self.objects[ROOT]) != token:
            raise RuntimeError('CAS conflict')
        if self.mode == 'lost-before':
            raise OSError('response lost before commit')
        if self.mode != 'unchanged':
            self.objects[ROOT] = raw
        if self.mode == 'lost-after':
            raise OSError('response lost after commit')


class Response:
    def __init__(self, raw):
        self.content = raw

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.content)

    def iter_bytes(self):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.hook = None

    def get(self, url, *, headers):
        assert headers == {'Cache-Control': 'no-cache'}
        self.calls.append(url)
        if self.hook:
            self.hook(url)
        return Response(self.responses[url])

    def stream(self, method, url, *, headers):
        assert method == 'GET'
        return self.get(url, headers=headers)


class ConsumerRefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.current_ref = {'bucket': PDF, 'path': 'objects/current/document.pdf'}
        self.other_reader = {'bucket': ASSETS, 'path': 'objects/current/book.epub'}
        self.old_reader = {'bucket': PDF, 'path': 'derived/old/document.pdf'}
        self.base = {'v': 1, 'f': {
            'repo\0book.pdf': {'s': 2, 'm': 'p', 'b': PDF, 'p': self.current_ref['path']},
            'repo\0book.epub': {'s': 2, 'm': 'e', 'b': ASSETS, 'p': self.other_reader['path']},
            'repo\0input': {'s': 2, 'm': 'i', 'b': INPUT, 'p': 'objects/new-external/image.png'}}}
        packed = gzip.compress(publication.encode(self.base), mtime=0)
        self.store.objects[ASSETS, collector.SIDECAR] = packed
        legacy, _ = lifecycle.catalog_generation(self.base)
        self.store.objects[ASSETS, collector.CATALOG] = publication.encode(legacy)
        snapshot = publication.metadata(ASSETS,
            f'reader-index/snapshots/{sha(packed)}/reader_assets.json.gz', packed)
        self.store.objects[ASSETS, snapshot['path']] = packed
        body = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                'base_sidecar': snapshot, 'parent_generation': None}
        self.generation = sha(publication.encode(body))
        catalog_raw = publication.encode({**body, 'generation': self.generation})
        self.catalog_ref = publication.metadata(ASSETS,
            f'reader-index/v3/generations/{self.generation}/catalog.json', catalog_raw)
        self.store.objects[ASSETS, self.catalog_ref['path']] = catalog_raw
        self.pointer = {'version': 1, 'kind': 'reader-reading-pointer',
                        'generation': self.generation, 'catalog': self.catalog_ref,
                        'resources': [self.catalog_ref]}
        self.store.objects[ASSETS, publication.POINTER] = publication.encode(self.pointer)
        ccrd_store, releases = build_store(generations=2)
        self.store.objects.update({(CCRD, path): raw for path, raw in ccrd_store.objects.items()})
        self.old_ccrd = copy.deepcopy(releases[0]['release'])
        self.old_ccrd['extension'] = {'retain': True}
        self.old = {'version': 1, 'kind': 'reader-gc-consumers',
                    'extension': {'resources': [self.old_reader], 'note': 'keep exactly'},
                    'receipts': {'hf': {'generation': 'old', 'extension': {'keep': True}},
                                 'pages': {'generation': 'old'},
                                 'ccrd': {'generation': releases[0]['generation'], 'opaque': [1, 2]},
                                 'future-domain': {'anything': 'retained'}},
                    'resources': [self.old_reader, self.old_ccrd,
                        {'bucket': INPUT, 'root': 'objects/old-input', 'extension': 1},
                        {'bucket': 'future/domain', 'path': 'opaque', 'extra': ['keep']} ]}
        self.store.objects[ROOT] = json.dumps(self.old, indent=2).encode()
        responses = {HF: publication.encode(self.base), PAGES: packed,
                     CCRD_URL: publication.encode({'ready': True, 'loaded_sources': ['CCRD', 'CW'],
                                                  'ocr_release': releases[-1]['generation']})}
        for surface, url in (('hf', 'https://voiceofml-search.hf.space/api/reader-v3-status'),
                             ('pages', 'https://vomebook.github.io/search/data/reader_v3_receipt.json')):
            responses[url] = publication.encode({'version': 1, 'surface': surface, 'active': True,
                'generation': self.generation, 'catalog_sha256': self.catalog_ref['sha256'], 'files': 0,
                'reader_contract': 'pdf-reading-v3-v1', 'sidecar_sha256': sha(packed)})
        self.client = Client(responses)
        self.old_sha = sha(self.store.objects[ROOT])

    def prepare(self):
        return refresh.prepare(self.store, self.client, current_generation=self.generation,
                               old_root_sha256=self.old_sha)

    def apply(self, plan):
        return refresh.apply(self.store, plan, self.client, current_generation=self.generation,
                             old_root_sha256=self.old_sha)

    def test_readonly_plan_replaces_reader_refs_from_full_current_projections(self):
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        after = json.loads(plan['after'])
        self.assertEqual(after['resources'][:3], self.old['resources'][1:])
        self.assertCountEqual(after['resources'][3:], [self.current_ref, self.other_reader])
        self.assertEqual(after['extension'], self.old['extension'])
        for surface in ('ccrd', 'future-domain'):
            self.assertEqual(after['receipts'][surface], self.old['receipts'][surface])
        self.assertEqual(after['receipts']['hf']['extension'], {'keep': True})
        for surface in ('hf', 'pages'):
            self.assertEqual(after['receipts'][surface]['generation'], self.generation)
            self.assertEqual(after['receipts'][surface]['catalog_sha256'], self.catalog_ref['sha256'])
        self.assertNotEqual(plan['observations']['ccrd']['generation'],
                            after['receipts']['ccrd']['generation'])
        self.assertEqual(self.client.calls.count(HF), 2)
        self.assertEqual(self.client.calls.count(PAGES), 2)
        self.assertEqual(self.client.calls.count(CCRD_URL), 1)
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.store.conditional_reads, [])

    def test_still_live_derived_ref_is_retained_not_removed_by_prefix(self):
        self.base['f']['repo\0book.pdf']['p'] = self.old_reader['path']
        # A different complete current generation legitimately still serves it.
        raw = publication.encode(self.base)
        packed = gzip.compress(raw, mtime=0)
        legacy, _ = lifecycle.catalog_generation(self.base)
        self.store.objects[ASSETS, collector.SIDECAR] = packed
        self.store.objects[ASSETS, collector.CATALOG] = publication.encode(legacy)
        body = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}}
        self.generation = sha(publication.encode(body))
        catalog_raw = publication.encode({**body, 'generation': self.generation})
        ref = publication.metadata(ASSETS,
            f'reader-index/v3/generations/{self.generation}/catalog.json', catalog_raw)
        self.store.objects[ASSETS, ref['path']] = catalog_raw
        self.store.objects[ASSETS, publication.POINTER] = publication.encode({
            **self.pointer, 'generation': self.generation, 'catalog': ref, 'resources': [ref]})
        self.client.responses.update({HF: raw, PAGES: packed})
        for url, response in list(self.client.responses.items()):
            if 'reader-v3-status' in url or 'reader_v3_receipt' in url:
                self.client.responses[url] = publication.encode({**json.loads(response),
                    'generation': self.generation, 'catalog_sha256': ref['sha256'],
                    'sidecar_sha256': sha(packed)})
        self.assertIn(self.old_reader, json.loads(self.prepare()['after'])['resources'])

    def test_unknown_schema_and_malformed_identity_fail_closed(self):
        for change in ({'version': 2}, {'version': True}, {'kind': 'other'},
                       {'resources': {}}, {'receipts': []},
                       {'resources': [{'path': 'missing-bucket'}]}):
            with self.subTest(change=change):
                self.store.objects[ROOT] = publication.encode({**self.old, **change})
                self.old_sha = sha(self.store.objects[ROOT])
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.client.calls, [])

    def test_requires_exact_generation_and_raw_old_root_sha(self):
        for generation, old_sha in (('', self.old_sha), ('a' * 64, self.old_sha),
                                    (self.generation, 'b' * 64),
                                    (self.generation, sha(publication.encode(self.old)))):
            with self.subTest(generation=generation, old_sha=old_sha):
                with self.assertRaises(ValueError):
                    refresh.prepare(self.store, self.client, current_generation=generation,
                                    old_root_sha256=old_sha)
        self.assertEqual(self.store.writes, [])

    def test_partial_projection_and_ccrd_mismatch_block_refresh(self):
        original = copy.deepcopy(self.client.responses)
        for url, raw in ((HF, publication.encode({'v': 1, 'f': {}})),
                         (PAGES, gzip.compress(publication.encode({'v': 1, 'f': {}}))),
                         (CCRD_URL, publication.encode({'ready': True, 'loaded_sources': ['CCRD', 'CW'],
                                                       'ocr_release': 'wrong'}))):
            with self.subTest(url=url):
                self.client.responses = {**original, url: raw}
                with self.assertRaises(ValueError):
                    self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_apply_reobserves_and_updates_only_consumer_root(self):
        with patch.object(lifecycle, 'now_iso', return_value='2026-11-15T01:00:00Z'):
            plan = self.prepare()
        before = copy.deepcopy(self.store.objects)
        with patch.object(lifecycle, 'now_iso', return_value='2026-11-15T02:00:00Z'):
            result = self.apply(plan)
        self.assertEqual(result['outcome'], 'applied')
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(self.client.calls.count(CCRD_URL), 2)
        self.assertEqual(len(self.store.writes), 1)
        self.assertEqual(self.store.conditional_reads, [ROOT, ROOT, ROOT])
        intent_ref = result['intent_ref']
        intent_raw = self.store.read_bytes(intent_ref['bucket'], intent_ref['path'])
        intent = json.loads(intent_raw)
        self.assertEqual(sha(intent_raw), intent_ref['sha256'])
        self.assertEqual(base64.b64decode(intent['before_base64']), before[ROOT])
        self.assertEqual(base64.b64decode(intent['after_base64']), self.store.objects[ROOT])
        self.assertEqual(intent['current_generation'], self.generation)
        self.assertEqual(set(intent['fresh_consumer_observations']), {'hf', 'pages', 'ccrd'})
        self.assertEqual(self.store.immutable_writes, [(ASSETS, intent_ref['path'])])
        after = json.loads(self.store.objects[ROOT])
        self.assertEqual(after['receipts']['hf']['checked_at'], '2026-11-15T02:00:00Z')
        self.assertEqual(after['receipts']['ccrd'], self.old['receipts']['ccrd'])
        self.assertEqual({k: v for k, v in self.store.objects.items()
                          if k != ROOT and not k[1].startswith(refresh.INTENTS)},
                         {k: v for k, v in before.items() if k != ROOT})

    def test_apply_requires_writer_window(self):
        plan = self.prepare()
        self.store.allowed = False
        with self.assertRaisesRegex(RuntimeError, 'serialized writer'):
            self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_caller_edited_plan_cannot_drop_external_refs_or_extensions(self):
        original = self.prepare()
        for field in ('resources', 'extension', 'receipts'):
            with self.subTest(field=field):
                plan = copy.deepcopy(original)
                after = json.loads(plan['after'])
                after.pop(field)
                plan['after'] = publication.encode(after)
                with self.assertRaises(ValueError):
                    self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_source_or_root_drift_between_prepare_and_apply_blocks_write(self):
        plan = self.prepare()
        before = copy.deepcopy(self.store.objects)
        for key in (ROOT, (ASSETS, publication.POINTER), (ASSETS, collector.SIDECAR),
                    (ASSETS, collector.CATALOG), (ASSETS, self.catalog_ref['path']),
                    (CCRD, 'control/ocr-current.json')):
            with self.subTest(key=key):
                self.store.objects = copy.deepcopy(before)
                self.store.objects[key] += b' '
                with self.assertRaises((ValueError, RuntimeError, gzip.BadGzipFile)):
                    self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_source_drift_during_observation_is_rejected(self):
        def drift(url):
            if url == CCRD_URL:
                self.store.objects[ASSETS, publication.POINTER] += b' '
        self.client.hook = drift
        with self.assertRaisesRegex(ValueError, 'source drift'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_last_boundary_checks_source_drift_and_root_raw_bytes(self):
        plan = self.prepare()
        before = copy.deepcopy(self.store.objects)
        for key in (ROOT, (ASSETS, collector.CATALOG), (ASSETS, collector.SIDECAR),
                    (ASSETS, publication.POINTER), (CCRD, 'control/ocr-current.json')):
            with self.subTest(key=key):
                self.store.objects = copy.deepcopy(before)
                def mutate():
                    self.store.objects[key] += b' '
                self.store.on_read_for_update = mutate
                with self.assertRaises(ValueError):
                    self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_missing_conditional_token_blocks_write(self):
        plan = self.prepare()
        with patch.object(self.store, 'read_for_update', return_value=(plan['before'], None)):
            with self.assertRaisesRegex(ValueError, 'conditional read'):
                self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_intent_failure_or_bad_readback_never_attempts_cas(self):
        plan = self.prepare()
        for mode in ('failure', 'bad-readback'):
            with self.subTest(mode=mode):
                def immutable(bucket, path, raw):
                    if mode == 'failure':
                        raise OSError('immutable outcome unknown')
                    self.store.objects[bucket, path] = b'wrong intent'
                with patch.object(self.store, 'immutable_put', side_effect=immutable) as put:
                    with self.assertRaises((ValueError, OSError)):
                        self.apply(plan)
                    put.assert_called_once()
                self.assertEqual(self.store.writes, [])

    def test_drift_during_intent_staging_prevents_cas(self):
        plan = self.prepare()
        before = copy.deepcopy(self.store.objects)
        original = self.store.immutable_put
        for key in (ROOT, (ASSETS, collector.SIDECAR), (ASSETS, collector.CATALOG),
                    (ASSETS, publication.POINTER), (CCRD, 'control/ocr-current.json')):
            with self.subTest(key=key):
                self.store.objects = copy.deepcopy(before)
                def stage_and_drift(bucket, path, raw):
                    original(bucket, path, raw)
                    self.store.objects[key] += b' '
                with patch.object(self.store, 'immutable_put', side_effect=stage_and_drift):
                    with self.assertRaises(ValueError):
                        self.apply(plan)
                self.assertEqual(self.store.writes, [])

    def test_token_drift_after_intent_staging_prevents_cas(self):
        plan = self.prepare()
        with patch.object(self.store, 'read_for_update', side_effect=[
                (plan['before'], 'original'), (plan['before'], 'different')]):
            with self.assertRaisesRegex(ValueError, 'changed before CAS'):
                self.apply(plan)
        self.assertEqual(self.store.writes, [])

    def test_one_cas_and_readback_outcomes_without_retries(self):
        plan = self.prepare()
        before = self.store.objects[ROOT]
        for mode, outcome in (('lost-before', 'unknown'), ('lost-after', 'applied'),
                              ('readback-failure', 'unknown'), ('unchanged', 'unknown'),
                              ('conflict', 'conflict')):
            with self.subTest(mode=mode):
                self.store.objects[ROOT] = before
                self.store.writes.clear()
                self.store.mode = mode
                result = self.apply(plan)
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['cas_attempts'], 1)
                self.assertEqual(len(self.store.writes), 1)
                self.assertEqual(result['applied'], outcome == 'applied')


if __name__ == '__main__':
    unittest.main()
