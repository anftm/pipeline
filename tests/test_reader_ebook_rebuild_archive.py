import hashlib
import io
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile, ZipInfo

from scripts import reader_ebook_rebuild_archive as archive


class EbookArchiveTests(unittest.TestCase):
    @staticmethod
    def archive(*rows):
        bio = io.BytesIO()
        with ZipFile(bio, 'w') as z:
            for name, data in rows:
                z.writestr(name, data)
        return bio.getvalue()

    def test_rejects_unpinned_bytes_and_traversal_before_extraction(self):
        raw = self.archive(('selection.json', b'{}'),
                           ('bundles/'+'a'*64+'/../elsewhere', b'X'))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'SHA or length'):
                archive.unpack(raw, '0'*64, root)
            with self.assertRaisesRegex(ValueError, 'archive member'):
                archive.unpack(raw, hashlib.sha256(raw).hexdigest(), root)
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_link_member_before_extraction(self):
        link = ZipInfo('bundles/'+'a'*64+'/chapter-manifest.json')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        raw = self.archive(('selection.json', b'{}'), (link, b'/etc/passwd'))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'archive member'):
                archive.unpack(raw, hashlib.sha256(raw).hexdigest(), root)
            self.assertEqual(list(root.iterdir()), [])
