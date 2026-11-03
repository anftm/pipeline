"""Keep category-band tables with multiple underlined column groups in row order."""
from __future__ import annotations

import copy
import hashlib
import re

PROFILE='grouped-statistical-source-bands-v1'


def source_underlines(gray):
    """Join only collinear detector fragments with continuous original ink.

    LSD can split either edge of the same slanted printed rule. An endpoint
    union alone is not evidence: all intervening source columns must also
    support that line, at three independent darkness thresholds.
    """
    import cv2
    import numpy as np
    height,width=gray.shape
    detected=cv2.createLineSegmentDetector().detect(gray)[0]
    if detected is None:return []
    segments=[]
    for values in detected.reshape(-1,4):
        x,y,u,v=map(float,values)
        if x>u:x,y,u,v=u,v,x,y
        if u-x<width*.08 or abs(v-y)>height*.02:continue
        segments.append([x,y,u,v])
    tolerance=max(2.,height*.002)
    groups=[]
    for segment in sorted(segments):
        x,y,u,v=segment;slope=(v-y)/(u-x)
        matches=[]
        for number,group in enumerate(groups):
            for a,b,c,d in group:
                if max(x,a)>min(u,c)+width*.006:continue
                other=(d-b)/(c-a)
                lo,hi=max(x,a),min(u,c)
                if hi<lo:lo=hi=(lo+hi)/2
                if max(abs((y+slope*(at-x))-(b+other*(at-a))) for at in (lo,hi))<=tolerance:
                    matches.append(number);break
        if not matches:groups.append([segment]);continue
        merged=[segment]
        for number in reversed(matches):merged.extend(groups.pop(number))
        groups.append(merged)
    underlines=[]
    for group in groups:
        endpoints=np.asarray([[x,y] for a,b,c,d in group for x,y in ((a,b),(c,d))])
        slope,intercept=map(float,np.polyfit(endpoints[:,0],endpoints[:,1],1))
        if any(abs(y-(slope*x+intercept))>tolerance for x,y in endpoints):continue
        left,right=max(0,round(float(endpoints[:,0].min()))),min(width-1,round(float(endpoints[:,0].max())))
        if right-left<width*.15:continue
        xx=np.arange(left,right+1);yy=np.rint(slope*xx+intercept).astype(int)
        band=max(2,round(tolerance))
        sample_y=yy[:,None]+np.arange(-band,band+1)
        if sample_y.min()<0 or sample_y.max()>=height:continue
        pixels=gray[sample_y,xx[:,None]];checks=[]
        for threshold in (128,180,220):
            support=(pixels<threshold).any(axis=1)
            longest=run=0
            for dark in support:
                run=0 if dark else run+1;longest=max(longest,run)
            if float(support.mean())<.9 or longest>max(2,round(len(xx)*.01)):break
            checks.append({'threshold':threshold,'column_support':float(support.mean()),
                           'longest_blank_run':longest})
        if len(checks)!=3:continue
        underlines.append({'line_pixels':[left,slope*left+intercept,right,slope*right+intercept],
            'segments_pixels':group,'band_half_width':band,'threshold_evidence':checks,
            'source_band_sha256':hashlib.sha256(pixels.tobytes()).hexdigest()})
    return underlines


