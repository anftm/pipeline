import itertools
import unittest

from scripts.reader_ebook_recovery_state import classify


INITIAL = dict(stage_complete=True, canonical_state='parent', canonical_permit='absent',
               catalog_state='parent', sidecar_state='parent', legacy_state=None,
               pointer_state='parent', v3_permit='absent')
CANONICAL_DONE = dict(INITIAL, canonical_state='target', canonical_permit='exact')
CATALOG_DONE = dict(CANONICAL_DONE, catalog_state='target', legacy_state='catalog-permitted')
SIDECAR_DONE = dict(CATALOG_DONE, sidecar_state='target', legacy_state='sidecar-permitted')
LEGACY_DONE = dict(SIDECAR_DONE, legacy_state='legacy-observed')
V3_DONE = dict(LEGACY_DONE, pointer_state='target', v3_permit='exact')


class RecoveryStateTests(unittest.TestCase):
    def test_only_exact_recovery_boundaries_are_accepted(self):
        accepted = {
            tuple(snapshot.values()): phase for snapshot, phase in (
                (INITIAL, 'canonical'),
                (CANONICAL_DONE, 'legacy-start'),
                (CATALOG_DONE, 'legacy-resume'),
                (SIDECAR_DONE, 'legacy-resume'),
                (LEGACY_DONE, 'v3'),
                (V3_DONE, 'pages'),
            )
        }
        roots = ('parent', 'target')
        permits = ('absent', 'exact')
        journals = (None, 'catalog-permitted', 'sidecar-permitted', 'legacy-observed')
        for values in itertools.product((False, True), roots, permits, roots, roots, journals, roots, permits):
            snapshot = dict(zip(INITIAL, values))
            with self.subTest(**snapshot):
                if values in accepted:
                    self.assertEqual(classify(**snapshot), accepted[values])
                else:
                    with self.assertRaises(ValueError):
                        classify(**snapshot)

    def test_spent_permit_with_parent_still_live_stops_instead_of_replaying(self):
        for root, snapshot in (
            ('canonical', dict(INITIAL, canonical_permit='exact')),
            ('legacy catalog', dict(CATALOG_DONE, catalog_state='parent')),
            ('legacy sidecar', dict(SIDECAR_DONE, sidecar_state='parent')),
            ('v3', dict(LEGACY_DONE, v3_permit='exact')),
        ):
            with self.subTest(root=root):
                with self.assertRaisesRegex(ValueError, f'{root} permit spent.*do not replay'):
                    classify(**snapshot)

    def test_unrecognized_or_unverified_evidence_is_rejected(self):
        for field, invalid_values in (
            ('stage_complete', (1, 0, 'true', None)),
            ('canonical_state', ('foreign', None, [])),
            ('catalog_state', ('unknown', None, [])),
            ('sidecar_state', ('TARGET', None, [])),
            ('pointer_state', ('missing', None, [])),
            ('canonical_permit', ('foreign', None, [])),
            ('v3_permit', ('foreign', None, [])),
            ('legacy_state', ('absent', 'foreign', False, [])),
        ):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        classify(**dict(INITIAL, **{field: value}))


if __name__ == '__main__':
    unittest.main()
