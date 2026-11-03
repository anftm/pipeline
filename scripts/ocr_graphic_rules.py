"""Separate repeated dashed circular rules from glyphs using source pixels."""
from __future__ import annotations

import hashlib
import itertools
import math
import copy
import base64
import io

PROFILE = 'dashed-circle-rules-v2-original-detections'


def detect(image):
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert('RGB'))
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    count, labels, stats, centers = cv2.connectedComponentsWithStats((gray < 128).astype(np.uint8))
    candidates = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < 8 or max(w, h) < 5:
            continue
        yy, xx = np.nonzero(labels[y:y+h, x:x+w] == index)
        covariance = np.cov(np.stack((xx, yy)))
        eigenvalues, vectors = np.linalg.eigh(covariance)
        if eigenvalues[1] < 4*max(eigenvalues[0], .1):
            continue
        candidates.append({'component': index, 'center': centers[index], 'direction': vectors[:, 1],
                           'length': float(max(w, h)), 'area': int(area), 'box': [int(x), int(y), int(x+w), int(y+h)]})
    selected, circles = set(), []
    remaining = candidates
    for _ in range(4):
        if len(remaining) < 8:
            break
        best = None
        # Evenly sampled triples bound work on diagrams with many small strokes.
        step = max(1, math.comb(len(remaining), 3)//2000)
        for sequence, triple in enumerate(itertools.combinations(remaining, 3)):
            if sequence % step:
                continue
            points = np.array([v['center'] for v in triple])
            matrix = 2*(points[1:]-points[0])
            if abs(np.linalg.det(matrix)) < 1:
                continue
            center = np.linalg.solve(matrix, np.sum(points[1:]**2-points[0]**2, axis=1))
            radius = float(np.linalg.norm(points[0]-center))
            if not 20 <= radius <= max(gray.shape)*1.5:
                continue
            members = []
            for item in remaining:
                radial = item['center']-center
                distance = float(np.linalg.norm(radial))
                if (abs(distance-radius) <= max(2., radius*.012) and item['length'] < radius*.3
                        and abs(np.dot(radial/max(distance, 1), item['direction'])) < .25):
                    members.append(item)
            if len(members) < 8:
                continue
            angles = sorted(math.atan2(*(v['center']-center)[::-1]) for v in members)
            max_gap = max(b-a for a,b in zip(angles, angles[1:]+[angles[0]+2*math.pi]))
            areas = [v['area'] for v in members]
            if max_gap > math.pi or max(areas) > min(areas)*2.5:
                continue
            if best is None or len(members) > len(best[2]):
                best = center, radius, members
        if best is None:
            break
        center, radius, members = best
        selected.update(v['component'] for v in members)
        circles.append({'center': center.tolist(), 'radius': radius,
                        'components': [{k:v[k] for k in ['component', 'area', 'length', 'box']} for v in members]})
        remaining = [v for v in remaining if v['component'] not in selected]
    if not selected:
        return None, None
    mask = np.isin(labels, list(selected)).astype(np.uint8)
    # Include antialiasing only within two pixels of a proved rule component.
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8)) > 0
    from PIL import Image
    stream = io.BytesIO()
    Image.fromarray(mask.astype(np.uint8)*255).save(stream, format='PNG')
    return mask, {'profile': PROFILE, 'circles': circles,
        'source_pixels_sha256': hashlib.sha256(pixels.tobytes()).hexdigest(),
        'mask_sha256': hashlib.sha256(mask.tobytes()).hexdigest(), 'masked_pixels': int(mask.sum()),
        'mask_png_base64': base64.b64encode(stream.getvalue()).decode()}


def filter_detections(image, blocks, models):
    """Exclude only detections whose original ink belongs entirely to proved rules.

    No replacement pixels or second OCR pass are used. Text touching a rule is
    retained whenever any substantial ink lies outside the proved components.
    Empty detections need their own positioned evidence before exclusion.
    """
    import cv2
    import numpy as np
    mask, proof = detect(image)
    if proof is None:
        return blocks, models, None
    width, height = image.size
    ink = np.asarray(image.convert('L')) < 128
    def coverage(block):
        box = block['b']
        x0, y0 = max(0, math.floor(box[0]*width)), max(0, math.floor(box[1]*height))
        x1, y1 = min(width, math.ceil(box[2]*width)), min(height, math.ceil(box[3]*height))
        if x1 <= x0 or y1 <= y0:
            return {'ink_pixels': 0, 'rule_pixels': 0, 'excluded': False}
        selected = ink[y0:y1, x0:x1].copy()
        if block.get('q'):
            polygon = np.rint(np.asarray(block['q'])*[width, height]-[x0, y0]).astype(np.int32)
            area = np.zeros(selected.shape, dtype=np.uint8)
            cv2.fillPoly(area, [polygon], 1)
            selected &= area.astype(bool)
        total = int(selected.sum())
        matched = int((selected & mask[y0:y1, x0:x1]).sum())
        return {'ink_pixels': total, 'rule_pixels': matched,
                'excluded': total >= 8 and matched >= total*.98}
    checks = [coverage(block) for block in blocks]
    retained = [block for block, check in zip(blocks, checks) if not check['excluded']]
    actual = copy.deepcopy(models)
    diagnostics = actual.setdefault('diagnostics', {})
    empty = set(diagnostics.get('empty_regions', []))
    empty_checks = []
    for region in diagnostics.get('detection_regions', []):
        if region['region'] not in empty:
            continue
        check = {'region': region['region'], **coverage(region)}
        empty_checks.append(check)
        if check['excluded']:
            empty.remove(region['region'])
    diagnostics['empty_regions'] = sorted(empty)
    diagnostics['recognized_regions'] = len(retained)
    diagnostics['graphic_exclusions'] = sum(check['excluded'] for check in checks + empty_checks)
    proof.update(original_blocks=copy.deepcopy(blocks), original_models=copy.deepcopy(models),
                 block_checks=checks, empty_detection_checks=empty_checks)
    return retained, actual, proof
