"""CCRD journal records intents, not cross-object atomicity or writer isolation."""
import hashlib
import json
import unittest

from scripts import ccrd_lifecycle
from scripts import ccrd_transaction as tx

T, R = '1' * 32, 'd' * 40
G, CHILD = 'ccrd-four-' + 'e' * 64, 'f' * 40


def snapshots():
    from tests.test_ccrd_lifecycle import CcrdLifecycleTests
    fixture = CcrdLifecycleTests('test_four_source_pointer_retains_all_databases_metadata_and_txt_archives')
    fixture.setUp(); parent = fixture.four_source_pointer()
    original_prefix = f"generations/{parent['generation']}/"
    parent['generation'] = 'ccrd-four-' + '9' * 64
    parent_prefix = f"generations/{parent['generation']}/"
    parent['metadata'] = [{**row, 'path': row['path'].replace(original_prefix, parent_prefix, 1)} for row in parent['metadata']]
    parent['archives'] = [{**row, 'path': row['path'].replace(original_prefix, parent_prefix, 1)} for row in parent['archives']]
    parent_raw = json.dumps(parent).encode(); parent_sha = hashlib.sha256(parent_raw).hexdigest()
    old_prefix = f"generations/{parent['generation']}/"
    new_prefix = f'generations/{G}/'
    candidate = {**parent, 'generation': G, 'previous_index_manifest_sha256': parent_sha,
                 'metadata': [{**row, 'path': row['path'].replace(old_prefix, new_prefix, 1)} for row in parent['metadata']],
                 'archives': [{**row, 'path': row['path'].replace(old_prefix, new_prefix, 1)} for row in parent['archives']]}
    candidate_raw = ccrd_lifecycle.canonical(candidate); candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
    activation_raw = ccrd_lifecycle.canonical({'version': 1, 'kind': 'ccrd-index-activation',
                                                'bucket': ccrd_lifecycle.BUCKET,
                                                'path': f"generations/{parent['generation']}/manifest.json",
                                                'sha256': parent_sha})
    plan = {'kind': 'ccrd-space-activation-plan', 'space': 'vomebook/Search', 'parent_revision': R,
            'parent_generation': parent['generation'], 'parent_pointer_sha256': parent_sha,
            'candidate_generation': G, 'candidate_manifest_sha256': candidate_sha,
            'parent_activation_hex': activation_raw.hex(),
            'parent_activation_sha256': hashlib.sha256(activation_raw).hexdigest()}
    plan['plan_sha256'] = hashlib.sha256(ccrd_lifecycle.canonical(plan)).hexdigest()
    raw = ccrd_lifecycle.canonical({'version': 1, 'kind': 'ccrd-cw0-durable-intent',
                                    'parent_pointer_hex': parent_raw.hex(), 'candidate_manifest_hex': candidate_raw.hex(),
                                    'activation_plan': plan})
    return raw, parent_sha, candidate_sha, plan['plan_sha256']


INTENT, P, M, A = snapshots()


class MemoryStore:
    def __init__(self):
        self.head = None; self.etag = None; self.records = {}; self.evidence = {}; self.intents = {}; self.writes = 0
        self.lose_head = False; self.lose_record = False; self.lose_evidence = False

    def read_head(self): return self.head, self.etag
    def read_record(self, key): return self.records.get(key)
    def read_evidence(self, key): return self.evidence.get(key)
    def read_intent(self, key): return self.intents.get(key)
    def put_intent(self, key, raw): self.intents[key] = raw
    def put_record(self, key, raw):
        self.records[key] = raw
        if self.lose_record: raise TimeoutError('unknown record write')
    def put_evidence(self, key, raw):
        self.evidence[key] = raw
        if self.lose_evidence: raise TimeoutError('unknown evidence write')
    def put_head(self, raw, expected_etag):
        self.writes += 1
        if self.etag != expected_etag: raise ValueError('CAS conflict')
        self.head = raw; self.etag = f'"v{self.writes}"'
        if self.lose_head: raise TimeoutError('unknown head write')


def evidence(**changes):
    return ccrd_lifecycle.canonical({'kind': 'ccrd-activation-evidence', 'revision': CHILD, 'generation': G,
                                     'manifest_sha256': M, 'space_plan_sha256': A, 'consumer_verified': True,
                                     'activation_verified': True, **changes})


