import copy
import unittest

from scripts import reader_producer_scope_transition as scope, reader_producer_writer_proofs as proof


class WriterProofTests(unittest.TestCase):
    def setUp(self):
        ids = {**scope.EXPECTED, **scope.ADDITIONAL}
        required = proof.requirements()
        self.records = {repo: {'repository_id': repo_id, 'secret_names': required[repo]}
                        for repo, repo_id in ids.items()}

    def test_complete_inventory_is_never_an_isolation_or_gc_proof(self):
        inventory = proof.build_inventory(self.records)
        self.assertEqual(len(inventory['repositories']), 9)
        self.assertFalse(inventory['external_writer_isolation_verified'])
        self.assertFalse(inventory['gc_authorized'])
        self.assertTrue(all(secret['source_status'] == 'unknown'
                            for repo in inventory['repositories'] for secret in repo['secrets']))
        with self.assertRaisesRegex(RuntimeError, 'has not been verified'):
            proof.require_source_isolation(inventory, {'revoked': True, 'force': True})

    def test_missing_extra_or_duplicate_secret_names_fail_closed(self):
        for mode in ('missing', 'extra', 'duplicate', 'wrong-id', 'value'):
            rows = copy.deepcopy(self.records)
            target = 'alicetran68/pipeline'
            if mode == 'missing':
                rows[target]['secret_names'].pop()
            elif mode == 'extra':
                rows[target]['secret_names'].append('UNREVIEWED_WRITER')
            elif mode == 'duplicate':
                rows[target]['secret_names'].append(rows[target]['secret_names'][0])
            elif mode == 'wrong-id':
                rows[target]['repository_id'] += 1
            else:
                rows[target]['token'] = 'must-never-record-a-credential'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                proof.build_inventory(rows)

    def test_later_discovered_forks_are_mandatory(self):
        rows = copy.deepcopy(self.records)
        rows.pop('vomebook/pipeline')
        with self.assertRaisesRegex(ValueError, 'repository set differs'):
            proof.build_inventory(rows)

    def test_no_credentials_are_accepted_in_the_inventory(self):
        value = proof.build_inventory(self.records)
        for row in value['repositories']:
            for secret in row['secrets']:
                self.assertEqual(set(secret), {'name', 'source_status'})
        self.assertEqual(len(proof.inventory_digest(value)), 64)


if __name__ == '__main__':
    unittest.main()
