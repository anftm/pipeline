"""Recover a molecular charge only from complete readings and raised source ink."""
from __future__ import annotations

import hashlib
import copy
import re
import statistics

PROFILE='formula-scripts-pixels-v5-geometry-pinned-charge-encodings'
CONFIRMATION_REC_SHA256='6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884'
SUPERSCRIPT=str.maketrans('+-0123456789','⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹')
SUBSCRIPT=str.maketrans('0123456789','₀₁₂₃₄₅₆₇₈₉')
PLAIN_CHARGE=str.maketrans('⁺⁻−⁰¹²³⁴⁵⁶⁷⁸⁹','+--0123456789')


def charge_parts(value):
    """Compare only charge-position encodings, never base glyphs or charge order.

    A recognizer can mix OH-1 and OH⁻1 for the very same raised source ink.
    Equivalence is restricted to that suffix *after* raised geometry is proven.
    No whitespace, bracket, letter/digit spelling or sign order is repaired.
    """
    match=re.fullmatch(r'([A-Za-z0-9()]+)([+\-⁺⁻−][0-9⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2})',value)
    return (match[1],match[2].translate(PLAIN_CHARGE)) if match else None


def charge_suffix(value):
    return value.translate(PLAIN_CHARGE) if re.fullmatch(r'[+\-⁺⁻−][0-9⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2}',value) else None


