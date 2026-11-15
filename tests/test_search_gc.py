"""Real in-memory manifests, terminal journal history and partial GC recovery."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import search_gc as gc, search_transition as tx
from scripts.search_artifact_store import make_manifest, object_key
from scripts.search_publication_pointer import make_pointer
from tests.test_search_transition import MemoryStore as JournalMemory, receipt, default_receipt, REV, WHEN

NOW = 100 * gc.DAY


class MemoryStore:
    def __init__(self):
        self.files, self.meta = {}, {}
        self.now = NOW
        self.writes, self.deletes, self.heads = [], [], []
        self.fail_delete = None
        self.fail_after = True
        self.fail_create = False
        self.on_delete = None

    def put(self, key, raw, age=0, size=None):
        self.files[key] = raw
        self.meta[key] = {'key': key, 'etag': '"' + hashlib.sha256(raw).hexdigest() + '"',
                          'size': len(raw) if size is None else size, 'mtime': self.now - age}

    def inventory(self):
        return [dict(m) for m in self.meta.values()]

    def head(self, key):
        self.heads.append(key)
        return copy.deepcopy(self.meta.get(key))

    def read(self, key, expected=None):
        if expected is not None and self.meta.get(key) != expected:
            raise ValueError('changed read')
        return self.files.get(key)

    def create(self, key, raw):
        self.writes.append(key)
        if key in self.files:
            raise FileExistsError(key)
        self.put(key, raw)
        if self.fail_create:
            raise TimeoutError('lost create response')

    def remove(self, key):
        del self.files[key]
        del self.meta[key]

    def delete(self, item):
        key = item['key']
        self.deletes.append(key)
        if self.meta.get(key) != item:
            raise ValueError('conditional conflict')
        if key == self.fail_delete and not self.fail_after:
            raise TimeoutError('unknown before delete')
        self.remove(key)
        if self.on_delete:
            self.on_delete(key)
        if key == self.fail_delete:
            raise TimeoutError('unknown after delete')

    def generation(self, name, age, shared=(), count=1, size=None):
        members = {}
        for i, raw in enumerate([name.encode() + str(n).encode() for n in range(count)] + list(shared)):
            digest = hashlib.sha256(raw).hexdigest()
            key = object_key(digest)
            if key not in self.files:
                self.put(key, raw, age, size=size)
            members[f'{i}.json'] = {'sha256': digest, 'bytes': len(raw) if size is None else size}
        identity = {'version': 1, 'kind': 'search-snapshot-local', 'record_count': 1,
                    'files': {'pages': members, 'space': members}, 'source_revisions': {name: REV}}
        generation = hashlib.sha256(gc.canonical(identity)).hexdigest()
        manifest = make_manifest({**identity, 'generation': generation, 'remote_writes': 0,
                                  'publication_ready': False})
        key = f'generations/{generation}/manifest.json'
        self.put(key, gc.canonical(manifest), age)
        return key

    def publish(self, desired, previous, transition='1' * 32, history=None):
        # Build and audit an actual terminal journal using the production state machine.
        desired, previous = desired.split('/')[1], previous.split('/')[1]
        s = history or JournalMemory()
        if history is None:
            s.pointer = make_pointer(previous, None, '0' * 32)
        c = tx.begin(s, parent_pointer=s.pointer, candidate=desired, source_revisions_sha256='d' * 64,
                     pages_receipt=receipt('pages', previous), space_receipt=receipt('space', previous),
                     pages_default=default_receipt('pages', previous),
                     space_default=default_receipt('space', previous), transition=transition)
        for site in ('pages', 'space'):
            kwargs = {'config_before': {'generation': previous, 'updated_at': WHEN}} if site == 'space' else {}
            c = tx.start_site(s, c, site=site, operation_id='3' * 32,
                              expected_revision=REV, created_at=WHEN, **kwargs)
            c = tx.observe_site(s, c, site=site, generation=desired, receipt=receipt(site, desired))
        c = tx.mark_publishing(s, c, pages_receipt=receipt('pages', desired), space_receipt=receipt('space', desired))
        c = tx.publish_pointer(s, s, c, pages_receipt=receipt('pages', desired), space_receipt=receipt('space', desired))
        for site in ('pages', 'space'):
            c = tx.start_default(s, c, site=site, operation_id='4' * 32, expected_revision=REV, created_at=WHEN)
            c = tx.observe_default(s, c, site=site, receipt=default_receipt(site, desired))
        tx.finish(s, c, pages_receipt=receipt('pages', desired), space_receipt=receipt('space', desired),
                  pages_default=default_receipt('pages', desired), space_default=default_receipt('space', desired))
        for key, raw in {**s.records, **s.receipts, tx.HEAD_KEY: s.head, 'published/current.json': s.pointer}.items():
            self.put(key, raw)
        return s


class SearchGCTests(unittest.TestCase):
    def setUp(self):
        self.s = MemoryStore()
        self.old = self.s.generation('old', 30 * gc.DAY, shared=(b'shared',))
        self.previous = self.s.generation('previous', 20 * gc.DAY)
        self.current = self.s.generation('current', 19 * gc.DAY, shared=(b'shared',))
        self.staged = self.s.generation('staged', 10 * gc.DAY)
        self.newest = self.s.generation('newest', 9 * gc.DAY)
        self.journal = self.s.publish(self.current, self.previous)

    def mark(self, run='100'):
        return gc.mark(self.s, run_id=run, now=self.s.now, known_writers_coordinated=True)

    def sweep(self, run='200', key='gc/plans/100.json'):
        self.s.now = max(self.s.now, NOW + gc.DAY)
        return gc.sweep(self.s, plan_key=key, run_id=run, now=self.s.now, known_writers_coordinated=True)

    def test_retention_current_previous_newest_two_and_young(self):
        young = self.s.generation('young', gc.DAY)
        young2 = self.s.generation('young2', 2 * gc.DAY)
        young3 = self.s.generation('young3', 3 * gc.DAY)
        value = gc.plan(self.s, run_id='100', now=NOW)
        self.assertEqual(set(value['manifests']), {self.old, self.staged})
        self.assertTrue({self.current, self.previous, young, young2, young3}.isdisjoint(value['manifests']))
        self.assertEqual(self.s.writes, [])
        self.assertEqual(self.s.deletes, [])

    def test_finite_retirement_shared_objects_and_metadata_preserved(self):
        before = {k: v for k, v in self.s.files.items() if k.startswith('published/')}
        for key in ('probes/keep', 'receipts/keep', 'journals/keep', 'other/keep'):
            self.s.put(key, b'untouched')
            before[key] = b'untouched'
        value = self.mark()['plan']
        self.assertEqual(set(value['manifests']), {self.old})
        self.assertNotIn(object_key(hashlib.sha256(b'shared').hexdigest()), value['objects'])
        result = self.sweep()
        self.assertEqual(result['status'], 'complete')
        self.assertNotIn(self.old, self.s.files)
        self.assertEqual(set(self.s.deletes), {self.old, *value['objects']})
        self.assertEqual(self.s.deletes[0], self.old)
        self.assertEqual(before, {k: self.s.files[k] for k in before})
        self.assertGreater(tx.audit_history(gc.JournalView(self.s), tx.load(gc.JournalView(self.s))), 1)
        with self.assertRaisesRegex(ValueError, 'same run/rerun'):
            self.sweep()

    def test_old_terminal_history_does_not_pin_payload(self):
        history = self.s.publish(self.previous, self.old, transition='2' * 32)
        self.s.publish(self.current, self.previous, transition='3' * 32, history=history)
        self.assertEqual(tx.audit_history(gc.JournalView(self.s), tx.load(gc.JournalView(self.s))), 24)
        historical = {k: v for k, v in self.s.files.items() if k.startswith('published/transitions/')}
        self.mark()
        self.sweep()
        self.assertNotIn(self.old, self.s.files)
        self.assertEqual(historical, {k: self.s.files[k] for k in historical})

    def test_new_manifest_reference_revokes_object_delete(self):
        value = self.mark()['plan']
        victim = next(iter(value['objects']))
        future = self.s.generation('future', 0, shared=(self.s.files[victim],))
        self.sweep()
        self.assertIn(victim, self.s.files)
        self.assertIn(future, self.s.files)
        self.assertNotIn(self.old, self.s.files)

    def test_new_current_or_previous_revokes_manifest_delete(self):
        for use_as_current in (True, False):
            with self.subTest(current=use_as_current):
                self.setUp()
                self.mark()
                self.s.publish(self.old if use_as_current else self.current,
                               self.previous if use_as_current else self.old, transition='5' * 32)
                result = self.sweep()
                self.assertIn(self.old, self.s.files)
                self.assertIn(self.old, result['cancelled_manifests'])
                self.assertEqual(self.s.deletes, [])

    def test_age_mark_delay_and_same_run_attempt(self):
        self.mark()
        for now in (NOW, NOW + gc.DAY - 0.001):
            with self.assertRaisesRegex(ValueError, '24h'):
                gc.sweep(self.s, plan_key='gc/plans/100.json', run_id='200', now=now,
                         known_writers_coordinated=True)
        with self.assertRaisesRegex(ValueError, 'same run/rerun'):
            gc.sweep(self.s, plan_key='gc/plans/100.json', run_id='100', run_attempt='2',
                     now=NOW + gc.DAY, known_writers_coordinated=True)
        self.assertEqual(self.s.deletes, [])
        self.sweep()

    def test_nonterminal_missing_malformed_and_unknown_fail_closed(self):
        cases = ['head', 'object', 'manifest', 'identity', 'unknown-object', 'unknown-manifest']
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                if case == 'head':
                    self.s.remove(tx.HEAD_KEY)
                elif case == 'object':
                    self.s.remove(next(iter(gc.manifest_refs(self.s.files[self.old], self.old))))
                elif case == 'manifest':
                    self.s.put(self.old, b'{bad')
                elif case == 'identity':
                    value = json.loads(self.s.files[self.old]); value['record_count'] = 2
                    self.s.put(self.old, gc.canonical(value))
                else:
                    self.s.put('objects/unknown' if case == 'unknown-object' else 'generations/unknown', b'{}')
                with self.assertRaises(ValueError):
                    self.mark()
                self.assertEqual(self.s.writes, [])
                self.assertEqual(self.s.deletes, [])
        self.setUp()
        c = tx.begin(self.journal, parent_pointer=self.journal.pointer, candidate=self.old.split('/')[1],
                     source_revisions_sha256='e' * 64,
                     pages_receipt=receipt('pages', self.current.split('/')[1]),
                     space_receipt=receipt('space', self.current.split('/')[1]),
                     pages_default=default_receipt('pages', self.current.split('/')[1]),
                     space_default=default_receipt('space', self.current.split('/')[1]), transition='6' * 32)
        for k, raw in {**self.journal.records, **self.journal.receipts, tx.HEAD_KEY: c.head_raw}.items():
            self.s.put(k, raw)
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.mark()

    def test_partial_manifest_unknown_stops_then_independent_run_recovers(self):
        value = self.mark()['plan']
        self.s.fail_delete = self.old
        with self.assertRaises(TimeoutError):
            self.sweep()
        self.assertEqual(self.s.deletes, [self.old])
        self.assertIn(self.old, self.s.heads)
        self.assertTrue(set(value['objects']) <= self.s.files.keys())
        self.assertEqual(json.loads(self.s.files['gc/results/200.json'])['status'], 'stopped')
        self.s.fail_delete = None
        self.sweep(run='201')
        self.assertTrue(set(value['objects']).isdisjoint(self.s.files))
        self.assertEqual(self.s.deletes.count(self.old), 1)

    def test_partial_object_unknown_and_future_reference(self):
        self.s.generation('also-old', 29 * gc.DAY)
        value = self.mark()['plan']
        victim = next(iter(value['objects']))
        self.s.fail_delete = victim
        with self.assertRaises(TimeoutError):
            self.sweep()
        pending = set(value['objects']) & self.s.files.keys()
        self.assertTrue(pending)
        keep = next(iter(pending))
        self.s.generation('future', 0, shared=(self.s.files[keep],))
        self.s.fail_delete = None
        self.sweep(run='201')
        self.assertIn(keep, self.s.files)
        self.assertEqual(self.s.deletes.count(victim), 1)

    def test_unknown_before_delete_does_not_retry_even_if_still_present(self):
        self.mark()
        self.s.fail_delete, self.s.fail_after = self.old, False
        with self.assertRaises(TimeoutError):
            self.sweep()
        self.assertEqual(self.s.deletes, [self.old])
        self.assertIn(self.old, self.s.files)
        with self.assertRaisesRegex(ValueError, 'same run/rerun'):
            self.sweep()

    def test_missing_unplanned_manifest_blocks_partial_recovery(self):
        self.mark()
        self.s.remove(self.staged)
        with self.assertRaisesRegex(ValueError, 'unexpected missing manifest'):
            self.sweep()
        self.assertEqual(self.s.deletes, [])

    def test_changed_manifest_or_object_never_deleted(self):
        self.mark()
        self.s.put(self.old, self.s.files[self.old])  # same bytes, new mtime
        self.sweep()
        self.assertIn(self.old, self.s.files)
        self.assertEqual(self.s.deletes, [])

    def test_budget_limits_and_invalid_plan_evidence(self):
        for n in range(4):
            self.s.generation(f'old-{n}', (40 + n) * gc.DAY)
        value = gc.plan(self.s, run_id='100', now=NOW)
        self.assertEqual(len(value['manifests']), 2)
        self.s.generation('huge-count', 60 * gc.DAY, count=101)
        self.s.generation('huge-bytes', 61 * gc.DAY, count=5, size=32 * 1024 * 1024)
        value = gc.plan(self.s, run_id='100', now=NOW)
        self.assertLessEqual(len(value['objects']), 100)
        self.assertLessEqual(sum(x['size'] for x in value['objects'].values()), gc.MAX_BYTES)
        gc.validate_plan(value)
        tampered = copy.deepcopy(value)
        next(iter(tampered['objects'].values()))['size'] += 1
        with self.assertRaises(ValueError):
            gc.validate_plan(tampered)

    def test_exact_seven_days_and_tied_newest(self):
        self.s.meta[self.old]['mtime'] = NOW - 7 * gc.DAY
        # Other unprotected generations can now be retired, but old is newest.
        self.assertNotIn(self.old, gc.plan(self.s, run_id='100', now=NOW)['manifests'])
        self.s.generation('fresh1', gc.DAY)
        self.s.generation('fresh2', gc.DAY)
        self.assertIn(self.old, gc.graph(self.s, NOW)['manifests'])
        self.assertNotIn(self.old, gc.graph(self.s, NOW)['protected'])
        self.s.meta[self.old]['mtime'] += 0.001
        self.assertIn(self.old, gc.graph(self.s, NOW)['protected'])

    def test_coordination_required_and_mark_unknown_never_retried(self):
        for fn in (gc.mark, gc.sweep, gc.auto):
            with self.assertRaisesRegex(ValueError, 'known-writers-coordinated'):
                fn(self.s, run_id='100', now=NOW)
        self.assertEqual(self.s.writes, [])
        self.s.fail_create = True
        with self.assertRaises(TimeoutError):
            self.mark()
        self.assertEqual(self.s.writes, ['gc/plans/100.json'])
        self.assertEqual(self.s.deletes, [])

    def test_auto_one_action_and_empty_skip_cli_preview(self):
        self.assertEqual(gc.auto(self.s, run_id='100', now=NOW, known_writers_coordinated=True)['status'], 'marked')
        self.s.now += gc.DAY
        result = gc.auto(self.s, run_id='200', now=self.s.now, known_writers_coordinated=True)
        self.assertEqual(result['status'], 'complete')
        self.assertNotIn('gc/plans/200.json', self.s.files)
        self.assertEqual(gc.auto(self.s, run_id='300', now=self.s.now,
                                 known_writers_coordinated=True)['status'], 'skip')
        self.assertEqual(gc.sweep(self.s, run_id='301', now=self.s.now,
                                  known_writers_coordinated=True)['status'], 'skip')
        before = list(self.s.writes)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            with patch('scripts.search_gc_s3.S3SearchGCStore', return_value=self.s), patch.object(gc.time, 'time', return_value=self.s.now), patch('builtins.print'):
                self.assertEqual(gc.main(['--mode', 'preview', '--run-id', '400', '--run-attempt', '2',
                                          '--output', str(output)]), 0)
            report = json.loads(output.read_bytes())
            self.assertEqual(report['remote_writes'], 0)
            self.assertEqual(report['run_attempt'], '2')
        self.assertEqual(self.s.writes, before)

    def test_old_orphans_without_retired_manifest_and_new_references(self):
        for key in (self.old, self.staged, self.newest):
            self.s.meta[key]['mtime'] = NOW  # all manifests now protected
        raw = b'old orphan'
        orphan = object_key(hashlib.sha256(raw).hexdigest())
        self.s.put(orphan, raw, 8 * gc.DAY)
        young = object_key(hashlib.sha256(b'young orphan').hexdigest())
        self.s.put(young, b'young orphan', gc.DAY)
        marked = self.mark()['plan']
        self.assertFalse(marked['manifests'])
        self.assertEqual(set(marked['objects']), {orphan})
        self.assertEqual(marked['objects'], marked['orphan_objects'])
        self.s.generation('new-ref', 0, shared=(raw,))
        self.sweep()
        self.assertIn(orphan, self.s.files)
        self.assertIn(young, self.s.files)
        self.assertNotIn(orphan, self.s.deletes)

    def test_large_generation_retires_and_leftover_orphans_progress_across_runs(self):
        for count, size in ((105, None), (5, 32 * 1024 * 1024)):
            with self.subTest(count=count, size=size):
                self.setUp()
                large = self.s.generation('large', 60 * gc.DAY, count=count, size=size)
                refs = set(gc.manifest_refs(self.s.files[large], large))
                first = self.mark()['plan']
                self.assertIn(large, first['manifests'])
                self.assertLessEqual(len(first['objects']), 100)
                self.assertLessEqual(sum(i['size'] for i in first['objects'].values()), gc.MAX_BYTES)
                self.assertGreater(len(refs - first['objects'].keys()), 0)
                self.sweep()
                self.assertNotIn(large, self.s.files)
                leftover = refs & self.s.files.keys()
                self.assertTrue(leftover)
                second = self.mark(run='300')['plan']
                self.assertFalse(second['manifests'])
                self.assertTrue(leftover <= second['orphan_objects'].keys())
                self.s.now += gc.DAY
                self.sweep(run='400', key='gc/plans/300.json')
                self.assertTrue(refs.isdisjoint(self.s.files))
                self.assertEqual(len(self.s.deletes), len(set(self.s.deletes)))

    def test_clock_advances_on_metadata_write_and_partial_recovery(self):
        create = self.s.create
        def advancing_create(key, raw):
            self.s.now += 0.25
            return create(key, raw)
        self.s.create = advancing_create
        self.mark()
        self.s.now = NOW + gc.DAY + 1
        self.s.fail_delete = self.old
        with self.assertRaises(TimeoutError):
            self.sweep()
        self.s.fail_delete = None
        self.assertEqual(self.sweep(run='201')['status'], 'complete')
        # Also cover the success path's second graph seeing this run's attempt
        # with LastModified strictly greater than the fixed invocation now.
        self.setUp()
        create = self.s.create
        self.s.create = advancing_create
        self.mark()
        self.s.now = NOW + gc.DAY + 1
        self.assertEqual(self.sweep()['status'], 'complete')

    def test_three_live_generations_preview_explains_retention_and_skip(self):
        self.s = MemoryStore()
        previous = self.s.generation('previous', 20 * gc.DAY)
        current = self.s.generation('current', 10 * gc.DAY)
        newest = self.s.generation('staged', gc.DAY)
        self.s.publish(current, previous)
        result = self.mark()
        self.assertEqual(result['status'], 'skip')
        self.assertEqual(self.s.writes, [])
        retained = result['plan']['retained_generations']
        self.assertIn('current-previous', retained[previous])
        self.assertIn('current-desired', retained[current])
        self.assertIn('newest-at-least-two-including-ties', retained[current])
        self.assertIn('age-less-than-seven-days', retained[newest])
        self.assertEqual(result['plan']['objects'], {})

    def test_orphan_unknown_partial_next_run_absent_and_recreated_identity(self):
        for key in (self.old, self.staged, self.newest):
            self.s.meta[key]['mtime'] = NOW
        raw = b'orphan-one'
        orphan = object_key(hashlib.sha256(raw).hexdigest())
        self.s.put(orphan, raw, 10 * gc.DAY)
        self.mark()
        self.s.fail_delete = orphan
        with self.assertRaises(TimeoutError):
            self.sweep()
        self.s.fail_delete = None
        self.assertEqual(self.sweep(run='201')['status'], 'complete')
        self.assertEqual(self.s.deletes.count(orphan), 1)
        # Recreating the same content-addressed key after GC is supported; old
        # plan metadata must not authorize removal of a newer incarnation.
        self.s.put(orphan, raw)
        self.sweep(run='202')
        self.assertIn(orphan, self.s.files)
        self.assertEqual(self.s.deletes.count(orphan), 1)

    def interleaved_plans(self, second_stopped=False):
        first = self.mark()['plan']
        self.s.fail_delete = self.old
        with self.assertRaises(TimeoutError):
            self.sweep()
        self.s.fail_delete = None
        # A's stopped sweep removed its manifest, leaving its objects. B can
        # now retire manifests which were protected when A captured inventory.
        self.s.generation('fresh-one', 0)
        self.s.generation('fresh-two', 0)
        second = self.mark(run='300')['plan']
        self.assertEqual(set(second['manifests']), {self.staged, self.newest})
        self.s.now += gc.DAY
        if second_stopped:
            self.s.fail_delete = next(iter(second['manifests']))
            with self.assertRaises(TimeoutError):
                self.sweep(run='400', key='gc/plans/300.json')
            self.s.fail_delete = None
        else:
            self.sweep(run='400', key='gc/plans/300.json')
        return first, second

    def test_oldest_stopped_plan_recovers_after_another_completed_retirement(self):
        first, second = self.interleaved_plans()
        result = gc.auto(self.s, run_id='500', now=self.s.now, known_writers_coordinated=True)
        self.assertEqual(result['plan_key'], 'gc/plans/100.json')
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(set(first['objects']).isdisjoint(self.s.files))
        self.assertTrue(set(second['manifests']).isdisjoint(self.s.files))
        self.assertEqual(len(self.s.deletes), len(set(self.s.deletes)))
        self.assertIsNone(gc.oldest_plan(self.s, run_id='600', now=self.s.now))

    def test_two_stopped_plans_recover_in_order_after_interleaved_retirements(self):
        first, second = self.interleaved_plans(second_stopped=True)
        for run, key in (('500', 'gc/plans/100.json'), ('600', 'gc/plans/300.json')):
            result = gc.auto(self.s, run_id=run, now=self.s.now, known_writers_coordinated=True)
            self.assertEqual(result['plan_key'], key)
            self.assertEqual(result['status'], 'complete')
        self.assertTrue((set(first['objects']) | set(second['objects'])).isdisjoint(self.s.files))
        self.assertEqual(len(self.s.deletes), len(set(self.s.deletes)))

    def test_other_plan_evidence_never_excuses_unexplained_missing_manifest(self):
        mutations = ('no-result', 'no-attempt', 'no-plan', 'hash', 'run', 'attempt',
                     'no-event', 'absent-only', 'present-readback', 'different-etag',
                     'different-size', 'different-mtime', 'not-selected', 'cancelled')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.setUp()
                self.interleaved_plans(second_stopped=True)
                result_key, plan_key = 'gc/results/400.json', 'gc/plans/300.json'
                result = json.loads(self.s.files[result_key])
                retired = result['events'][0]['key']
                if mutation.startswith('no-') and mutation != 'no-event':
                    key = {'no-result': result_key, 'no-attempt': 'gc/attempts/400.json',
                           'no-plan': plan_key}[mutation]
                    self.s.remove(key)
                elif mutation in {'different-etag', 'different-size', 'different-mtime'}:
                    # Change the old observer's exact identity. A valid deletion
                    # of the same key in B does not prove removal of this version.
                    original = json.loads(self.s.files['gc/plans/100.json'])
                    field = mutation.removeprefix('different-')
                    original['manifest_inventory'][retired][field] = (
                        '"another"' if field == 'etag' else original['manifest_inventory'][retired][field] + 1)
                    self.s.files['gc/plans/100.json'] = gc.canonical(original)
                    # Keep A's own result/attempt internally bound so failure
                    # specifically exercises the cross-plan exact identity check.
                    digest = hashlib.sha256(self.s.files['gc/plans/100.json']).hexdigest()
                    for key in ('gc/results/200.json', 'gc/attempts/200.json'):
                        evidence = json.loads(self.s.files[key])
                        evidence['plan_sha256'] = digest
                        self.s.files[key] = gc.canonical(evidence)
                else:
                    if mutation == 'hash':
                        result['plan_sha256'] = '0' * 64
                    elif mutation == 'run':
                        result['run_id'] = '999'
                    elif mutation == 'attempt':
                        result['run_attempt'] = '2'
                    elif mutation == 'no-event':
                        result['events'] = []
                    elif mutation == 'absent-only':
                        result['events'][0] = {'key': retired, 'status': 'absent'}
                    elif mutation == 'present-readback':
                        result['events'][0]['readback'] = {'key': retired}
                    elif mutation == 'not-selected':
                        value = json.loads(self.s.files[plan_key])
                        result['events'][0]['key'] = next(iter(value['objects']))
                    elif mutation == 'cancelled':
                        result['cancelled_manifests'] = [retired]
                    self.s.files[result_key] = gc.canonical(result)
                before = (list(self.s.deletes), list(self.s.writes))
                with self.assertRaises(ValueError):
                    self.sweep(run='500')
                self.assertEqual((self.s.deletes, self.s.writes), before)

    def test_auto_rejects_unbound_complete_result_instead_of_skipping_plan(self):
        self.interleaved_plans()
        result = json.loads(self.s.files['gc/results/400.json'])
        result['plan_key'] = 'gc/plans/100.json'
        self.s.files['gc/results/400.json'] = gc.canonical(result)
        before = list(self.s.deletes)
        with self.assertRaisesRegex(ValueError, 'plan identity'):
            gc.auto(self.s, run_id='500', now=self.s.now, known_writers_coordinated=True)
        self.assertEqual(self.s.deletes, before)

    def test_journal_change_between_delete_requests_stops_before_next_delete(self):
        self.s.generation('also-old', 31 * gc.DAY)
        self.mark()
        def change_journal(key):
            self.s.on_delete = None
            self.s.publish(self.current, self.previous, transition='7' * 32)
        self.s.on_delete = change_journal
        with self.assertRaisesRegex(ValueError, 'changed during the daily run'):
            self.sweep()
        self.assertEqual(len(self.s.deletes), 1)
        self.assertEqual(json.loads(self.s.files['gc/results/200.json'])['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
