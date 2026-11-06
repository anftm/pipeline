import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from scripts import reader_resource_repair as repair, reader_lifecycle
from tests.test_reader_gc_collect import Store


class ReaderResourceRepairTests(unittest.TestCase):
    def test_retired_text_mode_cannot_write_or_read_remote_data(self):
        class Store:
            def read_bytes(self, *args):raise AssertionError('unexpected remote read')
        with self.assertRaisesRegex(ValueError, 'text recovery has been retired'):
            repair.run(Store(), {}, Path('unused.json'), apply=True, mode='text')

    def test_source_artifacts_are_only_completed_central_gc_workflows(self):
        self.assertEqual(repair.report_artifact({'path': '.github/workflows/reader-gc.yml',
            'status': 'completed'}), ('reader-gc-report', 'reader-gc-report.json'))
        self.assertEqual(repair.report_artifact({'path': '.github/workflows/reader-gc-collect.yml',
            'status': 'completed'}), ('reader-gc-collection', 'collection.json'))
        for run in ({'path': '.github/workflows/reader-gc.yml', 'status': 'in_progress'},
                    {'path': '.github/workflows/foreign.yml', 'status': 'completed'}):
            with self.assertRaises(ValueError):
                repair.report_artifact(run)

    def test_image_identity_mismatch_stops_before_output_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def download(url, target, token):
                Image.new('RGB', (32, 24), 'white').save(target, 'PNG')
            item = {'repo': 'VoiceOfML/test', 'path': 'book.png', 'revision': 'a' * 40,
                    'extension': 'png', 'source_sha256': 'b' * 64}
            with patch.object(repair.images, 'download', side_effect=download):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    repair.images.build_one(item, root, None, repair.ASSETS)

    def test_missing_image_plan_preserves_all_keys_and_rejects_foreign_sources(self):
        path = 'pages/image/png/' + 'a' * 64 + '/page-manifest.json'
        entry = {'s': 2, 'm': 'i', 'p': path, 'b': repair.ASSETS}
        missing = {(repair.ASSETS, path)}
        sidecar = {'v': 1, 'f': {'VoiceOfML/test\0one.png': entry, 'VoiceOfML/test\0two.png': entry}}
        plan = repair.image_plan(sidecar, missing)
        self.assertEqual(len(plan[(repair.ASSETS, path)]), 2)
        sidecar['f'] = {'foreign/test\0one.png': entry}
        with self.assertRaises(ValueError):
            repair.image_plan(sidecar, missing)

    def test_immutable_conflict_stops_publication_and_image_processing_root_is_valid(self):
        store = Store()
        path = 'pages/image/png/' + 'a' * 64 + '/page-manifest.json'
        store.put(repair.ASSETS, path, b'existing')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'manifest.json'
            target.write_bytes(b'different')
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                repair.publish_bundle(store, repair.ASSETS, {path: target}, {'path': path}, apply=False)
        record = reader_lifecycle.processing_record([{'bucket': repair.ASSETS,
            'root': path.rsplit('/', 1)[0]}], 'repair', {})
        reader_lifecycle.validate_processing_handoff(record, {'path': path})
        with self.assertRaises(ValueError):
            reader_lifecycle.processing_record([{'bucket': repair.PDF,
                'root': path.rsplit('/', 1)[0]}], 'repair', {})


if __name__ == '__main__':
    unittest.main()
