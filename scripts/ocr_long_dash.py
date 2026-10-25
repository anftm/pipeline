"""Recover a printed long dash only with its pixels and both adjacent readings."""
from __future__ import annotations

import hashlib
import re
import unicodedata

PROFILE = 'japanese-long-dash-pixels-v3-edge-lines'


def recognize(crop, text, read_line):
    import cv2
    import numpy as np
    separators=list(re.finditer('-+',text))
    if len(separators)!=1 or not any('\u3040'<=c<='\u30ff' for c in text):
        return text, None
    height, width = crop.shape[:2]
    vertical = height>width*2
    if not vertical:
        return text, None
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    marker=separators[0]
    prefix,suffix = text[:marker.start()],text[marker.end():]
    candidates = []
    # Faint ink fragments at low thresholds. Require one continuous light-ink
    # stroke and substantial darker support along that same narrow footprint.
    for threshold in (180, 200, 220):
        count, labels, stats, _ = cv2.connectedComponentsWithStats((gray<threshold).astype(np.uint8))
        lines = []
        for index in range(1,count):
            x,y,w,h,area = map(int,stats[index])
            if (h>=width*1.8 and w<=max(5,width*.22) and h>10*w
                    and (y>width*.5 if prefix else y<=width*.5)
                    and (height-y-h>width*.5 if suffix else height-y-h<=width*.5)
                    and area>h*.8):
                lines.append([x,y,x+w,y+h])
        if len(lines)>1:
            return text, None
        candidates.append(lines[0] if lines else None)
    present=[box for box in candidates if box]
    if not present or any(max(abs(a-b) for a,b in zip(present[0],box))>4 for box in present[1:]):
        return text, None
    # Dark ink may merge with a neighboring glyph at 220. A matching 180/200
    # stroke is sufficient; faint strokes use 220 plus footprint support.
    if len(present)<2 and candidates[-1] is None:
        return text,None
    x,y,u,v = present[0]
    support = gray[y:v,x:u]
    dark_rows = float((support<128).any(axis=1).mean())
    medium_rows = (support<180).any(axis=1)
    if float(medium_rows.mean())<.6:
        return text, None
    gaps=[]
    for row in np.flatnonzero(~medium_rows):
        if gaps and row==gaps[-1][-1]+1:gaps[-1].append(int(row))
        else:gaps.append([int(row)])
    if max(map(len,gaps),default=0)>max(3,width*.35):
        return text,None
    attempts=[]
    for side, part, expected in [('before',crop[:max(1,y-2)],prefix),('after',crop[min(height-1,v+2):],suffix)]:
        if not expected:continue
        for model in ('rec','rec_long'):
            value,confidence,terminated=read_line(part,model,vertical=True)
            attempts.append({'side':side,'model':model,'text':value,'confidence':confidence,'terminated':terminated,
                             'matches_original':value==expected})
    accepted=bool(attempts) and all(a['matches_original'] and a['confidence']>=.9 and a['terminated'] for a in attempts)
    context_attempts=[]
    original_attempts=[]
    if not accepted and prefix and suffix and min(len(prefix),len(suffix))<=2:
        # A one-kana remainder becomes ambiguous in isolation. Remove only the
        # proven rule footprint and read all neighboring glyphs together.
        joined=np.concatenate([crop[:max(1,y-2)],np.full((6,width,3),255,np.uint8),
                               crop[min(height-1,v+2):]],axis=0)
        for model in ('rec','rec_long'):
            value,confidence,terminated=read_line(joined,model,vertical=True)
            context_attempts.append({'model':model,'text':value,'confidence':confidence,'terminated':terminated,
                                     'matches_original':value==prefix+suffix})
        accepted=all(a['matches_original'] and a['confidence']>=.98 and a['terminated'] for a in context_attempts)
        if not accepted:
            for model in ('rec','rec_long'):
                value,confidence,terminated=read_line(crop,model,vertical=True)
                original_attempts.append({'model':model,'text':value,'confidence':confidence,'terminated':terminated,
                                          'matches_original':value==text})
            # The entire original line preserves the tiny side's context.
            # Require both models to corroborate it and the longer side in
            # isolation. Geometry alone never manufactures neighboring text.
            longer='before' if len(prefix)>=len(suffix) else 'after'
            accepted=(all(a['matches_original'] and a['confidence']>=.98 and a['terminated'] for a in original_attempts)
                      and all(a['matches_original'] and a['confidence']>=.98 and a['terminated']
                              for a in attempts if a['side']==longer))
    proof={'profile':PROFILE,'original':text,'crop_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),
           'line_box':[x,y,u,v],'thresholds':[180,200,220],'threshold_boxes':candidates,
           'dark_row_share':dark_rows,'medium_row_share':float(medium_rows.mean()),
           'original_separator':marker.group(),'attempts':attempts,'context_attempts':context_attempts,
           'original_attempts':original_attempts,'accepted':accepted}
    return prefix+'―'+suffix if accepted else text, proof