def rollback_evidence(**changes):
    return ccrd_lifecycle.canonical({'kind': 'ccrd-rollback-evidence', 'revision': R, 'parent_sha256': P,
                                     'candidate_generation': G, 'space_plan_sha256': A,
                                     'consumer_parent_verified': True, 'activation_parent_verified': True,
                                     'pointer_parent_verified': True, **changes})


class CcrdTransactionTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore()

    def begin(self):
        return tx.begin(self.store, parent_sha256=P, candidate_generation=G, candidate_manifest_sha256=M,
                        activation_plan_sha256=A, parent_space_revision=R, durable_intent=INTENT, transaction=T)

    def test_each_side_effect_requires_preceding_durable_intent(self):
        c = self.begin(); self.assertEqual(c.record['state'], 'prepared')
        with self.assertRaises(ValueError):
            tx.advance(self.store, c, 'pointer-intent')
        c = tx.advance(self.store, c, 'space-intent')
        c = tx.advance(self.store, c, 'space-verified', space_revision=CHILD, evidence=evidence())
        self.assertEqual(c.record['space_revision'], CHILD)
        self.assertIn(tx._evidence_key(T, c.record['evidence_sha256']), self.store.evidence)
        c = tx.advance(self.store, c, 'pointer-intent')
        c = tx.advance(self.store, c, 'pointer-verified')
        c = tx.advance(self.store, c, 'complete')
        self.assertEqual(c.record['sequence'], 5); self.assertEqual(tx.load(self.store), c)
        self.assertEqual(tx.audit(self.store, c), 6)
        with self.assertRaisesRegex(ValueError, 'already exists'): self.begin()

    def test_evidence_must_match_exact_candidate_and_activation(self):
        c = tx.advance(self.store, self.begin(), 'space-intent')
        for changes in ({'revision': R}, {'generation': 'ccrd-four-' + '0' * 64}, {'consumer_verified': False},
                        {'manifest_sha256': '0' * 64}, {'activation_verified': False}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'evidence identity'):
                tx.advance(self.store, c, 'space-verified', space_revision=CHILD, evidence=evidence(**changes))
        self.assertEqual(tx.load(self.store), c)

    def test_lost_immutable_and_head_responses_are_read_back_without_replay(self):
        self.store.lose_head = self.store.lose_record = True
        c = self.begin(); self.assertEqual(c.record['state'], 'prepared'); self.assertEqual(self.store.writes, 1)
        self.store.lose_head = self.store.lose_record = False
        c = tx.advance(self.store, c, 'space-intent'); self.store.lose_evidence = True
        c = tx.advance(self.store, c, 'space-verified', space_revision=CHILD, evidence=evidence())
        self.assertEqual(len(self.store.evidence), 1); self.assertEqual(c.record['state'], 'space-verified')

    def test_stale_journal_cas_and_missing_evidence_stop(self):
        before = self.begin(); current = tx.advance(self.store, before, 'space-intent')
        with self.assertRaisesRegex(ValueError, 'head changed'): tx.advance(self.store, before, 'space-intent')
        current = tx.advance(self.store, current, 'space-verified', space_revision=CHILD, evidence=evidence())
        self.store.evidence.clear()
        with self.assertRaisesRegex(ValueError, 'evidence is missing'): tx.load(self.store)

    def test_gc_retention_roots_include_both_exact_manifest_resource_sets(self):
        c = self.begin()
        intent_key = tx._intent_key(T, c.record['durable_intent_sha256'])
        value = json.loads(self.store.intents[intent_key])
        refs = ccrd_lifecycle.references(value, intent_key)
        parent, candidate = json.loads(bytes.fromhex(value['parent_pointer_hex'])), json.loads(bytes.fromhex(value['candidate_manifest_hex']))
        expected = {item['path'] for item in ccrd_lifecycle.references(parent, 'current.json')}
        expected |= {item['path'] for item in ccrd_lifecycle.references(candidate, 'current.json')}
        expected.add(f"generations/{candidate['generation']}/manifest.json")
        self.assertEqual({item['path'] for item in refs}, expected)
        self.assertEqual(ccrd_lifecycle.references(c.head, tx.HEAD), [])
        self.assertEqual(ccrd_lifecycle.references(c.record, tx._record_key(T, c.head['record'])), [])
        with self.assertRaisesRegex(ValueError, 'content address'):
            ccrd_lifecycle.references(value, tx._intent_key(T, '0' * 64))
        with self.assertRaisesRegex(ValueError, 'unknown CCRD transaction'):
            ccrd_lifecycle.references(value, 'control/cw0-publications/unknown.json')

    def test_four_bucket_graph_preserves_both_generations_from_durable_intent(self):
        from scripts import reader_gc_graph as graph
        from tests.test_reader_gc_graph import MemoryStore as GraphStore
        c = self.begin(); key = tx._intent_key(T, c.record['durable_intent_sha256'])
        value = json.loads(self.store.intents[key]); refs = ccrd_lifecycle.references(value, key)
        store = GraphStore()
        # The graph's exact reference walker can mark the union without
        # claiming a complete scan or authorizing deletion.
        walker = graph.ReferenceGraph(store)
        walker.walk(value, ccrd_lifecycle.BUCKET, key)
        for ref in refs:
            self.assertIn((ref['bucket'], ref['path']), walker.references)
        self.assertTrue(any(G in ref['path'] for ref in refs))

    def test_durable_intent_is_required_and_rechecked_before_each_transition(self):
        c = self.begin()
        self.assertIn(tx._intent_key(T, c.record['durable_intent_sha256']), self.store.intents)
        self.store.intents.clear()
        with self.assertRaisesRegex(ValueError, 'durable transaction intent is missing'):
            tx.advance(self.store, c, 'space-intent')

    def test_missing_or_mismatched_intent_never_writes_a_head(self):
        invalid = json.loads(INTENT)
        invalid['parent_pointer_hex'] = b'wrong'.hex()
        with self.assertRaisesRegex(ValueError, 'parent bytes differ'):
            tx.begin(self.store, parent_sha256=P, candidate_generation=G, candidate_manifest_sha256=M,
                     activation_plan_sha256=A, parent_space_revision=R, durable_intent=ccrd_lifecycle.canonical(invalid),
                     transaction=T)
        self.assertIsNone(self.store.head); self.assertEqual(self.store.writes, 0)
        invalid = json.loads(INTENT)
        invalid['activation_plan']['parent_activation_hex'] = b'wrong'.hex()
        with self.assertRaisesRegex(ValueError, 'activation plan hash differs'):
            tx.begin(self.store, parent_sha256=P, candidate_generation=G, candidate_manifest_sha256=M,
                     activation_plan_sha256=A, parent_space_revision=R, durable_intent=ccrd_lifecycle.canonical(invalid),
                     transaction=T)
        self.assertIsNone(self.store.head)

    def test_history_audit_rejects_missing_older_immutable_record(self):
        root = self.begin(); c = tx.advance(self.store, root, 'space-intent')
        c = tx.advance(self.store, c, 'space-verified', space_revision=CHILD, evidence=evidence())
        self.store.records.pop(tx._record_key(T, root.head['record']))
        self.assertEqual(tx.load(self.store), c)
        with self.assertRaisesRegex(ValueError, 'history is missing'):
            tx.advance(self.store, c, 'pointer-intent')

    def test_rollback_intent_requires_exact_parent_consumer_activation_and_pointer_evidence(self):
        c = tx.advance(self.store, self.begin(), 'space-intent')
        c = tx.advance(self.store, c, 'rollback-intent')
        self.assertEqual(c.record['state'], 'rollback-intent')
        for bad in ({'parent_sha256': '0' * 64}, {'consumer_parent_verified': False},
                    {'activation_parent_verified': False}, {'pointer_parent_verified': False}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'rollback evidence'):
                tx.advance(self.store, c, 'rolled-back', space_revision=R, evidence=rollback_evidence(**bad))
        c = tx.advance(self.store, c, 'rolled-back', space_revision=R, evidence=rollback_evidence())
        self.assertEqual(c.record['rollback_revision'], R)
        self.assertEqual(c.record['state'], 'rolled-back')
        self.assertEqual(tx.audit(self.store, c), 4)
        self.store.evidence.clear()
        with self.assertRaisesRegex(ValueError, 'rollback evidence is missing'): tx.load(self.store)

    def test_malformed_or_changed_record_is_rejected(self):
        c = self.begin()
        self.store.records[tx._record_key(T, c.head['record'])] = b'broken'
        with self.assertRaisesRegex(ValueError, 'missing or altered'): tx.load(self.store)
        with self.assertRaises(ValueError): tx._head(b'{}')
        with self.assertRaises(ValueError): tx._record(b'{}')


if __name__ == '__main__':
    unittest.main()
