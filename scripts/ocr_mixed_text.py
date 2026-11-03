"""Recognize embedded Greek words using their own model and original pixels."""
from __future__ import annotations

import re
import subprocess
import unicodedata
import hashlib

try:
    from . import ocr_greek
except ImportError:
    import ocr_greek


PROFILE = 'mixed-greek-v5-' + ocr_greek.PROFILE


def greek(character):
    return 'GREEK' in unicodedata.name(character, '')


def candidates(text):
    return [match for match in re.finditer(r'[A-Za-z\u00c0-\u02ff\u0370-\u03ff\u1f00-\u1fff\u0300-\u036f]+', text)
             if sum(greek(c) for c in match[0]) >= 2]


def crop_bounds(image, characters, start, end):
    """Extend missing glyph tails through the pixel gap before adjacent text."""
    import cv2
    import numpy as np
    points = np.asarray([p for box in characters[start:end] if box is not None for p in box], dtype=float)
    x0, y0 = points.min(axis=0)
    x1, y1 = points.max(axis=0)
    height, width = image.shape[:2]
    line_height = y1 - y0
    pad = max(3, round(line_height * .15))
    previous = next((b for b in reversed(characters[:start]) if b is not None), None)
    following = next((b for b in characters[end:] if b is not None), None)
    left_limit = max(0, round(x0 - line_height * .65),
                     int(max(p[0] for p in previous)) if previous is not None else 0)
    right_limit = min(width, round(x1 + line_height * 1.6),
                      int(min(p[0] for p in following)) if following is not None else width)
    top, bottom = max(0, int(y0) - pad), min(height, int(y1) + pad)
    gray = cv2.cvtColor(image[top:bottom], cv2.COLOR_BGR2GRAY)
    ink = (gray < 180).any(axis=0)
    def gaps(first, last):
        runs = []
        for column in range(first, last):
            if not ink[column]:
                if runs and column == runs[-1][-1] + 1:
                    runs[-1].append(column)
                else:
                    runs.append([column])
        return [run for run in runs if len(run) >= 2]
    before = gaps(left_limit, max(left_limit, int(x0)))
    after = gaps(min(width, int(x1)), max(min(width, int(x1)), right_limit))
    left = round(sum(before[-1]) / len(before[-1])) if before else left_limit
    right = round(sum(after[-1]) / len(after[-1])) if after else right_limit
    # Keep both boundaries within the observed whitespace; changing the border
    # must not add a Chinese glyph or delete the final Greek letter.
    outer_left = before[-1][0] if before else left
    outer_right = after[-1][-1] + 1 if after else right
    return [[left, top, right, bottom], [outer_left, top, outer_right, bottom]], {
        'character_bounds': [float(x0), float(y0), float(x1), float(y1)],
        'neighbor_limits': [left_limit, right_limit], 'boundary_policy': 'pixel-whitespace-v1'}


def recognize(result, width, height, models):
    """Return corrected blocks; retain primary output and both crop hypotheses."""
    import numpy as np
    try:
        from . import pdf_ocr
    except ImportError:
        import pdf_ocr

    blocks = pdf_ocr.normalize_rapid_result(result, width, height)
    if not blocks or not any(candidates(text) for text in result.txts):
        return blocks
    auditor = pdf_ocr.get_ocr_engine('el')
    corrections, issues = [], []
    # normalize_blocks sorts boxes; identify a detection by its normalized quad.
    by_quad = {tuple(tuple(p) for p in block.get('q', [])): block for block in blocks}
    word_results = getattr(result, 'word_results', None)
    for index, (text, polygon) in enumerate(zip(result.txts, result.boxes)):
        words = word_results[index] if word_results is not None and index < len(word_results) else None
        spans = candidates(text)
        if not spans:
            continue
        quad = tuple(tuple(max(0., min(1., float(value) / (width if axis == 0 else height)))
                           for axis, value in enumerate(point)) for point in polygon)
        block = by_quad.get(quad)
        if block is None or not words:
            issues.append({'code': 'mixed-script-coordinates-unavailable', 'severity': 'review', 'evidence': {'region': index}})
            continue
        characters, cursor = [], 0
        for word, confidence, box in words:
            while cursor < len(text) and text[cursor].isspace() and not word.startswith(text[cursor]):
                characters.append(None)
                cursor += 1
            if text[cursor:cursor + len(word)] != word:
                characters = []
                break
            characters.extend([box] * len(word))
            cursor += len(word)
        while cursor < len(text) and text[cursor].isspace():
            characters.append(None)
            cursor += 1
        if cursor != len(text) or len(characters) != len(text):
            issues.append({'code': 'mixed-script-coordinates-unavailable', 'severity': 'review', 'evidence': {'region': index}})
            continue
        replacements = []
        for span in spans:
            bounds, boundary = crop_bounds(result.img, characters, span.start(), span.end())
            variants = [result.img[b[1]:b[3], b[0]:b[2]].copy() for b in bounds]
            recognized = auditor.recognize_txt(variants)
            values = [unicodedata.normalize('NFC', value.strip()) for value in recognized.txts]
            try:
                polytonic = ocr_greek.recognize(variants)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                polytonic = {'regions': [], 'error': f'{type(error).__name__}: {error}'[:500]}
            rows = polytonic['regions']
            poly_values = [r.get('core', '') for r in rows]
            accepted = (len(rows) == 4 and all(not r.get('returncode') and not r.get('error') for r in rows)
                        and len(set(poly_values)) == 1 and len(poly_values[0]) >= 2)
            selected = poly_values[0] if accepted else None
            evidence = {'region': index, 'range': [span.start(), span.end()], 'primary': span[0],
                        'box': [v / (width if i % 2 == 0 else height) for i, v in enumerate(bounds[0])],
                        'crop_bounds': bounds, 'boundary': boundary,
                        'crop_sha256': [hashlib.sha256(v.tobytes()).hexdigest() for v in variants],
                        'candidates': values, 'confidence': list(recognized.scores), 'polytonic': polytonic,
                        'selected': selected, 'accepted': accepted}
            corrections.append(evidence)
            if accepted:
                replacements.append((span.start(), span.end(), selected))
            else:
                issues.append({'code': 'mixed-script-recognition-unresolved', 'severity': 'review',
                               'evidence': {'region': index, 'range': evidence['range']}})
        if replacements:
            block['primary_text'] = text
            for start, end, value in reversed(replacements):
                text = text[:start] + value + text[end:]
            block['t'] = text
    secondary = pdf_ocr.actual_ocr_models('el')['models']['rec']
    models['models']['rec_greek'] = secondary
    for row in corrections:
        if row['polytonic'].get('model'):
            models['models']['rec_polytonic_greek'] = row['polytonic']['model']
    models.setdefault('diagnostics', {})['mixed_script'] = {
        'profile': PROFILE, 'model': secondary, 'regions': corrections, 'issues': issues}
    return blocks
