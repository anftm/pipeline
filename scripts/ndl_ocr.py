"""Pinned NDLOCR-Lite ONNX models for Japanese document pages.

Model architecture and preprocessing: National Diet Library, Japan, NDLOCR-Lite
https://github.com/ndl-lab/ndlocr-lite (CC BY 4.0). This adapter adds bounded CPU
workers, checksummed downloads, actual recognition confidence, strict result
validation, retained folios and shared page-coordinate/quality contracts.
The detector uses a DINOv3-derived backbone; see third_party/NDLOCR-Lite.txt.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import tempfile
import time

REVISION = 'd001dad2013ceabb5736ff8d637474e443eb73c2'
PROFILE = 'ndl202604-' + REVISION[:12] + '-adapter11-space-boundaries'
ASSETS = {
    'det': ('model/deim-s-1024x1024.onnx', 'c156ce0c4e704bc3bf7e4016d0a87b949cffa8b3724f4b4cc696b8284c3c7373'),
    'rec': ('model/parseq-ndl-24x384-50-tiny-300epoch-tegaki3-r8data-202604.onnx',
            '49cea9db4552f19eb05c8ee202fcf74714977749b2f4c9376b127fde41b07a99'),
    'rec_long': ('model/parseq-ndl-24x768-100-tiny-153epoch-tegaki3-r8data-202604.onnx',
                 '06462b0dbd5b0b8508545c8c3d485cf20dbf4ffa652fe145e69c9e7457080602'),
    'charset': ('config/NDLmoji.yaml', 'f6ad5a2de444b495155866af811cf1a98309dcae3225db802767ea531a2dc529'),
}


def asset(name):
    relative, expected = ASSETS[name]
    cache = Path(os.environ.get('NDLOCR_MODEL_DIR', str(Path.home() / '.cache/reader-ndlocr' / REVISION)))
    path = cache / relative
    if path.is_file():
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('NDLOCR model checksum mismatch: ' + relative)
        return path
    import httpx
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f'https://raw.githubusercontent.com/ndl-lab/ndlocr-lite/{REVISION}/src/{relative}'
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            digest = hashlib.sha256()
            total = 0
            with httpx.stream('GET', url, follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ValueError('NDLOCR model exceeds pinned asset limit')
                    digest.update(chunk)
                    temporary.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError('downloaded NDLOCR model checksum mismatch: ' + relative)
            temporary.flush()
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    return path


class Engine:
    def __init__(self):
        import onnxruntime as ort
        import yaml
        self.paths = {name: asset(name) for name in ASSETS}
        self.chars = yaml.safe_load(self.paths['charset'].read_text(encoding='utf-8'))['model']['charset_train']
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.sessions = {name: ort.InferenceSession(str(self.paths[name]), options,
                         providers=['CPUExecutionProvider']) for name in ('det', 'rec', 'rec_long')}
        self.diagnostics = {}

    def models(self):
        return {'backend': 'ndlocr_onnxruntime', 'revision': REVISION, 'profile': PROFILE,
                'models': {name: {'file': path.name, 'sha256': ASSETS[name][1], 'bytes': path.stat().st_size,
                                 'version': 'NDLOCR-Lite-202604', 'language': 'japan'}
                           for name, path in self.paths.items()}, 'diagnostics': self.diagnostics}

    def detect(self, image):
        import numpy as np
        from PIL import Image
        session = self.sessions['det']
        size = session.get_inputs()[0].shape[2:]
        h, w = image.shape[:2]
        side = max(h, w)
        padded = np.zeros((side, side, 3), dtype=np.uint8)
        padded[:h, :w] = image
        resized = np.asarray(Image.fromarray(padded).resize((size[1], size[0])), dtype=np.float32) / 255
        resized = (resized - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
        outputs = session.run(None, {session.get_inputs()[0].name: np.ascontiguousarray(resized.transpose(2, 0, 1)[None]),
                                    session.get_inputs()[1].name: np.array([size], dtype=np.int64)})
        labels, boxes, scores = [np.asarray(x).reshape(-1, 4 if index == 1 else 1)
                                for index, x in enumerate(outputs[:3])]
        if not len(labels) == len(boxes) == len(scores):
            raise ValueError('NDLOCR detector result lengths differ')
        result = []
        for label, box, score in zip(labels[:, 0], boxes, scores[:, 0]):
            if not np.isfinite(score) or score < .25:
                continue
            box = (box * np.array([side / size[1], side / size[0]] * 2)).astype(int)
            x0, y0, x1, y1 = map(int, box)
            x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
            if x1 <= x0 or y1 <= y0:
                continue
            result.append({'class': int(label) - 1, 'box': [x0, y0, x1, y1], 'confidence': float(score)})
        lines = []
        self.suppressed = []
        def overlap(a, b):
            x0, y0, x1, y1 = a
            u0, v0, u1, v1 = b
            area = max(0, min(x1, u1) - max(x0, u0)) * max(0, min(y1, v1) - max(y0, v0))
            return area / max(1, min((x1 - x0) * (y1 - y0), (u1 - u0) * (v1 - v0)))
        for line in sorted((r for r in result if r['class'] in {1, 2, 3, 4, 5, 16}),
                           key=lambda r: -r['confidence']):
            duplicate = next((p for p in lines if overlap(line['box'], p['box']) > .85), None)
            if duplicate:
                self.suppressed.append({'detection': line, 'retained': duplicate})
            else:
                lines.append(line)
        for candidate in result:
            if candidate['class'] not in {8, 9, 10, 14}:
                continue
            x0, y0, x1, y1 = candidate['box']
            area = (x1 - x0) * (y1 - y0)
            if any(max(0, min(x1, p['box'][2]) - max(x0, p['box'][0])) *
                   max(0, min(y1, p['box'][3]) - max(y0, p['box'][1])) > area * .5 for p in lines):
                continue
            lines.append(candidate)
        return lines

    def read_line(self, crop, name, vertical=None):
        import cv2
        import numpy as np
        session = self.sessions[name]
        height, width = session.get_inputs()[0].shape[2:]
        if vertical is True or vertical is None and crop.shape[0] > crop.shape[1] * .8:
            crop = cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)
        image = cv2.resize(crop, (width, height), interpolation=cv2.INTER_LINEAR)
        image = np.ascontiguousarray(image[:, :, ::-1]).astype(np.float32) / 127.5 - 1
        logits = session.run(None, {session.get_inputs()[0].name: image.transpose(2, 0, 1)[None]})[0][0]
        indices = logits.argmax(axis=1)
        stops = np.flatnonzero(indices == 0)
        end = int(stops[0]) if len(stops) else len(indices)
        if any(not 1 <= int(i) <= len(self.chars) for i in indices[:end]):
            raise ValueError('NDLOCR character index outside pinned vocabulary')
        probabilities = np.exp(logits - logits.max(axis=1, keepdims=True))
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        confidence = float(probabilities[np.arange(end), indices[:end]].mean()) if end else 0
        return ''.join(self.chars[int(i) - 1] for i in indices[:end]), confidence, bool(len(stops))

    def read_ruby(self, image, box, vertical, initial):
        """Recover clipped small kana using bounded margins and model agreement."""
        x0, y0, x1, y1 = box
        height, width = image.shape[:2]
        narrow = min(x1 - x0, y1 - y0)
        alternatives = [dict(initial, margin=0)]
        for margin in sorted({max(2, round(narrow * .12)), max(3, round(narrow * .25))}):
            crop = image[max(0, y0 - margin):min(height, y1 + margin),
                         max(0, x0 - margin):min(width, x1 + margin)]
            for model in ('rec', 'rec_long'):
                text, confidence, terminated = self.read_line(crop, model, vertical=vertical)
                alternatives.append({'text': text, 'confidence': confidence, 'terminated': terminated,
                                     'model': model, 'margin': margin})
        return select_ruby(alternatives), alternatives

    def read_segments(self, crop):
        """Split at empty pixel bands, retaining every pixel and model attempt."""
        import cv2
        import numpy as np
        vertical = crop.shape[0] > crop.shape[1] * .8
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        ink = gray < 220
        axis = 0 if vertical else 1
        projection = ink.sum(axis=1 if vertical else 0)
        extent, narrow = crop.shape[axis], crop.shape[1-axis]
        blank = np.flatnonzero(projection == 0)
        groups = []
        for index in blank:
            if groups and index == groups[-1][-1]+1:
                groups[-1].append(int(index))
            else:
                groups.append([int(index)])
        boundaries = [0]
        for group in groups:
            center = round((group[0]+group[-1])/2)
            if (len(group) >= max(2, narrow*.1) and center-boundaries[-1] >= narrow*2
                    and extent-center >= narrow*2):
                boundaries.append(center)
        boundaries.append(extent)
        if len(boundaries) < 3:
            return [], False
        segments = []
        for start, stop in zip(boundaries, boundaries[1:]):
            bounds = [0, start, crop.shape[1], stop] if vertical else [start, 0, stop, crop.shape[0]]
            part = crop[bounds[1]:bounds[3], bounds[0]:bounds[2]]
            attempts = []
            for model in ('rec', 'rec_long'):
                text, confidence, terminated = self.read_line(part, model, vertical=vertical)
                attempts.append({'text': text, 'confidence': confidence, 'terminated': terminated, 'model': model})
            a, b = attempts
            agreed = (a['text'] == b['text'] and bool(a['text'].strip()) and min(a['confidence'], b['confidence']) >= .8
                      and a['terminated'] and b['terminated'] and len(a['text']) < 45)
            segments.append({'crop_box': bounds, 'attempts': attempts, 'agreed': agreed})
        return segments, all(part['agreed'] for part in segments)

    def recognize(self, path):
        import cv2
        import numpy as np
        from PIL import Image
        with Image.open(path) as source:
            image = np.asarray(source.convert('RGB'))
        height, width = image.shape[:2]
        start = time.monotonic()
        lines = self.detect(image)
        detection_seconds = time.monotonic() - start
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        cuts, rule_evidence = vertical_separators(gray, [line['box'] for line in lines])
        def recognize_line(line):
            x0, y0, x1, y1 = line['box']
            crop = image[y0:y1, x0:x1]
            if line['class'] == 9:
                # Give isolated folios a horizontal square canvas. Rotating a
                # tall single digit can turn 4 into 44 in the line recognizer.
                pad = max(3, round((y1 - y0) * .35))
                crop = image[max(0, y0 - pad):min(height, y1 + pad), max(0, x0 - pad):min(width, x1 + pad)]
                canvas = np.full((crop.shape[0], crop.shape[1] * 2, 3), 255, dtype=np.uint8)
                left = crop.shape[1] // 2
                canvas[:, left:left + crop.shape[1]] = crop
                crop = canvas
            model = 'rec_long' if line['class'] == 9 else 'rec'
            text, confidence, terminated = self.read_line(crop, model)
            alternatives = []
            # The detector also labels real folios as body text. On an isolated
            # short numeric footer, compare both models without quarter-turning
            # a nearly square two-digit crop. Never infer a number from sequence.
            footer = (line['class'] in {1,2,3,4,5,9,16} and y0/height>.88
                      and y1-y0<height*.035 and x1-x0<width*.12
                      and text.strip().isdigit() and len(text.strip())<=4)
            if footer:
                original={'text':text,'confidence':confidence,'terminated':terminated,'model':model}
                candidates=[]
                for selected_model in ('rec','rec_long'):
                    value,score,ended=self.read_line(image[y0:y1,x0:x1],selected_model,vertical=False)
                    candidates.append({'text':value,'confidence':score,'terminated':ended,'model':selected_model,
                                       'orientation':'horizontal','reason':'isolated-folio'})
                if (candidates[0]['text']==candidates[1]['text'] and candidates[0]['text'].isdigit()
                        and all(c['terminated'] and c['confidence']>=.9 for c in candidates)):
                    selected=max(candidates,key=lambda c:c['confidence'])
                    text,confidence,terminated,model=(selected[k] for k in ('text','confidence','terminated','model'))
                alternatives=[original,*candidates]
            if line['class']==16 and (x1-x0)>3*(y1-y0):
                original={'text':text,'confidence':confidence,'terminated':terminated,'model':model}
                heading=[]
                for margin in (0,max(2,round((y1-y0)*.02))):
                    region=image[max(0,y0-margin):min(height,y1+margin),max(0,x0-margin):min(width,x1+margin)]
                    value,score,ended=self.read_line(region,'rec_long',vertical=False)
                    heading.append({'text':value,'confidence':score,'terminated':ended,'model':'rec_long',
                                    'margin':margin,'reason':'display-heading'})
                if (heading[0]['text']==heading[1]['text'] and heading[0]['text']
                        and all(c['terminated'] and c['confidence']>=.98 for c in heading)
                        and min(c['confidence'] for c in heading)>confidence):
                    selected=max(heading,key=lambda c:c['confidence'])
                    text,confidence,terminated,model=(selected[k] for k in ('text','confidence','terminated','model'))
                alternatives=[original,*heading]
            if line['class'] == 10 and (min(crop.shape[:2]) < 32 or confidence < .8):
                # A one-kana ruby must not be stretched across a 50-character
                # line. Preserve its orientation from the neighboring body.
                nearby = [p for p in lines if p['class'] in {1, 2, 3, 4, 5, 16}
                          and p['box'][1] <= (y0 + y1) / 2 <= p['box'][3]]
                closest = min(nearby, key=lambda p: abs((p['box'][0] + p['box'][2] - x0 - x1) / 2), default=None)
                vertical = bool(crop.shape[0] > crop.shape[1] * 1.5 and closest and
                                closest['box'][3] - closest['box'][1] > closest['box'][2] - closest['box'][0])
                initial = {'text': text, 'confidence': confidence, 'terminated': terminated, 'model': model}
                if max(crop.shape[:2]) > min(crop.shape[:2]) * 2.5:
                    selected, alternatives = self.read_ruby(image, line['box'], vertical, initial)
                    text, confidence, terminated, model = (selected[k] for k in ('text', 'confidence', 'terminated', 'model'))
                else:
                    border = max(2, round(min(crop.shape[:2]) * .12))
                    crop = image[max(0, y0 - border):min(height, y1 + border),
                                 max(0, x0 - border):min(width, x1 + border)]
                    side = max(crop.shape[:2])
                    pad = max(3, round(side * .4))
                    canvas = np.full((crop.shape[0] + pad if not vertical else side * 2,
                                      crop.shape[1] + 2 * pad if not vertical else side, 3), 255, dtype=np.uint8)
                    top, left = (canvas.shape[0] - crop.shape[0]) // 2, (canvas.shape[1] - crop.shape[1]) // 2
                    canvas[top:top + crop.shape[0], left:left + crop.shape[1]] = crop
                    candidate, score, ended = self.read_line(canvas, 'rec_long', vertical=vertical)
                    alternatives = [initial, {'text': candidate, 'confidence': score, 'terminated': ended,
                                             'model': 'rec_long', 'margin': border, 'canvas': True}]
                    if score > confidence and ended and any('\u3040' <= c <= '\u30ff' for c in candidate):
                        text, confidence, terminated, model = candidate, score, ended, 'rec_long'
            if not terminated or len(text) >= 45:
                text, confidence, terminated = self.read_line(crop, 'rec_long')
                model = 'rec_long'
            if not terminated or len(text) >= 98:
                import base64
                import io
                attempts, agreed = self.read_segments(crop)
                proof = {'reason': 'recognizer-capacity', 'original': {'text': text, 'confidence': confidence,
                          'terminated': terminated, 'model': model}, 'segments': attempts,
                         'box': line['box'], 'detector_class': line['class'], 'agreed': agreed}
                if not agreed:
                    stream = io.BytesIO()
                    Image.fromarray(crop).save(stream, format='PNG')
                    raw = stream.getvalue()
                    proof.update(crop_sha256=hashlib.sha256(raw).hexdigest(),
                                 crop_png_base64=base64.b64encode(raw).decode())
                    return {'t': '', 'b': [x0/width, y0/height, x1/width, y1/height],
                            'unresolved_region': proof}
                parts = []
                for part in attempts:
                    a, b, c, d = part['crop_box']
                    selected = max(part['attempts'], key=lambda row: row['confidence'])
                    parts.append({'t': selected['text'], 'c': round(selected['confidence'], 4), 's': 'ocr',
                                  'b': [(x0+a)/width, (y0+b)/height, (x0+c)/width, (y0+d)/height],
                                  'detector_class': line['class'], 'detector_confidence': line['confidence'],
                                  'recognizer': selected['model'], 'segmentation': proof})
                return {'segmented_blocks': parts}
            try:
                from . import ocr_long_dash
            except ImportError:
                import ocr_long_dash
            original_crop=image[y0:y1,x0:x1]
            parts,section_evidence=ocr_long_dash.split_sections(original_crop,text,self.read_line,
                                                               inline_spaces=line['class']!=10)
            section_meta=({'section':sum((x0+x1)/2>cut for cut in cuts),
                           'section_boundaries':[cut/width for cut in cuts]} if cuts else {})
            if parts:
                segmented=[]
                for part_index,part in enumerate(parts):
                    a,b,c,d=part['crop_box']
                    selected=max(part['attempts'],key=lambda r:r['confidence'])
                    content,dash_evidence=ocr_long_dash.recognize(original_crop[b:d,a:c],part['text'],self.read_line)
                    segmented.append({'t':content,'b':[(x0+a)/width,(y0+b)/height,(x0+c)/width,(y0+d)/height],
                        'c':round(min(r['confidence'] for r in part['attempts']),4),'s':'ocr',
                        'detector_confidence':line['confidence'],'detector_class':line['class'],
                        'recognizer':selected['model'],'segmentation':section_evidence,
                        'segmentation_part':part_index,**section_meta,
                        **({'long_dash':dash_evidence} if dash_evidence else {})})
                return {'segmented_blocks':segmented}
            text, dash_evidence = ocr_long_dash.recognize(original_crop,text,self.read_line)
            return {'t': text, 'b': [x0 / width, y0 / height, x1 / width, y1 / height],
                    'c': round(confidence, 4), 's': 'ocr', 'detector_confidence': line['confidence'],
                    'detector_class': line['class'], 'recognizer': model,
                    **({'alternatives': alternatives} if alternatives else {}),
                    **({'long_dash':dash_evidence} if dash_evidence else {}),
                    **({'section_check':section_evidence} if section_evidence else {}),
                    **section_meta}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(recognize_line, lines))
        blocks = [block for result in results for block in result.get('segmented_blocks', [result])]
        unresolved = [b['unresolved_region'] for b in blocks if b.get('unresolved_region')]
        empty = [i for i, b in enumerate(blocks) if not b['t'].strip() and not b.get('unresolved_region')]
        self.diagnostics = {'detected_regions': len(lines), 'recognized_regions': sum(bool(b['t'].strip()) for b in blocks),
                             'empty_regions': empty, 'detection_seconds': round(detection_seconds, 4),
                             'unresolved_regions': unresolved,
                            'suppressed_overlapping_detections': self.suppressed,
                             'vertical_separators': [cut / width for cut in cuts],
                             'separator_segments': rule_evidence,
                            'recognition_seconds': round(time.monotonic() - start - detection_seconds, 4)}
        return [b for b in blocks if b['t'].strip()]


def vertical_separators(gray, boxes):
    """Find printed rules, including bent scan lines, without cutting text."""
    import cv2
    import numpy as np
    height, width = gray.shape
    segments = cv2.createLineSegmentDetector().detect(gray)[0]
    if segments is None:
        return [], []
    candidates = []
    for x0, y0, x1, y1 in segments.reshape(-1, 4):
        if abs(y1 - y0) < height * .15 or abs(x1 - x0) > width * .04:
            continue
        center = (x0 + x1) / 2
        if not .05 * width < center < .95 * width:
            continue
        if any(b[0] < max(x0, x1) and b[2] > min(x0, x1)
               and b[1] < max(y0, y1) and b[3] > min(y0, y1) for b in boxes):
            continue
        candidates.append([float(x0), float(y0), float(x1), float(y1)])
    groups = []
    for segment in sorted(candidates, key=lambda s: (s[0] + s[2]) / 2):
        center = (segment[0] + segment[2]) / 2
        if groups and center - sum((s[0] + s[2]) / 2 for s in groups[-1]) / len(groups[-1]) <= .025 * width:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    cuts, evidence = [], []
    for group in groups:
        intervals = sorted((min(s[1], s[3]), max(s[1], s[3])) for s in group)
        covered, end = 0, 0
        for start, stop in intervals:
            covered += max(0, stop - max(end, start))
            end = max(end, stop)
        center = sum((s[0] + s[2]) / 2 for s in group) / len(group)
        if (covered < .6 * height or any(b[0] < center < b[2] for b in boxes)
                or not any(b[2] < center for b in boxes) or not any(b[0] > center for b in boxes)):
            continue
        cuts.append(center)
        evidence.append({'x': center / width, 'coverage': covered / height,
                         'segments': [[v / (width if i % 2 == 0 else height) for i, v in enumerate(s)] for s in group]})
    return cuts, evidence


def select_ruby(alternatives):
    initial = alternatives[0]
    groups = {}
    for candidate in alternatives[1:]:
        if (candidate['terminated'] and candidate['confidence'] >= .85
                and any('\u3040' <= c <= '\u30ff' for c in candidate['text'])):
            groups.setdefault(candidate['text'], []).append(candidate)
    supported = [values for values in groups.values()
                 if {v['model'] for v in values} == {'rec', 'rec_long'}
                 and len({v['margin'] for v in values}) > 1]
    if len(supported) != 1:
        return initial
    candidate = max(supported[0], key=lambda v: v['confidence'])
    return candidate if candidate['confidence'] > initial['confidence'] else initial


_ENGINE = None


def engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = Engine()
    return _ENGINE
