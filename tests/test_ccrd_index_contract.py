import unittest
from unittest.mock import patch

from scripts import ccrd_index_contract as contract
from scripts import publish_ccrd_index as publisher


class CcrdIndexContractTests(unittest.TestCase):
    def test_original_manifests_are_unchanged(self):
        for value in [None, {'format': 1, 'databases': {'CCRD': {}, 'CW': {}}}]:
            contract.require_legacy_manifest(value)
        contract.require_legacy_metadata({'records': [{'source': 'CW'}], 'sources': [{'slug': 'CW'}]})

    def test_additive_or_version_two_manifests_cannot_be_downgraded(self):
        for manifest in [{'format': 2, 'databases': {'CCRD': {}, 'CW': {}}},
                         {'format': 1, 'databases': {'CCRD': {}, 'CW': {}, 'RMRB': {}}}]:
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, 'cannot replace'):
                contract.require_legacy_manifest(manifest)

    def test_legacy_full_build_fails_before_archive_download_or_upload(self):
        manifest = {'format': 2, 'databases': {'CCRD': {}, 'CW': {}, 'RMRB': {}, 'PLA': {}}}
        with patch.object(publisher, 'read_current', return_value=manifest), \
                patch.object(publisher, 'download') as download, \
                patch.object(publisher, 'batch_bucket_files') as upload, \
                patch.dict('os.environ', {'HF_TOKEN': 'fixture'}), patch('sys.argv', ['publish_ccrd_index.py']):
            with self.assertRaisesRegex(ValueError, 'cannot replace'):
                publisher.main()
        download.assert_not_called()
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
