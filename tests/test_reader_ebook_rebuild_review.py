from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from scripts import epub_chapters, reader_ebook_rebuild_review as review


class EbookRebuildReviewTests(unittest.TestCase):
    def test_rehash_bundle_and_require_explicit_anchor_and_profile(self):
        with tempfile.TemporaryDirectory() as name:
            d = Path(name)
            source = d/'book.epub'
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf"/></container>')
                z.writestr('book.opf', '<package><manifest><item id="a" href="book.html" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/></spine></package>')
                z.writestr('book.html', '<html><body><h1 id="a">Chapter</h1><p>A real body.</p></body></html>')
            dest = d/'bundle'
            result = epub_chapters.build_bundle(source, dest, preserve_toc_targets=True,
                verified_toc=[{'title': 'Chapter', 'source_path': 'book.html', 'fragment': 'a', 'depth': 0}])
            manifest = dest/'chapter-manifest.json'
            with self.assertRaisesRegex(ValueError, 'successor profile'):
                review.read_bundle(dest)
            result['conversion_profile'] = review.EPUB_PROFILE
            manifest.write_bytes(review.v3.encode(result))
            observed, members, version = review.read_bundle(dest)
            self.assertEqual(observed, result)
            self.assertEqual(len(members), 3)
            self.assertEqual(version, epub_chapters.bundle_version(dest))
            changed = dest/result['chapters'][0]['path']
            changed.write_bytes(changed.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError, 'manifest member differs'):
                review.read_bundle(dest)

    def test_drift_or_unreviewed_input_stops_before_any_index_read(self):
        store = Mock()
        for plan in ({'kind': 'unsupported'}, {'kind': 'reader-ebook-full-rebuild-plan', 'remote_writes': 0,
                                               'source_heads_changed': ['example']}):
            with self.assertRaisesRegex(ValueError, 'exact read-only'):
                review.preview(store, plan, {'key': Path('.')})
        store.read_bytes.assert_not_called()
