import hashlib
import json
import os
import unittest
from unittest.mock import patch

from scripts import withdraw_ccrd_ocr_consumer as withdrawal


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()


class Store:
    def __init__(self, pointer):
        self.pointer = encode(pointer)
        self.writes = []
        self.maintenance = None
        self.consumer = None

    def read_bytes(self, bucket, path):
        if bucket == withdrawal.MAINTENANCE_BUCKET and path == withdrawal.MAINTENANCE:
            if self.maintenance is None:
                raise FileNotFoundError(path)
            return self.maintenance
        if bucket == withdrawal.lifecycle.BUCKET and path == 'control/ocr-consumer.json':
            if self.consumer is None:
                raise FileNotFoundError(path)
            return self.consumer
        if bucket != withdrawal.lifecycle.BUCKET or path != withdrawal.CURRENT:
            raise FileNotFoundError(path)
        return self.pointer

    def put_json(self, bucket, path, value):
        if bucket != withdrawal.lifecycle.BUCKET or path != 'control/ocr-consumer.json':
            raise AssertionError('unexpected write')
        self.writes.append((bucket, path))
        self.consumer = encode(value)


class Space:
    def __init__(self, base, activation):
        self.revision = 'a' * 40
        self.base = encode(base)
        self.activation = encode(activation)
        self.override = False
        self.commits = []
        self.on_commit = None
        self.history = {'a' * 40: {withdrawal.BASE_FILE: self.base, withdrawal.OCR_FILE: self.activation}}
        self.diff = ''

    def head(self):
        return self.revision

    def has_ocr_override(self):
        return self.override

    def read(self, path, revision):
        if revision == self.revision:
            if path == withdrawal.BASE_FILE:
                return self.base
            if path == withdrawal.OCR_FILE and self.activation is not None:
                return self.activation
        elif revision in self.history and path in self.history[revision]:
            return self.history[revision][path]
        raise FileNotFoundError(path)

    def exists(self, path, revision):
        return path == withdrawal.OCR_FILE and self.activation is not None

    def remove_ocr_activation(self, *, parent):
        if parent != self.revision:
            raise ValueError('stale parent')
        self.commits.append({'parent': parent, 'deleted': withdrawal.OCR_FILE})
        self.activation = None
        self.revision = 'b' * 40
        self.history[self.revision] = {withdrawal.BASE_FILE: self.base}
        self.diff = 'diff --git a/data/ocr-release.json b/data/ocr-release.json\ndeleted file mode 100644\n'
        if self.on_commit:
            self.on_commit()
        return self.revision

    def recent_commits(self, revision):
        return [revision, self.commits[-1]['parent']] if self.commits else [revision]

    def commit_diff(self, revision):
        return self.diff


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.generation = 'c' * 64
        self.release_sha = 'd' * 64
        self.base_generation = 'ccrd-four-' + 'e' * 64
        path = f'releases/ocr-{self.generation}/manifest.json'
        release = {'bucket': withdrawal.lifecycle.BUCKET, 'bytes': 123, 'path': path,
                   'role': 'runtime', 'sha256': self.release_sha}
        pointer = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': self.generation,
                   'release': release, 'resources': [release]}
        activation = {'version': 1, 'kind': 'ccrd-ocr-activation', 'bucket': withdrawal.lifecycle.BUCKET,
                      'path': path, 'sha256': self.release_sha}
        base = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': withdrawal.lifecycle.BUCKET,
                'path': f'generations/{self.base_generation}/manifest.json', 'sha256': 'f' * 64}
        self.store = Store(pointer)
        self.space = Space(base, activation)
        self.expected = withdrawal.expected_values(self.space.revision, self.generation, self.release_sha,
                                                    hashlib.sha256(self.space.base).hexdigest())
        self.status = {'ready': True, 'ocr_release': self.generation,
                       'loaded_sources': ['CCRD', 'CW', 'RMRB', 'PLA', 'Japanese-Materials'],
                       'bucket': {'state': 'ready', 'generation': self.base_generation}}
        self.live = lambda: self.status.copy()
        self.writer = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': withdrawal.REPOSITORY,
                       'GITHUB_WORKFLOW_REF': withdrawal.WORKFLOW, 'GITHUB_RUN_ID': '123'}
        self.fence = patch.object(withdrawal, 'active_maintenance_runs', return_value=[])
        self.fence.start()
        self.addCleanup(self.fence.stop)

    def apply(self, *args, **kwargs):
        return withdrawal.deactivate(*args, maintenance_runs=withdrawal.active_maintenance_runs, **kwargs)

    def test_plan_is_read_only(self):
        before = (self.space.base, self.space.activation, self.store.pointer)
        report = withdrawal.deactivate(self.store, self.space, self.live, self.expected)
        self.assertEqual(report['phase'], 'planned')
        self.assertFalse(report['applied'])
        self.assertEqual((self.space.base, self.space.activation, self.store.pointer), before)
        self.assertEqual(self.space.commits, [])

    def test_apply_deletes_only_ocr_activation_and_checks_consumer(self):
        self.space.on_commit = lambda: self.status.update(ocr_release=None)
        before = (self.space.base, self.store.pointer)
        snapshots = []
        with patch.dict(os.environ, self.writer, clear=False):
            report = self.apply(self.store, self.space, self.live, self.expected,
                                           apply=True, save=lambda row: snapshots.append(dict(row)))
        self.assertEqual([row['phase'] for row in snapshots],
                         ['planned', 'waiting-for-consumer', 'consumer-withdrawn'])
        self.assertEqual(self.space.commits, [{'parent': 'a' * 40, 'deleted': withdrawal.OCR_FILE}])
        self.assertEqual((self.space.base, self.store.pointer), before)
        self.assertEqual(self.store.writes, [(withdrawal.lifecycle.BUCKET, 'control/ocr-consumer.json')])
        self.assertEqual(json.loads(self.store.consumer)['status'], 'consumer-withdrawn')
        self.assertTrue(report['complete'])
        self.assertEqual(report['remote_objects_deleted'], 0)
        self.assertFalse(report['reader_gc_projection_blocked_until_safe_reconciliation'])

    def test_apply_requires_exact_workflow(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'serialized central workflow'):
                self.apply(self.store, self.space, self.live, self.expected, apply=True)
        self.assertEqual(self.space.commits, [])

    def test_mismatch_or_override_blocks_without_writes(self):
        for change in ('revision', 'generation', 'release', 'base', 'override', 'live'):
            with self.subTest(change=change):
                self.setUp()
                expected = dict(self.expected)
                if change == 'revision': expected['revision'] = 'f' * 40
                elif change == 'generation': expected['generation'] = '1' * 64
                elif change == 'release': expected['release_sha256'] = '2' * 64
                elif change == 'base': expected['base_sha256'] = '3' * 64
                elif change == 'override': self.space.override = True
                else: self.status['ready'] = False
                with patch.dict(os.environ, self.writer, clear=False):
                    with self.assertRaises(ValueError):
                        self.apply(self.store, self.space, self.live, expected, apply=True)
                self.assertEqual(self.space.commits, [])

    def test_unfinished_maintenance_blocks_without_writes(self):
        self.store.maintenance = encode({'version': 1, 'kind': 'reader-maintenance-journal',
                                         'restored': False})
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaisesRegex(ValueError, 'maintenance is unfinished'):
                self.apply(self.store, self.space, self.live, self.expected, apply=True)
        self.assertEqual(self.space.commits, [])

    def test_completed_maintenance_is_pinned_in_snapshot(self):
        self.store.maintenance = encode({'version': 1, 'kind': 'reader-maintenance-journal',
                                         'restored': True})
        result = withdrawal.deactivate(self.store, self.space, self.live, self.expected)
        self.assertEqual(result['before']['maintenance_journal_sha256'],
                         hashlib.sha256(self.store.maintenance).hexdigest())

    def test_queued_maintenance_blocks_before_commit(self):
        with patch.object(withdrawal, 'active_maintenance_runs', return_value=['7']):
            with patch.dict(os.environ, self.writer, clear=False):
                with self.assertRaisesRegex(ValueError, 'unfinished run'):
                    self.apply(self.store, self.space, self.live, self.expected, apply=True)
        self.assertEqual(self.space.commits, [])

    def test_pointer_change_after_commit_is_not_a_success(self):
        def change():
            self.status['ocr_release'] = None
            self.store.pointer += b' '
        self.space.on_commit = change
        receipts = []
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaisesRegex(ValueError, 'pointer changed'):
                self.apply(self.store, self.space, self.live, self.expected, apply=True,
                                      save=lambda value: receipts.append(dict(value)))
        self.assertEqual(receipts[-1]['phase'], 'waiting-for-consumer')

    def test_live_consumer_still_on_ocr_times_out_as_partial(self):
        receipts = []
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaises(TimeoutError):
                self.apply(self.store, self.space, self.live, self.expected, apply=True,
                                      save=lambda value: receipts.append(dict(value)), timeout=0)
        self.assertEqual(receipts[-1]['phase'], 'waiting-for-consumer')
        self.assertFalse(receipts[-1].get('complete', False))

    def test_base_index_drift_after_commit_is_not_accepted(self):
        def change():
            self.space.base += b' '
            self.status['ocr_release'] = None
        self.space.on_commit = change
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaises(TimeoutError):
                self.apply(self.store, self.space, self.live, self.expected, apply=True, timeout=0)

    def test_resume_interrupted_consumer_without_repeating_commit(self):
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaises(TimeoutError):
                self.apply(self.store, self.space, self.live, self.expected, apply=True, timeout=0)
            self.status['ocr_release'] = None
            result = self.apply(self.store, self.space, self.live, self.expected, apply=True,
                                resume_revision='b' * 40, timeout=0)
        self.assertTrue(result['complete'])
        self.assertTrue(result['resumed'])
        self.assertEqual(len(self.space.commits), 1)

    def test_resume_rejects_unrelated_space_change(self):
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaises(TimeoutError):
                self.apply(self.store, self.space, self.live, self.expected, apply=True, timeout=0)
            self.space.diff += 'diff --git a/unrelated b/unrelated\ndeleted file mode 100644\n'
            with self.assertRaisesRegex(ValueError, 'single-file withdrawal commit'):
                self.apply(self.store, self.space, self.live, self.expected, apply=True,
                           resume_revision='b' * 40, timeout=0)
        self.assertEqual(self.store.writes, [])

    def test_lost_commit_response_records_candidate_and_never_retries(self):
        original = self.space.remove_ocr_activation
        def ambiguous(*, parent):
            original(parent=parent)
            raise OSError('response lost')
        self.space.remove_ocr_activation = ambiguous
        records = []
        with patch.dict(os.environ, self.writer, clear=False):
            with self.assertRaises(OSError):
                self.apply(self.store, self.space, self.live, self.expected, apply=True,
                           save=lambda value: records.append(dict(value)))
        self.assertEqual(records[-1]['phase'], 'commit-outcome-unknown')
        self.assertEqual(records[-1]['candidate_space_revision'], 'b' * 40)
        self.assertEqual(len(self.space.commits), 1)


if __name__ == '__main__':
    unittest.main()
