"""Read-only geometry checks for historical JPEG XL page resources."""
from __future__ import annotations

import os


THREADS = int(os.environ.get('READER_JXL_THREADS', '2'))


def geometry(raw):
    """Decode the actual codestream when verifying staged/uploaded page bytes."""
    import imagecodecs
    pixels = imagecodecs.jpegxl_decode(raw, numthreads=THREADS)
    if pixels.ndim not in (2, 3) or min(pixels.shape[:2]) < 1:
        raise ValueError('invalid JPEG XL dimensions')
    return [pixels.shape[1], pixels.shape[0]]
