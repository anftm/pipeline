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


class ReaderGcCollectTests(unittest.TestCase):
    def test_complete_deployed_projection_is_required_before_catalog_retirement(self):
        store = Store()
        sidecar = {'v': 1, 'f': {'book': {'p': 'objects/live/document.pdf', 'b': PDF}}}
        store.put(ASSETS, SIDECAR, sidecar)
        catalog, _ = reader_lifecycle.catalog_generation(sidecar)
        store.put(ASSETS, collector.CATALOG, catalog)
        client = Mock()
        client.get.return_value.content = json.dumps(sidecar).encode()
        with patch.object(collector, 'current', return_value=(None, {'files': {}})):
            raw, consumers = collector.observe_projections(store, client)
            self.assertEqual(set(consumers['receipts']), {'hf', 'pages'})
            trimmed, _ = collector.retire_catalog(store, raw, consumers, 0, apply=True)
            self.assertEqual(trimmed['generations'][trimmed['current']['generation']]['acks'],
                             {'hf': True, 'pages': True})
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


if __name__ == '__main__':
    unittest.main()
