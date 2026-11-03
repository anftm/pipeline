import copy
import hashlib
import json
import unittest

from scripts import chm_subset_retirement as subset
from scripts import reader_gc_graph as graph

A = subset.ASSETS


def epub(number):
    return f'native/ebook/chm/{number:064x}/document.epub'


def manifest(number):
    return f'chapters/ebook/chm/{number:064x}/{number:016x}/chapter-manifest.json'


def row(number):
    return {'bucket': A, 'path': epub(number), 'bytes': number + 1,
            'sha256': hashlib.sha256(str(number).encode()).hexdigest()}


def mapping(path):
    return {'b': A, 'cb': A, 'c': path, 'p': path, 'm': 'e', 's': 2}


class ChmSubsetRetirementTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(i) for i in range(1, 6)]
        self.batches = [{'bucket': A, 'status': 'verified-absent', 'paths': [epub(1)]}]
        self.selected = copy.deepcopy(self.rows[1:4])
        self.withheld = copy.deepcopy(self.rows[4:])
        self.key = 'repo\0book.chm'
        self.history = {'old-v3': {'v': 1, 'f': {self.key: {**mapping(manifest(7)), 'f': epub(2)}}}}
        self.current = {'v': 1, 'f': {self.key: mapping(manifest(8))}}
        self.index = {'files': [{'key': self.key, 'manifest': manifest(8), 'bucket': A,
                                 'mode': 'chapter-stream', 'source_sha256': f'{8:064x}',
                                 'root': manifest(8).rsplit('/', 1)[0],
                                 'search_index': manifest(8).rsplit('/', 1)[0] + '/epub-search-index.json.gz',
                                 'chapter_count': 1}]}
        self.evidence = {manifest(7): 'a' * 64, manifest(8): 'b' * 64}

    def partition(self):
        return subset.verify_exact_partition(self.rows, self.batches, self.selected, self.withheld)

    def checklist(self):
        return subset.verify_route_limit_inputs(self.rows, [row(2)], self.history,
                                                self.current, self.index, self.evidence)

    def test_valid_partition_is_diagnostic_only(self):
        result = self.partition()
        self.assertEqual((result['old_receipts'], result['selected'], result['withheld']), (1, 3, 1))
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(result['remote_mutations'], 0)

    def test_foreign_or_changed_row_is_rejected(self):
        self.selected[0]['sha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, 'subset row'):
            self.partition()

    def test_duplicate_or_missing_target_is_rejected(self):
        self.selected.append(copy.deepcopy(self.selected[0]))
        with self.assertRaisesRegex(ValueError, 'subset row'):
            self.partition()
        self.selected.pop()
        self.selected.pop()
        with self.assertRaisesRegex(ValueError, 'partition'):
            self.partition()

    def test_overlap_with_old_receipt_is_rejected(self):
        self.selected.append(copy.deepcopy(self.rows[0]))
        with self.assertRaisesRegex(ValueError, 'subset row'):
            self.partition()

    def test_unverified_or_duplicate_old_receipts_are_rejected(self):
        self.batches[0]['status'] = 'submitted'
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.partition()
        self.batches[0]['status'] = 'verified-absent'
        self.batches.append(copy.deepcopy(self.batches[0]))
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.partition()

    def test_cross_source_fallback_uses_route_not_target_sha(self):
        result = self.checklist()
        self.assertEqual(set(result['manifests']), {manifest(7), manifest(8)})
        self.assertNotEqual(epub(2).split('/')[3], manifest(7).split('/')[3])
        self.assertEqual(result['routes'][epub(2)]['keys'], [self.key])
        self.assertFalse(result['deletion_authorized'])

    def test_missing_historical_route_is_not_safe(self):
        self.history['old-v3']['f'].clear()
        with self.assertRaisesRegex(ValueError, 'historical fallback'):
            self.checklist()

    def test_missing_old_or_new_evidence_rejected(self):
        for path in (manifest(7), manifest(8)):
            with self.subTest(path=path):
                previous = self.evidence.pop(path)
                with self.assertRaises(ValueError):
                    self.checklist()
                self.evidence[path] = previous

    def test_index_or_current_route_change_rejected(self):
        self.index['files'][0]['root'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'current CHM route or index'):
            self.checklist()

    def test_other_historical_version_also_requires_evidence(self):
        self.history['another'] = {'v': 1, 'f': {self.key: {**mapping(manifest(9)), 'f': epub(2)}}}
        with self.assertRaisesRegex(ValueError, 'not evidenced'):
            self.checklist()

    def test_missing_selected_source_from_prior_v3_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, 'historical fallback'):
            subset.verify_route_limit_inputs(self.rows, [row(3)], self.history,
                                              self.current, self.index, self.evidence)

    def graph_proof(self):
        return {'mode': 'actual', 'graph_complete': True, 'buckets': list(graph.BUCKETS),
                'root_recheck_complete': True, 'reader_acceptance_complete': True,
                'consumer_receipts': {'hf': True, 'pages': True}, 'rollback_alias_count': 5,
                'blockers': [], 'ambiguous_paths': [], 'remote_mutations': 0,
                'protected_targets': [{'bucket': A, 'path': self.withheld[0]['path']}],
                'unreferenced_targets': [{'bucket': A, 'path': item['path']} for item in self.selected],
                'root_sha256': [{'bucket': A, 'path': 'reader-index/root.json', 'sha256': 'a' * 64}],
                'closure_sha256': [{'bucket': A, 'path': 'reader-index/root.json', 'sha256': 'a' * 64}]}

    def test_graph_requires_exact_protected_and_selected_sets(self):
        proof = self.graph_proof()
        result = subset.verify_subset_graph_evidence(proof, self.selected, self.withheld)
        self.assertEqual((result['selected'], result['withheld']), (3, 1))
        self.assertFalse(result['deletion_authorized'])
        for field in ('protected_targets', 'unreferenced_targets'):
            with self.subTest(field=field):
                bad = copy.deepcopy(proof)
                bad[field].pop()
                with self.assertRaisesRegex(ValueError, 'match selected'):
                    subset.verify_subset_graph_evidence(bad, self.selected, self.withheld)

    def test_graph_requires_four_buckets_both_sites_aliases_and_no_blockers(self):
        changes = {'mode': 'simulated', 'buckets': list(graph.BUCKETS)[:3], 'graph_complete': False,
                   'consumer_receipts': {'hf': True, 'pages': False}, 'rollback_alias_count': 4,
                   'blockers': ['missing'], 'ambiguous_paths': ['ambiguous']}
        for field, value in changes.items():
            with self.subTest(field=field):
                proof = self.graph_proof()
                proof[field] = value
                with self.assertRaisesRegex(ValueError, 'incomplete'):
                    subset.verify_subset_graph_evidence(proof, self.selected, self.withheld)

    def test_graph_sha_must_bind_root_to_closure_without_duplicates(self):
        proof = self.graph_proof()
        proof['closure_sha256'][0]['sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'not bound'):
            subset.verify_subset_graph_evidence(proof, self.selected, self.withheld)
        proof = self.graph_proof()
        proof['root_sha256'].append(copy.deepcopy(proof['root_sha256'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            subset.verify_subset_graph_evidence(proof, self.selected, self.withheld)

    def test_multiple_targets_can_share_exact_historical_key(self):
        self.history['second'] = {'v': 1, 'f': {self.key: {**mapping(manifest(7)), 'f': epub(3)}}}
        result = subset.verify_route_limit_inputs(self.rows, [row(2), row(3)], self.history,
                                                   self.current, self.index, self.evidence)
        self.assertEqual(set(result['routes']), {epub(2), epub(3)})
        self.assertEqual(len(result['manifests']), 2)

    def test_supplied_manifest_bytes_must_be_exact_and_reader_compatible(self):
        payload = {'version': 1, 'kind': 'epub-chapters',
                   'chapters': [{'index': 1, 'path': 'chapters/chapter-0001.xhtml', 'title': 'A',
                                 'bytes': 100, 'sha256': 'a' * 64}]}
        raw = json.dumps(payload).encode()
        expected = {manifest(7): hashlib.sha256(raw).hexdigest()}
        result = subset.verify_manifest_bytes(expected, {manifest(7): raw})
        self.assertEqual(len(result['manifests']), 1)
        self.assertFalse(result['deletion_authorized'])
        for damaged in ({}, {manifest(7): raw + b' '}, {manifest(7): raw, manifest(8): raw}):
            with self.subTest(damaged=list(damaged)), self.assertRaises(ValueError):
                subset.verify_manifest_bytes(expected, damaged)
        payload['chapters'][0]['bytes'] = 8 * 1024 * 1024 + 1
        too_large = json.dumps(payload).encode()
        with self.assertRaisesRegex(ValueError, 'Reader limits'):
            subset.verify_manifest_bytes({manifest(7): hashlib.sha256(too_large).hexdigest()},
                                         {manifest(7): too_large})

    def test_manifest_matching_hash_still_needs_valid_schema(self):
        raw = b'{"kind":"wrong"}'
        with self.assertRaisesRegex(ValueError, 'Reader limits'):
            subset.verify_manifest_bytes({manifest(7): hashlib.sha256(raw).hexdigest()}, {manifest(7): raw})

    def cursor(self):
        return {'receipts': [], 'pending': None, 'subset_complete': False,
                'original_complete': False, 'producer_restore_authorized': False}

    def test_subset_cursor_preserves_exact_deterministic_order(self):
        state = self.cursor()
        state['receipts'] = [{'bucket': A, 'paths': [epub(2)], 'status': 'verified-absent'}]
        state['pending'] = {'bucket': A, 'paths': [epub(3), epub(4)], 'status': 'submitted'}
        result = subset.verify_subset_cursor(self.selected, self.withheld, state)
        self.assertEqual((result['verified'], result['pending']), (1, 2))
        self.assertFalse(result['original_complete'])
        self.assertFalse(result['producer_restore_authorized'])

    def test_subset_cursor_rejects_withheld_or_out_of_order_intent(self):
        for pending in ([epub(5)], [epub(3)], [epub(2), epub(2)]):
            with self.subTest(pending=pending):
                state = self.cursor()
                state['pending'] = {'bucket': A, 'paths': pending, 'status': 'submitted'}
                with self.assertRaisesRegex(ValueError, 'pending intent'):
                    subset.verify_subset_cursor(self.selected, self.withheld, state)

    def test_subset_cursor_cannot_claim_original_completion_or_restore(self):
        for field in ('original_complete', 'producer_restore_authorized'):
            with self.subTest(field=field):
                state = self.cursor()
                state[field] = True
                with self.assertRaisesRegex(ValueError, 'original transaction claim'):
                    subset.verify_subset_cursor(self.selected, self.withheld, state)

    def test_subset_complete_requires_every_receipt_no_pending(self):
        state = self.cursor()
        state['subset_complete'] = True
        with self.assertRaisesRegex(ValueError, 'missing or pending'):
            subset.verify_subset_cursor(self.selected, self.withheld, state)
        state['receipts'] = [{'bucket': A, 'paths': [epub(2), epub(3), epub(4)], 'status': 'verified-absent'}]
        result = subset.verify_subset_cursor(self.selected, self.withheld, state)
        self.assertTrue(result['subset_complete'])
        self.assertFalse(result['original_complete'])

    def closure_proofs(self):
        retention = {'bucket': A, 'path': 'reader-index/chm-epub-withdrawal-retention.json'}
        other = {'bucket': A, 'path': 'reader-index/other.json'}
        base = {'mode': 'actual', 'graph_complete': True, 'blockers': [], 'ambiguous_paths': [],
                'root_recheck_complete': True, 'reader_acceptance_complete': True, 'remote_mutations': 0,
                'target_count': 972, 'retained_targets': 0, 'origins': [], 'root_count': 2,
                'root_members': [retention, other], 'retired_roots': [],
                'root_sha256': [{**retention, 'sha256': 'a' * 64}, {**other, 'sha256': 'b' * 64}],
                'closure_sha256': [{**retention, 'sha256': 'a' * 64}, {**other, 'sha256': 'b' * 64}]}
        after = copy.deepcopy(base)
        after['root_sha256'][0]['sha256'] = 'c' * 64
        after['closure_sha256'][0]['sha256'] = 'c' * 64
        return base, after, (A, retention['path'])

    def test_closure_allows_only_reviewed_retention_sha_difference(self):
        before, after, ref = self.closure_proofs()
        result = subset.compare_subset_closure(before, after, old_retention_ref=ref,
                                                planned_retention_sha256='c' * 64)
        self.assertEqual(result['root_members'], 2)
        self.assertFalse(result['deletion_authorized'])

    def test_closure_rejects_unreviewed_changes(self):
        before, after, ref = self.closure_proofs()
        for change in ('root', 'closure', 'member', 'unsafe', 'wrong-retention'):
            with self.subTest(change=change):
                bad = copy.deepcopy(after)
                if change == 'root': bad['root_sha256'][1]['sha256'] = 'd' * 64
                if change == 'closure': bad['closure_sha256'][1]['sha256'] = 'd' * 64
                if change == 'member': bad['root_members'].pop()
                if change == 'unsafe': bad['graph_complete'] = False
                if change == 'wrong-retention': bad['closure_sha256'][0]['sha256'] = 'd' * 64
                with self.assertRaises(ValueError):
                    subset.compare_subset_closure(before, bad, old_retention_ref=ref,
                                                   planned_retention_sha256='c' * 64)

    def test_closure_rejects_growth_and_duplicate_entries(self):
        before, after, ref = self.closure_proofs()
        after['closure_sha256'].append({'bucket': A, 'path': 'new-resource', 'sha256': 'd' * 64})
        with self.assertRaisesRegex(ValueError, 'changed or grew'):
            subset.compare_subset_closure(before, after, old_retention_ref=ref, planned_retention_sha256='c' * 64)
        before, after, ref = self.closure_proofs()
        after['root_sha256'].append(copy.deepcopy(after['root_sha256'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            subset.compare_subset_closure(before, after, old_retention_ref=ref, planned_retention_sha256='c' * 64)

    def test_closure_requires_every_active_root_and_its_bound_hash(self):
        before, after, ref = self.closure_proofs()
        before['root_sha256'].pop()
        after['root_sha256'].pop()
        before['root_count'] = 1
        with self.assertRaisesRegex(ValueError, 'roots or retention'):
            subset.compare_subset_closure(before, after, old_retention_ref=ref, planned_retention_sha256='c' * 64)
        before, after, ref = self.closure_proofs()
        before['closure_sha256'].pop()
        after['closure_sha256'].pop()
        with self.assertRaisesRegex(ValueError, 'changed or grew'):
            subset.compare_subset_closure(before, after, old_retention_ref=ref, planned_retention_sha256='c' * 64)

    def test_closure_does_not_accept_retired_or_invented_retention_root(self):
        before, after, ref = self.closure_proofs()
        after['retired_roots'] = [{'bucket': A, 'path': ref[1]}]
        with self.assertRaisesRegex(ValueError, 'roots or retention'):
            subset.compare_subset_closure(before, after, old_retention_ref=ref, planned_retention_sha256='c' * 64)

    def test_closure_requires_original_zero_reference_proof(self):
        for field, value in (('target_count', 352), ('retained_targets', 1), ('origins', [{'path': 'x'}]),
                             ('root_count', 3)):
            with self.subTest(field=field):
                before, after, ref = self.closure_proofs()
                before[field] = value
                with self.assertRaisesRegex(ValueError, 'original all-target'):
                    subset.compare_subset_closure(before, after, old_retention_ref=ref,
                                                  planned_retention_sha256='c' * 64)

    def test_overlay_comparison_never_creates_actual_deletion_proof(self):
        from scripts import chm_subset_protection as protection
        manifest_path = manifest(7)
        retention = {'inventory_sha256': subset.chm.preview.INVENTORY_SHA256,
                     'kind': 'reader-chm-epub-withdrawal-retention', 'version': 1,
                     'resources': [{'bucket': A, 'path': manifest_path, 'bytes': 1,
                                    'sha256': 'e' * 64, 'role': 'runtime'}]}
        raw = json.dumps(retention).encode()
        prepared = protection.prepare(raw, self.withheld)
        before, after, ref = self.closure_proofs()
        before['root_sha256'][0]['sha256'] = before['closure_sha256'][0]['sha256'] = prepared['before_sha256']
        after['root_sha256'][0]['sha256'] = after['closure_sha256'][0]['sha256'] = prepared['after_sha256']
        after['mode'], after['deletion_authorized'] = 'hypothetical-overlay', False
        result = subset.compare_subset_overlay_graph(before, after, self.withheld, before_raw=raw)
        self.assertEqual(result['mode'], 'hypothetical-overlay-only')
        self.assertFalse(result['deletion_authorized'])
        with self.assertRaisesRegex(ValueError, 'claims authorization'):
            subset.compare_subset_overlay_graph(before, {**after, 'deletion_authorized': True},
                                                self.withheld, before_raw=raw)
        with self.assertRaisesRegex(ValueError, 'before bytes'):
            subset.compare_subset_overlay_graph(before, after, self.withheld, before_raw=raw + b' ')
        before, after, ref = self.closure_proofs()
        before['root_members'].pop(0)
        after['root_members'].pop(0)
        with self.assertRaisesRegex(ValueError, 'roots or retention'):
            subset.compare_subset_closure(before, after, old_retention_ref=ref, planned_retention_sha256='c' * 64)


if __name__ == '__main__':
    unittest.main(verbosity=2)
