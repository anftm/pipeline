"""Small qualified four-bucket graphs; never list production or delete objects."""
import gzip
import hashlib
import json
import unittest

from scripts import ccrd_lifecycle as lifecycle, reader_gc_graph as graph, reader_gc_collect as collector
from scripts.reader_gc_stream import graph_json
from tests.test_reader_gc_graph import MemoryStore


ASSETS, PDF, INPUT = graph.READER_BUCKETS
CCRD = lifecycle.BUCKET


class CcrdLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()

    def put(self, bucket, path, value):
        raw = (json.dumps(value, sort_keys=True, ensure_ascii=False).encode() if isinstance(value, dict) else value)
        if path.endswith('.gz'):
            raw = gzip.compress(raw, mtime=0)
        self.store.objects[bucket][path] = raw
        return {'bucket': bucket, 'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'role': 'provenance'}

    def release(self, suffix='a', input_ref=None):
        raw = self.put(PDF, f'objects/{suffix}/raw/page.json.gz', {'text': 'OCR text'})
        evidence = input_ref or self.put(INPUT, f'objects/{suffix}/input/page.png', b'lossless evidence')
        reader = self.put(ASSETS, f'objects/{suffix}/audit/proposal.json', {'kind': 'test audit'})
        text = self.put(CCRD, f'text/objects/{suffix * 64}.txt', b'plain text only')
        export = {'kind': 'ccrd-ocr-text-export', 'version': 1, 'complete': True, 'book_id': suffix * 64,
                  'generation': suffix * 64, 'page_count': 1, 'pages': [{}], 'text': text,
                  'retained_reader_resources': [raw, evidence, reader]}
        exported = self.put(CCRD, f'text/exports/{suffix * 64}/{suffix * 64}/manifest.json', export)
        identity = {'version': 1, 'kind': 'ccrd-ocr-search-release', 'base_metadata_sha256': 'd' * 64,
                    'builder_sha256': 'e' * 64, 'tokenizer_version': lifecycle.TOKENIZER, 'exports': [exported]}
        generation = lifecycle.digest(lifecycle.canonical(identity))
        database = self.put(CCRD, f'indexes/ocr/{generation}/OCR.sqlite3', b'small index fixture')
        manifest = {**identity, 'generation': generation, 'documents': 1, 'database': database}
        release = self.put(CCRD, f'releases/ocr-{generation}/manifest.json', manifest)
        return release, [raw, evidence, reader, text, exported, database]

    def root(self, refs, *, name='retention', kind='ccrd-ocr-retention'):
        return self.put(CCRD, f'control/{name}.json', {'version': 1, 'kind': kind, 'resources': refs})

    def test_export_retains_txt_index_and_all_three_reader_bucket_dependencies(self):
        release, objects = self.release()
        self.root([release])
        candidate = graph.ReferenceGraph(self.store); report = candidate.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        for ref in [release, *objects]:
            self.assertIn((ref['bucket'], ref['path']), candidate.references)
        self.assertEqual(set(report['buckets']), set(lifecycle.BUCKETS))
        self.assertFalse(report['deletion_enabled'])

    def test_retained_old_release_and_upload_intent_both_protect_complete_members(self):
        old, old_refs = self.release('a')
        pending, pending_refs = self.release('b')
        self.root([old])
        self.root([pending], name='intents/pending', kind='ccrd-ocr-intent')
        candidate = graph.ReferenceGraph(self.store); report = candidate.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertTrue(all((ref['bucket'], ref['path']) in candidate.references for ref in [*old_refs, *pending_refs]))

    def test_unfinished_upload_or_unreadable_fourth_bucket_blocks_all_candidates(self):
        self.root([{'bucket': CCRD, 'path': 'text/objects/not-uploaded.txt', 'sha256': 'a' * 64, 'bytes': 10, 'role': 'runtime'}])
        self.put(ASSETS, 'objects/unrelated/orphan.bin', b'orphan')
        report = graph.ReferenceGraph(self.store).build()
        self.assertFalse(report['graph_complete'])
        self.assertTrue(all(not item['candidates'] for item in report['buckets'].values()))
        class Unreadable(MemoryStore):
            def list_files(self, bucket, prefixes):
                if bucket == CCRD: raise PermissionError('fourth bucket denied')
                return super().list_files(bucket, prefixes)
        self.assertFalse(graph.ReferenceGraph(Unreadable()).build()['graph_complete'])

    def test_unknown_control_schema_wrong_bucket_or_corrupt_manifest_fail_closed(self):
        for change in ('schema', 'bucket', 'checksum'):
            with self.subTest(change=change):
                self.store = MemoryStore(); release, _ = self.release()
                if change == 'schema':
                    self.put(CCRD, 'control/current.json', {'kind': 'future-schema', 'version': 9, 'resources': []})
                elif change == 'bucket':
                    self.root([{**release, 'bucket': 'unknown/bucket'}])
                else:
                    self.root([{**release, 'sha256': 'f' * 64}])
                self.assertFalse(graph.ReferenceGraph(self.store).build()['graph_complete'])

    def test_reader_retirement_cannot_release_input_still_referenced_by_ccrd(self):
        source = 'a' * 64
        path = f'objects/aa/{source}/{"b" * 16}/ocr-input/page-000001.png'
        evidence = self.put(INPUT, path, b'original evidence')
        self.put(ASSETS, 'reader-index/v3/retirement.json', {'version': 1, 'kind': 'reader-v3-retirement',
                 'resources': [], 'retired_paths': [], 'replacement_acks': {'hf': True, 'pages': True},
                 'retired_inputs': {path: {'bucket': INPUT, 'path': path, 'source_sha256': source}}})
        release, _ = self.release(input_ref=evidence); self.root([release])
        candidate = graph.ReferenceGraph(self.store); report = candidate.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertIn((INPUT, path), candidate.references)
        self.assertNotIn(path, report['buckets'][INPUT]['candidates'])

    def test_managed_namespace_is_bucket_specific_and_never_pins_unreferenced_exports(self):
        self.put(CCRD, 'objects/foreign/unmanaged.bin', b'unmanaged')
        self.put(CCRD, 'text/objects/orphan.txt', b'orphan')
        report = graph.ReferenceGraph(self.store).build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertEqual(report['buckets'][CCRD]['candidates'], ['text/objects/orphan.txt'])
        self.assertIn('objects/foreign/unmanaged.bin', report['buckets'][CCRD]['unmanaged'])

    def test_streaming_omits_raw_text_but_preserves_root_text_resource(self):
        value = {'text': {'bucket': CCRD, 'path': 'text/objects/body.txt', 'bytes': 1, 'sha256': 'a' * 64, 'role': 'runtime'},
                 'pages': [{'text': 'large body omitted'}]}
        parsed, _ = graph_json([json.dumps(value).encode()])
        self.assertEqual(parsed['text'], value['text'])
        self.assertNotIn('text', parsed['pages'][0])
        parsed, _ = graph_json([b'{"text":"top level OCR body", "kind":"raw"}'])
        self.assertNotIn('text', parsed)

    def test_deleting_collector_refuses_new_domain_before_any_write_or_delete(self):
        release, _ = self.release(); self.root([release])
        before = {bucket: dict(files) for bucket, files in self.store.objects.items()}
        with self.assertRaisesRegex(RuntimeError, 'report-only'):
            collector.reject_unaccepted_ccrd_collection(self.store)
        self.assertEqual(self.store.objects, before)

    def test_exact_ocr_quarantine_is_a_known_non_deleting_control_root(self):
        self.put(CCRD, 'control/ocr-automation.json', {'version': 1, 'kind': 'ccrd-ocr-automation',
            'resources': [], 'failures': {'repo\0book.pdf': {'key': 'repo\0book.pdf',
                'fingerprint': 'a' * 64, 'error_type': 'ValueError'}}})
        report = graph.ReferenceGraph(self.store).build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertFalse(report['deletion_enabled'])


if __name__ == '__main__':
    unittest.main()
