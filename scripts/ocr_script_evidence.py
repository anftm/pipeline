"""Corroborate weak OSD script votes on actual, separately recognized text crops."""
from __future__ import annotations

import csv
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
from difflib import SequenceMatcher

from PIL import Image

try:
    from . import audit_ocr_language as languages
except ImportError:
    import audit_ocr_language as languages


PROFILE = 'script-crops-v5-explicit-latin-conflicts'


def recover_chinese_route(path, evidence, blocks, models, recognize):
    """Resolve false Japanese/Cyrillic OSD votes with independent Chinese crops.

    Keep the attempted recognition, its loaded models and the original OSD.
    Matching a filename or a model confidence is insufficient to switch back.
    """
    planned = evidence.get('planned_language')
    if planned not in {'ch', 'chinese_cht'}:
        return evidence, blocks, models
    observed = evidence['language']
    conflicts = [row for row in evidence.get('issues', [])
                 if row['code'] == 'explicit-language-conflicts-with-image']
    if observed == planned and not conflicts:
        return evidence, blocks, models
    counts = languages.script_counts(''.join(b['t'] for b in blocks))
    # Kana in the Japanese result is positive script evidence. Shared Han
    # characters alone do not prove Japanese, particularly on formula pages.
    if observed == 'japan' and counts.get('Japanese', 0) >= max(4, sum(counts.values()) * .015):
        return evidence, blocks, models
    candidate, candidate_models = (blocks, models) if observed == planned else recognize(planned)
    counts = languages.script_counts(''.join(b['t'] for b in candidate))
    if counts.get('Han', 0) < 20 or counts.get('Japanese', 0) >= 4:
        return evidence, blocks, models
    checked = corroborate(path, {**evidence, 'language': planned, 'scripts': []}, candidate)
    proof = checked.get('independent_script', {})
    if not proof.get('corroborated'):
        return {**evidence, 'chinese_candidate': {'blocks': candidate, 'models': candidate_models,
                                                'independent_script': proof}}, blocks, models
    checked['issues'] = [row for row in checked.get('issues', [])
                         if row['code'] != 'explicit-language-conflicts-with-image']
    checked['previous_route'] = {'language': observed, 'scripts': evidence.get('scripts', []),
                                 'source': evidence['source'], 'issues': evidence.get('issues', []),
                                 'blocks': blocks, 'models': models}
    return checked, candidate, candidate_models


def corroborate(path, evidence, blocks):
    explicit_conflict = (evidence['language'] in {'en','japan'} and evidence.get('planned_language') == evidence['language']
                         and any(row['code']=='explicit-language-conflicts-with-image' for row in evidence.get('issues',[])))
    if evidence.get('scripts') and not explicit_conflict or evidence['language'] not in {'ch', 'chinese_cht', 'en', 'japan'}:
        return evidence
    expected = languages.LANGUAGE_SCRIPTS[evidence['language']]
    model = 'chi_sim' if expected == 'Han' else 'jpn' if expected == 'Japanese' else 'eng'
    # No download or guessed model identity: the worker's installed tessdata is
    # recorded along with each independent recognition used as evidence.
    roots = [Path(os.environ.get('OCR_SCRIPT_MODEL_DIR', '/usr/share/tesseract-ocr/5/tessdata')),
             Path(os.environ.get('TESSDATA_PREFIX', '/usr/share/tesseract-ocr/5/tessdata')),
             Path('/usr/share/tesseract-ocr/4.00/tessdata')]
    trained = next((root / (model + '.traineddata') for root in roots
                    if (root / (model + '.traineddata')).is_file()), None)
    if trained is None:
        return {**evidence, 'independent_script': {'profile': PROFILE, 'model': model,
                'corroborated': False, 'regions': [], 'error': 'traineddata-unavailable'}}
    def letters(text):
        scripts={expected,'Han'} if expected=='Japanese' else {expected}
        return ''.join(c for c in text if any(languages.script_counts(c).get(s,0) for s in scripts))
    selected = []
    for lower, upper in [(0., .34), (.34, .67), (.67, 1.01)]:
        candidates = [b for b in blocks if lower <= (b['b'][1] + b['b'][3]) / 2 < upper
                      and len(letters(b['t'])) >= 6]
        # Sparse scientific tables may put all longer labels in the header.
        # Check another independent label before applying the same 18-character
        # corroboration threshold; two short headings alone are insufficient.
        selected.extend(sorted(candidates, key=lambda b: len(letters(b['t'])), reverse=True)[:3])
    rows = []
    with Image.open(path) as image, tempfile.TemporaryDirectory(dir=Path(path).parent) as temp:
        for number, block in enumerate(selected):
            box = [round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(block['b'])]
            pad = max(4, round((box[3] - box[1]) * .15))
            bounds = (max(0, box[0] - pad), max(0, box[1] - pad),
                      min(image.width, box[2] + pad), min(image.height, box[3] + pad))
            crop = Path(temp) / f'{number}.png'
            with image.crop(bounds) as region:
                if region.height > 96:
                    region.thumbnail((max(1, round(region.width * 96 / region.height)), 96), Image.Resampling.LANCZOS)
                region.save(crop, dpi=(300, 300))
                crop_size = list(region.size)
            row = {'box': block['b'], 'primary': block['t'], 'crop_pixels': list(bounds),
                   'recognition_pixels': crop_size,
                   'crop_sha256': hashlib.sha256(crop.read_bytes()).hexdigest()}
            try:
                run = subprocess.run(['tesseract', str(crop), 'stdout', '--tessdata-dir', str(trained.parent),
                                      '-l', model, '--psm', '7', '-c', 'tessedit_create_tsv=1'],
                                     env={**os.environ, 'OMP_THREAD_LIMIT': '1'}, capture_output=True,
                                     text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as exc:
                rows.append({**row, 'error': f'{type(exc).__name__}: {exc}'[:500],
                             'agreement': 0, 'matching_script_characters': 0})
                continue
            words = list(csv.DictReader(io.StringIO(run.stdout), delimiter='\t')) if run.returncode == 0 else []
            text = ''.join(w.get('text', '') for w in words if w.get('level') == '5')
            a, b = letters(block['t']), letters(text)
            matching = sum(x.size for x in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
            rows.append({**row, 'independent': text, 'stderr': run.stderr[-500:],
                         'matching_script_characters': matching,
                         'agreement': 2 * matching / max(1, len(a) + len(b)), 'returncode': run.returncode})
    valid = [row for row in rows if row['agreement'] >= .8 and row['matching_script_characters'] >= 6]
    corroborated = (len(valid) >= 2 and sum(r['matching_script_characters'] for r in valid) >= 18)
    if expected=='Japanese':
        corroborated = corroborated and any(sum(languages.script_counts(c).get('Japanese',0)
            for c in row['independent'] if c in row['primary']) >= 2 for row in valid)
    previous = {}
    if corroborated and explicit_conflict:
        previous={'previous_script_evidence': {k:evidence.get(k) for k in ['language','source','scripts','issues']},
                  'issues':[row for row in evidence.get('issues',[]) if row['code']!='explicit-language-conflicts-with-image']}
    return {**evidence, **previous, 'scripts': [expected] if corroborated else evidence.get('scripts', []),
            'source': 'page-image-corroborated' if corroborated else evidence['source'],
            'independent_script': {'profile': PROFILE, 'model': model,
                                   'sha256': hashlib.sha256(trained.read_bytes()).hexdigest(),
                                   'bytes': trained.stat().st_size,
                                   'regions': rows, 'corroborated': corroborated}}
