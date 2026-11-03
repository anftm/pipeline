import hashlib
import unittest

from scripts import chm_gc_terminal_guard as guard, chm_subset_journal as journal


class TerminalModelTests(unittest.TestCase):
    def setUp(self):
        self.subset = {'synthetic': 'subset-head'}
        self.final = {'synthetic': 'final-head'}
        evidence = {'kind': 'chm-exact-terminal-handoff', 'version': 1,
                    'inventory_sha256': guard.preflight.chm.preview.INVENTORY_SHA256,
                    'original_plan_sha256': guard.preflight.PLAN_SHA, 'original_progress_sha256': guard.preflight.PROGRESS_SHA,
                    'original_maintenance_sha256': guard.preflight.MAINTENANCE_SHA,
                    'original_receipts_sha256': guard.locked.IDENTITY['old_receipts_sha256'], 'original_confirmed': 600,
                    'subset_plan_sha256': guard.subset.PLAN_SHA, 'subset_head_sha256': journal.digest(self.subset),
                    'subset_confirmed': 352, 'final20_head_sha256': journal.digest(self.final), 'final20_confirmed': 20,
                    'verified_absent_objects': 972, 'verified_absent_bytes': guard.preflight.chm.preview.DATA_BYTES,
                    'original_progress_unchanged': True, 'ordinary_gc_authorized': False}
        self.evidence = journal.encode(evidence)
        digest = hashlib.sha256(self.evidence).hexdigest()
        self.state = {'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': guard.preflight.OWNER,
                      'callback_plan_sha256': guard.preflight.PLAN_SHA, 'callback_handoff_stage': 'exact-terminal-bridge',
                      'callback_committed': True, 'callback_recovery_required': False, 'restored': True,
                      'repositories': {'r': {'restored': True}},
                      'terminal_restore_exclusions': ['anftm/pipeline:.github/workflows/publish-ccrd-ocr.yml',
                                                      'anftm/pipeline:.github/workflows/reader-publish-text.yml'],
                      'terminal_handoff': {'bucket': guard.ASSETS, 'path': guard.PREFIX + digest + '.json', 'sha256': digest}}
        self.history = journal.encode(self.state)
        self.marker = journal.encode({'kind': 'chm-gc-release', 'version': 1,
                                      'maintenance_sha256': hashlib.sha256(self.history).hexdigest(),
                                      'handoff_sha256': digest})

    def test_valid_terminal_snapshot_is_consistent(self):
        self.assertTrue(guard.validate_gc_snapshot(self.marker, self.history, self.history,
                                                   self.evidence, self.subset, self.final))

    def test_forged_snapshot_or_head_is_rejected(self):
        for marker, history, current, evidence, head in (
                (self.marker + b' ', self.history + b' ', self.history, self.evidence, self.subset),
                (self.marker, self.history, journal.encode({**self.state, 'restored': False}), self.evidence, self.subset),
                (self.marker, self.history, self.history, self.evidence + b' ', self.subset),
                (self.marker, self.history, self.history, self.evidence, {'different': True})):
            with self.subTest(head=head), self.assertRaises(RuntimeError):
                guard.validate_gc_snapshot(marker, history, current, evidence, head, self.final)

    def test_new_maintenance_with_unfinished_callback_is_rejected(self):
        current = journal.encode({'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
                                  'repositories': {'r': {}}, 'callback_recovery_required': True})
        with self.assertRaisesRegex(RuntimeError, 'unfinished callback'):
            guard.validate_gc_snapshot(self.marker, self.history, current, self.evidence, self.subset, self.final)


if __name__ == '__main__':
    unittest.main()
