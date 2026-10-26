"""Recompute publication evidence for complete effective book text without OCR."""
from __future__ import annotations

import copy
import hashlib
import re

try:
    from . import native_text_quality, ocr_quality, pdf_reading_v3 as v3, pdf_text_layer as text
    from . import pdf_ocr_stages as stages
    from .reader_text_storage import VerifiedReader, TextPageSpool
except ImportError:
    import native_text_quality
    import ocr_quality
    import pdf_reading_v3 as v3
    import pdf_text_layer as text
    import pdf_ocr_stages as stages
    from reader_text_storage import VerifiedReader, TextPageSpool

POLICY = 'complete-body-quality-v1'


class QualityRejection(ValueError):
    """This exact text version needs recognition or review before publication."""


def check(spec, layers, read):
    profile = spec.get('recognition_profile')
    if not isinstance(profile, str) or '-quality-v1-layout-' not in profile:
        raise QualityRejection('body publication requires a versioned recognition profile')
    if [layer['page'] for layer in layers] != list(range(1, spec['page_count'] + 1)):
        raise QualityRejection('body publication requires every physical page')
    reports, pages = [], []
    for layer in layers:
        ref = layer['provenance'].get('raw_resource')
        if not ref:
            raise QualityRejection(f"page {layer['page']} lacks original recognition evidence")
        payload = v3.decode(v3.verified_read(ref, read))
        source = payload.get('source')
        language = layer['provenance'].get('language_hint', 'und')
        book = {'source_sha256': spec['source_sha256'], 'layout_options': spec.get('layout_options', {})}
        page = {'p': layer['page'], 'source': source, 'o': ref['path'], 'os': ref['sha256'], 'ob': ref['bytes']}
        baseline, _ = stages.assembled_page(book, page, payload, language)
        if (baseline['raw_sha256'] != layer['raw_sha256'] or baseline['page_identity'] != layer['page_identity']
                or layer['revision'] == 'raw' and baseline['generation'] != layer['generation']):
            raise QualityRejection(f"page {layer['page']} text differs from its recognition evidence")
        recognition = payload.get('recognition', {})
        for audit in layer.get('acceptances', []):
            visual = audit.get('visual_review')
            if visual and (visual.get('kind') != 'source-image-review'
                    or visual.get('source_sha256') != spec['source_sha256']
                    or visual.get('page') != layer['page']
                    or visual.get('input_png_sha256') != recognition.get('input_sha256')):
                raise QualityRejection(f"page {layer['page']} positioned review belongs to a different source image")
        if source == 'ocr':
            if not re.fullmatch(r'[0-9a-f]{64}', str(recognition.get('input_sha256', ''))):
                raise QualityRejection(f"page {layer['page']} lacks the OCR input identity")
            if not (recognition.get('profile') == profile or stages.reusable_profile(recognition.get('profile'), profile)):
                raise QualityRejection(f"page {layer['page']} belongs to a different recognition version")
        elif source == 'native':
            native = native_text_quality.assess(payload)
            if payload.get('native_check', {}).get('profile') != native_text_quality.PROFILE or not native['usable']:
                raise QualityRejection(f"page {layer['page']} native extraction lacks current quality evidence")
        else:
            raise QualityRejection(f"page {layer['page']} has an unsupported text source")
        report = ocr_quality.check_page(payload, source_sha256=spec['source_sha256'], profile=recognition.get('profile'))
        if report['status'] != 'passed':
            raise QualityRejection(f"page {layer['page']} body quality: " + ', '.join(i['code'] for i in report['issues']))
        reports.append(report)
        pages.append({'page': layer['page'], 'raw_resource': dict(ref), 'raw_sha256': layer['raw_sha256'],
                      'text_generation': layer['generation'], 'recognition_profile': recognition.get('profile'),
                      'source': source, 'models': copy.deepcopy(recognition.get('models', {}).get('models', {})),
                      'quality_check': report})
    quality = ocr_quality.book_report(reports, spec['page_count'])
    return {'version': 1, 'policy': POLICY, 'recognition_profile': profile,
            'layout_options': copy.deepcopy(spec.get('layout_options', {})),
            'source_sha256': spec['source_sha256'], 'text_generation': text.digest([layer['generation'] for layer in layers]),
            'quality_check': quality, 'pages': pages}


def verify_export(value, read):
    with TextPageSpool() as layers:
        return _verify_export(value, VerifiedReader(read), layers)


def _verify_export(value, read, layers):
    admission = value.get('body_admission')
    if not isinstance(admission, dict) or admission.get('policy') != POLICY:
        raise QualityRejection('new body publication lacks complete quality admission')
    if value.get('body_admission_sha256') != text.digest(admission):
        raise QualityRejection('body quality admission digest differs')
    refs = [ref for ref in value.get('retained_reader_resources', [])
            if ref['path'].endswith('/text-layer-manifest.json')]
    for ref in refs:
        manifest = v3.decode(v3.verified_read(ref, read))
        if manifest.get('generation') != value['text_generation']:
            continue
        v3.verify_text_bundle(ref, read, value['source_sha256'], value['page_count'], verified_layers=layers)
        actual = check({**value, 'recognition_profile': admission['recognition_profile'],
                        'layout_options': admission['layout_options']}, layers, read)
        if actual != admission:
            raise QualityRejection('body publication quality evidence no longer matches the complete text')
        if len(value.get('pages', [])) != len(layers) or any(page.get('page') != layer['page']
               or page.get('quality') != layer['quality'] or page['text_generation'] != layer['generation']
               or page['text_sha256'] != hashlib.sha256(layer['text'].encode()).hexdigest()
               for page, layer in zip(value['pages'], layers)):
            raise QualityRejection('exported body differs from admitted text pages')
        return actual
    raise QualityRejection('admitted body text manifest is missing')
