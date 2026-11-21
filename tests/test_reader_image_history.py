"""Static image history uses the real checkpoint adapter over memory only."""
import copy
import hashlib
import json
import unittest
from dataclasses import replace

from scripts import reader_image_history as history
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore


KEY = 'library/images\0album/cover.png'
OTHER = 'library/images\0other.jpg'
OP = '1' * 32


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class MemoryJournal:
    def __init__(self):
        self.records = {}
        self.intents = {}
        self.transitions = []
        self.lose_claim = False
        self.lose_finalize = False
        self.before_claim = lambda: None

    def read(self, key):
        return self.records.get(key)

    def claim(self, key, intent, record):
        self.before_claim()
        if key in self.records:
            return False
        self.intents[key] = intent
        self.records[key] = record
        self.transitions.append(record.state)
        if self.lose_claim:
            raise TimeoutError('claim outcome unknown')
        return True

    def advance(self, key, expected, record):
        if self.records.get(key) != expected:
            return False
        self.records[key] = record
        self.transitions.append(record.state)
        if record.state == 'finalized' and self.lose_finalize:
            raise TimeoutError('finalizer response lost')
        return True


class MemoryBucket:
    def __init__(self, raw, on_put):
        self.raw, self.etag = raw, '"parent"'
        self.read_etags = {}
        self.puts = []
        self.on_put = on_put
        self.fault = None
        self.fail_read = False

    def read_bytes(self, bucket, path):
        assert (bucket, path) == (history.BUCKET, history.CANONICAL)
        if self.fail_read:
            self.fail_read = False
            raise TimeoutError('checkpoint readback interrupted')
        self.read_etags[bucket, path] = self.etag
        return self.raw

    def _location(self, bucket):
        assert bucket == history.BUCKET
        return 'vomebook', 'reader-assets-v2'

    def _client(self, namespace):
        assert namespace == 'vomebook'
        return self

    def put_object(self, *, Bucket, Key, Body, ContentType, IfMatch):
        assert (Bucket, Key, ContentType) == ('reader-assets-v2', history.CANONICAL, 'application/json')
        self.puts.append((IfMatch, Body))
        self.on_put()
        if self.fault == 'before' or IfMatch != self.etag:
            raise TimeoutError('no commit')
        self.raw, self.etag = Body, '"target"'
        if self.fault == 'after':
            self.fail_read = True
            raise TimeoutError('response lost')


