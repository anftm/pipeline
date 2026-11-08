"""Inventory sees every CW:0 reference before a partial metadata patch."""
import copy
import tempfile
import unittest
from pathlib import Path

from scripts import ccrd_metadata_inventory as inv
from scripts import ccrd_metadata_patch as patch
from tests.test_ccrd_lifecycle import CcrdLifecycleTests
from tests.test_ccrd_metadata_patch import fixture, save


class CcrdMetadataInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fixture_test = CcrdLifecycleTests('test_four_source_pointer_retains_all_databases_metadata_and_txt_archives')
        fixture_test.setUp(); self.manifest = fixture_test.four_source_pointer()
        prefix = f"generations/{self.manifest['generation']}/"
        rows = []
        for name, value in fixture().items():
            path = self.root / name; save(path, value)
            rows.append({'bucket': 'vomebook/ccrd-index', 'path': prefix + name, 'role': 'runtime', **patch.identity(path)})
        self.manifest['base_metadata_sha256'] = rows[0]['sha256']
        self.manifest['metadata'] = rows

    def test_exact_four_targets_and_all_other_files_are_checked(self):
        result = inv.inventory(self.manifest, self.root)
        self.assertEqual(result['target_files'], sorted(patch.PATHS))
        self.assertEqual(len(result['files']), 4)
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)
        inv.classify(result)

    def test_unexpected_extra_target_is_rejected_by_classification(self):
        name = 'data/extra.json'; save(self.root / name, {'unrelated': {'doc_id': 'CW:0'}})
        prefix = f"generations/{self.manifest['generation']}/"
        self.manifest['metadata'].append({'bucket': 'vomebook/ccrd-index', 'path': prefix + name,
                                          'role': 'runtime', **patch.identity(self.root / name)})
        result = inv.inventory(self.manifest, self.root)
        self.assertIn(name, result['target_files'])
        with self.assertRaisesRegex(ValueError, 'supported four-file'):
            inv.classify(result)

    def test_newspaper_tsv_target_reference_is_conservatively_blocked(self):
        import gzip
        name = 'data/newspaper-catalog.tsv.gz'; path = self.root / name
        path.write_bytes(gzip.compress(b'id\ttext\nPLA:1\tno target\n', mtime=0))
        prefix = f"generations/{self.manifest['generation']}/"
        entry = {'bucket': 'vomebook/ccrd-index', 'path': prefix + name,
                 'role': 'runtime', **patch.identity(path)}
        self.manifest['metadata'].append(entry)
        inv.classify(inv.inventory(self.manifest, self.root))
        path.write_bytes(gzip.compress(b'PLA:1\tCW:0\n', mtime=0)); entry.update(patch.identity(path))
        with self.assertRaisesRegex(ValueError, 'supported four-file'):
            inv.classify(inv.inventory(self.manifest, self.root))

    def test_missing_altered_or_unsupported_member_fails_closed(self):
        altered = copy.deepcopy(self.manifest); altered['metadata'][0]['sha256'] = 'a' * 64
        altered['base_metadata_sha256'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            inv.inventory(altered, self.root)
        (self.root / patch.PATHS[1]).unlink()
        with self.assertRaises(ValueError): inv.inventory(self.manifest, self.root)


if __name__ == '__main__':
    unittest.main()
