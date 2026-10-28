"""Operator-facing facts must stay attached to the code and workflow contract."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StorageDocumentationTests(unittest.TestCase):
    def test_storage_document_covers_originals_compatibility_and_gc_write_boundary(self):
        text = (ROOT/'docs/reader-storage.md').read_text(encoding='utf-8')
        for term in ('Original PDF', 'Original DjVu', 'native/ebook/chm/',
                     'ps-rebuild-20261025', 'derived/', 'objects/',
                     'vomebook/ccrd-index', 'not report-only by default',
                     'four-bucket reference closure', 'not a deletion list'):
            with self.subTest(term=term):
                self.assertIn(term,text)
        self.assertIn('not proof that no other consumer uses it',text)
        self.assertIn('not a read-only inspection',text)
