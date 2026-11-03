"""Position numbered symbolic matrices without changing zeroes, letters or blanks."""
from __future__ import annotations

import hashlib
import re

PROFILE='symbolic-source-matrix-v1-numbered-axes'


def detect(image,blocks):
    import cv2
    import numpy as np
    width,height=image.size
    headings=[b for b in blocks if re.fullmatch(r'TABLE\s+\d+(?:\.\d+)+',b['t'])]
    if len(headings)!=1:return None
    heading=headings[0]
    footers=[b for b in blocks if re.match(r'(?:Note|Source)\s*:',b['t']) and b['b'][1]>heading['b'][3]]
    if not footers:return None
    stop=min(b['b'][1] for b in footers)
    serials=sorted([(i,b) for i,b in enumerate(blocks) if re.match(r'^[1-9][0-9]*\.\s+\S',b['t'])
                    and heading['b'][3]<b['b'][1]<stop],key=lambda pair:pair[1]['b'][1])
    if not 5<=len(serials)<=40:return None
    numbers=[int(re.match(r'^[0-9]+',b['t'])[0]) for i,b in serials]
    if numbers!=list(range(1,len(serials)+1)):return None
    first=min(b['b'][1] for i,b in serials)
    headnums=sorted([(int(b['t']),i,b) for i,b in enumerate(blocks) if re.fullmatch(r'[1-9][0-9]?',b['t'])
                     and heading['b'][3]<b['b'][1] and b['b'][3]<first],key=lambda item:item[0])
    if not 4<=len(headnums)<=20 or [n for n,i,b in headnums]!=list(range(1,len(headnums)+1)):return None
    centers=[(b['b'][0]+b['b'][2])/2 for n,i,b in headnums]
    if any(a+.025>=b for a,b in zip(centers,centers[1:])):return None
    label_right=max(b['b'][2] for i,b in serials)
    if label_right>=centers[0]-.015:return None
    symbol=re.compile(r'[A-Z0-9](?:\s*,\s*[A-Z0-9])*')
    body_ids=[i for i,b in enumerate(blocks) if first-.02<(b['b'][1]+b['b'][3])/2<stop and b['b'][0]>label_right]
    body=[blocks[i] for i in body_ids]
    if len(body)<24 or any(not symbol.fullmatch(b['t']) for b in body):return None
    gray=np.asarray(image.convert('L'))
    ink=cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,51,15)
    mask=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((1,max(3,round(width*.006))),np.uint8))
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((1,max(20,round(width*.2))),np.uint8))
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
    rules=[(index,s) for index,s in enumerate(stats[1:],1) if s[2]>width*.65 and s[3]<height*.04
           and heading['b'][3]*height<s[1]<height*first]
    if not rules:return None
    rule_index,top=min(rules,key=lambda item:item[1][1])
    yy,xx=np.nonzero(labels==rule_index)
    line_slope,intercept=np.polyfit(xx,yy,1)
    slope=float(line_slope*width/height)
    if abs(slope)>.04:return None
    left,right=float(top[0]/width),float((top[0]+top[2])/width)
    def canonical(block):
        return (block['b'][1]+block['b'][3])/2-slope*((block['b'][0]+block['b'][2])/2-left)
    row_centers=[canonical(b) for i,b in serials]
    if any(a>=b for a,b in zip(row_centers,row_centers[1:])):return None
    shared=[(i,b) for i,b in enumerate(blocks) if b['b'][2]-b['b'][0]>.15
            and top[1]/height<b['b'][1]<min(b['b'][1] for n,j,b in headnums)
            and len(b['t'])>=12]
    # The spanning heading must sit above and horizontally within the numbered
    # data columns. Page titles and captions do not establish a table span.
    shared=[(i,b) for i,b in shared if b['b'][0]>centers[0] and b['b'][2]<centers[-1]+.03]
    if len(shared)!=1:return None
    shared_index,shared_block=shared[0]
    header_split=(shared_block['b'][3]+min(b['b'][1] for n,i,b in headnums))/2
    body_start=first-.006
    xs=[left,(label_right+centers[0]-.04)/2,*[(a+b)/2 for a,b in zip(centers,centers[1:])],right]
    ys=[float(top[1]/height),header_split,body_start,
        *[(a+b)/2 for a,b in zip(row_centers,row_centers[1:])],stop]
    if any(a>=b for a,b in zip(xs,xs[1:])) or any(a>=b for a,b in zip(ys,ys[1:])):return None
    cells=[];owned=set()
    for row in range(len(ys)-1):
        for col in range(len(xs)-1):
            if row==0 and col not in (0,1) or row==1 and col==0:continue
            rowspan=2 if row==0 and col==0 else 1
            colspan=len(xs)-2 if row==0 and col==1 else 1
            a,b,c,d=xs[col],ys[row],xs[col+colspan],ys[row+rowspan]
            members=[]
            for index,block in enumerate(blocks):
                if index in owned or block['b'][1]>=stop or block is heading:continue
                cx=(block['b'][0]+block['b'][2])/2
                cy=canonical(block)
                if col==0 and row>=2:
                    target=next((k for k in reversed(range(len(serials)))
                                 if block['b'][1]>=serials[k][1]['b'][1]-.003),None)
                    selected=target==row-2 and a<=cx<c
                else:selected=a<=cx<c and b<=cy<d
                if selected:members.append(index);owned.add(index)
            members.sort(key=lambda i:(blocks[i]['b'][1],blocks[i]['b'][0]))
            cells.append({'row':row,'column':col,'rowspan':rowspan,'colspan':colspan,
                'box':[a,max(0,min(b,b+slope*(a-left),b+slope*(c-left))),c,
                       min(1,max(d,d+slope*(a-left),d+slope*(c-left)))],
                'blocks':members,'text':'\n'.join(blocks[i]['t'] for i in members),
                'boundary_status':'numbered-axes-and-original-rule'})
    if shared_index not in owned:return None
    if not set(body_ids)<=owned:return None
    return {'profile':PROFILE,'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),
        'box':[left,ys[0],right,stop],'rows':ys,'columns':xs,'cells':cells,'review':[],
        'character_accuracy':'unreviewed','body_rows':[2,len(ys)-2],
        'evidence':{'row_label_blocks':[i for i,b in serials],'column_number_blocks':[i for n,i,b in headnums],
                    'slope':slope,'top_rule':list(map(int,top)),
                    'symbol_policy':'verbatim; no zero/letter/blank normalization'}}


def arrange(blocks,width,height,table,options=None):
    try:
        from . import ocr_table_geometry
    except ImportError:
        import ocr_table_geometry
    return ocr_table_geometry.arrange(blocks,width,height,table,options,include_writing_modes=True)
