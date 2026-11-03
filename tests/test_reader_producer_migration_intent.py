from datetime import datetime, timezone
import copy
import json
import unittest

from scripts import reader_gc_graph, reader_producer_migration_intent as intent
from scripts import reader_producer_scope_transition as scope, reader_producer_transition_state as transition
from scripts import reader_producer_writer_proofs as proof


class MigrationIntentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.receipts = {}
        for repo, rid in {**scope.EXPECTED, **scope.ADDITIONAL}.items():
            self.receipts[repo] = transition.encode({
                'version': 1, 'kind': 'retired-pipeline-repository-deletion', 'repository': repo,
                'repository_id': rid, 'owner': repo.split('/')[0], 'git_bundle_sha256': 'a' * 64,
                'actions_backup_sha256': 'b' * 64, 'intent_sha256': 'c' * 64,
                'deleted_at': '2026-11-02T23:59:00Z', 'delete_http': 204, 'owner_http': 200,
                'owner_matches': True, 'name_get_http': 404, 'id_get_http': 404})
        original = {repo: self.receipts[repo] for repo in scope.EXPECTED}
        extra = {repo: self.receipts[repo] for repo in scope.ADDITIONAL}
        self.first = transition.create(original, now=self.now)
        self.candidate = transition.add_additional_deletions(self.first, extra, now=self.now)
        all_ids = {**scope.EXPECTED, **scope.ADDITIONAL}
        required = proof.requirements()
        self.inventory = proof.build_inventory({repo: {'repository_id': rid, 'secret_names': required[repo]}
                                                for repo, rid in all_ids.items()})
        self.writer_raw = transition.encode(self.inventory)

    def test_all_intents_are_content_addressed_non_roots_and_no_remote_write_occurs(self):
        plan = intent.plan(self.candidate, self.receipts, self.writer_raw, now=self.now)
        self.assertEqual(plan['object_count'], 12)
        self.assertEqual(plan['receipt_count'], 9)
        self.assertEqual(plan['remote_mutations'], 0)
        self.assertFalse(plan['gc_authorized'])
        for path in plan['objects']:
            self.assertTrue(path.startswith(intent.PREFIX))
            self.assertFalse(reader_gc_graph.is_root(path, reader_gc_graph.READER_ASSETS_BUCKET))

    def test_missing_receipt_and_forged_writer_authorization_are_rejected(self):
        missing = dict(self.receipts)
        missing.pop(next(iter(missing)))
        with self.assertRaises(ValueError):
            intent.plan(self.candidate, missing, self.writer_raw, now=self.now)
        forged = copy.deepcopy(self.inventory)
        forged['external_writer_isolation_verified'] = True
        with self.assertRaisesRegex(ValueError, 'unsafe authorization'):
            intent.plan(self.candidate, self.receipts, transition.encode(forged), now=self.now)
        forged = copy.deepcopy(self.inventory)
        forged['repositories'][0]['secrets'][0]['value'] = 'must-not-enter-audit'
        with self.assertRaisesRegex(ValueError, 'inventory rows differ'):
            intent.plan(self.candidate, self.receipts, transition.encode(forged), now=self.now)

    def test_receipt_bytes_or_candidate_drift_is_rejected(self):
        damaged = dict(self.receipts)
        damaged[next(iter(damaged))] += b' '
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            intent.plan(self.candidate, damaged, self.writer_raw, now=self.now)
        changed = copy.deepcopy(self.candidate)
        changed['phase'] = 'committed'
        with self.assertRaises(ValueError):
            intent.plan(changed, self.receipts, self.writer_raw, now=self.now)


if __name__ == '__main__':
    unittest.main()
