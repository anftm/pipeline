"""Keep native PDF text and recognize embedded raster regions in page coordinates."""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path

PROFILE = 'native-raster-text-v3-rule-evidence'


def simple_fill(pixels):
    import numpy as np
    if np.all(pixels == pixels[:1, :, :]) or np.all(pixels == pixels[:, :1, :]):
        return True
    if pixels.shape[2] not in {2, 4}:
        return False
    # A uniform colored rectangle can have a separable antialiased alpha edge.
    # Require both edge profiles to rise once and fall once, with no glyph holes.
    color, alpha = pixels[:, :, :-1], pixels[:, :, -1].astype(float)
    visible = alpha > 0
    if not visible.any():
        return True
    if not np.all(color[visible] == color[visible][0]):
        return False
    vertical, horizontal = alpha.max(axis=1), alpha.max(axis=0)
    def unimodal(values):
        peak = int(values.argmax())
        return bool(np.all(np.diff(values[:peak+1]) >= 0) and np.all(np.diff(values[peak:]) <= 0))
    if not unimodal(vertical) or not unimodal(horizontal):
        return False
    predicted = vertical[:, None] * horizontal[None, :] / alpha.max()
    return bool(np.max(np.abs(alpha-predicted)) <= 1)


def inspect(page):
    """Record every drawn image; only simple fills are exempt from recognition."""
    import numpy as np
    import pymupdf
    masks = {row[0]: row[1] for row in page.get_images(full=True)}
    regions, fills = [], []
    for image in page.get_image_info(xrefs=True):
        bbox = pymupdf.Rect(image['bbox']) & page.rect
        if bbox.is_empty:
            continue
        box = [max(0., min(1., value)) for value in
               [bbox.x0/page.rect.width, bbox.y0/page.rect.height,
                bbox.x1/page.rect.width, bbox.y1/page.rect.height]]
        row = {'box': box, 'pixels': [image['width'], image['height']], 'xref': image['xref']}
        simple = False
        if image['xref']:
            pix = pymupdf.Pixmap(page.parent, image['xref'])
            mask = masks.get(image['xref'])
            if mask and not pix.alpha:
                pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(page.parent, mask))
            raw = pix.samples
            pixels = np.frombuffer(raw, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            row['pixel_sha256'] = hashlib.sha256(raw).hexdigest()
            # A constant field or a one-dimensional fill cannot carry glyphs.
            # Alpha participates: a black raster with a text-shaped mask is text.
            simple = simple_fill(pixels)
        row['classification'] = 'simple-fill' if simple else 'recognition-required'
        (fills if simple else regions).append(row)
    return {'profile': PROFILE, 'regions': regions, 'fills': fills}


def recognize(image, components, language, backend, ocr):
    """Use full-resolution page crops and map every retained block back exactly."""
    from PIL import Image
    blocks = copy.deepcopy(components['blocks'])
    reports, empty, detected = [], [], 0
    actual = None
    with Image.open(image) as page:
        for number, region in enumerate(components['raster']['regions']):
            box = region['box']
            bounds = [math.floor(box[0]*page.width), math.floor(box[1]*page.height),
                      math.ceil(box[2]*page.width), math.ceil(box[3]*page.height)]
            crop_path = Path(image).with_name(f'raster-{number}.png')
            try:
                with page.crop(bounds) as crop:
                    crop.save(crop_path)
                    width, height = crop.size
                recognized = ocr.ocr_page(crop_path, width, height, language, backend)
                models = ocr.actual_ocr_models(language, backend)
                try:
                    from . import ocr_graphic_rules
                except ImportError:
                    import ocr_graphic_rules
                with Image.open(crop_path) as original:
                    recognized, models, graphic = ocr_graphic_rules.filter_detections(original, recognized, models)
                if actual is None:
                    actual = copy.deepcopy(models)
                diagnostics = models.get('diagnostics', {})
                empty.extend(f'{number}:{index}' for index in diagnostics.get('empty_regions', []))
                detected += diagnostics.get('detected_regions', len(recognized))
                reports.append({'region': number, 'source': region, 'crop_pixels': bounds,
                                'crop_sha256': hashlib.sha256(crop_path.read_bytes()).hexdigest(),
                                'blocks': copy.deepcopy(recognized), 'models': models,
                                **({'graphic_rules': graphic} if graphic else {})})
                def point(x, y):
                    return [(bounds[0]+x*width)/page.width, (bounds[1]+y*height)/page.height]
                for original in recognized:
                    block = copy.deepcopy(original)
                    b = block['b']
                    block['b'] = point(b[0], b[1]) + point(b[2], b[3])
                    if 'q' in block:
                        block['q'] = [point(x, y) for x, y in block['q']]
                    block['raster_region'] = number
                    blocks.append(block)
            finally:
                crop_path.unlink(missing_ok=True)
    actual = actual or ocr.actual_ocr_models(language, backend)
    actual['diagnostics'] = {'detected_regions': detected,
                             'recognized_regions': sum(len(r['blocks']) for r in reports),
                             'empty_regions': empty,
                             'mixed_native': {'profile': PROFILE, 'regions': reports}}
    return blocks, actual
