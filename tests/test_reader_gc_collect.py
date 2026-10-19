import gzip
import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from scripts import reader_gc_collect as collector, reader_gc_graph as graph, reader_lifecycle
from scripts.reader_gc_fence import GitHubProducerFence, producer_repositories, assert_collector_context
from scripts.reader_gc_stream import graph_json
from tests.test_reader_gc_graph import MemoryStore, ASSETS, PDF, INPUT, SIDECAR


class Store(MemoryStore):
    def __init__(self):
        super().__init__()
        self.object_sizes = {}

    def read_bytes(self, bucket, path):
        try:
            return super().read_bytes(bucket, path)
        except KeyError:
            raise FileNotFoundError(path)

    def put_bytes(self, bucket, path, payload):
        self.objects[bucket][path] = payload

    def list_files(self, bucket, prefixes):
        for path, raw in self.objects[bucket].items():
            self.object_sizes[(bucket, path)] = len(raw)
        return super().list_files(bucket, prefixes)


class StreamingStore(Store):
    """Adds the streaming/digest interface used by the publishing receipt path."""

    def read_graph_json(self, bucket, path, parser=None):
        from scripts.reader_gc_stream import graph_json
        raw = self.objects[bucket][path]
        return (parser or graph_json)([raw], path.endswith('.gz'))

    def digest(self, bucket, path):
        return hashlib.sha256(self.objects[bucket][path]).hexdigest()