def detect(image,blocks,grid):
    import cv2
    import numpy as np
    width,height=image.size;xs=grid['columns'];rows=grid['evidence'].get('numeric_row_centers',[])
    if len(rows)<5 or len(xs)<5:return None
    start,stop=grid['rows'][0],grid['rows'][-1]
    shared=[(i,b) for i,b in enumerate(blocks) if b['b'][0]>=xs[1]
            and start<(b['b'][1]+b['b'][3])/2<rows[0]-.04
            and len(b['t'].split())>=2 and b['b'][2]-b['b'][0]>.07]
    shared.sort(key=lambda item:item[1]['b'][0])
    if not 2<=len(shared)<=4:return None
    top_end=max(b['b'][3] for i,b in shared)
    following=[b['b'][1] for i,b in enumerate(blocks) if i not in {j for j,c in shared}
               and top_end-.005<b['b'][1]<rows[0]-.03 and re.search('[A-Za-z]',b['t'])]
    if not following:return None
    split=(top_end+min(following))/2
    gray=np.asarray(image.convert('L'))
    underlines=source_underlines(gray)
    anchors=grid['evidence'].get('numeric_columns',[])
    if len(anchors)!=len(xs)-1:return None
    spans=[];proof=[]
    for index,heading in shared:
        choices=[]
        for underline in underlines:
            x,y,u,v=underline['line_pixels']
            left,right=x/width,u/width;line_y=(y+v)/2/height
            if not (heading['b'][3]-.003<=line_y<=split+.02 and left<=heading['b'][0]+.01
                    and right>=heading['b'][2]-.01 and right-left<.65):continue
            covered=[c for c,anchor in enumerate(anchors) if left<=anchor<=right]
            if len(covered)<2:continue
            choices.append((len(covered),covered,left,right,line_y,underline))
        if not choices:return None
        _,covered,left,right,line_y,underline=max(choices,key=lambda item:item[0])
        x,y,u,v=underline['line_pixels']
        span=(min(covered),max(covered)+1)
        if covered!=list(range(*span)):return None
        spans.append(span);proof.append({'heading_block':index,'columns':list(span),
                                       'source_underline':[left,y/height,right,v/height],
                                       'pixel_evidence':copy.deepcopy(underline)})
    occupied=[c for a,b in spans for c in range(a,b)]
    if len(set(occupied))!=len(occupied) or set(occupied)!=set(range(1,len(xs)-1)):return None
    categories=[(i,b) for i,b in enumerate(blocks) if b['b'][0]<xs[1]
                and b['b'][2]-b['b'][0]>.08 and top_end<b['b'][1]<stop
                and len(b['t'].split())>=2 and re.fullmatch(r'[A-Za-z ,’\-]+',b['t'])]
    if len(categories)<2:return None
    first_category=min(b['b'][1] for i,b in categories)-.005
    if first_category<=split:return None
    events=[(center,'data',None) for center in rows]
    events.extend(((b['b'][1]+b['b'][3])/2,'category',i) for i,b in categories)
    events.sort()
    if events[0][1]!='category':return None
    for at,event in enumerate(events):
        if event[1]=='category' and (at==len(events)-1 or events[at+1][1]!='data'):return None
    ys=[start,split,first_category,*[(a[0]+b[0])/2 for a,b in zip(events,events[1:])],stop]
    if any(a>=b for a,b in zip(ys,ys[1:])):return None
    cells=[];cols=len(xs)-1
    for row,(top,bottom) in enumerate(zip(ys,ys[1:])):
        col=0
        while col<cols:
            if row==0 and col==0:end,rowspan=1,2
            elif row==0:
                match=next((b for a,b in spans if a==col),None)
                if match is None:return None
                end,rowspan=match,1
            elif row==1 and col==0:col=1;continue
            elif row>=2 and events[row-2][1]=='category':end,rowspan=cols,1
            else:end,rowspan=col+1,1
            members=[i for i,b in enumerate(blocks) if xs[col]<=(b['b'][0]+b['b'][2])/2<xs[end]
                     and top<=(b['b'][1]+b['b'][3])/2<ys[row+rowspan]]
            members.sort(key=lambda i:(blocks[i]['b'][1],blocks[i]['b'][0]))
            cells.append({'row':row,'column':col,'rowspan':rowspan,'colspan':end-col,
                'box':[xs[col],top,xs[end],ys[row+rowspan]],'blocks':members,
                'text':'\n'.join(blocks[i]['t'] for i in members),'boundary_status':'source-shared-header-and-category-band'})
            col=end
    result=copy.deepcopy(grid)
    original_members={i for cell in grid['cells'] for i in cell['blocks']}
    actual_members=[i for cell in cells for i in cell['blocks']]
    if len(actual_members)!=len(set(actual_members)) or set(actual_members)!=original_members:return None
    resolved={i for i,b in shared}|{i for i,b in categories}
    review=[]
    for item in grid.get('review',[]):
        if item.get('code')=='aligned-table-empty-cell-with-ink':continue
        if (item.get('code') in {'aligned-table-header-span-review','aligned-table-category-band-review'}
                and item.get('block') in resolved):continue
        review.append(copy.deepcopy(item))
    # Empty numeric cells are not assumed blank merely because OCR missed a
    # detection. Recompute source-ink evidence against the rebuilt row bands.
    for cell in cells:
        if cell['row']<2 or events[cell['row']-2][1]!='data' or cell['blocks']:continue
        x,y,u,v=[round(a*(width if axis%2==0 else height)) for axis,a in enumerate(cell['box'])]
        if min(u-x,v-y)<3:continue
        _,_,components,_=cv2.connectedComponentsWithStats((gray[y:v,x:u]<180).astype(np.uint8))
        significant=[list(map(int,c)) for c in components[1:] if c[4]>=4 and c[2]>=3
                     and not(c[2]>=(u-x)*.8 and c[3]<=5)]
        if significant:review.append({'code':'aligned-table-empty-cell-with-ink',
            'row':cell['row'],'column':cell['column'],'components':significant})
    result.update(profile=PROFILE,rows=ys,cells=cells,review=review,body_rows=[2,len(ys)-2])
    result['evidence'].update(shared_header_underlines=proof,category_blocks=[i for i,b in categories],
        numeric_row_indices=[row+2 for row,event in enumerate(events) if event[1]=='data'])
    return result
