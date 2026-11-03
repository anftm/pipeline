"""Preserve numeric fractions with proved source bars and distinct positioned parts."""
from __future__ import annotations

import copy
import hashlib
import re

PROFILE='numeric-fraction-source-bars-v1'


def detect(image,blocks):
    import cv2
    import numpy as np
    gray=np.asarray(image.convert('L'));width,height=image.size
    numbers=[(i,b) for i,b in enumerate(blocks) if re.fullmatch(r'[0-9]{1,5}',b['t']) and b.get('c',0)>=.95]
    candidates=[]
    for first,numerator in numbers:
        a=numerator['b'];nw=(a[2]-a[0])*width;nh=(a[3]-a[1])*height
        for second,denominator in numbers:
            b=denominator['b'];dw=(b[2]-b[0])*width;dh=(b[3]-b[1])*height
            if first==second or (b[1]+b[3])<=(a[1]+a[3]):continue
            gap=((b[1]+b[3]-a[1]-a[3])/2)*height
            if not (.5*min(nh,dh)<=gap<=2.5*max(nh,dh)):continue
            overlap=(min(a[2],b[2])-max(a[0],b[0]))*width
            if overlap<min(nw,dw)*.65 or abs((a[0]+a[2]-b[0]-b[2])/2*width)>max(nw,dw)*.3:continue
            left=max(0,round(min(a[0],b[0])*width)-4)
            right=min(width,round(max(a[2],b[2])*width)+4)
            top=max(0,round((a[1]+a[3])/2*height)+1)
            bottom=min(height,round((b[1]+b[3])/2*height))
            if bottom<=top or right-left<5:continue
            checks=[];reference=None
            for threshold in [220,180,128]:
                ink=(gray[top:bottom,left:right]<threshold).astype(np.uint8)
                if reference is None:
                    closed=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((1,3),np.uint8))
                    count,labels,stats,_=cv2.connectedComponentsWithStats(closed)
                    bars=[]
                    for index in range(1,count):
                        x,y,w,h,area=map(int,stats[index])
                        if not (.4*max(nw,dw)<=w<=1.6*max(nw,dw) and 1<=h<=3 and w>=h*6):continue
                        original=ink[y:y+h,x:x+w]
                        support=float(original.any(axis=0).mean())
                        if support>=.65:bars.append({'box':[left+x,top+y,left+x+w,top+y+h],'column_support':support})
                    if len(bars)!=1:break
                    reference=bars[0]['box']
                x,y,u,v=reference
                original=ink[max(0,y-top-1):min(bottom-top,v-top+1),x-left:u-left]
                support=float(original.any(axis=0).mean())
                if support<.65:break
                checks.append({'threshold':threshold,'box':reference,'column_support':support})
            if len(checks)!=3:continue
            x,y,u,v=reference
            if not ((a[0]+a[2])/2*width>=x-2 and (a[0]+a[2])/2*width<=u+2):continue
            pair_box=[min(a[0],b[0]),a[1],max(a[2],b[2]),b[3]]
            # A third textual region in the fraction's own band is ambiguous,
            # not permission to merge an equals sign or adjacent table cell.
            if any(i not in (first,second)
                   and pair_box[0]<=(c['b'][0]+c['b'][2])/2<=pair_box[2]
                   and pair_box[1]<=(c['b'][1]+c['b'][3])/2<=pair_box[3] for i,c in enumerate(blocks)):
                continue
            candidates.append({'numerator':first,'denominator':second,'box':pair_box,
                'bar_box':[x/width,y/height,u/width,v/height],'threshold_evidence':checks,
                'bar_pixels_sha256':hashlib.sha256(gray[y:v,x:u].tobytes()).hexdigest()})
    counts={}
    for row in candidates:
        for index in [row['numerator'],row['denominator']]:counts[index]=counts.get(index,0)+1
    return [row for row in candidates if all(counts[i]==1 for i in [row['numerator'],row['denominator']])]


def arrange(blocks,width,height,fractions,options=None):
    try:
        from . import ocr_layout
    except ImportError:
        import ocr_layout
    original=copy.deepcopy(blocks);working=[];mapping=[];owned=set();relations=[]
    for number,fraction in enumerate(fractions):
        first,second=fraction['numerator'],fraction['denominator']
        if owned.intersection([first,second]):raise ValueError('fraction parts reused')
        owned.update([first,second])
        operator=len(original)
        original.append({'t':'/','b':fraction['bar_box'],'c':1.,'s':'ocr',
            'confidence_source':'original-fraction-bar-geometry','fraction_bar_evidence':copy.deepcopy(fraction),
            'structural_parent_blocks':[first,second]})
        content=blocks[first]['t']+'/'+blocks[second]['t']
        working.append({'t':content,'b':fraction['box'],'c':min(blocks[first]['c'],blocks[second]['c']),'s':'ocr'})
        mapping.append([first,operator,second])
        relations.append({'profile':PROFILE,'box':fraction['box'],'bar_box':fraction['bar_box'],
            'numerator_block':first,'denominator_block':second,'operator_block':operator,
            'bar_pixels_sha256':fraction['bar_pixels_sha256'],'threshold_evidence':fraction['threshold_evidence']})
    for index,block in enumerate(blocks):
        if index not in owned:working.append(copy.deepcopy(block));mapping.append([index])
    result=ocr_layout.arrange(working,width,height,options,include_writing_modes=True)
    spans=[];ordered=[]
    for span in result['text_spans']:
        offset=span['start']
        for index in mapping[span['block']]:
            block=original[index];end=offset+len(block['t'])
            if result['text'][offset:end]!=block['t']:raise ValueError('fraction text lost a positioned part')
            spans.append({**span,'block':index,'start':offset,'end':end,'box':block['b']})
            ordered.append({**copy.deepcopy(block),'id':index});offset=end
    return {**result,'raw_blocks':original,'blocks':ordered,'text_spans':spans,
            'layout':{**result['layout'],'evidence':PROFILE,'fractions':relations}}
