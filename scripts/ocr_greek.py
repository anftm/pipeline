"""Pinned polytonic Greek recognition with original-pixel crop evidence."""
from __future__ import annotations

import csv
from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unicodedata

REVISION = 'e12c65a915945e4c28e237a9b52bc4a8f39a0cec'
SHA256 = 'dfc9bda286cd9d8755b1832e5731a5425d1cf0803393a5fa6dee078466178cf1'
SIZE = 5168122
PROFILE = 'polytonic-grc-v3-lines-' + SHA256[:12]


@lru_cache(maxsize=1)
def model():
    cache = Path(os.environ.get('OCR_GREEK_MODEL_DIR', str(Path.home() / '.cache/reader-grc' / REVISION)))
    path = cache / 'grc.traineddata'
    cache.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        import httpx
        url = f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{REVISION}/grc.traineddata'
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            pending = Path(stream.name)
            try:
                total = 0
                with httpx.stream('GET', url, follow_redirects=True, timeout=120) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > SIZE:
                            raise ValueError('Greek model exceeds pinned size')
                        stream.write(chunk)
                stream.flush()
                if total != SIZE or hashlib.sha256(pending.read_bytes()).hexdigest() != SHA256:
                    raise ValueError('Greek model download checksum mismatch')
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
    if path.stat().st_size != SIZE or hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Greek model checksum mismatch')
    version = subprocess.run(['tesseract', '--version'], check=True, capture_output=True, text=True, timeout=10)
    return path, {'file': path.name, 'sha256': SHA256, 'bytes': SIZE, 'revision': REVISION,
                  'language': 'grc', 'backend': version.stdout.splitlines()[0], 'profile': PROFILE}


def core(text):
    """Keep all Greek marks and internal spaces; expose adjacent punctuation."""
    text = unicodedata.normalize('NFC', text.strip())
    indices = [i for i, c in enumerate(text) if 'GREEK' in unicodedata.name(c, '')
               and unicodedata.category(c).startswith('L')]
    if not indices:
        return '', {'prefix': text, 'suffix': ''}
    first, last = indices[0], indices[-1] + 1
    while last < len(text) and unicodedata.combining(text[last]):
        last += 1
    value = text[first:last]
    if any(not (c.isspace() or unicodedata.combining(c) or 'GREEK' in unicodedata.name(c, '')) for c in value):
        return '', {'prefix': text, 'suffix': ''}
    return value, {'prefix': text[:first], 'suffix': text[last:]}


def line_pixels(pixels):
    """Separate neighboring lines while retaining detached marks above the word."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY) if pixels.ndim == 3 else pixels
    ink = gray < 180
    runs = []
    for y in np.flatnonzero(ink.any(axis=1)):
        if runs and y == runs[-1][-1] + 1:
            runs[-1].append(int(y))
        else:
            runs.append([int(y)])
    if not runs:
        return pixels.copy(), {'bounds': [0, len(pixels)], 'blank': True}
    main = max(runs, key=lambda run: int(ink[run].sum()))
    # Compare every detached run to the original body, so including an accent
    # cannot bridge the whitespace to a different printed line.
    gap_limit = max(3, len(main) * .3)
    kept = [run for run in runs if max(main[0] - run[-1] - 1, run[0] - main[-1] - 1, 0) <= gap_limit]
    first, last = min(run[0] for run in kept), max(run[-1] for run in kept)
    top, bottom = max(0, first - 3), min(len(pixels), last + 4)
    return pixels[top:bottom].copy(), {'bounds': [top, bottom], 'body': [main[0], main[-1] + 1],
                                     'retained_runs': [[run[0], run[-1] + 1] for run in kept]}


def consensus(rows, count):
    values = [row.get('core', '') for row in rows]
    return (len(rows) == count and count > 0 and len(set(values)) == 1 and len(values[0]) >= 2
            and all(not row.get('returncode') and not row.get('error') for row in rows))


def recognize_variants(crops, *, isolated=False):
    from PIL import Image, ImageOps
    import cv2
    import numpy as np
    path, identity = model()
    rows = []
    with tempfile.TemporaryDirectory(prefix='reader-grc-') as directory:
        for number, pixels in enumerate(crops):
            geometry = None
            if isolated:
                pixels, geometry = line_pixels(pixels)
            original = Image.fromarray(pixels).convert('RGB')
            variants = [('native', 1), ('nearest', 2)] if isolated else [('bilinear', 2), ('bilinear-otsu', 2)]
            for preprocessing, scale in variants:
                resampling = Image.Resampling.NEAREST if isolated else Image.Resampling.BILINEAR
                with original.resize((original.width * scale, original.height * scale), resampling) as resized:
                    if preprocessing == 'bilinear-otsu':
                        binary = cv2.threshold(np.array(resized.convert('L')), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
                        resized.paste(Image.fromarray(binary).convert('RGB'))
                    with ImageOps.expand(resized, border=12, fill='white') as padded:
                        file = Path(directory) / 'crop.png'
                        padded.save(file, dpi=(300, 300))
                row = {'crop': number, 'scale': scale, 'psm': 7,
                       'preprocessing': preprocessing,
                       'input_sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                       'source_pixels': list(original.size)}
                if geometry:
                    row['line_geometry'] = geometry
                try:
                    run = subprocess.run(['tesseract', str(file), 'stdout', '--tessdata-dir', str(path.parent),
                                          '-l', 'grc', '--psm', '7', '-c', 'tessedit_create_tsv=1']
                                          + (['-c', 'load_system_dawg=0', '-c', 'load_freq_dawg=0'] if isolated else []),
                                          capture_output=True, text=True, timeout=20,
                                         env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
                    words = [word for word in csv.DictReader(io.StringIO(run.stdout), delimiter='\t')
                             if word.get('level') == '5' and word.get('text', '').strip()]
                    text = ' '.join(word['text'] for word in words)
                    value, adjacent = core(text)
                    row.update(text=text, core=value, adjacent=adjacent, words=words,
                               returncode=run.returncode, stderr=run.stderr[-500:])
                except (OSError, subprocess.TimeoutExpired) as error:
                    row['error'] = f'{type(error).__name__}: {error}'[:500]
                rows.append(row)
            original.close()
    return {'model': identity, 'regions': rows}


def recognize(crops):
    """Keep initial evidence; resolve crop artifacts with two original-pixel scales."""
    result = recognize_variants(crops)
    initial = result['regions']
    if consensus(initial, len(crops) * 2) or any(row.get('error') or row.get('returncode') for row in initial):
        return result
    repaired = recognize_variants(crops, isolated=True)
    return {**repaired, 'initial_regions': initial,
            'retry_reason': 'inconsistent word crops; isolate printed line without smoothing or dictionary correction'}
