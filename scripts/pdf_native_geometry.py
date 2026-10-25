"""Match Poppler words to glyph advances when font-wide bounds overlap lines.

Text is retained verbatim. A match must contain the same Unicode sequence and
occupy the original word's horizontal interval; ambiguous matches keep the old
coordinates. Glyph advances use the text matrix rather than font ascent tables.
"""
from __future__ import annotations

from collections import defaultdict
import copy
import math

PROFILE = 'native-geometry-v1'


def refine(page, payload):
    glyphs = []
    for span in page.get_texttrace():
        if (tuple(span['dir']) != (1., 0.) or span['type'] != 0 or span['opacity'] != 1
                or span['font'].startswith('Type3')):
            continue
        for codepoint, _, origin, box in span['chars']:
            char = chr(codepoint)
            if char.isspace():
                continue
            if not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]:
                continue
            glyphs.append({'char': char, 'box': box, 'origin': origin, 'draw_order': span['seqno']})
    index = defaultdict(list)
    text = ''.join(g['char'] for g in glyphs)
    for offset, char in enumerate(text):
        index[char].append(offset)
    width, height = payload['width'], payload['height']
    blocks = copy.deepcopy(payload['blocks'])
    changed = []
    for number, block in enumerate(blocks):
        needle = ''.join(c for c in block['t'] if not c.isspace())
        if not needle:
            continue
        original = [v * (width if i % 2 == 0 else height) for i, v in enumerate(block['b'])]
        matches = []
        for offset in index[needle[0]]:
            if not text.startswith(needle, offset):
                continue
            selected = glyphs[offset:offset + len(needle)]
            bounds = [min(g['box'][0] for g in selected), min(g['box'][1] for g in selected),
                      max(g['box'][2] for g in selected), max(g['box'][3] for g in selected)]
            size = max(g['box'][3] - g['box'][1] for g in selected)
            if (max(abs(bounds[i] - original[i]) for i in (0, 2)) > max(1., size * .15)
                    or bounds[1] < original[1] - size * .2 or bounds[3] > original[3] + size * .2
                    or max(g['origin'][1] for g in selected) - min(g['origin'][1] for g in selected) > size * .2):
                continue
            matches.append((bounds, selected))
        if len(matches) != 1:
            continue
        bounds, selected = matches[0]
        # Correct only inflated font ascender/descender bounds. Ordinary fonts
        # retain their independently extracted boxes.
        if original[3] - original[1] < (bounds[3] - bounds[1]) * 1.5:
            continue
        box = [max(0., min(1., v / (width if i % 2 == 0 else height))) for i, v in enumerate(bounds)]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        block['native_geometry'] = {'profile': PROFILE, 'original_box': block['b'],
                                    'draw_orders': sorted({g['draw_order'] for g in selected})}
        block['b'] = box
        if 'q' in block:
            block['q'] = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
        changed.append(number)
    if not changed:
        return payload
    return {**payload, 'blocks': blocks, 'extracted_text': payload['text'],
            'extracted_blocks': payload['blocks'],
            'native_evidence': {**payload.get('native_evidence', {}), 'geometry_profile': PROFILE,
                                'geometry_refined_blocks': changed}}
