"""Preserve horizontal glyph orientation and separate merged printed scan lines."""
from __future__ import annotations

import hashlib

PROFILE = 'horizontal-crops-v1'


def crop(image, points):
    import cv2
    import numpy as np
    points = np.asarray(points, dtype=np.float32)
    width = max(1, int(max(np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3]))))
    height = max(1, int(max(np.linalg.norm(points[0] - points[3]), np.linalg.norm(points[1] - points[2]))))
    target = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(points, target)
    return cv2.warpPerspective(image, transform, (width, height), borderMode=cv2.BORDER_REPLICATE,
                               flags=cv2.INTER_CUBIC)


def bands(pixels):
    """Use background-corrected ink only to find whitespace; recognize raw pixels."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY) if pixels.ndim == 3 else pixels
    height, width = gray.shape
    if height < 16 or width < 3:
        return [(0, height)]
    size = min(15, height | 1, width | 1)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    corrected = cv2.divide(gray, background, scale=255)
    _, mask = cv2.threshold(corrected, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    ink = np.zeros_like(mask)
    for index, (x, y, w, h, area) in enumerate(stats[1:], 1):
        if area >= 3 and h >= 2:
            ink[labels == index] = 1
    projection = ink.sum(axis=1)
    active = np.flatnonzero(projection >= max(1, projection.max() * .08))
    runs = []
    for y in active:
        if runs and y <= runs[-1][-1] + 2:
            runs[-1].append(int(y))
        else:
            runs.append([int(y)])
    # Short detached marks belong to a neighboring line, never their own text row.
    bodies = [run for run in runs if run[-1] - run[0] + 1 >= 5]
    if len(bodies) < 2:
        return [(0, height)]
    sizes = [run[-1] - run[0] + 1 for run in bodies]
    if min(sizes) < max(sizes) * .55:
        return [(0, height)]
    cuts = []
    for first, second in zip(bodies, bodies[1:]):
        if second[0] - first[-1] - 1 < 2:
            return [(0, height)]
        cuts.append((first[-1] + second[0] + 1) // 2)
    boundaries = [0, *cuts, height]
    return list(zip(boundaries, boundaries[1:]))


def split(crops, detection, recognition):
    import cv2
    import numpy as np
    output, boxes, scores, evidence = [], [], [], []
    for index, (pixels, polygon) in enumerate(zip(crops, detection.boxes)):
        rows = bands(pixels)
        height, width = pixels.shape[:2]
        original = np.asarray(polygon, dtype=np.float32)
        rect = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
        inverse = cv2.getPerspectiveTransform(rect, original)
        pieces = []
        for first, last in rows:
            output.append(pixels[first:last].copy())
            points = np.array([[[0, first], [width, first], [width, last], [0, last]]], dtype=np.float32)
            mapped = cv2.perspectiveTransform(points, inverse)[0]
            boxes.append(mapped)
            scores.append(detection.scores[index])
            pieces.append({'bounds': [0, first, width, last], 'box': mapped.tolist(), 'region': len(output) - 1})
        if len(rows) > 1:
            evidence.append({'detected_region': index, 'box': original.tolist(),
                             'crop_sha256': hashlib.sha256(pixels.tobytes()).hexdigest(),
                             'primary': recognition.txts[index], 'confidence': float(recognition.scores[index]),
                             'lines': pieces})
    if evidence:
        detection.boxes = np.asarray(boxes, dtype=np.float32)
        detection.scores = scores
    return output if evidence else crops, evidence
