import copy
import gzip
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts import expanded_ocr_retirement as expanded
from scripts import ccrd_ocr_exact_retirement as exact
from tests.test_ccrd_ocr_exact_retirement import MemoryStore, Fence


class ExpandedRetirementSimulationTests(unittest.TestCase):
    def fixture(self):
        store = MemoryStore()
        source = 'a' * 64
        root = f'objects/aa/{source}/' + 'b' * 16
        retained = f'derived/migration-20261007/{"c" * 32}/derived-manifest.json'
        text = root + '/text/page-000001.json.gz'
        old = b'ocr text'
        store.put(expanded.PAGES, text, old)
        provenance = exact.encode({'kind': 'pdf-derived-source', 'resources': []})
        store.put(expanded.PAGES, retained, provenance)
        current = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': 'd' * 64,
                   'resources': [], 'release': {}}
        store.put(expanded.CCRD, exact.CURRENT, current)
        intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': 'd' * 64,
                  'status': 'committed', 'resources': [{'bucket': expanded.PAGES, 'path': text,
                      'sha256': exact.sha(old), 'bytes': len(old), 'role': 'runtime'}]}
        store.put(expanded.CCRD, exact.INTENT, intent)
        ids = {'version': 1, 'kind': 'ccrd-ocr-id-registry', 'ids': {}, 'resources': []}
        store.put(expanded.CCRD, exact.IDS, ids)
        consumer = {'version': 1, 'kind': 'ccrd-ocr-consumer', 'resources': []}
        store.put(expanded.CCRD, exact.CONSUMER, consumer)
        store.put(expanded.ASSETS, 'reader-index/reader_assets.json.gz', gzip.compress(exact.encode({'v': 1, 'f': {}})))
        old_consumer = {'version': 1, 'kind': 'reader-gc-consumers', 'resources': []}
        store.put(expanded.ASSETS, exact.READER_CONSUMERS, old_consumer)
        rows = [{'bucket': expanded.PAGES, 'path': text, 'bytes': len(old), 'sha256': exact.sha(old)}]
        external = {'version': 1, 'kind': 'ccrd-ocr-retention', 'resources': [{
            'bucket': expanded.PAGES, 'path': retained, 'bytes': len(provenance),
            'sha256': exact.sha(provenance), 'role': 'provenance'}]}
        plan = {'expanded_targets': rows,
                'reader_consumer_sha256_before': store.digest(expanded.ASSETS, exact.READER_CONSUMERS),
                'control_hashes_before': {path: store.digest(expanded.CCRD, path) for path in
                                          (exact.CURRENT, exact.INTENT, exact.IDS, exact.CONSUMER)},
                'updates': {'external': external, 'intent': {**intent, 'status': 'retired', 'resources': []},
                            'ids': ids, 'reader_consumers': old_consumer}}
        return store, plan, text

    def test_simulation_reports_an_independent_retained_reference(self):
        store, plan, path = self.fixture()
        store.put(expanded.ASSETS, 'reader-index/independent-pin.json', {'resources': [{
            'bucket': expanded.PAGES, 'path': path}]})
        before = copy.deepcopy(store.objects)
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            result = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1)
        self.assertTrue(result['graph_complete'])
        self.assertEqual(result['retained_target_count'], 1)
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(store.objects, before)
        self.assertEqual(store.deletions, [])

    def test_unreadable_or_unknown_root_blocks_simulation(self):
        store, plan, path = self.fixture()
        store.put(expanded.ASSETS, 'reader-index/unknown.json', {'unexpected': True})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1)
        self.assertEqual(store.deletions, [])

    def test_source_document_path_never_matches_external_target_pattern(self):
        self.assertFalse(expanded.ALLOWED[expanded.PAGES].fullmatch(
            'derived/migration-20261007/' + 'c' * 32 + '/document.pdf'))
        self.assertFalse(expanded.ALLOWED[expanded.PAGES].fullmatch(
            'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/document.djvu'))
        self.assertFalse(expanded.ALLOWED[expanded.ASSETS].fullmatch(
            'documents/pdf/ps/' + 'a' * 64 + '/document.pdf'))

    def test_saved_plan_cannot_expand_processing_or_reader_scope(self):
        preserved = {'bucket': expanded.PAGES, 'root': expanded.PRESERVED_DERIVED_ROOT}
        first = {'resources': [{'bucket': expanded.PAGES, 'root': 'objects/aa/first'}, preserved]}
        second = {'status': 'uploaded', 'resources': [{'bucket': expanded.PAGES, 'root': 'objects/aa/first'}]}
        originals = {expanded.FIRST_PROCESSING: first, expanded.SECOND_PROCESSING: second}
        hashes = {p: exact.sha(expanded.encode(v)) for p, v in originals.items()}
        changes = {}
        for path, before in originals.items():
            keep = [preserved] if path == expanded.FIRST_PROCESSING else []
            after = {**before, 'resources': keep, 'withdrawal': {'kind': 'explicit-ocr-withdrawal',
                'inventory_sha256': expanded.INVENTORY_SHA256, 'removed_root_count': len(before['resources']) - len(keep),
                'original_sha256': hashes[path]}}
            if not keep:
                after['status'] = 'withdrawn'
            changes[path] = {'sha256_before': hashes[path], 'before': before,
                             'before_raw': expanded.encode(before).decode(), 'after': after}
        rows = [{'bucket': expanded.PAGES, 'path': 'objects/aa/first/target', 'bytes': 1, 'sha256': 'a' * 64}]
        plan = {'kind': 'ccrd-exact-ocr-retirement-plan', 'version': 1, 'generation': exact.GENERATION,
                'inventory_sha256': expanded.INVENTORY_SHA256, 'expanded_targets': rows, 'targets': [],
                'control_hashes_before': exact.INITIAL_HASHES, 'processing_updates': changes,
                'updates': {'external': {'resources': [{'bucket': expanded.PAGES,
                    'path': expanded.PRESERVED_DERIVED_ROOT + '/derived-manifest.json'}]},
                    'intent': {'status': 'retired', 'resources': []}, 'ids': {'status': 'withdrawn', 'resources': []},
                    'reader_consumers': {'resources': []}}}
        with patch.object(expanded, 'PROCESSING_HASHES', hashes):
            expanded.assert_same_inventory(plan, rows)
            damaged = copy.deepcopy(plan)
            damaged['updates']['external']['resources'] = []
            with self.assertRaisesRegex(ValueError, 'narrowly scoped'):
                expanded.assert_same_inventory(damaged, rows)
            damaged = copy.deepcopy(plan)
            damaged['processing_updates'][expanded.FIRST_PROCESSING]['after']['resources'] = []
            with self.assertRaisesRegex(ValueError, 'processing update'):
                expanded.assert_same_inventory(damaged, rows)
            damaged = copy.deepcopy(plan)
            damaged['updates']['reader_consumers']['resources'] = [{'bucket': rows[0]['bucket'], 'path': rows[0]['path']}]
            with self.assertRaisesRegex(ValueError, 'still pins'):
                expanded.assert_same_inventory(damaged, rows)

    def test_expanded_prepare_requests_fresh_consumer_snapshot(self):
        external = [{'bucket': expanded.PAGES,
                     'path': expanded.PRESERVED_DERIVED_ROOT + '/derived-manifest.json',
                     'bytes': 1, 'sha256': 'a' * 64, 'role': 'provenance'}]
        target = {'bucket': expanded.PAGES,
                  'path': 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/ocr/page-000001.json.gz'}
        external.extend({'bucket': expanded.PAGES, 'path': target['path'], 'bytes': 1,
                         'sha256': 'a' * 64, 'role': 'runtime'} for _ in range(2530))
        prior = {'updates': {'external': {'version': 1, 'kind': 'ccrd-ocr-retention',
                                          'resources': external},
                             'withdrawal': {'preserved_reader_provenance': {'resources': 2531}}}}
        with patch.object(exact, 'create_plan', return_value=prior) as create, \
                patch.object(expanded, 'prepare_processing_updates', return_value={}), \
                patch.object(expanded.lifecycle, 'references'):
            result = expanded.prepare(None, None, [target])
        self.assertIsNone(create.call_args.kwargs['expected_reader_consumer_sha'])
        self.assertEqual(result['preserved_external_resources'], 1)
        self.assertEqual(result['updates']['withdrawal']['preserved_reader_provenance']['resources'], 1)
        self.assertEqual(result['updates']['withdrawal']['retired_external_resources'], 2530)

    def test_external_sibling_bundle_pin_blocks_target_retirement(self):
        bucket = expanded.PAGES
        parent = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/ocr/'
        target = parent + 'page-000001.json.gz'
        unrelated_sibling = parent + 'page-000002.json.gz'
        origin = (expanded.ASSETS, 'reader-index/live-consumer.json')
        graph = SimpleNamespace(direct={origin: {(bucket, unrelated_sibling)}}, trees=set(), tree_origins={})
        hits = expanded.external_target_origins(graph, [{'bucket': bucket, 'path': target}])
        self.assertEqual(hits[origin], {(bucket, target)})

    def test_target_internal_origin_does_not_imply_external_ownership(self):
        bucket = expanded.PAGES
        origin = (bucket, 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/text-layer-manifest.json')
        target = (bucket, 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/text/page-000001.json.gz')
        graph = SimpleNamespace(direct={origin: {target}}, trees=set(), tree_origins={})
        rows = [{'bucket': b, 'path': p} for b, p in (origin, target)]
        self.assertEqual(expanded.external_target_origins(graph, rows), {})

    def test_unknown_tree_owner_blocks_conservatively(self):
        bucket = expanded.PAGES
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        target = root + '/text/page-000001.json.gz'
        graph = SimpleNamespace(direct={}, trees={(bucket, root)}, tree_origins={})
        hits = expanded.external_target_origins(graph, [{'bucket': bucket, 'path': target}])
        self.assertEqual(hits[(bucket, root + '/<unknown-tree-owner>')], {(bucket, target)})

    def test_internal_tree_owner_is_excluded_but_independent_owner_is_not(self):
        bucket = expanded.PAGES
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        target = root + '/text/page-000001.json.gz'
        origin = (bucket, root + '/text-layer-manifest.json')
        rows = [{'bucket': b, 'path': p} for b, p in (origin, (bucket, target))]
        graph = SimpleNamespace(direct={}, trees={(bucket, root)},
                                tree_origins={(bucket, root): {origin}})
        self.assertEqual(expanded.external_target_origins(graph, rows), {})
        external = (expanded.ASSETS, 'reader-index/audit.json')
        graph.tree_origins[bucket, root].add(external)
        self.assertEqual(expanded.external_target_origins(graph, rows)[external],
                         {origin, (bucket, target)})

    def test_same_tree_records_every_owner_even_after_first_expansion(self):
        store = MemoryStore()
        bucket = expanded.PAGES
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        one, two = root + '/ocr/page-000001.json.gz', root + '/ocr/page-000002.json.gz'
        store.put(bucket, one, b'one')
        store.put(bucket, two, b'two')
        graph = expanded.OriginReferenceGraph(store)
        for domain in expanded.graph.BUCKETS:
            graph.set_inventory(domain, set(store.objects[domain]))
        first = (bucket, root + '/text-review-manifest.json')
        second = (expanded.ASSETS, 'reader-index/live-consumer.json')
        graph.origin = first
        graph.mark(bucket, one)
        graph.origin = second
        graph.mark(bucket, two)
        self.assertEqual(graph.tree_origins[bucket, root + '/ocr'], {first, second})
        self.assertIn(second, expanded.external_target_origins(
            graph, [{'bucket': bucket, 'path': one}, {'bucket': first[0], 'path': first[1]}]))

    def test_missing_tree_tracking_never_silently_grants_authority(self):
        with self.assertRaisesRegex(ValueError, 'origins were not recorded'):
            expanded.external_target_origins(SimpleNamespace(direct={}, trees=set()), [])

    def test_attestation_rejects_unread_or_missing_bucket_roots(self):
        fake = SimpleNamespace(files={b: set() for b in expanded.graph.BUCKETS},
                               root_hashes={}, retired_roots=set())
        fake.files[expanded.ASSETS].add('reader-index/a.json')
        report = {'graph_complete': True, 'blockers': [], 'ambiguous_paths': []}
        with self.assertRaisesRegex(ValueError, 'did not read every active root'):
            expanded.attest_root_fingerprints(report, fake)
        fake.root_hashes[(expanded.ASSETS, 'reader-index/a.json')] = 'a' * 64
        proof = expanded.attest_root_fingerprints(report, fake)
        self.assertEqual(len(proof['root_members']), 1)
        self.assertEqual(proof['root_sha256'][0]['sha256'], 'a' * 64)
        fake.files.pop(expanded.CCRD)
        with self.assertRaisesRegex(ValueError, 'complete four-bucket'):
            expanded.attest_root_fingerprints(report, fake)

    def test_target_internal_manifest_cycle_is_not_a_live_root(self):
        store, plan, path = self.fixture()
        root = path.rsplit('/text/', 1)[0]
        manifest = root + '/text-layer-manifest.json'
        doc = {'version': 1, 'kind': 'pdf-text-layer-index', 'resources': [
            {'bucket': expanded.PAGES, 'path': path}]}
        store.put(expanded.PAGES, manifest, doc)
        raw = store.read_bytes(expanded.PAGES, manifest)
        plan['expanded_targets'].append({'bucket': expanded.PAGES, 'path': manifest,
                                        'bytes': len(raw), 'sha256': exact.sha(raw)})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            report = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True)
        self.assertTrue(report['target_exclusivity_proven'])
        self.assertEqual(report['retained_target_count'], 0)
        self.assertEqual(store.deletions, [])

    def test_external_root_pin_still_blocks_without_reading_target_manifest(self):
        store, plan, path = self.fixture()
        root = path.rsplit('/text/', 1)[0]
        manifest = root + '/text-layer-manifest.json'
        store.put(expanded.PAGES, manifest, {'kind': 'pdf-text-layer-index', 'resources': []})
        raw = store.read_bytes(expanded.PAGES, manifest)
        plan['expanded_targets'].append({'bucket': expanded.PAGES, 'path': manifest,
                                        'bytes': len(raw), 'sha256': exact.sha(raw)})
        store.put(expanded.ASSETS, 'reader-index/independent-pin.json', {'resources': [{
            'bucket': expanded.PAGES, 'path': manifest}]})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            report = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True)
        self.assertFalse(report['target_exclusivity_proven'])
        self.assertGreater(report['retained_target_count'], 0)
        self.assertTrue(report['non_target_origins'])
        self.assertEqual(store.deletions, [])

    def test_retained_directory_root_still_blocks_internal_scc_retirement(self):
        store, plan, path = self.fixture()
        directory = path.rsplit('/text/', 1)[0]
        store.put(expanded.ASSETS, 'reader-index/independent-tree.json', {'resources': [{
            'bucket': expanded.PAGES, 'root': directory}]})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            report = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True)
        self.assertFalse(report['target_exclusivity_proven'])
        self.assertEqual(report['retained_target_count'], 1)
        self.assertTrue(report['non_target_origins'])
        self.assertEqual(store.deletions, [])

    def test_processing_roots_keep_only_non_target_derived_tree(self):
        store = MemoryStore()
        target = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/ocr/page-000001.json.gz'
        root = target.rsplit('/ocr/', 1)[0]
        retained_root = expanded.PRESERVED_DERIVED_ROOT
        store.put(expanded.PAGES, target, b'target')
        store.put(expanded.PAGES, retained_root + '/document.pdf', b'keep')
        first = {'version': 1, 'kind': 'reader-processing-roots',
                 'operation': 'ccrd-body-evidence-upload', 'status': 'uploaded',
                 'release_policy': 'explicit-publisher-handoff',
                 'resources': [{'bucket': expanded.PAGES, 'root': root},
                               {'bucket': expanded.PAGES, 'root': retained_root}]}
        first_raw = store.put(expanded.ASSETS, expanded.FIRST_PROCESSING, first)
        second = {'version': 1, 'kind': 'reader-processing-roots',
                  'operation': 'ccrd-body-evidence-upload', 'status': 'uploaded',
                  'release_policy': 'explicit-publisher-handoff', 'candidate_generation': exact.GENERATION,
                  'previous_protection': expanded.FIRST_PROCESSING,
                  'previous_protection_sha256': exact.sha(first_raw),
                  'resources': [{'bucket': expanded.PAGES, 'root': root}]}
        second_raw = store.put(expanded.ASSETS, expanded.SECOND_PROCESSING, second)
        hashes = {expanded.FIRST_PROCESSING: exact.sha(first_raw),
                  expanded.SECOND_PROCESSING: exact.sha(second_raw)}
        with patch.object(expanded, 'PROCESSING_HASHES', hashes):
            result = expanded.prepare_processing_updates(
                store, [{'bucket': expanded.PAGES, 'path': target}])
        self.assertEqual(result[expanded.FIRST_PROCESSING]['after']['resources'],
                         [{'bucket': expanded.PAGES, 'root': retained_root}])
        self.assertEqual(result[expanded.SECOND_PROCESSING]['after']['resources'], [])
        self.assertEqual(result[expanded.SECOND_PROCESSING]['after']['status'], 'withdrawn')
        self.assertNotIn('removed_roots', result[expanded.FIRST_PROCESSING]['after']['withdrawal'])
        self.assertEqual(store.deletions, [])

    def test_mixed_processing_tree_must_not_be_partially_retired(self):
        store = MemoryStore()
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        target = root + '/ocr/page-000001.json.gz'
        store.put(expanded.PAGES, target, b'target')
        store.put(expanded.PAGES, root + '/document.pdf', b'shared')
        store.put(expanded.PAGES, expanded.PRESERVED_DERIVED_ROOT + '/document.pdf', b'keep')
        first = {'version': 1, 'kind': 'reader-processing-roots',
                 'operation': 'ccrd-body-evidence-upload', 'status': 'uploaded',
                 'release_policy': 'explicit-publisher-handoff',
                 'resources': [{'bucket': expanded.PAGES, 'root': root},
                               {'bucket': expanded.PAGES, 'root': expanded.PRESERVED_DERIVED_ROOT}]}
        first_raw = store.put(expanded.ASSETS, expanded.FIRST_PROCESSING, first)
        second = {**first, 'resources': [{'bucket': expanded.PAGES, 'root': root}],
                  'candidate_generation': exact.GENERATION,
                  'previous_protection': expanded.FIRST_PROCESSING,
                  'previous_protection_sha256': exact.sha(first_raw)}
        second_raw = store.put(expanded.ASSETS, expanded.SECOND_PROCESSING, second)
        with patch.object(expanded, 'PROCESSING_HASHES', {
                expanded.FIRST_PROCESSING: exact.sha(first_raw), expanded.SECOND_PROCESSING: exact.sha(second_raw)}):
            with self.assertRaisesRegex(ValueError, 'no exact retired resources'):
                expanded.prepare_processing_updates(store, [{'bucket': expanded.PAGES, 'path': target}])
        self.assertEqual(store.deletions, [])

    def test_actual_deletion_requires_a_fresh_zero_reachable_graph(self):
        rows = [{'bucket': expanded.PAGES, 'path': 'some-target'}]
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            expanded.require_actual_zero_proof({'graph_complete': True,
                'target_exclusivity_proven': True, 'target_count': 1,
                'retained_target_count': 0, 'tree_roots_without_origin': 0}, rows)
        proof = {'graph_complete': True, 'actual_retired_controls_checked': True,
                 'internal_target_manifests_omitted': True, 'target_exclusivity_proven': True,
                 'target_count': 1, 'retained_target_count': 0, 'non_target_origins': [],
                 'tree_roots_without_origin': 0}
        expanded.require_actual_zero_proof(proof, rows)
        proof['non_target_origins'] = [{'path': 'reader-index/live.json'}]
        with self.assertRaisesRegex(ValueError, 'does not authorize'):
            expanded.require_actual_zero_proof(proof, rows)

    def test_actual_graph_reads_real_controls_and_never_hides_recreated_pointer(self):
        store, plan, path = self.fixture()
        # Install only the planned control replacements in the in-memory store.
        for bucket, name, raw in ((expanded.CCRD, expanded.PRESERVED, expanded.encode(plan['updates']['external'])),
                                  (expanded.CCRD, exact.INTENT, expanded.encode(plan['updates']['intent'])),
                                  (expanded.CCRD, exact.IDS, expanded.encode(plan['updates']['ids'])),
                                  (expanded.ASSETS, exact.READER_CONSUMERS,
                                   expanded.encode(plan['updates']['reader_consumers']))):
            store.put(bucket, name, raw)
        store.objects[expanded.CCRD].pop(exact.CURRENT)
        with patch.object(exact, 'verify_reference_retirement'):
            result = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True, actual=True)
        self.assertTrue(result['actual_retired_controls_checked'])
        self.assertTrue(result['target_exclusivity_proven'])
        store.put(expanded.CCRD, exact.CURRENT, {'kind': 'unexpected'})
        with self.assertRaisesRegex(ValueError, 'pointer still exists'):
            expanded.simulate(store, plan, Fence(), actual=True)

    def test_actual_scan_fails_if_pointer_reappears_after_first_check(self):
        store, plan, _ = self.fixture()
        for bucket, name, raw in ((expanded.CCRD, expanded.PRESERVED, expanded.encode(plan['updates']['external'])),
                                  (expanded.CCRD, exact.INTENT, expanded.encode(plan['updates']['intent'])),
                                  (expanded.CCRD, exact.IDS, expanded.encode(plan['updates']['ids'])),
                                  (expanded.ASSETS, exact.READER_CONSUMERS,
                                   expanded.encode(plan['updates']['reader_consumers']))):
            store.put(bucket, name, raw)
        store.objects[expanded.CCRD].pop(exact.CURRENT)
        original = store.list_files
        def inject(bucket, prefixes):
            if bucket == expanded.CCRD and not store.objects[bucket].get(exact.CURRENT):
                store.put(bucket, exact.CURRENT, {'kind': 'unexpected'})
            return original(bucket, prefixes)
        store.list_files = inject
        with patch.object(exact, 'verify_reference_retirement'):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                  omit_internal_target_manifests=True, actual=True)

    def test_exact_targets_and_partial_submission_reconcile(self):
        store = MemoryStore()
        rows = []
        for n in range(3):
            data = bytes([n + 1])
            row = {'bucket': expanded.PAGES, 'path': f'objects/aa/target-{n}',
                   'bytes': len(data), 'sha256': exact.sha(data)}
            rows.append(row)
            store.put(row['bucket'], row['path'], data)
        self.assertEqual(expanded.verify_exact_targets(store, rows, workers=2), 3)
        store.delete(expanded.PAGES, [rows[0]['path']])
        with self.assertRaisesRegex(ValueError, 'without a durable'):
            expanded.reconcile_targets(store, rows, {'batches': []})
        prior = {'batches': [{'bucket': expanded.PAGES, 'paths': [rows[0]['path'], rows[1]['path']],
                              'status': 'submitted'}]}
        result = expanded.reconcile_targets(store, rows, prior)
        self.assertEqual((result['present'], result['confirmed_absent']), (2, 1))
        receipts = []
        result = expanded.delete_approved_batches(store, rows, Fence(), None,
                    lambda phase, report: receipts.append((phase, copy.deepcopy(report))),
                    batch_size=2, progress=prior)
        self.assertEqual(result['confirmed_deleted_objects'], 3)
        self.assertEqual(result['confirmed_deleted_bytes'], 3)
        self.assertEqual(len(result['batches']), 2)
        self.assertEqual({entry['status'] for entry in result['batches']}, {'verified-absent'})

    def test_read_failure_is_not_classified_as_deleted(self):
        class Unavailable(MemoryStore):
            def read_bytes(self, bucket, path):
                raise TimeoutError('S3 unavailable')
        row = {'bucket': expanded.PAGES, 'path': 'objects/aa/target', 'bytes': 1, 'sha256': 'a' * 64}
        with self.assertRaises(TimeoutError):
            expanded.reconcile_targets(Unavailable(), [row], {'batches': []})

    def test_root_recheck_rejects_changed_bytes_and_new_root(self):
        store = MemoryStore()
        root = 'reader-index/test-root.json'
        raw = store.put(expanded.ASSETS, root, {'kind': 'reader-gc-consumers', 'resources': []})
        rows = [{'bucket': expanded.PAGES, 'path': 'objects/aa/target'}]
        proof = {'graph_complete': True, 'actual_retired_controls_checked': True,
                 'internal_target_manifests_omitted': True, 'target_exclusivity_proven': True,
                 'target_count': 1, 'retained_target_count': 0, 'non_target_origins': [],
                 'tree_roots_without_origin': 0,
                 'root_members': [{'bucket': expanded.ASSETS, 'path': root}],
                 'root_sha256': [{'bucket': expanded.ASSETS, 'path': root, 'sha256': exact.sha(raw)}],
                 'retired_root_members': []}
        plan = {'expanded_targets': rows, 'processing_updates': {}, 'base_generation': 'base'}
        with patch.object(exact, 'verify_reference_retirement'), patch.object(exact, 'current_status'):
            expanded.recheck_roots_and_controls(store, plan, proof, Fence(), None)
            store.put(expanded.ASSETS, root, {'kind': 'different'})
            with self.assertRaisesRegex(ValueError, 'root changed'):
                expanded.recheck_roots_and_controls(store, plan, proof, Fence(), None)
            store.put(expanded.ASSETS, root, raw)
            store.put(expanded.ASSETS, 'reader-index/new-root.json', {'kind': 'unexpected'})
            with self.assertRaisesRegex(ValueError, 'root inventory changed'):
                expanded.recheck_roots_and_controls(store, plan, proof, Fence(), None)

    def test_exact_delete_rejects_false_not_found_and_keeps_partial_receipt(self):
        class StickyStore(MemoryStore):
            def delete(self, bucket, paths):
                self.deletions.extend((bucket, path) for path in paths)
        store = StickyStore()
        row = {'bucket': expanded.PAGES, 'path': 'objects/aa/target', 'bytes': 1, 'sha256': exact.sha(b'x')}
        store.put(expanded.PAGES, row['path'], b'x')
        receipts = []
        with self.assertRaisesRegex(ValueError, 'remains directly readable'):
            expanded.delete_approved_batches(store, [row], Fence(), None,
                lambda phase, report: receipts.append((phase, copy.deepcopy(report))))
        self.assertEqual(receipts[-1][0], 'deletion-batch-submitted')
        self.assertEqual(receipts[-1][1]['confirmed_deleted_objects'], 0)


if __name__ == '__main__':
    unittest.main()
