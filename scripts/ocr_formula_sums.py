"""Position scripts in complete binary charged sums, never inferred reactions."""
from __future__ import annotations

import copy
import hashlib
import re
import statistics

try:
    from . import ocr_formula_scripts as scripts
except ImportError:
    import ocr_formula_scripts as scripts

PROFILE='binary-charged-source-sums-v1-all-parts-required'
SUM=re.compile(r'([0-9]*[A-Za-z]+[+\-][0-9]{1,2})\+([0-9]*[A-Za-z]+[+\-][0-9]{1,2})')


def baseline_crosses(gray):
    """Require one body-sized original cross at the printed token baseline.

    Broken crosses and full reactions are deliberately out of scope. A tall H
    stem is not an operator: significant side ink above/below the cross rules
    it out, and all three source thresholds must independently agree.
    """
    import cv2
    import numpy as np
    checks=[]
    for threshold in (128,180,220):
        _,_,stats,_=cv2.connectedComponentsWithStats((gray<threshold).astype(np.uint8))
        parts=[list(map(int,s)) for s in stats[1:] if s[4]>=3]
        maximum=max((p[3] for p in parts),default=0)
        bodies=[p for p in parts if p[3]>=maximum*.85]
        if maximum<8 or not bodies:return []
        baseline=statistics.median(p[1]+p[3] for p in bodies)
        height=statistics.median(p[3] for p in bodies);crosses=[]
        for x,y,w,h,area in parts:
            if not (.6*height<=h<=1.1*height and .65<w/h<1.7
                    and abs(y+h-baseline)<=height*.25):continue
            crop=gray[y:y+h,x:x+w]
            proof=scripts.cross_sign(crop)
            if proof is None:continue
            ink=crop<threshold
            row=proof['cross_row'];column=proof['cross_column']
            if row<2 or row>=h-2 or not .25*w<=column<=.75*w:continue
            # Reject letters with vertical edge stems, especially H/O/0.
            outside=ink.copy();outside[max(0,row-1):min(h,row+2)]=False
            edge=max(1,round(w*.2))
            if max(float(outside[:,:edge].any(axis=1).mean()),float(outside[:,-edge:].any(axis=1).mean()))>.25:continue
            if not (ink[max(0,row-1):row+2].any(axis=0).mean()>=.8 and ink[:,max(0,column-1):column+2].any(axis=1).mean()>=.8):continue
            crosses.append({'box':[x,y,x+w,y+h],'cross':proof,'body_baseline':baseline,'body_height':height})
        checks.append({'threshold':threshold,'crosses':crosses})
    stable=[]
    for cross in checks[-1]['crosses']:
        if not all(any(max(abs(a-b) for a,b in zip(cross['box'],p['box']))<=2 for p in c['crosses']) for c in checks[:-1]):continue
        x,y,u,v=cross['box']
        stable.append({'box':cross['box'],'threshold_evidence':copy.deepcopy(checks),
            'source_cross_sha256':hashlib.sha256(gray[y:v,x:u].tobytes()).hexdigest()})
    return stable


def refine(image,blocks,read,*,read_secondary=None):
    import numpy as np
    pixels=np.asarray(image.convert('RGB'));gray_pixels=np.asarray(image.convert('L'));width,height=image.size
    output=copy.deepcopy(blocks);reports=[];issues=[]
    for index,block in enumerate(output):
        match=SUM.fullmatch(block['t'])
        if match is None:continue
        a,b,c,d=[round(v*(width if axis%2==0 else height)) for axis,v in enumerate(block['b'])]
        if min(c-a,d-b)<8:continue
        pad=min(5,max(2,round((d-b)*.15)))
        x,y,u,v=max(0,a-pad),max(0,b-pad),min(width,c+pad),min(height,d+pad)
        neighbors=[i for i,b in enumerate(blocks) if i!=index and
            max(x,b['b'][0]*width)<min(u,b['b'][2]*width) and
            max(y,b['b'][1]*height)<min(v,b['b'][3]*height)]
        # A neighboring region centered inside the primary formula makes token
        # ownership ambiguous. A peripheral bounding-box overlap (e.g. an old
        # oversized vertical-arrow detection) is retained, never removed or
        # counted as successfully resolved merely because both terms agree.
        if any(a<=(blocks[i]['b'][0]+blocks[i]['b'][2])/2*width<=c and
               b<=(blocks[i]['b'][1]+blocks[i]['b'][3])/2*height<=d for i in neighbors):continue
        crop=pixels[y:v,x:u].copy();gray=gray_pixels[y:v,x:u]
        crosses=baseline_crosses(gray)
        proof={'region':index,'primary':copy.deepcopy(blocks[index]),'crop_box':[x,y,u,v],
            'source_pixels_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'operator_candidates':crosses,
            'parts':[],'accepted':False}
        if neighbors:
            proof['adjacent_original_regions']=[{'index':i,'block':copy.deepcopy(blocks[i])} for i in neighbors]
            issues.append({'code':'binary-formula-adjacent-region-unresolved','severity':'review',
                'evidence':{'region':index,'neighbors':neighbors,'box':block['b']}})
        if len(crosses)==1:
            left,top,right,bottom=crosses[0]['box']
            for number,bounds in enumerate([[0,0,max(1,left-1),v-y],[right+1,0,u-x,v-y]]):
                lo,up,hi,down=bounds
                if hi<=lo:break
                term=crop[up:down,lo:hi].copy()
                value,evidence=scripts.recognize(term,read,read_secondary=read_secondary)
                part={'box':bounds,'text':value,'recognition':evidence,
                    'source_pixels_sha256':hashlib.sha256(term.tobytes()).hexdigest(),
                    'source_box':[(x+lo)/width,(y+up)/height,(x+hi)/width,(y+down)/height]}
                if value is not None and evidence and evidence.get('geometry'):
                    sx,sy,su,sv=evidence['geometry']['script_box']
                    part['script_source_box']=[(x+lo+sx)/width,(y+up+sy)/height,
                                               (x+lo+su)/width,(y+up+sv)/height]
                proof['parts'].append(part)
            reverse=str.maketrans('⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹','+-0123456789')
            accepted=len(proof['parts'])==2 and all(p['text'] is not None and
                p['text'].translate(reverse)==match[n+1] for n,p in enumerate(proof['parts']))
            if accepted:
                # Only script positions change in the primary string. The
                # source operator and every base glyph stay literally intact.
                value='+'.join(p['text'] for p in proof['parts'])
                offset=0
                for part in proof['parts']:
                    part['text_start']=offset;part['text_end']=offset+len(part['text'])
                    offset=part['text_end']+1
                proof.update(accepted=True,text=value,operator_source_box=[(x+left)/width,(y+top)/height,(x+right)/width,(y+bottom)/height])
                block.update(t=value,primary_text=blocks[index]['t'],formula_sum_evidence=copy.deepcopy(proof))
        reports.append(proof)
        if not proof['accepted']:
            issues.append({'code':'binary-formula-sum-unresolved','severity':'review',
                'evidence':{'region':index,'box':block['b'],'primary':blocks[index]['t'],
                            'source_pixels_sha256':proof['source_pixels_sha256']}})
    return output,({'profile':PROFILE,'sums':reports,'issues':issues} if reports else None)
