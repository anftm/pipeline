import base64
import copy
import json
import unittest
from unittest.mock import Mock

from scripts import reader_obsolete_pdf_apply as retirement
from tests import test_reader_obsolete_pdf as fixtures


class ConditionalStore:
    """Opaque revision tokens deliberately unrelated to object hashes."""
    def __init__(self, objects):
        self.objects = objects
        self.versions = {key: 17 for key in objects}
        self.events = []
        self.behavior = None
        self.unreadable = set()
        self.on_stage = None

    def read_bytes(self, bucket, path):
        if (bucket, path) in self.unreadable:
            raise OSError('readback lost')
        return self.objects[bucket, path]

    def read_for_update(self, bucket, path):
        return self.read_bytes(bucket, path), ('server-revision', self.versions[bucket, path])

    def immutable_put(self, bucket, path, raw):
        self.events.append(('intent', bucket, path))
        if (bucket, path) in self.objects:
            if self.objects[bucket, path] != raw:
                raise ValueError('immutable conflict')
        else:
            self.objects[bucket, path] = raw
        if self.on_stage:
            self.on_stage(bucket, path)

    def put_if_match(self, bucket, path, raw, token):
        self.events.append(('cas', bucket, path))
        if token != ('server-revision', self.versions[bucket, path]):
            raise ValueError('conditional conflict')
        mode = self.behavior(bucket, path) if self.behavior else None
        if mode == 'before':
            raise OSError('request lost')
        if mode != 'noop':
            self.objects[bucket, path] = b'foreign' if mode == 'foreign' else raw
            self.versions[bucket, path] += 1
        if mode == 'unreadable':
            self.unreadable.add((bucket, path))
        if mode in ('lost', 'unreadable'):
            raise OSError('response lost')


class ObsoletePDFApplyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ObsoletePDFTests()
        self.fixture.setUp()
        old_root = self.fixture.ccrd_root
        self.fixture.ccrd_root = (retirement.ccrd_lifecycle.BUCKET, retirement.CCRD_ROOT)
        self.fixture.store.objects[self.fixture.ccrd_root] = self.fixture.store.objects.pop(old_root)
        self.store = ConditionalStore(self.fixture.store.objects)
        self.plan = self.fixture.prepare()
        self.window = Mock(return_value=True)

    def apply(self, plan=None, reprepare=None):
        return retirement.apply(self.store, self.plan if plan is None else plan,
                                reprepare or self.fixture.prepare, self.window)

    def test_checkpoint_precedes_intent_and_each_attempt_and_binds_owner(self):
        saved = []
        def checkpoint(value):
            saved.append((copy.deepcopy(value), len(self.store.events)))
        result = retirement.apply(self.store, self.plan, self.fixture.prepare, self.window,
                                  checkpoint=checkpoint, owner={'run_id': '123'})
        self.assertEqual(saved[0][1], 0)
        self.assertFalse(saved[0][0]['intent_verified'])
        attempts = [(row, count) for row, count in saved
                    if any(x['cas_attempts'] == 1 and x['outcome'] == 'unknown' for x in row['roots'])]
        self.assertEqual(len(attempts), 9)
        self.assertEqual([count for _, count in attempts], list(range(1, 10)))
        ref = result['intent_ref']
        self.assertEqual(json.loads(self.store.objects[ref['bucket'], ref['path']])['owner'], {'run_id': '123'})

    def test_failed_checkpoint_prevents_next_cas(self):
        def checkpoint(value):
            if value['intent_verified']:
                raise OSError('journal unavailable')
        with self.assertRaises(OSError):
            retirement.apply(self.store, self.plan, self.fixture.prepare, self.window, checkpoint=checkpoint)
        self.assertEqual([e[0] for e in self.store.events], ['intent'])

    def test_order_durable_intent_exact_bytes_and_no_fake_generation(self):
        before = copy.deepcopy(self.store.objects)
        result = self.apply()
        self.assertEqual(result['outcome'], 'applied')
        self.assertTrue(result['applied'])
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(result['generation'], self.plan['generation'])
        ordered = retirement._ordered(self.plan['mutations'])
        self.assertEqual(self.store.events[0][0], 'intent')
        self.assertEqual(self.store.events[1:], [('cas', m['bucket'], m['path']) for m in ordered])
        self.assertEqual(self.store.events[-2][1:], self.fixture.ccrd_root)
        self.assertEqual(self.store.events[-1][1:], (retirement.BUCKET, retirement.INDEX))
        ref = result['intent_ref']
        raw = self.store.read_bytes(ref['bucket'], ref['path'])
        self.assertEqual(ref['path'], retirement.INTENTS + retirement.proof(raw)['sha256'] + '.json')
        intent = json.loads(raw)
        self.assertEqual(len(intent['mutations']), 9)
        for packed, m in zip(intent['mutations'], ordered):
            for field in ('before', 'after'):
                decoded = base64.b64decode(packed[field + '_base64'])
                self.assertEqual(decoded, m[field])
                self.assertEqual(retirement.proof(decoded), packed[field + '_identity'])
            self.assertEqual(self.store.objects[m['bucket'], m['path']], m['after'])
        changed = {(m['bucket'], m['path']) for m in ordered}
        for key, value in before.items():
            if key not in changed:
                self.assertEqual(self.store.objects[key], value)
        self.assertEqual(set(self.store.objects) - set(before), {(ref['bucket'], ref['path'])})
        self.assertEqual(json.loads(self.store.objects[retirement.BUCKET, retirement.INDEX])['generation'],
                         'unchanged')
        self.assertTrue(all(row['cas_attempts'] == 1 for row in result['roots']))
        self.assertEqual(self.window.call_count, 11)

    def test_wrong_plan_rejected_without_writes(self):
        for mode in ('path', 'before', 'after', 'hash', 'generation', 'receipt', 'extra_timestamp'):
            with self.subTest(mode=mode):
                bad = copy.deepcopy(self.plan)
                if mode in ('path', 'before', 'after'):
                    bad['mutations'][0][mode] = b'bad' if mode != 'path' else 'arbitrary/path'
                elif mode == 'hash':
                    bad['mutations'][0]['after_identity']['sha256'] = '0' * 64
                elif mode == 'generation':
                    bad['generation'] = '0' * 64
                elif mode == 'receipt':
                    bad['observations']['hf']['checked_url'] = 'https://other'
                else:
                    bad['checked_at'] = 'ignored?'
                with self.assertRaisesRegex(ValueError, 'fresh preparation'):
                    self.apply(bad)
                self.assertEqual(self.store.events, [])

    def test_only_receipt_checked_at_may_advance(self):
        fresh = copy.deepcopy(self.plan)
        fresh['observations']['hf']['checked_at'] = 'later-hf'
        fresh['observations']['pages']['checked_at'] = 'later-pages'
        result = self.apply(reprepare=lambda: fresh)
        self.assertTrue(result['applied'])
        intent = json.loads(self.store.read_bytes(retirement.ASSETS, result['intent_ref']['path']))
        self.assertEqual(intent['observations'], fresh['observations'])

    def test_even_matching_reprepare_cannot_widen_root_allowlist(self):
        for mode in ('ccrd', 'bucket', 'manifest', 'index', 'duplicate', 'extra_edit', 'blocked'):
            with self.subTest(mode=mode):
                bad = copy.deepcopy(self.plan)
                if mode == 'ccrd':
                    next(m for m in bad['mutations'] if m['bucket'] == retirement.ccrd_lifecycle.BUCKET)['path'] = 'control/ocr-current.json'
                elif mode == 'bucket':
                    bad['mutations'][0]['bucket'] = 'other/bucket'
                elif mode == 'manifest':
                    bad['mutations'][0]['path'] = 'objects/arbitrary/derived-manifest.json'
                elif mode == 'index':
                    bad['mutations'][-1]['path'] = 'reader-index/v3/current.json'
                elif mode == 'duplicate':
                    bad['mutations'][1] = copy.deepcopy(bad['mutations'][0])
                elif mode == 'blocked':
                    bad['blockers'] = ['blocked']
                else:
                    bad['mutations'][0]['after'] += b' '
                    bad['mutations'][0]['after_identity'] = retirement.proof(bad['mutations'][0]['after'])
                with self.assertRaises(ValueError):
                    self.apply(bad, lambda: bad)
                self.assertEqual(self.store.events, [])

    def test_reordered_trusted_mutations_still_index_last(self):
        plan = copy.deepcopy(self.plan)
        plan['mutations'].reverse()
        result = self.apply(plan, lambda: plan)
        self.assertTrue(result['applied'])
        self.assertEqual(self.store.events[-1][1:], (retirement.BUCKET, retirement.INDEX))

    def test_cas_ambiguous_readback_stops_without_retry_or_rollback(self):
        for mode, expected in [('before', 'unknown'), ('noop', 'unknown'),
                               ('unreadable', 'unknown'), ('foreign', 'conflict')]:
            with self.subTest(mode=mode):
                self.setUp()
                self.store.behavior = lambda b, p: mode
                result = self.apply()
                self.assertEqual(result['outcome'], 'partial')
                self.assertFalse(result['applied'])
                self.assertEqual(result['roots'][0]['outcome'], expected)
                self.assertEqual(result['roots'][0]['cas_attempts'], 1)
                self.assertTrue(all(row['outcome'] == 'not_attempted' for row in result['roots'][1:]))
                self.assertEqual(len(self.store.events), 2)

    def test_lost_cas_response_exact_after_is_recognized_from_own_attempt(self):
        self.store.behavior = lambda b, p: 'lost'
        result = self.apply()
        self.assertTrue(result['applied'])
        self.assertTrue(all(row['recognized'] for row in result['roots']))
        self.assertEqual(len(self.store.events), 10)

    def test_partial_is_inspectable_but_cannot_resume(self):
        ordered = retirement._ordered(self.plan['mutations'])
        self.store.behavior = lambda b, p: 'before' if p == ordered[3]['path'] else None
        result = self.apply()
        self.assertEqual([row['outcome'] for row in result['roots']],
                         ['applied'] * 3 + ['unknown'] + ['not_attempted'] * 5)
        events = list(self.store.events)
        inspection = retirement.inspect(self.store, result['intent_ref'])
        self.assertEqual([row['outcome'] for row in inspection['roots']], ['after'] * 3 + ['before'] * 6)
        self.assertFalse(inspection['resume_authorized'])
        with self.assertRaises(ValueError):
            self.apply()
        # Even a stale callback cannot authorize skipping previously changed roots.
        with self.assertRaisesRegex(ValueError, 'root changed before intent'):
            self.apply(reprepare=lambda: self.plan)
        self.assertEqual(self.store.events, events)

    def test_preexisting_after_is_not_recognized(self):
        first = retirement._ordered(self.plan['mutations'])[0]
        self.store.objects[first['bucket'], first['path']] = first['after']
        with self.assertRaisesRegex(ValueError, 'root changed before intent'):
            self.apply(reprepare=lambda: self.plan)
        self.assertEqual(self.store.events, [])

    def test_drift_or_token_change_after_intent_stops_before_cas(self):
        for mode in ('after', 'foreign', 'token'):
            with self.subTest(mode=mode):
                self.setUp()
                m = retirement._ordered(self.plan['mutations'])[0]
                key = m['bucket'], m['path']
                def drift(b, p):
                    if mode == 'token':
                        self.store.versions[key] += 1
                    else:
                        self.store.objects[key] = m['after'] if mode == 'after' else b'foreign'
                self.store.on_stage = drift
                result = self.apply()
                self.assertEqual(result['roots'][0]['outcome'], 'conflict')
                self.assertEqual(result['roots'][0]['cas_attempts'], 0)
                self.assertEqual(len(self.store.events), 1)

    def test_intent_failure_or_bad_readback_prevents_mutable_writes(self):
        for mode in ('lost', 'bad'):
            with self.subTest(mode=mode):
                self.setUp()
                def fail(b, p):
                    if mode == 'lost':
                        raise OSError('lost staging response')
                    self.store.objects[b, p] = b'bad'
                self.store.on_stage = fail
                result = self.apply()
                self.assertEqual(result['outcome'], 'partial')
                self.assertFalse(result['intent_verified'])
                self.assertEqual(len(self.store.events), 1)
                self.assertTrue(all(row['cas_attempts'] == 0 for row in result['roots']))

    def test_window_loss_prevents_writes_or_stops_partial_attempt(self):
        self.window.return_value = False
        with self.assertRaisesRegex(ValueError, 'writer window'):
            self.apply()
        self.assertEqual(self.store.events, [])
        self.window.side_effect = [True, True, True, False]
        result = self.apply()
        self.assertEqual(result['roots'][0]['outcome'], 'applied')
        self.assertEqual(result['roots'][1]['outcome'], 'unknown')
        self.assertEqual(len(self.store.events), 2)

    def test_missing_conditional_token_fails_before_intent(self):
        original = self.store.read_for_update
        self.store.read_for_update = lambda b, p: (original(b, p)[0], None)
        with self.assertRaisesRegex(ValueError, 'root changed before intent'):
            self.apply()
        self.assertEqual(self.store.events, [])

    def test_inspection_foreign_unknown_and_tampered_intent_are_read_only(self):
        result = self.apply()
        roots = result['roots']
        self.store.objects[roots[0]['bucket'], roots[0]['path']] = b'foreign'
        self.store.unreadable.add((roots[1]['bucket'], roots[1]['path']))
        events = list(self.store.events)
        report = retirement.inspect(self.store, result['intent_ref'])
        self.assertEqual([r['outcome'] for r in report['roots'][:2]], ['foreign', 'unknown'])
        ref = result['intent_ref']
        self.store.objects[ref['bucket'], ref['path']] += b' '
        with self.assertRaisesRegex(ValueError, 'intent proof mismatch'):
            retirement.inspect(self.store, ref)
        self.assertEqual(self.store.events, events)


if __name__ == '__main__':
    unittest.main()
