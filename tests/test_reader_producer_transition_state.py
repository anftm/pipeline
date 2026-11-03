from datetime import datetime, timezone
import copy
import json
import unittest

from scripts import reader_producer_scope_transition as scope, reader_producer_transition_state as migration


class TransitionStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.raws = {}
        for name, repo_id in scope.EXPECTED.items():
            value = {'version': 1, 'kind': 'retired-pipeline-repository-deletion',
                     'repository': name, 'repository_id': repo_id, 'owner': name.split('/')[0],
                     'git_bundle_sha256': 'a' * 64, 'actions_backup_sha256': 'b' * 64,
                     'intent_sha256': 'c' * 64, 'deleted_at': '2026-11-02T23:59:00Z',
                     'delete_http': 204, 'owner_http': 200, 'owner_matches': True,
                     'name_get_http': 404, 'id_get_http': 404}
            self.raws[name] = migration.encode(value)

    def test_exact_candidate_is_prepared_only_and_deterministic(self):
        first = migration.create(self.raws, now=self.now)
        second = migration.create(dict(reversed(list(self.raws.items()))), now=self.now)
        self.assertEqual(first, second)
        self.assertEqual(migration.digest(first), migration.digest(second))
        self.assertEqual(first['phase'], 'prepared')
        self.assertIsNone(first['source_writer_proofs'])
        self.assertEqual(first['additional_repository_ids'], scope.ADDITIONAL)
        with self.assertRaisesRegex(RuntimeError, 'cannot advance'):
            migration.advance(first, 'committed', writer_proof=True, force=True)

    def test_incomplete_or_swapped_receipts_fail(self):
        missing = dict(self.raws)
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            migration.create(missing, now=self.now)
        swapped = dict(self.raws)
        a, b = list(swapped)[:2]
        swapped[a] = swapped[b]
        with self.assertRaises(ValueError):
            migration.create(swapped, now=self.now)

    def test_state_cannot_be_forged_or_carry_untrusted_writer_claims(self):
        initial = migration.create(self.raws, now=self.now)
        for field, value in (('phase', 'committed'), ('phase', 'verified'),
                             ('legacy_scope_sha256', '0' * 64),
                             ('source_writer_proofs', {'all_revoked': True}),
                             ('additional_repository_ids', {})):
            altered = copy.deepcopy(initial)
            altered[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                migration.validate(altered)

    def test_historical_rollback_or_receipt_mutation_changes_digest(self):
        state = migration.create(self.raws, now=self.now)
        altered = copy.deepcopy(state)
        altered['deleted_receipts'][next(iter(scope.EXPECTED))]['sha256'] = 'f' * 64
        self.assertNotEqual(migration.digest(state), migration.digest(altered))

    def test_additional_deletion_stage_preserves_prior_candidate_and_never_activates(self):
        first = migration.create(self.raws, now=self.now)
        extra = {}
        for name, rid in scope.ADDITIONAL.items():
            template = json.loads(next(iter(self.raws.values())))
            template.update(repository=name, repository_id=rid, owner=name.split('/')[0])
            extra[name] = migration.encode(template)
        second = migration.add_additional_deletions(first, extra, now=self.now)
        self.assertEqual(second['parent_candidate_sha256'], migration.digest(first))
        self.assertEqual(second['phase'], 'prepared')
        self.assertIsNone(second['source_writer_proofs'])
        self.assertEqual(set(second['additional_deleted_receipts']), set(scope.ADDITIONAL))
        self.assertEqual(migration.validate_extended(second, first), second)
        verified = migration.verify_candidate_receipts(second, {**self.raws, **extra}, now=self.now)
        self.assertEqual(verified['receipt_count'], 9)
        self.assertFalse(verified['gc_authorized'])
        with self.assertRaises(RuntimeError):
            migration.advance(second, 'committed')
        incomplete = dict(extra)
        incomplete.pop(next(iter(incomplete)))
        with self.assertRaises(ValueError):
            migration.add_additional_deletions(first, incomplete, now=self.now)
        altered = copy.deepcopy(second)
        altered['parent_candidate_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            migration.validate_extended(altered, first)
        tampered = {**self.raws, **extra}
        tampered[next(iter(tampered))] += b' '
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            migration.verify_candidate_receipts(second, tampered, now=self.now)


if __name__ == '__main__':
    unittest.main()
