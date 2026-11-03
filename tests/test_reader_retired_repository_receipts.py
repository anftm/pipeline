from datetime import datetime, timezone
import hashlib
import json
import unittest

from scripts import reader_producer_scope_transition as scope
from scripts import reader_retired_repository_receipts as receipts


class DeletionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.repo_id = next(iter(scope.EXPECTED.items()))
        self.clock = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.receipt = {'version': 1, 'kind': 'retired-pipeline-repository-deletion',
                        'repository': self.repo, 'repository_id': self.repo_id,
                        'owner': self.repo.split('/')[0], 'git_bundle_sha256': 'a' * 64,
                        'actions_backup_sha256': 'b' * 64, 'intent_sha256': 'c' * 64,
                        'deleted_at': '2026-11-02T23:59:00Z', 'delete_http': 204, 'owner_http': 200,
                        'owner_matches': True, 'name_get_http': 404, 'id_get_http': 404}

    def encode(self, value):
        return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()

    def test_exact_receipt_is_deletion_evidence_only(self):
        raw = self.encode(self.receipt)
        result = receipts.verify(raw, self.repo, self.repo_id, now=self.clock,
                                 expected_sha256=hashlib.sha256(raw).hexdigest())
        self.assertTrue(result['deletion_only'])
        self.assertFalse(result['writer_isolation_verified'])
        self.assertFalse(result['gc_authorized'])
        with self.assertRaisesRegex(RuntimeError, 'external-writer isolation'):
            receipts.require_isolated([result], {'claimed': True})

    def test_404_without_ack_owner_and_exact_identity_never_suffices(self):
        alters = {
            'no-delete': {'delete_http': 404}, 'owner-denied': {'owner_http': 403},
            'owner-false': {'owner_matches': False}, 'name-live': {'name_get_http': 200},
            'id-live': {'id_get_http': 200}, 'wrong-id': {'repository_id': self.repo_id + 1},
            'wrong-name': {'repository': 'foreign/repo'}, 'wrong-owner': {'owner': 'foreign'},
            'boolean-status': {'delete_http': True}, 'bad-hash': {'intent_sha256': 'a' * 63},
            'missing-log-backup': {'actions_backup_sha256': None},
            'future-date': {'deleted_at': '2026-11-04T00:00:00Z'},
            'naive-date': {'deleted_at': '2026-11-02T23:59:00'},
        }
        for name, change in alters.items():
            value = {**self.receipt, **change}
            with self.subTest(name=name), self.assertRaises(ValueError):
                receipts.verify(self.encode(value), self.repo, self.repo_id, now=self.clock)

    def test_duplicate_keys_hash_mismatch_and_foreign_repos_are_rejected(self):
        raw = self.encode(self.receipt)
        duplicate = raw.replace(b'"version":1', b'"version":0,"version":1')
        with self.assertRaises(ValueError):
            receipts.verify(duplicate, self.repo, self.repo_id, now=self.clock)
        with self.assertRaises(ValueError):
            receipts.verify(raw, self.repo, self.repo_id, now=self.clock, expected_sha256='0' * 64)
        with self.assertRaises(ValueError):
            receipts.verify(raw, 'anftm/pipeline', 1260985830, now=self.clock)

    def test_actual_list_of_nine_identities_has_no_duplicates(self):
        all_ids = {**scope.EXPECTED, **scope.ADDITIONAL}
        self.assertEqual(len(all_ids), 9)
        self.assertEqual(len(set(all_ids.values())), 9)
        self.assertNotIn('anftm/pipeline', all_ids)


if __name__ == '__main__':
    unittest.main()
