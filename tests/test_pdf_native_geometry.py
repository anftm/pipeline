import unittest
from types import SimpleNamespace

from scripts import pdf_native_geometry as geometry, ocr_layout


def trace(text, y, height=20):
    return {'dir': (1., 0.), 'type': 0, 'opacity': 1, 'font': 'TestFont', 'seqno': 0,
            'chars': [(ord(c), n, (30 + n * height, y), (30 + n * height, y - height, 30 + (n + 1) * height, y))
                      for n, c in enumerate(text)]}


class NativeGeometryTests(unittest.TestCase):
    def test_font_ascent_overlap_cannot_reorder_heading_after_body(self):
        spans = [trace('标题', 60, 24), trace('第一行正文', 90, 16)]
        page = SimpleNamespace(get_texttrace=lambda: spans)
        blocks = [{'t': '标题', 'b': [.15, .1, .39, .35], 's': 'native', 'c': 1},
                  {'t': '第一行正文', 'b': [.15, .2, .55, .4], 's': 'native', 'c': 1}]
        source = {'text': '标题\n第一行正文', 'blocks': blocks, 'width': 200, 'height': 300}
        result = geometry.refine(page, source)
        self.assertEqual(result['extracted_blocks'], blocks)
        self.assertLess(result['blocks'][0]['b'][3], result['blocks'][1]['b'][1])
        self.assertEqual([b['t'] for b in ocr_layout.arrange(result['blocks'], 200, 300)['blocks']], ['标题', '第一行正文'])
        self.assertEqual(result['text'], source['text'])

    def test_different_text_or_ambiguous_position_keeps_original_geometry(self):
        block = {'t': '标题', 'b': [.15, .1, .39, .35]}
        payload = {'text': '标题', 'blocks': [block], 'width': 200, 'height': 300}
        for spans in [[trace('错误', 60, 24)], [trace('标题', 60, 24), trace('标题', 60, 24)]]:
            with self.subTest(spans=spans):
                self.assertIs(geometry.refine(SimpleNamespace(get_texttrace=lambda: spans), payload), payload)
