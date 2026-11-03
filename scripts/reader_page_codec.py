"""One reading-image codec for document streams; OCR still uses lossless PNG."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile


THREADS = int(os.environ.get('READER_JXL_THREADS', '2'))
PROFILE = 'reader-jxl-v3-adaptive-small1.3e3-gray2m7-color2.1m5-progressive'


def validate_settings():
    if not 1 <= THREADS <= 32:
        raise ValueError('invalid document JXL encoding settings')


def encode_png(source: Path, target: Path):
    from PIL import Image
    with Image.open(source) as image:
        return encode_image(image, target)


def encoding_recipe(image):
    """Keep source samples; vary codec effort for fine grayscale scans vs color."""
    import numpy as np
    rgb = np.asarray(image.convert('RGB'))
    gray = bool(np.array_equal(rgb[:, :, 0], rgb[:, :, 1]) and np.array_equal(rgb[:, :, 0], rgb[:, :, 2]))
    if image.width * image.height <= 1_000_000:
        return {'distance': 1.3, 'effort': 3, 'modular': False, 'gray': gray}
    if gray:
        return {'distance': 2.0, 'effort': 7, 'modular': True, 'gray': True}
    return {'distance': 2.1, 'effort': 5, 'modular': True, 'gray': False}


def _encode(source, target, recipe):
    validate_settings()
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [os.environ.get('CJXL', 'cjxl'), str(source), str(target),
               '-d', str(recipe['distance']), '-e', str(recipe['effort']),
               '--num_threads', str(THREADS)]
    command += ['-m', '1', '--progressive'] if recipe['modular'] else ['--progressive_dc=1', '--progressive_ac']
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=600, check=False)
    if result.returncode or not target.is_file() or not target.stat().st_size:
        raise RuntimeError('JXL encoding failed: ' + result.stderr.decode('utf-8', 'replace')[-800:])
    raw = target.read_bytes()
    if not (raw.startswith(b'\xff\x0a') or raw.startswith(b'\x00\x00\x00\x0cJXL \r\n\x87\n')):
        raise ValueError('encoder output is not JPEG XL')
    return hashlib.sha256(raw).hexdigest(), len(raw)


def encode_image(image, target: Path):
    """Pass raw pixels to cjxl, avoiding an extra PNG encode/decode cycle."""
    target.parent.mkdir(parents=True, exist_ok=True)
    recipe = encoding_recipe(image)
    with tempfile.TemporaryDirectory(prefix='reader-jxl-', dir=target.parent) as directory:
        source = Path(directory) / 'input.ppm'
        converted = image.convert('L' if recipe['gray'] else 'RGB')
        converted.save(source)
        converted.close()
        return _encode(source, target, recipe)


def geometry(raw):
    """Decode the actual codestream when verifying staged/uploaded page bytes."""
    import imagecodecs
    pixels = imagecodecs.jpegxl_decode(raw, numthreads=THREADS)
    if pixels.ndim not in (2, 3) or min(pixels.shape[:2]) < 1:
        raise ValueError('invalid JPEG XL dimensions')
    return [pixels.shape[1], pixels.shape[0]]