class ReaderGcCollectTests(unittest.TestCase):
    def test_complete_deployed_projection_is_required_before_catalog_retirement(self):
        store = Store()
        sidecar = {'v': 1, 'f': {'book': {'p': 'objects/live/document.pdf', 'b': PDF}}}
        store.put(ASSETS, SIDECAR, sidecar)
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        store.put(ASSETS, collector.CATALOG, catalog)
        client = Mock()
        client.get.return_value.content = json.dumps(sidecar).encode()
        with patch.object(collector, 'current', return_value=(None, {'files': {}})):
            identity, consumers = collector.observe_projections(store, client)
            self.assertEqual(set(consumers['receipts']), {'hf', 'pages'})
            self.assertEqual(identity['current'], generation)
            self.assertRegex(identity['digest'], r'^[0-9a-f]{64}$')
            encoded, report = collector.retire_catalog(store, identity, consumers, 0, apply=True)
            trimmed = json.loads(encoded)
            # The surviving current generation keeps its complete reference list
            # so the four-bucket graph can still resolve every live object.
            self.assertEqual(list(trimmed['generations']), [generation])
            self.assertEqual(trimmed['generations'][generation]['references'],
                             catalog['generations'][generation]['references'])
            self.assertEqual(report['current'], generation)
            self.assertEqual(report['after_generations'], 1)
            client.get.return_value.content = json.dumps({'v': 1, 'f': {}}).encode()
            with self.assertRaisesRegex(ValueError, 'complete sidecar'):
                collector.observe_projections(store, client)

    def test_partial_batch_failure_is_journaled_without_claiming_verified_deletion(self):
        store = Store()
        for number in range(2):
            store.put(PDF, f'objects/orphan/{number}/document.pdf', b'orphan')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        snapshots = []
        delete = store.delete
        calls = 0
        def failing_delete(bucket, paths):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('partial mutation failure')
            delete(bucket, paths)
        with patch.object(store, 'delete', side_effect=failing_delete):
            with self.assertRaisesRegex(RuntimeError, 'partial mutation'):
                collector.collect(store, Mock(), reference, report, {}, apply=True, batch_size=1,
                    checkpoint=lambda result: snapshots.append(json.loads(json.dumps(result))))
        self.assertEqual(snapshots[-1]['confirmed_deleted_objects'], 0)
        self.assertEqual([batch['status'] for batch in snapshots[-1]['batches']],
                         ['accepted', 'submitted'])

    def test_zero_day_retirement_preserves_current_unacked_and_handoff_tombstone(self):
        catalog = {'version': 1, 'kind': 'reader-catalog', 'current': {'generation': 'new'},
                   'generations': {
                       'new': {'generation': 'new', 'status': 'current'},
                       'old': {'generation': 'old', 'status': 'superseded',
                               'superseded_at': '2026-10-10T00:00:00+00:00',
                               'references': [{'path': 'objects/old/content'}],
                               'replacement_acks': {'hf': True, 'pages': True}},
                       'pending': {'generation': 'pending', 'status': 'superseded',
                                   'superseded_at': '2026-10-10T00:00:00+00:00'}}}
        trimmed = reader_lifecycle.compact_catalog(catalog, 'new',
            datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc), 0)
        self.assertEqual(set(trimmed['generations']), {'new', 'pending'})
        self.assertNotIn('references', trimmed['retired_generations']['old'])
        store = Store()
        store.put(ASSETS, collector.CATALOG, trimmed)
        store.put(ASSETS, 'reader-index/processing/released.json', {
            'kind': 'reader-processing-roots', 'status': 'released', 'generation': 'old',
            'resources': [{'bucket': PDF, 'root': 'objects/old'}]})
        store.put(PDF, 'objects/old/content', b'old')
        scanned = graph.ReferenceGraph(store).build()
        self.assertTrue(scanned['graph_complete'], scanned['blockers'])
        self.assertEqual(scanned['buckets'][PDF]['candidates'], ['objects/old/content'])
        sidecar = {'v': 1, 'f': {}}
        current_catalog, _ = reader_lifecycle.catalog_generation(sidecar)
        current_catalog['retired_generations'] = trimmed['retired_generations']
        advanced, _ = reader_lifecycle.catalog_generation({'v': 1, 'f': {'changed': {'s': 4}}}, current_catalog)
        self.assertEqual(advanced['retired_generations'], trimmed['retired_generations'])

    def test_streaming_preserves_qualified_references_and_exact_wire_hash(self):
        payload = {'files': {'book': {'pages': {'1': {'text': 'x' * 10000,
                   'text_spans': [{'box': [0, 0, 1, 1]}], 'layout': {'writing_mode': 'ltr'},
                   'raw_resource': {'bucket': PDF, 'path': 'objects/raw/page.json.gz'}}}}},
                   'components': {'text': 'ready', 'document': 'ready'}}
        for compressed in (False, True):
            wire = json.dumps(payload).encode()
            wire = gzip.compress(wire) if compressed else wire
            parsed, digest = graph_json((wire[n:n + 7] for n in range(0, len(wire), 7)), compressed)
            self.assertEqual(digest, hashlib.sha256(wire).hexdigest())
            page = parsed['files']['book']['pages']['1']
            self.assertEqual(page, {'raw_resource': payload['files']['book']['pages']['1']['raw_resource']})
            self.assertEqual(parsed['components']['document'], 'ready')
            self.assertEqual(parsed['components']['text'], 'ready')
        with self.assertRaises(Exception):
            graph_json([b'{"files":{'])

    def test_zero_day_collects_unreferenced_bytes_without_touching_live_or_uploading_objects(self):
        store = Store()
        store.put(ASSETS, SIDECAR, {'v': 1, 'f': {'book': {'p': 'objects/live/document.pdf', 'b': PDF}}})
        store.put(PDF, 'objects/live/document.pdf', b'live')
        store.put(PDF, 'objects/orphan/document.pdf', b'orphan')
        store.put(INPUT, 'objects/uploading/page.png', b'input')
        store.put(ASSETS, 'reader-index/processing/live.json', {
            'kind': 'reader-processing-roots', 'status': 'uploaded',
            'resources': [{'bucket': INPUT, 'root': 'objects/uploading'}]})
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        deleted = collector.collect(store, Mock(), reference, report, {}, apply=True, grace_days=0)
        self.assertEqual(deleted['confirmed_deleted_objects'], 1)
        self.assertEqual(deleted['confirmed_deleted_bytes'], 6)
        self.assertEqual(store.objects[PDF]['objects/live/document.pdf'], b'live')
        self.assertEqual(store.objects[INPUT]['objects/uploading/page.png'], b'input')
        self.assertNotIn('objects/orphan/document.pdf', store.objects[PDF])

    def test_root_change_or_unpaused_producer_blocks_deletion(self):
        for producer_changes in (False, True):
            with self.subTest(producer_changes=producer_changes):
                store = Store()
                store.put(PDF, 'objects/orphan/document.pdf', b'orphan')
                reference = graph.ReferenceGraph(store)
                report = reference.build()
                fence = Mock()
                if producer_changes:
                    fence.check.side_effect = [None, RuntimeError('producer resumed')]
                else:
                    store.put(ASSETS, SIDECAR, {'v': 1, 'f': {'now-live': {'p': 'objects/orphan/document.pdf', 'b': PDF}}})
                with self.assertRaises(RuntimeError):
                    collector.collect(store, fence, reference, report, {}, apply=True)
                self.assertIn('objects/orphan/document.pdf', store.objects[PDF])

    def test_incomplete_graph_and_unverified_delete_do_not_claim_reclamation(self):
        store = Store()
        store.put(PDF, 'objects/orphan/document.pdf', b'orphan')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        with self.assertRaises(RuntimeError):
            collector.collect(store, Mock(), reference, {**report, 'graph_complete': False}, {}, apply=True)
        with patch.object(store, 'delete', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'readback'):
                collector.collect(store, Mock(), reference, report, {}, apply=True)

    def test_current_snapshot_is_kept_and_only_retired_snapshots_are_deleted(self):
        store = Store()
        old_id = 'a' * 32
        old = f'reader-index/generations/{old_id}/reader_assets.json.gz'
        live = 'reader-index/generations/' + 'b' * 32 + '/reader_assets.json.gz'
        catalog = {'retired_generations': {old_id: {'snapshot': {'bucket': ASSETS, 'path': old},
                    'replacement_acks': {'hf': True, 'pages': True}}}}
        store.put(ASSETS, old, b'old')
        store.put(ASSETS, live, b'live')
        store.put(ASSETS, 'reader-index/pinned.json', {'resources': [{'bucket': ASSETS, 'path': live}]})
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        result = collector.collect(store, Mock(), reference, report, catalog, apply=True)
        self.assertEqual(result['confirmed_deleted_objects'], 1)
        self.assertNotIn(old, store.objects[ASSETS])
        self.assertIn(live, store.objects[ASSETS])

    def test_snapshot_retirement_verifies_identity_and_keeps_current(self):
        store = Store()
        def snapshot(generation, content):
            path = f'reader-index/generations/{generation}/reader_assets.json.gz'
            raw = gzip.compress(content)
            store.objects[ASSETS][path] = raw
            return {'bucket': ASSETS, 'path': path, 'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest()}
        old, live = 'a' * 32, 'b' * 32
        old_ref, live_ref = snapshot(old, b'old'), snapshot(live, b'live')
        catalog = {'version': 1, 'kind': 'reader-catalog', 'current': {'generation': live},
            'generations': {live: {'snapshot': live_ref}}, 'retired_generations': {
                old: {'snapshot': old_ref, 'replacement_acks': {'hf': True, 'pages': True}}}}
        store.put(ASSETS, collector.CATALOG, catalog)
        result = collector.collect_retired_snapshots(store, Mock(), catalog, apply=True)
        self.assertEqual(result['confirmed_deleted_objects'], 1)
        self.assertEqual(result['confirmed_deleted_bytes'], old_ref['bytes'])
        self.assertIn(live_ref['path'], store.objects[ASSETS])
        self.assertNotIn(old_ref['path'], store.objects[ASSETS])

    def test_planned_primary_is_not_an_uploaded_dependency_but_real_candidate_is(self):
        store = Store()
        store.put(ASSETS, 'reader-index/v3/automation.json', {'version': 1,
            'kind': 'reader-v3-automation', 'tasks': {'one': {'spec': {
                'primary_source': {'repo': 'VoiceOfML/test', 'path': 'original.pdf'},
                'primary': {'bucket': PDF, 'path': 'objects/planned/document.pdf'}}}}, 'days': {}})
        scanned = graph.ReferenceGraph(store).build()
        self.assertTrue(scanned['graph_complete'], scanned['blockers'])
        store.put(ASSETS, 'reader-index/candidate.json', {'resources': [
            {'bucket': PDF, 'path': 'objects/actual/reading-manifest.json'}]})
        self.assertFalse(graph.ReferenceGraph(store).build()['graph_complete'])

    def test_fence_rejects_enabled_workflow_and_running_jobs_across_all_lanes(self):
        def api(endpoint):
            if '/actions/workflows' in endpoint:
                return [{'workflows': [{'path': '.github/workflows/pdf-account-worker.yml',
                                        'state': 'disabled_manually'}]}]
            return [{'workflow_runs': []}]
        self.assertEqual(len(GitHubProducerFence(api).check()['repositories']), len(producer_repositories()))
        def enabled(endpoint):
            pages = api(endpoint)
            if '/actions/workflows' in endpoint:
                pages[0]['workflows'][0]['state'] = 'active'
            return pages
        with self.assertRaisesRegex(RuntimeError, 'not paused'):
            GitHubProducerFence(enabled).check()
        def running(endpoint):
            pages = api(endpoint)
            if 'status=in_progress' in endpoint:
                pages[0]['workflow_runs'] = [{'id': 1, 'path': '.github/workflows/pdf-account-worker.yml',
                                              'status': 'in_progress'}]
            return pages
        with self.assertRaisesRegex(RuntimeError, 'not drained'):
            GitHubProducerFence(running).check()
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(RuntimeError):
                assert_collector_context()

    def test_retired_routing_lanes_stay_in_the_producer_fence(self):
        # A routing change must not drop an account that still owns a queued run.
        config = {'publisher_repository': 'anftm/pipeline',
                  'native_lanes': [{'name': 'current', 'owner': 'vomebook',
                                    'repository': 'vomebook/pdf-pipeline'}],
                  'converted_lane': {'name': 'converted', 'owner': 'ambrossee768',
                                     'repository': 'ambrossee768/pdf-pipeline'},
                  'legacy_configs': [{
                      'publisher_repository': 'anftm/pipeline',
                      'native_lanes': [{'name': 'retired', 'owner': 'alicetran68',
                                        'repository': 'alicetran68/pipeline'}],
                      'converted_lane': {'name': 'converted', 'owner': 'ambrossee768',
                                         'repository': 'ambrossee768/pdf-pipeline'}}]}
        with patch('scripts.reader_gc_fence.load_config', return_value=config):
            self.assertIn('alicetran68/pipeline', producer_repositories())
            self.assertIn('vomebook/pdf-pipeline', producer_repositories())

    def test_reader_only_deletion_requires_the_complete_four_bucket_closure(self):
        store = Store()
        store.put(PDF, 'objects/orphan/document.pdf', b'orphan')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        # Without the fourth domain inventoried the reader-only tier must refuse,
        # because a CCRD text export may still retain this Reader resource.
        del reference.files[graph.ccrd_lifecycle.BUCKET]
        with self.assertRaisesRegex(RuntimeError, 'four-bucket'):
            collector.collect(store, Mock(), reference, report, {}, apply=True, reader_only=True)
        self.assertIn('objects/orphan/document.pdf', store.objects[PDF])

    def test_reader_only_deletion_never_targets_the_fourth_domain(self):
        store = Store()
        ccrd = graph.ccrd_lifecycle.BUCKET
        store.put(ccrd, 'text/objects/aa/' + 'a' * 64 + '.txt', b'ocr text')
        store.put(PDF, 'objects/orphan/document.pdf', b'orphan')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        result = collector.collect(store, Mock(), reference, report, {}, apply=True, reader_only=True)
        self.assertEqual(result['confirmed_deleted_objects'], 1)
        self.assertTrue(result['reader_only'])
        self.assertIn('text/objects/aa/' + 'a' * 64 + '.txt', store.objects[ccrd])
        self.assertNotIn('objects/orphan/document.pdf', store.objects[PDF])

    def test_retired_v3_and_processing_roots_are_collected(self):
        store = Store()
        generation = "a" * 64
        retired = [f"reader-index/v3/promotions/{generation}.json",
                   f"reader-index/v3/generations/{generation}/catalog.json",
                   "reader-index/processing/v3-" + "b" * 32 + ".json"]
        for path in retired:
            store.put(ASSETS, path, {"kind": "reader-v3-history", "generation": generation})
        store.put(ASSETS, 'reader-index/v3/retirement.json', {
            'version': 1, 'kind': 'reader-v3-retirement', 'resources': [],
            'retired_paths': retired, 'replacement_acks': {'hf': True, 'pages': True}})
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        result = collector.collect(store, Mock(), reference, report, {}, apply=True)
        self.assertEqual(result['confirmed_deleted_objects'], len(retired))
        for path in retired:
            self.assertNotIn(path, store.objects[ASSETS])

    def test_collection_plan_is_journaled_before_the_first_delete(self):
        store = Store()
        store.put(PDF, 'objects/orphan/document.pdf', b'orphan')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        journals = []
        collector.collect(store, Mock(), reference, report, {}, apply=True,
                          checkpoint=lambda value: journals.append(json.loads(json.dumps(value))))
        self.assertEqual(journals[0]['targets'],
                         [{'bucket': PDF, 'path': 'objects/orphan/document.pdf'}])
        self.assertTrue(journals[0]['plan_sha256'])
        self.assertNotIn('objects/orphan/document.pdf', store.objects[PDF])

    def test_retirement_writes_nothing_without_a_current_acknowledgment(self):
        store = Store()
        sidecar = {'v': 1, 'f': {}}
        store.put(ASSETS, SIDECAR, sidecar)
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        store.put(ASSETS, collector.CATALOG, catalog)
        identity = {
            'current': generation,
            'bound_sidecar_sha256': catalog['generations'][generation]['sidecar_sha256'],
            'digest': hashlib.sha256(store.read_bytes(ASSETS, collector.CATALOG)).hexdigest(),
        }
        before = store.read_bytes(ASSETS, collector.CATALOG)
        _, report = collector.retire_catalog(store, identity, {'receipts': {}}, 0, apply=True)
        # Without the publishing workflow's receipt the canonical catalog is kept
        # byte-identical: an unverified consumer must never lose history.
        self.assertFalse(report['acknowledged'])
        self.assertEqual(report['acknowledgment_source'], 'no-acknowledgment-receipt')
        self.assertEqual(store.read_bytes(ASSETS, collector.CATALOG), before)

    def test_matching_acknowledgment_receipt_allows_compaction(self):
        store = Store()
        sidecar = {'v': 1, 'f': {}}
        store.put(ASSETS, SIDECAR, sidecar)
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        store.put(ASSETS, collector.CATALOG, catalog)
        identity = {
            'current': generation,
            'bound_sidecar_sha256': catalog['generations'][generation]['sidecar_sha256'],
            'digest': hashlib.sha256(store.read_bytes(ASSETS, collector.CATALOG)).hexdigest(),
        }
        store.put(ASSETS, collector.ACKNOWLEDGMENTS, {
            'version': 1, 'kind': 'reader-gc-acknowledgments',
            'current': {'generation': generation,
                        'sidecar_sha256': identity['bound_sidecar_sha256'],
                        'catalog_sha256': identity['digest']}})
        _, report = collector.retire_catalog(store, identity, {'receipts': {}}, 0, apply=True)
        self.assertTrue(report['acknowledged'])
        self.assertEqual(report['acknowledgment_source'], 'workflow-receipt')
        # A stale receipt (different catalog digest) must not be trusted.
        store.put(ASSETS, collector.CATALOG, catalog)
        store.put(ASSETS, collector.ACKNOWLEDGMENTS, {
            'version': 1, 'kind': 'reader-gc-acknowledgments',
            'current': {'generation': generation,
                        'sidecar_sha256': identity['bound_sidecar_sha256'],
                        'catalog_sha256': '0' * 64}})
        acknowledged, reason = collector.catalog_acknowledged(store, identity)
        self.assertFalse(acknowledged)
        self.assertEqual(reason, 'stale-acknowledgment-receipt')

    def test_acknowledgment_receipt_binds_catalog_sidecar_and_pinned_projection(self):
        from scripts import publish_reader_v3 as publication
        store = StreamingStore()
        sidecar = {'v': 1, 'f': {}}
        store.put(ASSETS, SIDECAR, sidecar)
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        store.put(ASSETS, collector.CATALOG, catalog)
        pinned = {'sha256': store.digest(ASSETS, SIDECAR)}
        with patch.object(publication, 'assert_writer', return_value=None), \
                patch.object(publication, 'current',
                             return_value=({'generation': 'v3'}, {'base_sidecar': pinned})):
            report = publication.record_legacy_acknowledgments(store, apply=True)
        self.assertEqual(report['current']['generation'], generation)
        acknowledged, reason = collector.catalog_acknowledged(
            store, collector.catalog_identity(store))
        self.assertTrue(acknowledged, reason)
        # A pinned projection that still serves an older sidecar must not
        # authorize retiring history the deployed consumers may use.
        with patch.object(publication, 'current',
                          return_value=({'generation': 'v3'},
                                        {'base_sidecar': {'sha256': '0' * 64}})):
            report = publication.record_legacy_acknowledgments(store, apply=True)
        self.assertEqual(report['reason'], 'pinned-sidecar-does-not-match-canonical')
        self.assertEqual(json.loads(store.read_bytes(ASSETS, collector.ACKNOWLEDGMENTS))
                         ['current']['generation'], generation)
        # A canonical sidecar newer than the catalog generation is equally unsafe.
        store.put(ASSETS, SIDECAR, {'v': 1, 'f': {'new': {'s': 4}}})
        with patch.object(publication, 'current', return_value=(None, {'files': {}})):
            report = publication.record_legacy_acknowledgments(store, apply=True)
        self.assertEqual(report['reason'], 'canonical-sidecar-newer-than-catalog')

    def test_compaction_refuses_a_receipt_for_a_superseded_sidecar(self):
        from scripts import publish_reader_v3 as publication
        store = StreamingStore()
        sidecar = {'v': 1, 'f': {}}
        store.put(ASSETS, SIDECAR, sidecar)
        catalog, generation = reader_lifecycle.catalog_generation(sidecar)
        store.put(ASSETS, collector.CATALOG, catalog)
        identity = collector.catalog_identity(store)
        pinned = {'sha256': store.digest(ASSETS, SIDECAR)}
        with patch.object(publication, 'assert_writer', return_value=None), \
                patch.object(publication, 'current',
                             return_value=({'generation': 'v3'}, {'base_sidecar': pinned})):
            publication.record_legacy_acknowledgments(store, apply=True)
        before = store.read_bytes(ASSETS, collector.CATALOG)
        # Deploying a newer sidecar without a new catalog means the receipt is
        # stale; compaction must not bind to the old digest.
        store.put(ASSETS, SIDECAR, {'v': 1, 'f': {'later': {'s': 4}}})
        report = publication.compact_legacy_catalog(store, apply=True)
        self.assertEqual(report['reason'], 'stale-acknowledgment-receipt')
        self.assertFalse(report['applied'])
        self.assertEqual(store.read_bytes(ASSETS, collector.CATALOG), before)
        self.assertEqual(identity['current'], generation)

    def test_ccrd_projection_must_load_the_exact_current_release(self):
        store = Store()
        ccrd = graph.ccrd_lifecycle.BUCKET
        release = {'version': 1, 'kind': 'ccrd-ocr-search-release',
                   'base_metadata_sha256': 'b' * 64, 'builder_sha256': 'c' * 64,
                   'tokenizer_version': graph.ccrd_lifecycle.TOKENIZER,
                   'generation': 'e' * 64, 'documents': 1,
                   'exports': [{'bucket': ccrd, 'path': 'text/exports/' + 'a' * 64 + '/' + 'e' * 64 + '/manifest.json',
                                'bytes': 1, 'sha256': 'f' * 64, 'role': 'runtime'}],
                   'database': {'bucket': ccrd, 'path': 'indexes/ocr/' + 'e' * 64 + '/OCR.sqlite3',
                                'bytes': 1, 'sha256': 'd' * 64, 'role': 'runtime'}}
        release['generation'] = graph.ccrd_lifecycle.digest(graph.ccrd_lifecycle.canonical({
            key: release[key] for key in ('version', 'kind', 'base_metadata_sha256', 'builder_sha256',
                                          'tokenizer_version', 'exports')}))
        raw = json.dumps(release, sort_keys=True).encode()
        path = f'releases/ocr-{release["generation"]}/manifest.json'
        release['generation'] = release['generation']
        store.put(ccrd, path, raw)
        pointer = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': release['generation'],
                   'release': {'bucket': ccrd, 'path': path, 'bytes': len(raw),
                               'sha256': hashlib.sha256(raw).hexdigest(), 'role': 'runtime'},
                   'resources': [{'bucket': ccrd, 'path': path, 'bytes': len(raw),
                                  'sha256': hashlib.sha256(raw).hexdigest(), 'role': 'runtime'}]}
        store.put(ccrd, 'control/ocr-current.json', pointer)
        client = Mock()
        client.get.return_value.json.return_value = {
            'ready': True, 'ocr_release': release['generation'],
            'loaded_sources': ['CCRD', 'CW', 'OCR']}
        receipt = collector.observe_ccrd_projection(store, client)
        self.assertEqual(receipt['generation'], release['generation'])
        # A stale deployed index must block Reader deletion.
        client.get.return_value.json.return_value = {
            'ready': True, 'ocr_release': '0' * 64,
            'loaded_sources': ['CCRD', 'CW', 'OCR']}
        with self.assertRaisesRegex(ValueError, 'exact current OCR release'):
            collector.observe_ccrd_projection(store, client)


if __name__ == '__main__':
    unittest.main()
