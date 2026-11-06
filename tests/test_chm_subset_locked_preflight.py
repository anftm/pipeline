import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from scripts import chm_subset_locked_preflight as preflight


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class MemoryStore:
    def __init__(self, objects):
        self.objects = dict(objects)

    def read_bytes(self, bucket, path):
        try:
            return self.objects[(bucket, path)]
        except KeyError as error:
            raise FileNotFoundError(path) from error

    def list_files(self, bucket, prefixes):
        return {path for owner, path in self.objects if owner == bucket and path.startswith(prefixes)}

    def assert_buckets_available(self):
        return None


class LockedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        for n in range(1, 973):
            body = f'epub-{n}'.encode()
            self.rows.append({'bucket': preflight.chm.ASSETS,
                              'path': f'native/ebook/chm/{n:064x}/document.epub',
                              'bytes': len(body), 'sha256': sha(body)})
        self.old, self.selected, self.withheld = self.rows[:600], self.rows[600:952], self.rows[952:]
        batches = [{'bucket': preflight.chm.ASSETS, 'status': 'verified-absent',
                    'paths': [row['path'] for row in self.old[start:start + 100]]}
                   for start in range(0, 600, 100)]
        self.progress = {'kind': 'chm-epub-retirement', 'plan_sha256': preflight.PLAN_SHA,
                         'maintenance_id': preflight.OWNER, 'complete': False,
                         'confirmed_deleted_objects': 600, 'batches': batches}
        self.window = {'maintenance_id': preflight.OWNER, 'callback_plan_sha256': preflight.PLAN_SHA,
                       'callback_recovery_required': True}
        self.progress_raw = json.dumps(self.progress, sort_keys=True).encode()
        self.window_raw = json.dumps(self.window, sort_keys=True).encode()
        self.root_raw = b'fake retention'
        self.classification = json.dumps({'old_plan_sha256': preflight.PLAN_SHA,
            'maintenance_id': preflight.OWNER, 'deletion_authorized': False,
            'targets': [{'path': row['path'], 'category': 'mapped-with-saved-evidence'} for row in self.selected]
                       + [{'path': row['path'], 'category': 'needs-independent-review'} for row in self.withheld]},
            sort_keys=True).encode()
        self.plan = {'overrides': {preflight.RETENTION: self.root_raw}, 'immutable': {},
                     'rollback_aliases': {str(n): {} for n in range(5)}}
        objects = {(preflight.chm.ASSETS, preflight.journal.PROGRESS): self.progress_raw,
                   (preflight.chm.ASSETS, preflight.maintenance.DURABLE_JOURNAL): self.window_raw,
                   (preflight.chm.ASSETS, preflight.RETENTION): self.root_raw}
        for n, row in enumerate(self.rows, 1):
            if n > 600:
                objects[(row['bucket'], row['path'])] = f'epub-{n}'.encode()
        self.store = MemoryStore(objects)
        self.fence = Mock()
        self.fence.check.return_value = {'kind': 'synthetic-fence'}
        self.projections = {'hf': {'surface': 'hf', 'generation': 'g', 'catalog_sha256': 'a'},
                            'pages': {'surface': 'pages', 'generation': 'g', 'catalog_sha256': 'a'}}
        self.patches = [
            patch.object(preflight, 'PROGRESS_SHA', sha(self.progress_raw)),
            patch.object(preflight, 'MAINTENANCE_SHA', sha(self.window_raw)),
            patch.object(preflight, 'RETENTION_SHA', sha(self.root_raw)),
            patch.object(preflight, 'CLASSIFICATION_SHA', sha(self.classification)),
            patch.object(preflight.journal, 'load_plan', return_value=(self.plan, {}, {}, preflight.PLAN_SHA)),
            patch.object(preflight.journal, 'verify_initial_chapter_evidence'),
            patch.object(preflight.safety, 'verify_live_chm_controls'),
            patch.object(preflight.safety, 'verify_live_processing_controls'),
            patch.object(preflight.safety, 'verify_retired_rollback_aliases'),
            patch.object(preflight.safety, 'observe_planned_projection', return_value=self.projections),
            patch.object(preflight, 'verify_selected_manifests', return_value={'manifests': []}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def check(self):
        return preflight.check(self.store, self.fence, Mock(), self.rows, self.classification)

    def test_checks_every_withheld_and_never_grants_mutation(self):
        report = self.check()
        self.assertEqual(len(report['withheld']), 20)
        self.assertEqual((report['partition']['old_receipts'], report['partition']['selected']), (600, 352))
        self.assertFalse(report['deletion_authorized'])
        self.assertFalse(report['root_created'])
        self.assertFalse(report['journal_created'])
        self.assertEqual(report['target_observations']['counts']['unsubmitted-present']['objects'], 352)
        self.assertEqual(self.fence.check.call_count, 2)

    def test_changed_candidate_or_reappearing_old_target_stops(self):
        row = self.selected[0]
        key = (row['bucket'], row['path'])
        raw = self.store.objects[key]
        self.store.objects[key] = b'changed'
        with self.assertRaisesRegex(ValueError, 'exact target changed'):
            self.check()
        self.store.objects[key] = raw
        self.store.objects[(self.old[0]['bucket'], self.old[0]['path'])] = b'epub-1'
        with self.assertRaisesRegex(ValueError, 'target reappeared'):
            self.check()

    def test_modified_withheld_or_extra_sibling_stops(self):
        row = self.withheld[0]
        key = (row['bucket'], row['path'])
        raw = self.store.objects[key]
        self.store.objects[key] = b'changed'
        with self.assertRaisesRegex(ValueError, 'exact target changed'):
            self.check()
        self.store.objects[key] = raw
        self.store.objects[(row['bucket'], row['path'].rsplit('/', 1)[0] + '/other')] = b'x'
        with self.assertRaisesRegex(ValueError, 'parent has changed siblings'):
            self.check()

    def test_foreign_classification_or_controls_stop(self):
        with patch.object(preflight, 'CLASSIFICATION_SHA', '0' * 64):
            with self.assertRaisesRegex(ValueError, 'classification SHA'):
                self.check()
        self.store.objects[(preflight.chm.ASSETS, preflight.journal.PROGRESS)] = b'changed'
        with self.assertRaisesRegex(ValueError, 'progress, maintenance or retention bytes changed'):
            self.check()

    def test_consumer_drift_and_wrong_alias_count_stop(self):
        after = {**self.projections, 'pages': {**self.projections['pages'], 'generation': 'new'}}
        with patch.object(preflight.safety, 'observe_planned_projection', side_effect=[self.projections, after]):
            with self.assertRaisesRegex(ValueError, 'Reader projection changed'):
                self.check()
        self.plan['rollback_aliases'].pop('0')
        with self.assertRaisesRegex(ValueError, 'five rollback aliases'):
            self.check()

    def test_manifest_limit_failure_stops_preflight(self):
        with patch.object(preflight, 'verify_selected_manifests', side_effect=ValueError('manifest too large')):
            with self.assertRaisesRegex(ValueError, 'manifest too large'):
                self.check()

    def test_selected_manifest_helper_reads_exact_checklist(self):
        import gzip
        self.patches[-1].stop()
        evidence = [{'manifest': f'manifest-{n}', 'manifest_sha256': f'{n:064x}'} for n in range(950)]
        plan = {'overrides': {preflight.chm.SIDECAR: gzip.compress(b'{}'), preflight.chm.INDEX: b'{}'}}
        progress = {'chapter_evidence': {'results': evidence}}
        checklist = {f'manifest-{n}': f'{n:064x}' for n in range(358)}
        store = Mock()
        import threading
        lock, reads = threading.Lock(), []
        def read(bucket, path):
            with lock:
                reads.append((bucket, path))
            return path.encode()
        store.read_bytes.side_effect = read
        histories = {str(n): {} for n in range(5)}
        with patch.object(preflight.safety, 'load_plan_history_catalogs', return_value=histories), \
             patch.object(preflight.publication, 'pinned_base', return_value={}), \
             patch.object(preflight.subset, 'verify_route_limit_inputs', return_value={'manifests': checklist}), \
             patch.object(preflight.subset, 'verify_manifest_bytes', return_value={'manifests': [1]}) as verify:
            result = preflight.verify_selected_manifests(store, self.rows, self.selected, plan, progress, {})
        self.assertEqual(result, {'manifests': [1]})
        self.assertEqual(len(reads), 358)
        self.assertEqual({path for _, path in reads}, set(checklist))
        self.assertEqual(set(verify.call_args.args[1]), set(checklist))

    def test_only_central_main_workflow_can_run_entrypoint(self):
        context = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
                   'GITHUB_WORKFLOW_REF': preflight.WORKFLOW_REF, 'GITHUB_RUN_ID': '123'}
        with patch.dict('os.environ', context, clear=True):
            preflight.require_context()
            for key in context:
                with self.subTest(key=key), patch.dict('os.environ', {key: 'foreign'}):
                    with self.assertRaises(RuntimeError):
                        preflight.require_context()

    def test_github_get_retries_transient_reads_without_leaking_response_or_token(self):
        token = 'synthetic-secret'
        bad = Mock(status_code=503)
        good = Mock(status_code=200, links={})
        good.json.return_value = {'workflows': []}
        with patch.dict('os.environ', {'GC_PRODUCER_TOKENS_JSON': json.dumps({'x/y': token})}, clear=True), \
             patch.object(preflight.httpx, 'get', side_effect=[bad, good]) as get, \
             patch.object(preflight.time, 'sleep'):
            result = preflight.github_get('repos/x/y/actions/workflows?per_page=100')
        self.assertEqual(result, [{'workflows': []}])
        self.assertEqual(get.call_count, 2)
        forbidden = Mock(status_code=403, text=token)
        with patch.dict('os.environ', {'GC_PRODUCER_TOKENS_JSON': json.dumps({'x/y': token})}, clear=True), \
             patch.object(preflight.httpx, 'get', return_value=forbidden):
            with self.assertRaisesRegex(RuntimeError, 'x/y: HTTP 403') as error:
                preflight.github_get('repos/x/y/actions/workflows?per_page=100')
        self.assertNotIn(token, str(error.exception))

    def test_github_get_rejects_foreign_pagination_host(self):
        response = Mock(status_code=200, links={'next': {'url': 'https://foreign.example/steal'}})
        response.json.return_value = {}
        with patch.dict('os.environ', {'GC_PRODUCER_TOKENS_JSON': json.dumps({'x/y': 'synthetic'})}, clear=True), \
             patch.object(preflight.httpx, 'get', return_value=response) as get:
            with self.assertRaisesRegex(RuntimeError, 'pagination endpoint'):
                preflight.github_get('repos/x/y/actions/workflows?per_page=100')
        self.assertEqual(get.call_count, 1)

    def test_read_credential_fallback_does_not_replace_explicit_inventory(self):
        with patch.dict('os.environ', {'GH_TOKEN': 'synthetic'}, clear=True), \
             patch.object(preflight, 'producer_repositories', return_value=['x/y', 'z/w']):
            preflight.ensure_read_credentials()
            import os
            self.assertEqual(json.loads(os.environ['GC_PRODUCER_TOKENS_JSON']),
                             {'x/y': 'synthetic', 'z/w': 'synthetic'})
        with patch.dict('os.environ', {'GC_PRODUCER_TOKENS_JSON': '{}', 'GH_TOKEN': 'fallback'}, clear=True):
            preflight.ensure_read_credentials()
            self.assertEqual(os.environ['GC_PRODUCER_TOKENS_JSON'], '{}')

    def test_unauthorized_per_repo_token_can_use_readonly_fallback_once(self):
        expired = Mock(status_code=401)
        good = Mock(status_code=200, links={})
        good.json.return_value = {'workflows': []}
        env = {'GC_PRODUCER_TOKENS_JSON': json.dumps({'x/y': 'expired'}), 'GH_TOKEN': 'read-only'}
        with patch.dict('os.environ', env, clear=True), \
             patch.object(preflight.httpx, 'get', side_effect=[expired, good]) as get:
            self.assertEqual(preflight.github_get('repos/x/y/actions/workflows?per_page=100'), [{'workflows': []}])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs['headers']['Authorization'], 'Bearer read-only')


if __name__ == '__main__':
    unittest.main()
