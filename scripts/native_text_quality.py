"""Decide whether extracted text can be used, retaining why image OCR is needed."""
import unicodedata

try:
    from . import audit_ocr_language as languages
except ImportError:
    import audit_ocr_language as languages


PROFILE = 'native-text-v3'


def assess(payload, language=None, explicit=False):
    text = payload.get('text', '')
    blocks = payload.get('blocks', [])
    reasons = list(payload.get('native_evidence', {}).get('reasons', []))
    if not text.strip() or not blocks:
        reasons.append('no-positioned-native-text')
    invalid = [i for i, c in enumerate(text) if c == '\ufffd' or
               unicodedata.category(c) in {'Co', 'Cs', 'Cn'} or
               unicodedata.category(c) == 'Cc' and c not in '\n\r\t']
    if invalid:
        reasons.append('invalid-native-characters')
    counts = languages.script_counts(text)
    letters = sum(counts.values())
    expected = languages.LANGUAGE_SCRIPTS.get(language)
    if explicit and expected and letters >= 20 and counts.get(expected, 0) / letters < languages.MIN_SCRIPT_SHARE.get(language, .05):
        reasons.append('native-language-script-conflict')
    # Corrupt DjVu alphabets mix operators into Greek-looking words. A filename
    # alone cannot justify changing a correctly encoded passage in another script.
    symbols = sum(unicodedata.category(c) == 'Sm' for c in text)
    if counts.get('Greek', 0) >= max(20, letters * .5) and symbols >= max(8, letters * .05):
        reasons.append('native-substitution-alphabet')
    boxes = []
    for block in blocks:
        b = block.get('b', [])
        if len(b) != 4 or not 0 <= b[0] < b[2] <= 1 or not 0 <= b[1] < b[3] <= 1:
            reasons.append('invalid-native-geometry')
            continue
        boxes.append(b)
    boxes.sort(key=lambda box: box[0])
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if b[0] >= a[2]:
                break
            intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            if intersection / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) >= .85:
                reasons.append('overlapping-native-regions')
                break
        if 'overlapping-native-regions' in reasons:
            break
    return {'profile': PROFILE, 'usable': not reasons, 'reasons': sorted(set(reasons)),
            'script_counts': counts, 'invalid_character_offsets': invalid,
            'characters': len(text), 'character_accuracy': 'unreviewed'}