def raised_ink(gray):
    import cv2
    import numpy as np
    masks=[]
    for threshold in (128,180,220):
        count,labels,stats,_=cv2.connectedComponentsWithStats((gray<threshold).astype(np.uint8))
        components=[list(map(int,s)) for s in stats[1:] if s[4]>=3]
        tall=max((c[3] for c in components),default=0)
        if tall<8:return None
        masks.append((threshold,components,tall))
    # A broken body stem at 128 must not masquerade as a raised charge. The
    # two lighter thresholds independently establish the body's full extent.
    tall_bodies=[c for th,components,tall in masks[1:] for c in components if c[3]>=tall*.7]
    if not tall_bodies:return None
    baseline=float(statistics.median(c[1]+c[3] for c in tall_bodies))
    tall=float(statistics.median(c[3] for c in tall_bodies))
    # Lowercase u and other x-height glyphs can be shorter than capital Cu/OH.
    # Their bottoms align with the body baseline, unlike the raised charge.
    bodies=[c for th,components,h in masks[1:] for c in components
            if c[3]>=tall*.35 and c[1]+c[3]>=baseline-tall*.15]
    right=max(c[0]+c[2] for c in bodies)
    groups=[]
    for threshold,components,_ in masks:
        selected=[c for c in components if c[0]>=right+1 and c[3]<=tall*.65
                  and c[1]+c[3]<=baseline-tall*.18]
        if len(selected)<2 and threshold>=180:return None
        groups.append({'threshold':threshold,'components':selected})
    selected=groups[-1]['components']
    left=min(c[0] for c in selected);top=min(c[1] for c in selected)
    end=max(c[0]+c[2] for c in selected);bottom=max(c[1]+c[3] for c in selected)
    # Include faint detached arms inside the already proven raised band.
    roi=gray[max(0,top-1):bottom+1,right+1:end]<220
    yy,xx=np.nonzero(roi)
    if not len(xx):return None
    left=right+1+int(xx.min())
    reference=[left,top,end,bottom]
    for row in groups[1:]:
        c=row['components'];box=[min(p[0] for p in c),min(p[1] for p in c),
                                max(p[0]+p[2] for p in c),max(p[1]+p[3] for p in c)]
        if max(abs(a-b) for a,b in zip(reference,box))>3:return None
    return {'body_components':bodies,'body_baseline':baseline,'body_height':tall,
            'body_right':right,'script_box':reference,'threshold_evidence':groups,
            'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest()}


def cross_sign(gray):
    """Require a raised cross footprint, not just a dash, dot or serif."""
    import numpy as np
    ink=gray<220
    yy,xx=np.nonzero(ink)
    if not len(xx):return None
    x,y,u,v=int(xx.min()),int(yy.min()),int(xx.max())+1,int(yy.max())+1
    crop=ink[y:v,x:u];height,width=crop.shape
    if min(width,height)<3 or max(width,height)>2*min(width,height):return None
    row=int(crop.sum(axis=1).argmax());col=int(crop.sum(axis=0).argmax())
    if not (.2*width<=col<=.8*width and .2*height<=row<=.8*height
            and crop[row].sum()>=width*.6 and crop[:,col].sum()>=height*.6):return None
    return {'box':[x,y,u,v],'cross_row':row,'cross_column':col,
            'row_support':float(crop[row].mean()),'column_support':float(crop[:,col].mean())}


def minus_sign(gray):
    """Prove a stable thin horizontal mark; never reinterpret a faint plus."""
    import numpy as np
    checks=[]
    for threshold in (128,180,220):
        ink=gray<threshold
        yy,xx=np.nonzero(ink)
        if not len(xx):return None
        x,y,u,v=int(xx.min()),int(yy.min()),int(xx.max())+1,int(yy.max())+1
        width,height=u-x,v-y
        if width<4 or height>3 or width<height*3:return None
        if float(ink[y:v,x:u].any(axis=0).mean())<.9:return None
        checks.append({'threshold':threshold,'box':[x,y,u,v]})
    if any(max(abs(a-b) for a,b in zip(checks[1]['box'],row['box']))>1 for row in checks):return None
    return {'thresholds':checks}


def recognize(crop,read,*,read_secondary=None):
    """Return a charged term and proof; callers retain rejected attempts too.

    ``read`` recognizes an RGB image as ``(text, confidence)``. No dictionary,
    expected chemical symbol, source-page reference or FormulaNet output is
    used to choose a charge or change its printed sign/number order.
    """
    import cv2
    import numpy as np
    from PIL import Image,ImageOps
    gray=cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY) if crop.ndim==3 else crop
    geometry=raised_ink(gray)
    if geometry is None:return None,None
    image=Image.fromarray(crop).convert('RGB');attempts=[]
    for padding in (4,8):
        padded=ImageOps.expand(image,padding,fill='white')
        for scale in (2,3):
            selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
            value,confidence=read(np.asarray(selected))
            attempts.append({'kind':'complete-term','padding':padding,'scale':scale,
                'text':value,'confidence':float(confidence),'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
    proof={'profile':PROFILE,'geometry':geometry,'attempts':attempts,'script_attempts':[],
           'body_attempts':[],'accepted':False}
    values={charge_parts(a['text']) for a in attempts}
    if len(values)!=1 or None in values:return None,proof
    base,suffix=next(iter(values));original=attempts[0]['text']
    match=re.fullmatch(r'([A-Za-z0-9()]+)([+\-][0-9]{1,2})',base+suffix)
    proof['original']=original
    if len({a['text'] for a in attempts})>1:
        proof['equivalent_charge_encodings']={'base_unchanged':base,'charge_order_unchanged':suffix,
            'basis':'same raised source geometry; only ASCII/superscript charge suffix encoding differs'}
    complete_confident=all(a['confidence']>=.9 for a in attempts)
    if not complete_confident:
        # The mean line score can be depressed by tiny raised ink. Do not
        # raise it or discard it: verify the unchanged base glyphs separately.
        end=geometry['body_right']
        body=image.crop((0,0,min(image.width,end+1),image.height))
        for padding in (4,8):
            padded=ImageOps.expand(body,padding,fill='white')
            for scale in (2,3):
                selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
                value,confidence=read(np.asarray(selected))
                proof['body_attempts'].append({'padding':padding,'scale':scale,'text':value,
                    'confidence':float(confidence),'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
        verified_base=all(a['text']==match[1] and a['confidence']>=.98 for a in proof['body_attempts'])
        if not verified_base:return None,proof
    x,y,u,v=geometry['script_box']
    script=image.crop((max(0,x-1),max(0,y-1),min(image.width,u+1),min(image.height,v+1)))
    for padding in (3,6):
        padded=ImageOps.expand(script,padding,fill='white')
        for scale in (3,4):
            selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
            value,confidence=read(np.asarray(selected))
            proof['script_attempts'].append({'padding':padding,'scale':scale,'text':value,
                'confidence':float(confidence),'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
    eligible=[a for a in proof['script_attempts'] if charge_suffix(a['text'])==match[2] and a['confidence']>=.9]
    # Both scales of a bounded complete crop must agree. A rejected, tighter
    # crop stays in evidence and cannot be disguised as a successful reading.
    accepted=any(all(any(a['padding']==pad and a['scale']==scale for a in eligible)
                         for scale in (3,4)) for pad in (3,6))
    if not accepted:
        # Faint plus arms disappear in isolated OCR. Require the complete-term
        # readings above, a visibly crossed sign and two high-confidence digit
        # readings; never infer the sign from valence or chemical convention.
        comps=geometry['threshold_evidence'][-1]['components']
        digits=[c for c in comps if c[3]>=geometry['body_height']*.3]
        rightmost=max(digits,key=lambda c:c[0]+c[2],default=None)
        digit_left=rightmost[0] if rightmost else u
        sign_crop=gray[max(0,y-1):v+1,x:digit_left]
        sign=(cross_sign(sign_crop) if match[2].startswith('+') else minus_sign(sign_crop))
        proof['isolated_sign_geometry']=sign
        digit_attempts=[]
        if sign and rightmost and len(match[2][1:])==1:
            a,b,w,h,area=rightmost
            digit=image.crop((max(0,a-1),max(0,b-1),min(image.width,a+w+1),min(image.height,b+h+1)))
            for padding in (3,6):
                padded=ImageOps.expand(digit,padding,fill='white')
                for scale in (3,4):
                    selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
                    value,confidence=read(np.asarray(selected))
                    digit_attempts.append({'padding':padding,'scale':scale,'text':value,'confidence':float(confidence),
                        'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
        proof['isolated_digit_attempts']=digit_attempts
        accepted=bool(sign) and any(all(any(a['padding']==pad and a['scale']==scale
            and a['text']==match[2][1:] and a['confidence']>=.95 for a in digit_attempts)
            for scale in (3,4)) for pad in (3,6))
    if not accepted and read_secondary is not None:
        # A separately trained recognizer can verify the *complete* unchanged
        # term and raised suffix; it cannot repair spelling, charge order or a
        # different base. The primary failures above remain verbatim.
        secondary_whole=[];secondary_script=[]
        for padding in (4,8):
            padded=ImageOps.expand(image,padding,fill='white')
            for scale in (2,3):
                selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
                value,confidence=read_secondary(np.asarray(selected))
                secondary_whole.append({'padding':padding,'scale':scale,'text':value,'confidence':float(confidence),
                    'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
        if all(charge_parts(a['text'])==(base,suffix) and a['confidence']>=.9 for a in secondary_whole):
            for padding in (3,6):
                padded=ImageOps.expand(script,padding,fill='white')
                for scale in (3,4):
                    selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
                    value,confidence=read_secondary(np.asarray(selected))
                    secondary_script.append({'padding':padding,'scale':scale,'text':value,'confidence':float(confidence),
                        'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
        proof['secondary_complete_attempts']=secondary_whole
        proof['secondary_script_attempts']=secondary_script
        accepted=bool(proof.get('isolated_sign_geometry')) and any(all(any(
            a['padding']==pad and a['scale']==scale and charge_suffix(a['text'])==match[2] and a['confidence']>=.95
            for a in secondary_script) for scale in (3,4)) for pad in (3,6))
        proof['secondary_confirmation_accepted']=accepted
    proof['accepted']=accepted
    if not accepted:return None,proof
    value=match[1]+match[2].translate(SUPERSCRIPT);proof['text']=value
    return value,proof


def lowered_ink(gray):
    """Locate one small internal or terminal digit group below a Latin baseline."""
    import cv2
    import numpy as np
    masks=[]
    for threshold in (128,180,220):
        count,labels,stats,_=cv2.connectedComponentsWithStats((gray<threshold).astype(np.uint8))
        components=[list(map(int,s)) for s in stats[1:] if s[4]>=3]
        maximum=max((c[3] for c in components),default=0)
        substantial=[c for c in components if c[3]>=maximum*.7]
        first_top=min((c[1] for c in substantial),default=0)
        bodies=[c for c in substantial if c[1]<=first_top+maximum*.15]
        if maximum<8 or not bodies:return None
        baseline=float(statistics.median(c[1]+c[3] for c in bodies))
        left=min(c[0] for c in bodies)
        candidates=[c for c in components if c[0]>=left and c[3]<=maximum*.95 and c[3]>=2
                    and c[1]>=first_top+maximum*.2 and c[1]<baseline
                    and c[1]+c[3]>=baseline+maximum*.08]
        if not candidates:
            # A disconnected lower tail is admissible only with a matching
            # upper component in the same digit band; isolated diagram lines
            # below the formula cannot become subscripts.
            candidates=[c for c in components if c[3]>=2 and c[3]<=maximum*.65 and c[1]>=baseline
                        and c[1]<=baseline+maximum*.15 and any(
                            p[1]>=first_top+maximum*.2 and p[1]<baseline and p[1]+p[3]<c[1]
                            and abs((p[0]+p[2]/2)-(c[0]+c[2]/2))<=maximum*.15
                            for p in components if p is not c)]
        if not candidates:return None
        xs=sorted(candidates,key=lambda c:c[0])
        if any(b[0]>a[0]+a[2]+maximum*.35 for a,b in zip(xs,xs[1:])):return None
        lo=min(c[0] for c in xs);hi=max(c[0]+c[2] for c in xs)
        # A broken small 2 has an upper curl above the lowered baseline tail.
        # Recover the whole original digit in that same horizontal band, not
        # merely its bottom component (which can resemble punctuation).
        xs=[c for c in components if c[0]>=lo-1 and c[0]+c[2]<=hi+1
            and c[1]>=first_top+maximum*.2 and c[1]+c[3]<=max(q[1]+q[3] for q in candidates)+1]
        bounds=[min(c[0] for c in xs),min(c[1] for c in xs),
                max(c[0]+c[2] for c in xs),max(c[1]+c[3] for c in xs)]
        masks.append({'threshold':threshold,'components':xs,'body_components':bodies,
                      'body_baseline':baseline,'body_height':maximum,'script_box':bounds})
    if any(max(abs(a-b) for a,b in zip(masks[1]['script_box'],row['script_box']))>2 for row in masks):return None
    return {'threshold_evidence':masks,'script_box':masks[1]['script_box'],
            'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest()}


def recognize_subscript(crop,read):
    """Require the whole molecular token and an independent lowered digit read.

    This interface handles one proven digit group, not a guessed chemical
    formula. Leading coefficients and unchanged letter case remain verbatim.
    Unresolved or multiple groups are left to their original recognizer.
    """
    import cv2
    import numpy as np
    from PIL import Image,ImageOps
    gray=cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY) if crop.ndim==3 else crop
    geometry=lowered_ink(gray)
    if geometry is None:return None,None
    image=Image.fromarray(crop).convert('RGB')
    proof={'profile':PROFILE,'kind':'lowered-digit','geometry':geometry,'attempts':[],
           'digit_attempts':[],'accepted':False}
    for padding in (4,8):
        padded=ImageOps.expand(image,padding,fill='white')
        for scale in (2,3):
            selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
            value,confidence=read(np.asarray(selected))
            proof['attempts'].append({'padding':padding,'scale':scale,'text':value,'confidence':float(confidence),
                                     'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
    values={a['text'] for a in proof['attempts']}
    if len(values)!=1:return None,proof
    original=next(iter(values))
    if (not re.fullmatch(r'[0-9]*[A-Za-z][A-Za-z0-9()]*',original)
            or any(a['confidence']<.9 for a in proof['attempts'])):return None,proof
    x,y,u,v=geometry['script_box']
    digit=image.crop((max(0,x-1),max(0,y-1),min(image.width,u+1),min(image.height,v+1)))
    for padding in (3,6):
        padded=ImageOps.expand(digit,padding,fill='white')
        for scale in (3,4):
            selected=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
            value,confidence=read(np.asarray(selected))
            proof['digit_attempts'].append({'padding':padding,'scale':scale,'text':value,'confidence':float(confidence),
                'pixels_sha256':hashlib.sha256(selected.tobytes()).hexdigest()})
    digits={a['text'] for a in proof['digit_attempts']}
    if len(digits)!=1:return None,proof
    value=next(iter(digits))
    if not re.fullmatch(r'[0-9]{1,2}',value) or any(a['confidence']<.95 for a in proof['digit_attempts']):return None,proof
    matches=[m for m in re.finditer(r'[0-9]+',original) if m.group()==value and m.start()>0
             and re.search('[A-Za-z)]',original[:m.start()])]
    if len(matches)!=1:return None,proof
    match=matches[0]
    # Geometry must support the textual position: an internal lowered group
    # has body ink on both sides; a terminal group has no body glyph after it.
    bodies=geometry['threshold_evidence'][1]['body_components']
    left_body=any(c[0]+c[2]<=x+1 for c in bodies)
    right_body=any(c[0]>=u-1 for c in bodies)
    if not left_body or bool(original[match.end():])!=right_body:return None,proof
    selected=original[:match.start()]+value.translate(SUBSCRIPT)+original[match.end():]
    proof.update(original=original,text=selected,accepted=True)
    return selected,proof


def refine_tokens(image,blocks,read,*,read_secondary=None):
    """Refine positioned complete Latin tokens; preserve every primary block.

    Whole reactions, prose, operators, mixed Han and already positioned Unicode
    scripts are out of scope. A rejected positive geometry stays reviewable.
    No caller-provided expected text or chemical dictionary is consulted.
    """
    import numpy as np
    pixels=np.asarray(image.convert('RGB'))
    width,height=image.size
    output=copy.deepcopy(blocks);reports=[];issues=[]
    for index,block in enumerate(output):
        primary=block['t']
        if not (2<=len(primary)<=24 and re.fullmatch(r'[0-9]*[A-Za-z][A-Za-z0-9()]*(?:[+\-][0-9]{1,2})?',primary)):
            continue
        if not any(c.isdigit() for c in primary):continue
        bounds=[round(a*(width if axis%2==0 else height)) for axis,a in enumerate(block['b'])]
        x,y,u,v=bounds
        if min(u-x,v-y)<4:continue
        pad=min(5,max(2,round((v-y)*.15)))
        x0,y0,x1,y1=max(0,x-pad),max(0,y-pad),min(width,u+pad),min(height,v+pad)
        # Neighbour text cannot be clipped or swallowed to make a token agree.
        if any(i!=index and max(x0,b['b'][0]*width)<min(x1,b['b'][2]*width)
               and max(y0,b['b'][1]*height)<min(y1,b['b'][3]*height) for i,b in enumerate(blocks)):
            continue
        crop=pixels[y0:y1,x0:x1].copy()
        charged=bool(re.search(r'[+\-][0-9]{1,2}$',primary))
        selected,proof=(recognize(crop,read,read_secondary=read_secondary) if charged else recognize_subscript(crop,read))
        if proof is None:continue
        row={'region':index,'primary':copy.deepcopy(blocks[index]),'source_crop_box':[x0,y0,x1,y1],
             'source_pixels_sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'recognition':proof,
             'accepted':False}
        if selected is not None:
            # Match precisely, including case and the sign/number order. Only
            # script position may change, never an OCR spelling or a letter.
            reverse=str.maketrans('⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉','+-01234567890123456789')
            if selected.translate(reverse)==primary:
                block['t']=selected
                block['primary_text']=primary
                sx,sy,su,sv=proof['geometry']['script_box']
                row.update(accepted=True,text=selected,script_source_box=[
                    (x0+sx)/width,(y0+sy)/height,(x0+su)/width,(y0+sv)/height])
                block['formula_script_evidence']=copy.deepcopy(row)
        reports.append(row)
        if not row['accepted']:
            issues.append({'code':'formula-script-token-unresolved','severity':'review',
                           'evidence':{'region':index,'box':block['b'],'primary':primary,
                                       'source_pixels_sha256':row['source_pixels_sha256']}})
    return output,({'profile':PROFILE,'tokens':reports,'issues':issues} if reports else None)
