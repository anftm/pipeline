import copy
import json
import unittest
from unittest.mock import Mock

from scripts import reader_gc_graph as gc
from scripts import reader_obsolete_pdf as preparation
from scripts import reader_obsolete_pdf_references as references
from tests.test_reader_gc_graph import MemoryStore


class ObsoletePDFReferenceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.mutations, rows = [], []
        for number in range(7):
            root = f'objects/aa/{number:064x}/{number:016x}'
            path = root + '/derived-manifest.json'
            previous = {'bucket': preparation.BUCKET, 'path': f'derived/old/{number}/document.pdf',
                        'sha256': 'a' * 64, 'bytes': 3, 'role': 'provenance'}
            self.store.put(preparation.BUCKET, previous['path'], b'old')
            self.store.put(preparation.BUCKET, root + '/document.pdf', b'%PDF-preserved')
            row = {'key': str(number), 'bucket': preparation.BUCKET,
                   'profile': 'gbk-font-repair-v2', 'generation': 'gbk-font-repair-v2',
                   'manifest_path': path, 'new_path': root + '/document.pdf',
                   'derived_sha256': 'b' * 64, 'derived_bytes': 14, 'previous_pdf': previous}
            rows.append(row)
            raw = self.encode({**row, 'version': 1, 'kind': 'pdf-derived-source'})
            self.store.put(preparation.BUCKET, path, raw)
            self.mutations.append(self.mutation(path, raw, preparation.remove_reference(raw, 'previous_pdf', previous)))
        index = self.encode({'version': 1, 'kind': 'pdf-derived-source-index', 'files': rows})
        after = index
        for row in rows:
            after = preparation.remove_reference(after, 'previous_pdf', row['previous_pdf'])
        self.store.put(preparation.BUCKET, preparation.INDEX, index)
        self.mutations.append(self.mutation(preparation.INDEX, index, after))
        self.mutations.append({'bucket': gc.ccrd_lifecycle.BUCKET, 'path': 'control/other.json',
                               'before': b'{}', 'after': b'{}'})
        self.plan = {'mutations': self.mutations}
        self.target = preparation.BUCKET, self.mutations[0]['path']

    @staticmethod
    def encode(payload):
        return (json.dumps(payload, indent=2) + '\n').encode()

    @staticmethod
    def mutation(path, before, after):
        return {'bucket': preparation.BUCKET, 'path': path, 'before': before, 'after': after,
                'before_identity': preparation.proof(before), 'after_identity': preparation.proof(after)}

    def build(self):
        reference = gc.ReferenceGraph(self.store)
        report = reference.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        return reference, report

    def inspect(self, reference, report):
        evidence = references.inspect_graph(reference, report, self.plan['mutations'])
        if evidence['blockers']:
            with self.assertRaises(references.ReferenceProofBlocked) as caught:
                references.verify(reference, report, self.plan)
            self.assertEqual(caught.exception.proof, evidence)
            json.dumps(caught.exception.proof)
        else:
            self.assertEqual(references.verify(reference, report, self.plan), evidence)
        return evidence

    def verify(self):
        reference, report = self.build()
        return self.inspect(reference, report)

    def test_no_hash_qualified_references_and_self_identity_allowed_without_writes(self):
        self.store.put(gc.READER_ASSETS_BUCKET, 'reader-index/extra.json',
                       {'version': 1, 'files': [{'bucket': self.target[0], 'path': self.target[1]}]})
        before = copy.deepcopy(self.store.objects)
        result = self.verify()
        self.assertEqual(result['blockers'], [])
        self.assertTrue(result['incoming_bindings_absent'])
        self.assertEqual(len(result['targets']), 7)
        self.assertEqual(set(result['readonly_domains']), set(gc.BUCKETS))
        item = result['targets'][0]
        self.assertEqual(item['before_identity'], preparation.proof(self.mutations[0]['before']))
        relations = {origin.get('relation') for origin in item['incoming_origins']}
        self.assertEqual(relations, {'manifest-self', 'planned-canonical-index-without-hash-binding',
                                     'qualified-reference-without-hash-binding'})
        self.assertTrue(result['root_digests'])
        self.assertFalse(result['mutation_authorized'])
        json.dumps(result)
        self.assertEqual(self.store.objects, before)  # Includes every target PDF, in all domains.

    def test_expected_binding_blocks_every_role_even_advisory_drift(self):
        for role in ('runtime', 'review', 'provenance'):
            with self.subTest(role=role):
                resource = {'bucket': self.target[0], 'path': self.target[1], 'role': role,
                            **preparation.proof(self.mutations[0]['before'])}
                # Provenance mismatch is advisory to GC, but still blocks this edit.
                if role == 'provenance':
                    resource['sha256'] = 'f' * 64
                self.store.put(gc.ccrd_lifecycle.BUCKET, 'control/retention.json',
                               {'version': 1, 'kind': 'ccrd-ocr-retention', 'resources': [resource]})
                result = self.verify()
                self.assertFalse(result['incoming_bindings_absent'])
                self.assertTrue(any('expected resource binding' in b for b in result['blockers']))
                item = result['targets'][0]
                self.assertEqual(item['expected_resources'], [resource])
                origin = next(o for o in item['incoming_origins'] if o['bucket'] == gc.ccrd_lifecycle.BUCKET)
                self.assertEqual(origin['bindings'][0]['sha256'], resource['sha256'])
                self.assertEqual(origin['bindings'][0]['bytes'], resource['bytes'])
                json.dumps(result)

    def test_reader_incoming_hash_or_bytes_blocks_including_provenance(self):
        for identity in ({'sha256': 'a' * 64}, {'bytes': 10}, {'manifest_sha256': 'b' * 64}):
            with self.subTest(identity=identity):
                self.store.put(gc.READER_ASSETS_BUCKET, 'reader-index/bound.json',
                               {'version': 1, 'files': [{'bucket': self.target[0], 'path': self.target[1],
                                                        'role': 'provenance', **identity}]})
                result = self.verify()
                self.assertTrue(any('incoming hash/bytes binding' in b for b in result['blockers']))
                self.assertFalse(result['incoming_bindings_absent'])

    def test_unqualified_assets_origin_hash_resolves_across_inventory(self):
        origin = gc.READER_ASSETS_BUCKET, 'reader-index/unqualified.json'
        self.store.put(*origin, {'version': 1, 'files': [
            {'path': self.target[1], 'sha256': 'f' * 64, 'role': 'provenance'}]})
        reference, report = self.build()
        self.assertIn(self.target, reference.direct[origin])
        result = self.inspect(reference, report)
        self.assertFalse(result['incoming_bindings_absent'])
        evidence = next(o for o in result['targets'][0]['incoming_origins'] if o['path'] == origin[1])
        self.assertEqual(evidence['bindings'], [{'field': 'path', 'sha256': 'f' * 64, 'role': 'provenance'}])

    def test_relative_manifest_hash_blocks_and_preserves_pdf_bytes(self):
        origin = self.target[0], self.target[1].replace('derived-manifest.json', 'linked-manifest.json')
        self.store.put(*origin, {'version': 1, 'kind': 'pdf-derived-source',
                                'resources': [{'path': 'derived-manifest.json', 'bytes': 42,
                                               'sha256': 'f' * 64, 'role': 'provenance'}]})
        before = copy.deepcopy(self.store.objects)
        reference, report = self.build()
        self.assertIn(self.target, reference.direct[origin])
        result = self.inspect(reference, report)
        self.assertFalse(result['incoming_bindings_absent'])
        evidence = next(o for o in result['targets'][0]['incoming_origins'] if o['path'] == origin[1])
        self.assertEqual(evidence['bindings'][0]['sha256'], 'f' * 64)
        self.assertEqual(evidence['bindings'][0]['bytes'], 42)
        self.assertEqual(self.store.objects, before)

    def test_alternate_fields_with_hash_block_known_incoming_origin(self):
        origin = gc.READER_ASSETS_BUCKET, 'reader-index/alternate.json'
        for field in ('c', 'r', 'root', 'paths', 'references'):
            for qualified in (False, True):
                with self.subTest(field=field, qualified=qualified):
                    entry = {field: [self.target[1]] if field in ('paths', 'references') else self.target[1],
                             'sha256': 'f' * 64}
                    if qualified:
                        entry[gc.BUCKET_FIELDS.get(field, 'bucket')] = self.target[0]
                    # A tree edge is not kept in direct; this origin is already
                    # known to reference the manifest through the bare path.
                    self.store.put(*origin, {'version': 1, 'files': [
                        {'bucket': self.target[0], 'path': self.target[1]}, entry]})
                    reference, report = self.build()
                    self.assertIn(self.target, reference.direct[origin])
                    result = self.inspect(reference, report)
                    self.assertFalse(result['incoming_bindings_absent'])
                    evidence = next(o for o in result['targets'][0]['incoming_origins'] if o['path'] == origin[1])
                    self.assertIn({'field': field, 'sha256': 'f' * 64}, evidence['bindings'])

    def test_binding_path_vocabulary_and_unknown_qualifiers_fail_closed(self):
        files = {bucket: set() for bucket in gc.BUCKETS}
        files[self.target[0]].add(self.target[1])
        origin = gc.READER_ASSETS_BUCKET, 'reader-index/test.json'
        for field in sorted(gc.PATH_FIELDS | {'references'}):
            with self.subTest(field=field):
                for qualifier in ({}, {'bucket': 'unknown/domain'},
                                  {gc.BUCKET_FIELDS.get(field, 'bucket'): self.target[0]}):
                    payload = {field: self.target[1], 'sha256': 'f' * 64, **qualifier}
                    self.assertTrue(references._bindings(payload, self.target, origin, files))
        # A bucket override inherited by a nested record must also be respected.
        payload = {'c': {'path': self.target[1], 'bytes': 42}, 'cb': self.target[0]}
        self.assertTrue(references._bindings(payload, self.target, origin, files))
        payload['cb'] = gc.PDF_OCR_INPUT_BUCKET
        self.assertEqual(references._bindings(payload, self.target, origin, files), [])

    def test_missing_mock_or_unscanned_graph_cannot_be_authorized_by_boolean(self):
        for reference in (None, True, Mock(graph_complete=True)):
            result = self.inspect(reference, {'graph_complete': True, 'blockers': []})
            self.assertTrue(result['blockers'])
            self.assertFalse(result['incoming_bindings_absent'])
        for mode in ('inventory', 'loaded', 'direct', 'domain', 'unreadable', 'pending', 'root'):
            with self.subTest(mode=mode):
                reference, report = self.build()
                if mode == 'inventory':
                    reference.files[self.target[0]].remove(self.target[1])
                elif mode == 'loaded':
                    del reference.loaded_digests[self.target]
                elif mode == 'direct':
                    del reference.direct[self.target]
                elif mode == 'domain':
                    del reference.files[gc.PDF_OCR_INPUT_BUCKET]
                elif mode == 'unreadable':
                    reference.unreadable[self.target] = 'denied'
                elif mode == 'pending':
                    reference.pending.add(self.target)
                else:
                    del reference.root_hashes[preparation.BUCKET, preparation.INDEX]
                result = self.inspect(reference, report)
                self.assertTrue(result['blockers'])
                self.assertFalse(result['incoming_bindings_absent'])

    def test_unreadable_full_graph_blocks(self):
        self.store.put(gc.PDF_OCR_INPUT_BUCKET, 'reader-index/broken.json', b'not JSON')
        reference = gc.ReferenceGraph(self.store)
        report = reference.build()
        self.assertFalse(report['graph_complete'])
        result = self.inspect(reference, report)
        self.assertTrue(result['blockers'])
        self.assertFalse(result['incoming_bindings_absent'])

    def test_changed_manifest_or_incoming_origin_blocks(self):
        for mode in ('manifest', 'origin'):
            with self.subTest(mode=mode):
                self.setUp()
                origin = gc.READER_ASSETS_BUCKET, 'reader-index/extra.json'
                self.store.put(*origin, {'version': 1, 'files': [{'bucket': self.target[0], 'path': self.target[1]}]})
                reference, report = self.build()
                ref = self.target if mode == 'manifest' else origin
                self.store.put(*ref, self.store.read_bytes(*ref) + b' ')
                result = self.inspect(reference, report)
                self.assertFalse(result['incoming_bindings_absent'])
                self.assertTrue(any('changed' in b for b in result['blockers']))

    def test_same_path_in_other_bucket_is_not_an_incoming_binding(self):
        self.store.put(gc.PDF_OCR_INPUT_BUCKET, self.target[1],
                       {'version': 1, 'kind': 'pdf-derived-source'})
        self.store.put(gc.READER_ASSETS_BUCKET, 'reader-index/other.json',
                       {'version': 1, 'files': [{'bucket': gc.PDF_OCR_INPUT_BUCKET,
                                                'path': self.target[1], 'sha256': 'f' * 64, 'bytes': 42}]})
        self.assertEqual(self.verify()['blockers'], [])

    def test_retained_audit_intent_hash_bindings_are_not_runtime_roots(self):
        path = 'reader-index/gc/obsolete-pdf-retirement/' + 'f' * 64 + '.json'
        raw = self.encode({'mutations': [{'bucket': self.target[0], 'path': self.target[1],
                                         **preparation.proof(self.mutations[0]['before'])}]})
        self.store.put(gc.READER_ASSETS_BUCKET, path, raw)
        reference, report = self.build()
        result = references.verify(reference, report, self.plan)
        self.assertNotIn((gc.READER_ASSETS_BUCKET, path), reference.loaded_digests)
        self.assertEqual(result['blockers'], [])
        self.assertEqual(self.store.read_bytes(gc.READER_ASSETS_BUCKET, path), raw)
        self.assertIn('preserve original before bytes', result['audit_policy'])

    def test_expected_resources_blocks_any_of_seven_targets_without_a_direct_edge(self):
        for mutation in self.mutations[:7]:
            with self.subTest(path=mutation['path']):
                reference, report = self.build()
                ref = mutation['bucket'], mutation['path']
                reference.expected_resources[ref] = {'bucket': ref[0], 'path': ref[1],
                                                     'bytes': 42, 'role': 'provenance'}
                result = self.inspect(reference, report)
                self.assertFalse(result['incoming_bindings_absent'])
                self.assertTrue(any('expected resource binding' in b for b in result['blockers']))

    def test_planned_canonical_index_is_not_exempt_from_hash_binding(self):
        index = self.mutations[7]
        before = json.loads(index['before'])
        before['files'][0]['manifest_sha256'] = 'f' * 64
        index['before'] = index['after'] = self.encode(before)
        for row in before['files']:
            index['after'] = preparation.remove_reference(index['after'], 'previous_pdf', row['previous_pdf'])
        index['before_identity'] = preparation.proof(index['before'])
        target = self.mutations[0]
        old = json.loads(target['before'])
        old['manifest_sha256'] = 'f' * 64
        target['before'] = self.encode(old)
        target['after'] = preparation.remove_reference(target['before'], 'previous_pdf', old['previous_pdf'])
        target['before_identity'] = preparation.proof(target['before'])
        self.store.put(preparation.BUCKET, preparation.INDEX, index['before'])
        self.store.put(*self.target, target['before'])
        result = self.verify()
        self.assertFalse(result['incoming_bindings_absent'])
        self.assertTrue(any(preparation.INDEX in b for b in result['blockers']))


if __name__ == '__main__':
    unittest.main()
