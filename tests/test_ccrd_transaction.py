"""CCRD journal records intents, not cross-object atomicity or writer isolation."""
import unittest

from scripts import ccrd_lifecycle
from scripts import ccrd_transaction as tx

T, P, M, A, R = '1' * 32, 'a' * 64, 'b' * 64, 'c' * 64, 'd' * 40
G, CHILD = 'ccrd-four-' + 'e' * 64, 'f' * 40


class MemoryStore:
    def __init__(self):
        self.head = None; self.etag = None; self.records = {}; self.evidence = {}; self.writes = 0
        self.lose_head = False; self.lose_record = False; self.lose_evidence = False

    def read_head(self): return self.head, self.etag
    def read_record(self, key): return self.records.get(key)
    def read_evidence(self, key): return self.evidence.get(key)
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


class CcrdTransactionTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore()

    def begin(self):
        return tx.begin(self.store, parent_sha256=P, candidate_generation=G, candidate_manifest_sha256=M,
                        activation_plan_sha256=A, parent_space_revision=R, transaction=T)

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

    def test_history_audit_rejects_missing_older_immutable_record(self):
        root = self.begin(); c = tx.advance(self.store, root, 'space-intent')
        c = tx.advance(self.store, c, 'space-verified', space_revision=CHILD, evidence=evidence())
        self.store.records.pop(tx._record_key(T, root.head['record']))
        self.assertEqual(tx.load(self.store), c)
        with self.assertRaisesRegex(ValueError, 'history is missing'):
            tx.advance(self.store, c, 'pointer-intent')

    def test_malformed_or_changed_record_is_rejected(self):
        c = self.begin()
        self.store.records[tx._record_key(T, c.head['record'])] = b'broken'
        with self.assertRaisesRegex(ValueError, 'missing or altered'): tx.load(self.store)
        with self.assertRaises(ValueError): tx._head(b'{}')
        with self.assertRaises(ValueError): tx._record(b'{}')


if __name__ == '__main__':
    unittest.main()
