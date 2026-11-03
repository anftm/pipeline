"""Positioned text from inspected TeX bitmap fonts with incomplete Unicode maps.

Font identity is the digest of its actual character programs and encoding. An
unrecognized Type3 font is left for image recognition, never guessed from its
font number, document title, or the misleading WinAnsiEncoding declaration.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unicodedata

import pymupdf


PROFILE = 'bitmap-text-v1'
REGISTRY = Path(__file__).with_name('data') / 'tex_bitmap_fonts.json'
_NUMBER = rb'[-+]?(?:\d+(?:\.\d*)?|\.\d+)'
_METRICS = re.compile(rb'^\s*(' + _NUMBER + rb')\s+(' + _NUMBER + rb')\s+('
                      + _NUMBER + rb')\s+(' + _NUMBER + rb')\s+(' + _NUMBER
                      + rb')\s+(' + _NUMBER + rb')\s+d1\b')


def _object(document, xref, key):
    kind, value = document.xref_get_key(xref, key)
    return document.xref_object(int(value.split()[0])) if kind == 'xref' else value


def font_programs(document, xref):
    encoding = _object(document, xref, 'Encoding')
    match = re.search(r'/Differences\s*\[([^]]*)\]', encoding)
    if not match:
        raise ValueError('bitmap font has no explicit character encoding')
    code, names = None, {}
    for token in re.findall(r'/[^\s/<>\[\]()]+|\d+', match[1]):
        if token.startswith('/'):
            if code is None or code > 255:
                raise ValueError('invalid bitmap font character code')
            names[code] = token[1:]
            code += 1
        else:
            code = int(token)
    procedures = dict(re.findall(r'/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R',
                                 _object(document, xref, 'CharProcs')))
    programs = {code: document.xref_stream(int(procedures[name])) for code, name in names.items()
                if name in procedures}
    if len(programs) != len(procedures):
        raise ValueError('bitmap font contains unmapped character programs')
    signature = hashlib.sha256(json.dumps([
        [code, hashlib.sha256(raw).hexdigest()] for code, raw in sorted(programs.items())
    ], separators=(',', ':')).encode()).hexdigest()
    return signature, programs


def character(encoding, code):
    """Unicode semantics of the inspected OT1, typewriter and math code slots."""
    if encoding == 'typewriter':
        return chr(code) if 32 <= code <= 126 else None
    if encoding == 'OT1':
        special = {11: 'ff', 12: 'fi', 13: 'fl', 14: 'ffi', 15: 'ffl',
                   16: 'ı', 17: 'ȷ', 18: '\u0300', 19: '\u0301', 20: '\u030c',
                   21: '\u0306', 22: '\u0304', 23: '\u030a', 24: '\u0327',
                   25: 'ß', 26: 'æ', 27: 'œ', 28: 'ø', 29: 'Æ', 30: 'Œ', 31: 'Ø',
                   34: '”', 39: '’', 60: '¡', 62: '¿', 92: '“', 94: '\u0302',
                   95: '\u0307', 96: '‘', 123: '–', 124: '—', 125: '\u030b',
                   126: '\u0303', 127: '\u0308'}
        return special.get(code, chr(code) if 32 <= code <= 126 else None)
    if encoding == 'OML':
        special = {11: 'α', 12: 'β', 13: 'γ', 14: 'δ', 15: 'ϵ', 16: 'ζ', 17: 'η',
                   18: 'θ', 19: 'ι', 20: 'κ', 21: 'λ', 22: 'μ', 23: 'ν', 24: 'ξ',
                   25: 'π', 26: 'ρ', 27: 'σ', 28: 'τ', 29: 'υ', 30: 'ϕ', 31: 'χ',
                   32: 'ψ', 33: 'ω', 34: 'ε', 35: 'ϑ', 36: 'ϖ', 37: 'ϱ',
                   38: 'ς', 39: 'φ', 58: '.', 59: ',', 60: '<', 61: '/', 62: '>',
                   63: '⋆', 64: '∂', 96: 'ℓ', 123: 'ı', 124: 'ȷ', 125: '℘'}
        return special.get(code, chr(code) if 48 <= code <= 57 or 65 <= code <= 90 or 97 <= code <= 122 else None)
    if encoding == 'OMS':
        return {0: '−', 1: '·', 3: '∗', 6: '±', 14: '∘', 17: '≡', 20: '≤',
                21: '≥', 25: '≈', 33: '→', 34: '↑', 35: '↓', 41: '⇒',
                43: '⇓', 48: '′', 49: '∞', 50: '∈', 54: '/', 60: 'ℜ',
                61: 'ℑ', 98: '⌊', 99: '⌋', 102: '{', 103: '}', 104: '⟨',
                105: '⟩', 106: '|', 112: '√'}.get(code)
    if encoding == 'OMX':
        return {0: '(', 1: ')', 16: '(', 17: ')', 91: '⋃', 112: '√', 113: '√'}.get(code)
    return None


class BitmapText:
    def __init__(self, path):
        self.document = pymupdf.open(path)
        self.fonts = {}
        self.registry = json.loads(REGISTRY.read_text())['fonts']

    def close(self):
        self.document.close()

    def font(self, xref):
        if xref in self.fonts:
            return self.fonts[xref]
        result = None
        try:
            matrix = [float(n) for n in _object(self.document, xref, 'FontMatrix').strip('[]').split()]
            signature, programs = font_programs(self.document, xref)
            record = self.registry.get(signature)
            if record and matrix == [1, 0, 0, -1, 0, 0]:
                glyphs = {}
                for code, raw in programs.items():
                    match = _METRICS.match(raw)
                    value = character(record['encoding'], code)
                    if not match or value is None:
                        raise ValueError('inspected font has unsupported glyph metrics or code')
                    glyphs[code] = {'text': value, 'metrics': [float(v) for v in match.groups()],
                                    'sha256': hashlib.sha256(raw).hexdigest()}
                result = {'signature': signature, 'encoding': record['encoding'], 'glyphs': glyphs}
        except (ValueError, KeyError):
            pass
        self.fonts[xref] = result
        return result

    def extract(self, number):
        page = self.document[number - 1]
        if not any(info[2] == 'Type3' for info in page.get_fonts()):
            return None
        glyphs = []
        fonts = {}
        for span in page.get_texttrace():
            match = re.fullmatch(r'Type3 \((\d+) 0 R\)', span['font'])
            if not match or tuple(span['dir']) != (1., 0.) or span['type'] != 0 or span['opacity'] != 1:
                return None
            xref = int(match[1])
            font = self.font(xref)
            if font is None:
                return None
            fonts[str(xref)] = {'signature': font['signature'], 'encoding': font['encoding']}
            for original, code, origin, _ in span['chars']:
                glyph = font['glyphs'].get(code)
                if glyph is None:
                    return None
                advance, _, x0, y0, x1, y1 = glyph['metrics']
                scale = span['size']
                ox, oy = origin
                box = [ox + x0 * scale, oy - y1 * scale, ox + x1 * scale, oy - y0 * scale]
                glyphs.append({'text': glyph['text'], 'original': chr(original), 'code': code,
                               'font': xref, 'encoding': font['encoding'], 'origin': [ox, oy],
                               'box': box, 'advance': advance * scale, 'scale': scale,
                               'draw_order': span['seqno'],
                               'program_sha256': glyph['sha256']})
        if not glyphs:
            return None
        raw = [dict(g) for g in glyphs]
        composed, consumed = [], set()
        excluded = []
        covers = [(drawing['seqno'], list(item[1])) for drawing in page.get_drawings()
                  if drawing['type'] in {'f', 'fs'} and drawing['fill_opacity'] == 1
                  and len(drawing['items']) == 1 for item in drawing['items'] if item[0] == 're']
        duplicates = {}
        for index, glyph in enumerate(glyphs):
            if index in consumed:
                continue
            x0, y0, x1, y1 = glyph['box']
            if x1 <= 0 or y1 <= 0 or x0 >= page.rect.width or y0 >= page.rect.height:
                excluded.append({'glyph': index, 'reason': 'outside-visible-page'})
                continue
            hidden = next((order for order, rect in covers if order > glyph['draw_order']
                           and rect[0] <= x0 <= x1 <= rect[2] and rect[1] <= y0 <= y1 <= rect[3]), None)
            if hidden is not None:
                excluded.append({'glyph': index, 'reason': 'covered-by-later-opaque-rectangle', 'draw_order': hidden})
                continue
            identity = (glyph['program_sha256'], *(round(v, 2) for v in glyph['box']))
            if identity in duplicates:
                duplicates[identity]['glyphs'].append(index)
                continue
            sources = [index]
            if glyph['text'] == '#' and glyph['encoding'] == 'typewriter':
                strokes = [(i, g) for i, g in enumerate(glyphs[max(0, index - 2):index + 4], max(0, index - 2))
                           if i != index and g['text'] == '-' and g['font'] == glyph['font']
                           and abs(g['origin'][0] - glyph['origin'][0]) < glyph['scale'] * .05
                           and min(abs(abs(g['origin'][1] - glyph['origin'][1]) - glyph['scale'] * n)
                                   for n in (3, 4)) < glyph['scale'] * .05
                           and glyph['box'][1] <= g['box'][1] < g['box'][3] <= glyph['box'][3]]
                if len(strokes) == 2 and (strokes[0][1]['origin'][1] - glyph['origin'][1]) * (
                        strokes[1][1]['origin'][1] - glyph['origin'][1]) < 0:
                    sources.extend(i for i, _ in strokes)
                    consumed.update(sources[1:])
                    glyph = {**glyph, 'box': union([glyph['box'], *(g['box'] for _, g in strokes)])}
            composed.append({**glyph, 'glyphs': sources})
            duplicates[identity] = composed[-1]
        words = []
        for glyph in composed:
            previous = words[-1] if words else None
            baseline = glyph['origin'][1]
            gap = glyph['origin'][0] - previous['end'] if previous else 0
            if (previous and abs(baseline - previous['baseline']) <= .05
                    and -min(previous['advance'], glyph['advance']) * .35 <= gap
                    <= max(.6, min(previous['advance'], glyph['advance']) * .25)):
                previous['text'] += glyph['text']
                previous['box'] = union([previous['box'], glyph['box']])
                previous['end'] = glyph['origin'][0] + glyph['advance']
                previous['glyphs'].extend(glyph['glyphs'])
            else:
                words.append({'text': glyph['text'], 'box': glyph['box'], 'baseline': baseline,
                              'end': glyph['origin'][0] + glyph['advance'], 'advance': glyph['advance'],
                              'glyphs': list(glyph['glyphs'])})
        # TeX emits the raised star after the complete declaration. Its bitmap
        # overlaps the italic M, while its origin is between '[' and 'M'. Keep
        # all source glyphs in one positioned declaration instead of two regions.
        attached = set()
        for index, word in enumerate(words):
            if word['text'] != '∗':
                continue
            star = raw[word['glyphs'][0]]
            for host in words:
                if not host['text'].startswith('[') or len(host['glyphs']) < 2:
                    continue
                first, second = (raw[i] for i in host['glyphs'][:2])
                if (first['origin'][0] < star['origin'][0] < second['origin'][0]
                        and first['draw_order'] == star['draw_order']
                        and host['box'][1] <= star['box'][1] < star['box'][3] <= host['box'][3]):
                    host['text'] = '[∗' + host['text'][1:]
                    host['glyphs'][1:1] = word['glyphs']
                    host['box'] = union([host['box'], word['box']])
                    attached.add(index)
                    break
        words = [word for index, word in enumerate(words) if index not in attached]
        width, height = page.rect.width, page.rect.height
        blocks = [{'t': unicodedata.normalize('NFC', w['text']),
                   'b': [max(0, min(1, v / (width if i % 2 == 0 else height))) for i, v in enumerate(w['box'])],
                   'c': 1., 's': 'native', 'glyphs': w['glyphs']} for w in words]
        return {'page': number, 'source': 'native', 'width': width, 'height': height,
                'text': '\n'.join(b['t'] for b in blocks), 'blocks': blocks,
                'native_evidence': {'profile': PROFILE, 'fonts': fonts, 'glyphs': raw, 'excluded': excluded,
                                    'status': 'decoded', 'character_accuracy': 'unreviewed'}}


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]
