"""Exercise cell ownership, empty columns and retained text-layer coordinates."""
import copy
import unittest

from PIL import Image, ImageDraw

from scripts import ocr_table_geometry as tables, pdf_text_layer


def fixture():
    blocks = [{'t': text, 'b': bounds, 'c': .999, 's': 'ocr'} for text, bounds in [
        ('样本', [.05, .04, .15, .36]), ('合并表头', [.25, .04, .75, .16]),
        ('2', [.25, .24, .35, .36]), ('8', [.65, .24, .75, .36]),
        ('续行', [.05, .44, .15, .56]), ('4', [.25, .44, .35, .56]),
        ('5', [.65, .44, .75, .56])]]
    cells = [(0, 0, 2, 1, [0]), (0, 1, 1, 3, [1]),
             (1, 1, 1, 1, [2]), (1, 2, 1, 1, []), (1, 3, 1, 1, [3]),
             (2, 0, 1, 1, [4]), (2, 1, 1, 1, [5]), (2, 2, 1, 1, []), (2, 3, 1, 1, [6])]
    grid = {'profile': tables.PROFILE, 'source_pixels_sha256': 'b'*64,
            'box': [0, 0, .8, .6], 'rows': [0, .2, .4, .6], 'columns': [0, .2, .4, .6, .8],
            'review': [], 'cells': []}
    for row, column, rowspan, colspan, indices in cells:
        grid['cells'].append({'row': row, 'column': column, 'rowspan': rowspan, 'colspan': colspan,
            'box': [grid['columns'][column], grid['rows'][row],
                    grid['columns'][column+colspan], grid['rows'][row+rowspan]],
            'blocks': indices, 'text': '\n'.join(blocks[i]['t'] for i in indices)})
    return blocks, grid


class TableGeometryTests(unittest.TestCase):
    def test_empty_and_spanning_cells_keep_columns_and_every_text_offset(self):
        blocks, grid = fixture()
        before = copy.deepcopy((blocks, grid))
        arranged = tables.arrange(blocks, 100, 100, grid, include_writing_modes=True)
        self.assertEqual(arranged['text'], '样本\t合并表头\t\t\n\n\t2\t\t8\n\n续行\t4\t\t5')
        self.assertEqual((blocks, grid), before)
        self.assertEqual(sorted(s['block'] for s in arranged['text_spans']), list(range(len(blocks))))
        for span in arranged['text_spans']:
            self.assertEqual(arranged['text'][span['start']:span['end']], blocks[span['block']]['t'])
            self.assertEqual(span['box'], blocks[span['block']]['b'])

    def test_table_survives_layer_and_accepted_correction_without_stale_cell_text(self):
        blocks, grid = fixture()
        payload = {'version': 1, 'kind': 'pdf-ocr-page', 'page': 46, 'source': 'ocr',
                   'width': 100, 'height': 100, **tables.arrange(blocks, 100, 100, grid)}
        layer = pdf_text_layer.from_page(payload, 'a'*64, 'ch')
        self.assertEqual(layer['tables'][0]['cells'][0]['region_ids'], ['b0'])
        self.assertEqual(layer['tables'][0]['cells'][3]['region_ids'], [])
        self.assertNotIn('text', layer['tables'][0]['cells'][0])
        proposal = {'version': 1, 'kind': 'pdf-text-correction', 'base_generation': layer['generation'],
                    'raw_sha256': layer['raw_sha256'], 'page_identity': layer['page_identity'],
                    'replacements': [{'region_id': 'b2', 'before': '2', 'after': '20'}]}
        accepted = pdf_text_layer.accept_proposal(layer, proposal, actor='reviewer')
        self.assertEqual(accepted['tables'], layer['tables'])
        self.assertIn('\t20\t\t8', accepted['text'])
        self.assertEqual(accepted['regions'][2]['box'], blocks[2]['b'])
        for region in accepted['regions']:
            self.assertEqual(accepted['text'][region['start']:region['end']], region['text'])
        for change in ('missing-cell', 'overlap', 'unknown-region'):
            broken = copy.deepcopy(layer)
            cells = broken['tables'][0]['cells']
            if change == 'missing-cell':
                cells.pop()
            elif change == 'overlap':
                cells[1]['column'] = 0
            else:
                cells[0]['region_ids'] = ['missing']
            broken['generation'] = pdf_text_layer.digest({k:v for k,v in broken.items() if k != 'generation'})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'table'):
                pdf_text_layer.validate(broken)

    def test_visible_ruled_grid_and_prose_do_not_share_detection(self):
        image = Image.new('RGB', (800, 1000), 'white')
        draw = ImageDraw.Draw(image)
        for x in range(100, 701, 75):
            draw.line((x, 100, x, 820), fill='black', width=2)
        for y in range(100, 821, 60):
            draw.line((100, y, 700, y), fill='black', width=2)
        result = tables.detect(image, [])
        self.assertIsNotNone(result)
        self.assertEqual(len(result['cells']), 8*12)
        self.assertTrue(all(c['rowspan'] == c['colspan'] == 1 for c in result['cells']))
        plain = Image.new('RGB', (800, 1000), 'white')
        draw = ImageDraw.Draw(plain)
        for y in range(100, 821, 60):
            draw.text((100, y), 'Prose with numbers 1 2 3 and no grid', fill='black')
        self.assertIsNone(tables.detect(plain, []))