class ImageHistoryTests(unittest.TestCase):
    def setUp(self):
        repo, path = KEY.split('\0')
        self.source = history.Source(repo, path, 'a' * 40, sha(b'source'), 6)
        old_root = 'pages/image/png/' + 'b' * 64
        new_root = 'pages/image/png/' + 'c' * 64
        old = history.Asset(old_root, old_root + '/document.webp', sha(b'webp'), 4)
        new = history.Asset(new_root, new_root + '/document.avif', sha(b'avif'), 4)
        self.preflight = history.Preflight(self.source, old, new, sha(b'rollback receipt'), sha(b'pinned inputs'))
        self.old_row = {'key': KEY, 'repo': repo, 'source_path': path, 'extension': 'png',
                        'source_revision': self.source.revision, 'source_sha256': self.source.sha256,
                        'source_bytes': self.source.size, 'mode': 'image', 'page_count': 1,
                        'bucket': history.BUCKET, 'root': old.root, 'path': old.path, 'sha256': old.sha256,
                        'bytes': old.size, 'processing_roots': ['reader-index/processing/old.json']}
        self.new_row = {**self.old_row, 'root': new.root, 'path': new.path, 'sha256': new.sha256, 'bytes': new.size,
                        'processing_roots': ['reader-index/processing/new.json']}
        self.parent = {'version': 1, 'kind': 'image-stream-index', 'checkpoint_operation': '2' * 32,
                       'files': [{'key': OTHER, 'mode': 'image', 'path': 'other.webp'}, self.old_row],
                       'failures': [{'key': 'unmapped', 'error': 'retained'}], 'metadata': {'kept': True}}
        self.parent_raw = json.dumps(self.parent).encode()  # Not the checkpoint wire encoding.
        self.journal = MemoryJournal()
        self.bucket = MemoryBucket(self.parent_raw, lambda: self.assertEqual(self.journal.transitions, ['claimed']))
        self.checkpoints = ConditionalCheckpointStore(self.bucket)
        self.before = self.project(self.parent)
        self.after = self.project({**self.parent, 'files': [self.parent['files'][0], self.new_row]})
        self.calls = []
        self.intent = self.seal()

    @staticmethod
    def project(index):
        files = {row['key']: {'s': 2, 'm': 'i', 'b': history.BUCKET, 'p': row['path']} for row in index['files']}
        files['not in image index'] = {'s': 2, 'm': 'p', 'b': 'vomebook/pdf-pages-v2', 'p': 'elsewhere'}
        return {'v': 1, 'f': files, 'v3': {'generation': 'fixture'}}

    def seal(self, *, operation=OP, **kwargs):
        return history.seal(operation, **({'key': KEY, 'parent': self.checkpoints.read(history.BUCKET, history.CANONICAL),
            'old_row_sha256': history.row_digest(self.old_row), 'new_row': self.new_row,
            'checks': history.Checks(self.preflight, self.before, self.after)} | kwargs))

    def fresh(self, intent, phase):
        self.calls.append(phase)
        return history.Checks(self.preflight, self.project(json.loads(intent.parent)),
                              self.project(json.loads(intent.candidate)))

    def run_stage(self, stage, *, intent=None, revalidate=None):
        return stage(self.journal, intent or self.intent, checkpoints=self.checkpoints,
                     revalidate=revalidate or self.fresh)

    def assert_no_publication(self):
        self.assertEqual(self.bucket.puts, [])
        self.assertEqual(self.bucket.raw, self.parent_raw)
        self.assertEqual(self.journal.transitions, [])

    def test_exact_single_row_and_route_then_journal_only_finalize(self):
        self.assertEqual(self.intent.parent_raw_sha256, sha(self.parent_raw))
        self.assertEqual(self.intent.old_row_sha256, history.row_digest(self.old_row))
        self.assertEqual(self.intent.new_row_sha256, history.row_digest(self.new_row))
        result = self.run_stage(history.apply_once)
        self.assertEqual(result.state, 'observed')
        self.assertEqual(self.calls, ['before', 'before', 'after'])
        self.assertEqual(self.bucket.puts, [('"parent"', self.intent.candidate)])
        self.assertEqual(json.loads(self.bucket.raw), {**self.parent, 'checkpoint_operation': OP,
                         'files': [self.parent['files'][0], self.new_row]})
        self.assertEqual(self.run_stage(history.finalize).state, 'finalized')
        self.assertEqual(self.calls[-1], 'after')
        self.assertEqual(self.journal.transitions, ['claimed', 'observed', 'finalized'])
        self.assertEqual(len(self.bucket.puts), 1)

    def test_seal_rejects_unmapped_nonstatic_wrong_source_and_old_digest(self):
        cases = [({'key': 'library/images\0absent.png'}, 'not already mapped'),
                 ({'old_row_sha256': '0' * 64}, 'row digest'),
                 ({'new_row': {**self.new_row, 'source_revision': 'b' * 40}}, 'exact static-image'),
                 ({'new_row': {**self.new_row, 'mode': 'image-page-stream'}}, 'exact static-image'),
                 ({'new_row': {**self.new_row, 'manifest': 'chapters/ebook/chm/foo'}}, 'exact static-image'),
                 ({'new_row': {**self.new_row, 'path': self.preflight.old.path}}, 'exact static-image')]
        for fields, error in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, error):
                self.seal(**fields)
        self.assert_no_publication()

    def test_wrong_fresh_preflight_cannot_publish(self):
        for wrong in (replace(self.preflight, source=replace(self.source, revision='d' * 40)),
                      replace(self.preflight, old=replace(self.preflight.old, size=1)),
                      replace(self.preflight, new=replace(self.preflight.new, sha256='e' * 64)),
                      replace(self.preflight, rollback_sha256='f' * 64),
                      replace(self.preflight, pins_sha256='0' * 64)):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, 'preflight differs'):
                self.run_stage(history.apply_once, revalidate=lambda *_: history.Checks(wrong, self.before, self.after))
            self.assert_no_publication()

    def test_wrong_route_and_unrelated_projection_drift_cannot_publish(self):
        wrong_before, wrong_after, lost_other = (copy.deepcopy(self.before), copy.deepcopy(self.after),
                                                  copy.deepcopy(self.after))
        wrong_before['f'][KEY]['p'] = self.preflight.new.path
        wrong_after['f'][KEY]['b'] = 'foreign/reader-assets-v2'
        del lost_other['f']['not in image index']
        changed_metadata = {**self.after, 'v3': {'generation': 'other'}}
        for before, after in ((wrong_before, self.after), (self.before, wrong_after),
                              (self.before, lost_other), (self.before, changed_metadata)):
            checks = history.Checks(self.preflight, before, after)
            with self.subTest(before=before, after=after):
                with self.assertRaisesRegex(ValueError, 'projected route'):
                    self.seal(checks=checks)
                with self.assertRaisesRegex(ValueError, 'projected route'):
                    self.run_stage(history.apply_once, revalidate=lambda *_: checks)
            self.assert_no_publication()
        # Even an exact one-key delta on a different baseline is not the sealed delta.
        before = {**self.before, 'v3': {'generation': 'other'}}
        with self.assertRaisesRegex(ValueError, 'whole fresh projected route'):
            self.run_stage(history.apply_once, revalidate=lambda *_: history.Checks(self.preflight, before, changed_metadata))
        self.assert_no_publication()

    def test_candidate_and_parent_are_exact(self):
        candidate = json.loads(self.intent.candidate)
        candidate['files'][0]['path'] = 'other.avif'
        changed = replace(self.intent, candidate=json.dumps(candidate).encode())
        with self.assertRaisesRegex(ValueError, 'sealed canonical delta'):
            self.run_stage(history.apply_once, intent=changed)
        self.assert_no_publication()
        for raw, etag in ((self.intent.parent, '"parent"'), (self.parent_raw, '"rotated"')):
            self.bucket.raw, self.bucket.etag = raw, etag
            with self.assertRaisesRegex(ValueError, 'exact canonical parent'):
                self.run_stage(history.apply_once)
            self.assertEqual(self.bucket.puts, [])
            self.assertEqual(self.journal.transitions, [])

    def test_claim_outcome_unknown_or_failed_post_claim_preflight_spends_permission(self):
        self.journal.lose_claim = True
        with self.assertRaisesRegex(TimeoutError, 'claim outcome unknown'):
            self.run_stage(history.apply_once)
        self.assertEqual(self.bucket.puts, [])
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_stage(history.reconcile)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.run_stage(history.apply_once)

        self.journal = MemoryJournal()
        self.journal.before_claim = lambda: setattr(self, 'preflight', replace(self.preflight, pins_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'preflight differs'):
            self.run_stage(history.apply_once)
        self.assertEqual(self.journal.transitions, ['claimed'])
        self.assertEqual(self.bucket.puts, [])
        self.assertEqual(self.bucket.raw, self.parent_raw)

    def test_only_creator_may_write_even_if_competing_claim_is_identical(self):
        def competitor():
            self.journal.before_claim = lambda: None
            self.assertTrue(self.journal.claim(self.intent.claim_key, self.intent,
                                              history.Record(self.intent.sha256, 'claimed')))

        self.journal.before_claim = competitor
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.run_stage(history.apply_once)
        self.assertEqual(self.journal.transitions, ['claimed'])
        self.assertEqual(self.bucket.puts, [])

    def test_unknown_before_cas_and_foreign_bytes_cannot_reconcile_or_replay(self):
        alternate = self.seal(operation='3' * 32)
        self.bucket.fault = 'before'
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed or conflicted'):
            self.run_stage(history.apply_once)
        for raw in (self.parent_raw, self.intent.candidate.rstrip(),
                    self.intent.candidate.replace(b'document.avif', b'foreign.avif')):
            self.bucket.raw = raw
            with self.assertRaisesRegex(ValueError, 'do not replay'):
                self.run_stage(history.reconcile)
            with self.assertRaisesRegex(ValueError, 'no matching observed'):
                self.run_stage(history.finalize)
        for intent in (self.intent, alternate):
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                self.run_stage(history.apply_once, intent=intent)
        self.assertEqual(self.journal.transitions, ['claimed'])
        self.assertEqual(len(self.bucket.puts), 1)

    def test_post_cas_recovery_rechecks_protection_and_route_without_replay(self):
        self.bucket.fault = 'after'
        with self.assertRaisesRegex(TimeoutError, 'readback interrupted'):
            self.run_stage(history.apply_once)
        self.assertEqual(self.bucket.raw, self.intent.candidate)
        wrong = replace(self.preflight, rollback_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'preflight differs'):
            self.run_stage(history.reconcile, revalidate=lambda *_: history.Checks(wrong, self.before, self.after))
        with self.assertRaisesRegex(ValueError, 'projected route'):
            self.run_stage(history.reconcile, revalidate=lambda *_: history.Checks(self.preflight, self.before, self.before))
        self.assertEqual(self.journal.transitions, ['claimed'])
        persisted = self.journal.intents[self.intent.claim_key]
        self.assertEqual(self.run_stage(history.reconcile, intent=persisted).state, 'observed')
        self.assertEqual(len(self.bucket.puts), 1)

    def test_recovery_does_not_advance_if_target_changes_during_fresh_checks(self):
        self.bucket.fault = 'after'
        with self.assertRaises(TimeoutError):
            self.run_stage(history.apply_once)

        def changes_during_check(intent, phase):
            checks = self.fresh(intent, phase)
            self.bucket.raw = self.parent_raw
            return checks

        with self.assertRaisesRegex(ValueError, 'exact canonical target'):
            self.run_stage(history.reconcile, revalidate=changes_during_check)
        self.assertEqual(self.journal.transitions, ['claimed'])
        self.assertEqual(len(self.bucket.puts), 1)

    def test_finalizer_only_rechecks_exact_target_before_and_after_lost_response(self):
        self.run_stage(history.apply_once)
        for raw, etag in ((self.parent_raw, '"parent"'), (self.intent.candidate.rstrip(), '"target"'),
                          (self.intent.candidate, '"rotated"')):
            self.bucket.raw, self.bucket.etag = raw, etag
            with self.assertRaisesRegex(ValueError, 'exact canonical target'):
                self.run_stage(history.finalize)
        self.bucket.raw, self.bucket.etag = self.intent.candidate, '"target"'
        self.journal.lose_finalize = True
        with self.assertRaisesRegex(TimeoutError, 'finalizer response lost'):
            self.run_stage(history.finalize)
        wrong = replace(self.preflight, pins_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'preflight differs'):
            self.run_stage(history.finalize, revalidate=lambda *_: history.Checks(wrong, self.before, self.after))
        self.bucket.raw = self.parent_raw
        with self.assertRaisesRegex(ValueError, 'exact canonical target'):
            self.run_stage(history.finalize)
        self.bucket.raw = self.intent.candidate
        self.assertEqual(self.run_stage(history.finalize).state, 'finalized')
        self.assertEqual(self.journal.transitions, ['claimed', 'observed', 'finalized'])
        self.assertEqual(len(self.bucket.puts), 1)


if __name__ == '__main__':
    unittest.main()