def split_sections(crop, text, read_line, *, inline_spaces=True):
    """Retain printed title spaces and colophon gaps with full text agreement."""
    import cv2
    import numpy as np
    height,width=crop.shape[:2]
    if height<=width*8:
        return None,None
    gray=cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY)
    blank=~(gray<220).any(axis=1)
    gaps=[]
    for y in np.flatnonzero(blank):
        if gaps and y==gaps[-1][-1]+1:gaps[-1].append(int(y))
        else:gaps.append([int(y)])
    minimum=1.1 if inline_spaces else 1.5
    gaps=[g for g in gaps if len(g)>width*minimum and g[0]>width and height-g[-1]>width]
    if not gaps:
        return None,None
    cuts=[0,*[(g[0]+g[-1])//2 for g in gaps],height]
    parts=[]
    for index,(a,b) in enumerate(zip(cuts,cuts[1:])):
        nonempty=np.flatnonzero(~blank[a:b])
        if not len(nonempty):return None,None
        start=max(a,a+int(nonempty[0])-3);end=min(b,a+int(nonempty[-1])+4)
        attempts=[]
        for model in ('rec','rec_long'):
            value,confidence,terminated=read_line(crop[start:end],model,vertical=True)
            attempts.append({'model':model,'text':value,'confidence':confidence,'terminated':terminated})
        readings=[a['text'] for a in attempts]
        if (readings[0]!=readings[1] and re.sub('-+','―',readings[0])==re.sub('-+','―',readings[1])):
            for i,value in enumerate(readings):
                recovered,dash=recognize(crop[start:end],value,read_line)
                if dash:
                    attempts[i]['long_dash']=dash
                    attempts[i]['verified_text']=recovered
                    readings[i]=recovered
        parts.append({'crop_box':[0,start,width,end],'attempts':attempts,
                      'text':readings[0],
                      'separator_before':('　' if len(gaps[index-1])<=width*1.5 else '\n\n') if index else '',
                      'accepted':readings[0]==readings[1] and bool(readings[0])
                          and all(a['terminated'] and a['confidence']>=.9 for a in attempts)})
    joined=''.join(part['text'] for part in parts)
    original_dash=None
    verified=text
    if joined!=text and re.sub('-+','―',joined)==re.sub('-+','―',text):
        verified,original_dash=recognize(crop,text,read_line)
    accepted=all(part['accepted'] for part in parts) and joined==verified
    # A comma and the next short glyph can leave a blank band as wide as a
    # space. Its white pixels alone cannot establish an extra printed space.
    punctuation_gap=any(part['separator_before']=='　' and
        (not parts[index-1]['text'] or unicodedata.category(parts[index-1]['text'][-1])[0] in {'P','S'}
         or len(parts[index-1]['text'])<2 or len(part['text'])<2)
        for index,part in enumerate(parts) if index)
    if punctuation_gap:accepted=False
    proof={'profile':'vertical-blank-sections-v2-title-spaces','crop_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),
           'original':text,'original_long_dash':original_dash,
           'gaps':[[g[0],g[-1]+1] for g in gaps],'parts':parts,'accepted':accepted}
    if not accepted and inline_spaces and any(len(g)>width*1.5 for g in gaps):
        coarse,coarse_proof=split_sections(crop,text,read_line,inline_spaces=False)
        if coarse:
            coarse_proof['rejected_finer_split']=proof
            return coarse,coarse_proof
    return parts if accepted else None,proof
