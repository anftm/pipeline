import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import bucket_file


BUCKET = 'vomebook/reader-assets-v2'
PATH = 'native/ebook/chm/书.epub'
HASH = 'a' * 64


def item(path=PATH):
    return SimpleNamespace(type='file', path=path, size=23, xet_hash=HASH,
                           mtime=datetime(2026, 1, 2, tzinfo=timezone.utc))


class BucketFileTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.get_bucket_paths_info.return_value = [item()]
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.patches = [patch('huggingface_hub.HfApi', return_value=self.api),
                        patch.dict(os.environ, {'HF_TOKEN': 'secret'}, clear=True),
                        contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr)]
        for p in self.patches:
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)

    def test_inspection_is_one_exact_lookup_no_tree_or_delete(self):
        self.assertEqual(bucket_file.main([BUCKET, PATH]), 0)
        self.assertEqual(json.loads(self.stdout.getvalue())['xet_hash'], HASH)
        self.api.get_bucket_paths_info.assert_called_once_with(BUCKET, [PATH])
        self.api.list_bucket_tree.assert_not_called()
        self.api.batch_bucket_files.assert_not_called()

    def test_metacharacters_are_literal_exact_paths(self):
        literal = '目录/*.json'
        self.api.get_bucket_paths_info.return_value = [item(path=literal)]
        self.assertEqual(bucket_file.main([BUCKET, literal]), 0)
        self.api.get_bucket_paths_info.assert_called_once_with(BUCKET, [literal])
        self.api.list_bucket_tree.assert_not_called()

    def test_missing_unexpected_identity_and_invalid_targets_fail(self):
        for bucket, path in [('https://foo/bar', PATH), ('owner/bucket', ''), (BUCKET, '/x'),
                             (BUCKET, 'x/'), (BUCKET, '../x'), (BUCKET, 'x//y'), (BUCKET, 'x\ny')]:
            with self.subTest(bucket=bucket, path=path):
                self.assertEqual(bucket_file.main([bucket, path]), 1)
        self.api.get_bucket_paths_info.return_value = []
        self.assertEqual(bucket_file.main([BUCKET, PATH]), 1)
        self.api.get_bucket_paths_info.return_value = [item(path='other')]
        self.assertEqual(bucket_file.main([BUCKET, PATH]), 1)

    def test_non_default_missing_token_cannot_fall_back(self):
        with patch('huggingface_hub.HfApi') as client:
            self.assertEqual(bucket_file.main([BUCKET, PATH, '--token-env', 'HF_INPUT_TOKEN']), 1)
        client.assert_not_called()

    def test_delete_entrypoint_no_longer_exists(self):
        with self.assertRaises(SystemExit):
            bucket_file.main(['delete', BUCKET, PATH, '--apply'])
        self.api.batch_bucket_files.assert_not_called()

    def test_old_fixed_chm_workflow_entries_are_removed_together(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        self.assertEqual(list(root.glob('chm-*.yml')), [])
        self.assertEqual(list(root.glob('publish-chm-gc-release*.yml')), [])
        self.assertTrue((root / 'reader-chm-repair.yml').is_file())

    def test_inspection_does_not_need_a_separate_workflow(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse((root / 'bucket-file.yml').exists())
        self.assertTrue((root / 'reader-gc-collect.yml').exists())


if __name__ == '__main__':
    unittest.main()
