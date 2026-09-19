import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import recover_chm
from PIL import Image


class ChmRecoveryTests(unittest.TestCase):
    def test_recovery_retains_unlisted_txt_static_scripts_and_images(self):
        def extract(_source, root):
            (root / 'first.htm').write_text('<title>First</title><p>first body</p><img src="cover.png">')
            (root / 'second.txt').write_text('document.write("<p>second body</p>");')
            (root / 'third.txt').write_text('third body')
            Image.new('RGB', (2, 2)).save(root / 'cover.png')
            (root / 'toc.hhc').write_text('<object><param name="Local" value="first.htm"></object>')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.chm'
            source.write_bytes(b'ITSF')
            target = root / 'book.epub'
            with patch.object(recover_chm, 'extract', side_effect=extract):
                report = recover_chm.recover(source, target, 'Book')
            self.assertEqual([r['source'] for r in report['chapters']], ['first.htm', 'second.txt', 'third.txt'])
            self.assertEqual(report['images'], 1)
            self.assertEqual(report['missing_images'], [])
            with zipfile.ZipFile(target) as archive:
                pages = ''.join(archive.read('EPUB/' + r['chapter']).decode() for r in report['chapters'])
            for text in ['first body', 'second body', 'third body']:
                self.assertIn(text, pages)
            self.assertNotIn('document.write', pages)

    def test_dynamic_txt_is_reported_instead_of_published_as_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'page.txt').write_text('document.write(loadBody());')
            with self.assertRaisesRegex(ValueError, 'unsupported script-backed page'):
                recover_chm.source_pages(root, root)

    def test_legacy_punctuation_and_malformed_attributes_keep_visible_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'page.htm').write_bytes(
                b'<meta charset="iso-8859-1"><body><p>Before\x85after</p>'
                b'<div id="bad" <="">tail</div></body>'
            )
            pages = recover_chm.source_pages(root, root)
            self.assertEqual(pages[0]['expected'], 'Before…aftertail')

    def test_image_content_detection_and_case_insensitive_parent_paths(self):
        def extract(_source, root):
            (root / 'IMAGES').mkdir()
            Image.new('RGB', (3, 2)).save(root / 'IMAGES/cover.jpg;12345', format='JPEG')
            (root / 'page.htm').write_text('<p>Body</p><img src="images/cover.jpg;12345">')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'book.chm'
            source.write_bytes(b'ITSF')
            with patch.object(recover_chm, 'extract', side_effect=extract):
                report = recover_chm.recover(source, root / 'book.epub', 'Book')
            self.assertEqual(report['images'], 1)
            self.assertEqual(report['missing_images'], [])
