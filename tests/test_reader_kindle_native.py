import tempfile
import unittest
from pathlib import Path
import zipfile

from scripts import epub_chapters, reader_kindle_native as native


class KindleNativeNavigationTests(unittest.TestCase):
    def fixture(self, path, *, flatten=False, duplicate=False, empty=False):
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="p.opf"/></rootfiles></container>')
            z.writestr('p.opf', '<package><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="n" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="n"><itemref idref="a"/></spine></package>')
            row = '<navPoint><navLabel><text>Part</text></navLabel><content src="a.xhtml#A"/>'
            child = '<navPoint><navLabel><text>Same title</text></navLabel><content src="a.xhtml' + ('' if empty else '#B') + '"/></navPoint>'
            tree = row + '</navPoint>' + child if flatten else row + child + '</navPoint>'
            z.writestr('toc.ncx', '<ncx><navMap>' + tree + '</navMap></ncx>')
            z.writestr('a.xhtml', '<html><body><h1 id="A">Part</h1><p id="B">Same title</p>' +
                       ('<a name="B"/>' if duplicate else '') + '</body></html>')

    def test_exact_output_and_bundle_keep_source_order_depth_and_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); f = root/'book.epub'; self.fixture(f)
            with zipfile.ZipFile(f) as z:
                source, _ = native._navigation(z)
            self.assertEqual([(r['title'],r['depth'],r['fragment']) for r in source],
                             [('Part',0,'A'),('Same title',1,'B')])
            self.assertEqual(native.verify_output(source, f), source)
            bundle = root/'bundle'; result = epub_chapters.build_bundle(f, bundle, preserve_toc_targets=True)
            native.verify_bundle(source, bundle, result)
            result['toc'][1]['depth'] = 0
            with self.assertRaisesRegex(ValueError, 'identity did not survive'):
                native.verify_bundle(source, bundle, result)

    def test_wrong_hierarchy_or_ambiguous_target_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source_file = root/'source.epub'; self.fixture(source_file)
            with zipfile.ZipFile(source_file) as z:
                source, _ = native._navigation(z)
            for defect in ('flatten','duplicate'):
                target = root/(defect+'.epub'); self.fixture(target, **{defect:True})
                with self.subTest(defect=defect), self.assertRaises(ValueError):
                    native.verify_output(source, target)

    def test_calibre_elided_fragment_recovers_only_exact_anchor_in_selected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.epub'
            self.fixture(source)
            with zipfile.ZipFile(source) as z:
                native_rows, _ = native._navigation(z)
            split = root/'split.epub'
            self.fixture(split, empty=True)
            restored = native.verify_output(native_rows, split)
            self.assertEqual(restored, native_rows)
            bundle = root/'bundle'
            emitted = epub_chapters.build_bundle(split, bundle, preserve_toc_targets=True, verified_toc=restored)
            native.verify_bundle(native_rows, bundle, emitted)
            self.assertEqual(emitted['toc'][1]['fragment'], 'B')
            wrong = root/'wrong.epub'
            self.fixture(wrong, empty=True, duplicate=True)
            with self.assertRaisesRegex(ValueError, 'absent|missing or ambiguous'):
                native.verify_output(native_rows, wrong)

    def test_page_start_only_allowed_if_the_body_path_did_not_change(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Path(directory)/'book.epub'; self.fixture(f, empty=True)
            with zipfile.ZipFile(f) as z:
                source, _ = native._navigation(z)
            with self.assertRaisesRegex(ValueError, 'provable XHTML anchor'):
                native.verify_output(source, f)
            self.assertEqual(native.verify_output(source, f, exact_paths=True), source)
