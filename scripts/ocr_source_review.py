"""Apply exact-source, pixel-pinned nontext reviews without deleting source evidence."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from pathlib import Path

PROFILE='source-image-reviews-v3-scientific-symbol-coverage'
STATE=Path(__file__).resolve().parents[1]/'state/ocr_image_reviews.json'


def for_source(source):
    if not STATE.is_file():return []
    state=json.loads(STATE.read_text())
    if state.get('version')!=1 or state.get('kind')!='ocr-image-reviews':raise ValueError('invalid image reviews')
    return state['sources'].get(source,[])


def apply(image, blocks, models, reviews, *, page, input_sha256):
    width,height=image.size
    selected=[r for r in reviews if r['page']==page and r['input_sha256']==input_sha256]
    if not selected:return blocks,models
    def matched_review(block):
        b=block.get('b')
        if not isinstance(b,list) or len(b)!=4:return None
        return next((r for r in selected if r.get('classification')=='scan-artifact' and r.get('basis')
            and r['box'][0]<=b[0] and r['box'][1]<=b[1] and r['box'][2]>=b[2] and r['box'][3]>=b[3]),None)
    def proof_for(block,review):
        x,y,u,v=[round(a*(width if k%2==0 else height)) for k,a in enumerate(review['box'])]
        stream=io.BytesIO();image.crop((x,y,u,v)).save(stream,format='PNG');raw=stream.getvalue()
        return {'original_block':copy.deepcopy(block),'review':copy.deepcopy(review),
                'crop_sha256':hashlib.sha256(raw).hexdigest(),'crop_png_base64':base64.b64encode(raw).decode()}
    removed=[];retained=[]
    for index,block in enumerate(blocks):
        review=matched_review(block)
        if review is None:
            retained.append(copy.deepcopy(block));continue
        removed.append({'original_index':index,**proof_for(block,review)})
    actual=copy.deepcopy(models)
    diagnostics=actual.setdefault('diagnostics',{})
    original_empty=list(diagnostics.get('empty_regions',[]))
    pending=set(original_empty);empty_checks=[]
    # A model can change a reviewed ink mark from a spurious letter to an
    # empty reading. Require the detector's positioned original region: an
    # unpositioned empty result is never excused by a nearby reviewed mark.
    for detection in diagnostics.get('detection_regions',[]):
        if detection.get('region') not in pending:continue
        review=matched_review(detection)
        if review is None:continue
        empty_checks.append({'region':detection['region'],**proof_for(detection,review)})
        pending.remove(detection['region'])
    if removed or empty_checks:
        diagnostics['empty_regions']=[i for i in original_empty if i in pending]
        diagnostics['source_nontext_reviews']={'profile':PROFILE,'page':page,'input_sha256':input_sha256,
            'original_blocks':copy.deepcopy(blocks),'original_empty_regions':original_empty,
            'excluded':removed,'excluded_empty_detections':empty_checks}
    return retained,actual


def scientific_coverage(reviews,*,page,input_sha256,source_sha256,language):
    """Exact source-image review of scientific symbols is not an English route.

    This records only script coverage, not spelling/character acceptance. The
    consistency checker independently verifies that the loaded CJK recognizer
    covers Latin and the retained page actually has formula-like tokens.
    """
    matches=[r for r in reviews if r.get('page')==page and r.get('input_sha256')==input_sha256
             and r.get('classification')=='scientific-symbols' and r.get('basis')]
    if len(matches)!=1 or language not in {'ch','chinese_cht'}:return None
    row=matches[0]
    if row.get('script_coverage')!=['Han','Latin']:return None
    return {'profile':PROFILE,'classification':'scientific-symbols','page':page,
        'source_sha256':source_sha256,'input_sha256':input_sha256,'language':language,
        'script_coverage':['Han','Latin'],'review':copy.deepcopy(row),
        'character_accuracy':'unreviewed'}
