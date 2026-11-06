"""Native Reader helpers and exact historical serialization, without OCR."""
import gzip
import hashlib
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts import djvu_pages, reader_native_text as native
from tests.djvu_fixture import build_djvu


class NativeTextTests(unittest.TestCase):
    def test_clean_and_normalize_native_text(self):
        self.assertEqual(native.clean_text(' A  &amp; B\0 '), 'A & B')
        blocks = native.normalize_blocks([
            {'text': ' second ', 'x0': 50, 'y0': 80, 'x1': 90, 'y1': 100, 'source': 'native'},
            {'text': 'first', 'x0': 0, 'y0': 0, 'x1': 30, 'y1': 20, 'source': 'native'},
        ], 100, 100)
        self.assertEqual([item['t'] for item in blocks], ['first', 'second'])
        self.assertEqual(blocks[0]['b'], [0., 0., .3, .2])
        self.assertTrue(all(item['s'] == 'native' for item in blocks))

    def test_json_output_is_deterministic_and_matches_existing_format(self):
        value = {'z': 1, 'title': '资料', 'a': [3, 2]}
        with tempfile.TemporaryDirectory() as directory:
            for compressed in (False, True):
                path = Path(directory) / ('data.gz' if compressed else 'data.json')
                digest, size = native.write_json(path, value, compressed=compressed)
                raw = path.read_bytes()
                if compressed:
                    expected = gzip.compress(json.dumps(value, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':')).encode(), compresslevel=9, mtime=0)
                else:
                    expected = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
                self.assertEqual(raw, expected)
                self.assertEqual((digest, size), (hashlib.sha256(raw).hexdigest(), len(raw)))
                self.assertEqual(native.write_json(path, value, compressed=compressed), (digest, size))

    def test_djvu_magic_and_hidden_text_are_still_native(self):
        self.assertEqual(djvu_pages.sniff(b'AT&TFORM\0\0\0\0DJVU'), 'djvu')
        info = {'width': 400, 'height': 600, 'dpi': 300, 'rotation': 0}
        expression = djvu_pages.parse_sexprs(
            b'(page 0 0 400 600 (line 50 400 350 480 (word 50 400 150 480 "hello")))')[0]
        payload = djvu_pages.page_text(expression, info)
        self.assertEqual(payload['text'], 'hello')
        self.assertEqual(payload['blocks'][0]['s'], 'native')

    def test_djvu_inspection_does_not_render_or_publish(self):
        info = {'width': 400, 'height': 600, 'dpi': 300, 'rotation': 0}
        output = b'(page 0 0 400 600 (line 50 400 350 480 (word 50 400 150 480 "hello")))'
        with patch.object(djvu_pages.subprocess, 'run') as run:
            run.return_value.stdout = output
            pages = djvu_pages.text_pages(Path('original.djvu'), [info])
        self.assertEqual(pages[1]['text'], 'hello')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ['djvutxt', '--detail=line', 'original.djvu'])
        self.assertFalse(hasattr(djvu_pages, 'render'))
        self.assertFalse(hasattr(djvu_pages, 'render_page'))

    @unittest.skipUnless(all(shutil.which(name) for name in
        ('cjb2', 'c44', 'djvm', 'djvused', 'ddjvu', 'djvutxt')), 'DjVuLibre tools unavailable')
    def test_real_djvu_page_map_and_hidden_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = build_djvu(Path(directory))
            pages = djvu_pages.inspect(source.read_bytes())
            self.assertEqual(len(pages), 2)
            result = djvu_pages.text_pages(source, pages)
            self.assertIn('world', result[1]['text'])
            self.assertTrue(all(block['s'] == 'native' for block in result[1]['blocks']))


if __name__ == '__main__':
    unittest.main()
