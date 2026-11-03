import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from scripts import pdf_bitmap_text as bitmap, pdf_ocr, native_text_quality


def fixture(path, content):
    document = pymupdf.open()
    page = document.new_page(width=200, height=300)
    programs = {}
    for code in (35, 45, 49, 85):
        xref = document.get_new_xref()
        document.update_object(xref, '<<>>')
        metrics = b'22 0 0 0 21 25 d1\n0 0 21 25 re f' if code != 45 else b'22 0 0 11 21 13 d1\n0 11 21 2 re f'
        document.update_stream(xref, metrics)
        programs[code] = xref
    font = document.get_new_xref()
    differences = ' '.join(f'{c} /g{c}' for c in programs)
    charprocs = ' '.join(f'/g{c} {xref} 0 R' for c, xref in programs.items())
    document.update_object(font, f'<< /Type /Font /Subtype /Type3 /FontBBox [0 0 21 25] '
                           f'/FontMatrix [1 0 0 -1 0 0] /FirstChar 35 /LastChar 85 '
                           f'/Widths [{" ".join(["22"] * 51)}] /CharProcs <<{charprocs}>> '
                           f'/Encoding << /Type /Encoding /Differences [{differences}] >> /Resources <<>> >>')
    document.xref_set_key(page.xref, 'Resources', f'<< /Font << /FX {font} 0 R >> >>')
    stream = document.get_new_xref()
    document.update_object(stream, '<<>>')
    document.update_stream(stream, content.encode())
    document.xref_set_key(page.xref, 'Contents', f'{stream} 0 R')
    signature, _ = bitmap.font_programs(document, font)
    document.save(path)
    document.close()
    registry = path.with_suffix('.json')
    registry.write_text(json.dumps({'fonts': {signature: {'encoding': 'typewriter'}}}))
    return registry, font


def draw(code, x, y):
    return f'BT /FX .24 Tf 1 0 0 -1 {x} {y} Tm <{code:02x}> Tj ET\n'


class BitmapTextTests(unittest.TestCase):
    def test_actual_font_program_identity_controls_mapping_and_unknown_font_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'font.pdf'
            registry, font = fixture(path, draw(49, 20, 200))
            with patch.object(bitmap, 'REGISTRY', registry):
                payload = pdf_ocr.native_page(path, 1)
            self.assertEqual(payload['text'], '1')
            self.assertIn('extracted_text', payload)
            self.assertEqual(payload['native_evidence']['glyphs'][0]['code'], 49)
            document = pymupdf.open(path)
            _, programs = bitmap.font_programs(document, font)
            proc = int(document.xref_get_key(font, 'CharProcs/g49')[1].split()[0])
            document.update_stream(proc, programs[49] + b'\n% changed bitmap program')
            changed = Path(temp) / 'changed.pdf'
            document.save(changed)
            document.close()
            with patch.object(bitmap, 'REGISTRY', registry):
                payload = pdf_ocr.native_page(changed, 1)
            self.assertIn('unverified-type3-font', payload['native_evidence']['reasons'])
            self.assertFalse(native_text_quality.assess(payload)['usable'])

    def test_hash_overstrike_and_visibility_keep_every_draw_accounted_for(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'symbols.pdf'
            content = draw(35, 20, 200) + draw(45, 20, 200.96) + draw(45, 20, 199.04)
            content += draw(49, 50, 200) + draw(49, 50, 200)
            content += draw(85, 100, 200) + '1 g 99 199 8 8 re f\n'
            content += draw(49, 250, 200)
            registry, _ = fixture(path, content)
            with patch.object(bitmap, 'REGISTRY', registry):
                reader = bitmap.BitmapText(path)
                try:
                    result = reader.extract(1)
                finally:
                    reader.close()
            self.assertEqual([b['t'] for b in result['blocks']], ['#', '1'])
            evidence = result['native_evidence']
            accounted = [i for b in result['blocks'] for i in b['glyphs']] + [g['glyph'] for g in evidence['excluded']]
            self.assertEqual(sorted(accounted), list(range(7)))
            self.assertEqual({g['reason'] for g in evidence['excluded']},
                             {'outside-visible-page', 'covered-by-later-opaque-rectangle'})
