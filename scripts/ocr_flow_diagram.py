"""Trace boxed, orthogonal source diagrams without treating crossings as junctions.

The result is positioned structure, not a transcription or an accuracy badge.
Printed labels, line breaks and all original OCR failures stay unchanged.
"""
from __future__ import annotations

import hashlib
import copy
import base64
import io
import re
import statistics

PROFILE='boxed-source-flow-v1-original-routes'


def stroke(gray,points,band):
    """Check every original route leg at three thresholds, including any gaps."""
    import numpy as np
    height,width=gray.shape;checks=[];digest=hashlib.sha256()
    for first,last in zip(points,points[1:]):
        x,y=first;u,v=last
        if min(abs(u-x),abs(v-y))>1e-6:return None
        length=round(max(abs(u-x),abs(v-y)))
        if length<2:return None
        xx=np.rint(np.linspace(x,u,length+1)).astype(int)
        yy=np.rint(np.linspace(y,v,length+1)).astype(int)
        offset=np.arange(-band,band+1)
        sample_x=xx[:,None]+(offset if x==u else np.zeros_like(offset))
        sample_y=yy[:,None]+(offset if y==v else np.zeros_like(offset))
        if sample_x.min()<0 or sample_x.max()>=width or sample_y.min()<0 or sample_y.max()>=height:return None
        pixels=gray[sample_y,sample_x];digest.update(pixels.tobytes());thresholds=[]
        for threshold in (128,180,220):
            dark=(pixels<threshold).any(axis=1);run=longest=0
            for value in dark:
                run=0 if value else run+1;longest=max(run,longest)
            if float(dark.mean())<.8 or longest>max(4,band*6):return None
            thresholds.append({'threshold':threshold,'support':float(dark.mean()),'longest_blank_run':longest})
        checks.append({'from':list(first),'to':list(last),'thresholds':thresholds})
    return {'source_path_sha256':digest.hexdigest(),'band_half_width':band,'legs':checks}


def arrowhead(gray,endpoint,direction,glyph_height):
    """Prove two backward-facing arms near a route's destination, not OCR > or 4."""
    import cv2
    import numpy as np
    forward=np.asarray({'right':[1,0],'left':[-1,0],'down':[0,1],'up':[0,-1]}[direction],float)
    perpendicular=np.asarray([-forward[1],forward[0]])
    endpoint=np.asarray(endpoint,float);height,width=gray.shape;radius=max(1,round(glyph_height*.06))
    lo=np.maximum(0,np.floor(endpoint-glyph_height*2)).astype(int)
    hi=np.minimum([width,height],np.ceil(endpoint+glyph_height*2)).astype(int)
    detected=cv2.createLineSegmentDetector().detect(gray[lo[1]:hi[1],lo[0]:hi[0]])[0]
    if detected is None:return None
    arms=[]
    for segment in detected.reshape(-1,4):
        points=np.asarray([segment[:2]+lo,segment[2:]+lo]);distance=(endpoint-points)@forward
        side=(points-endpoint)@perpendicular
        if (distance.min()<-glyph_height*.25 or distance.max()>glyph_height*1.7
                or abs(distance[1]-distance[0])<glyph_height*.25
                or abs(side).max()>glyph_height*.8):continue
        slope=float((side[1]-side[0])/(distance[1]-distance[0]))
        if not .15<abs(slope)<.9:continue
        offset=float(side[0]-slope*distance[0]);checks=[]
        count=max(4,round(float(np.linalg.norm(points[1]-points[0]))))
        xx=np.rint(np.linspace(points[0,0],points[1,0],count)).astype(int)
        yy=np.rint(np.linspace(points[0,1],points[1,1],count)).astype(int)
        if min(xx.min(),yy.min())<radius or xx.max()>=width-radius or yy.max()>=height-radius:continue
        pixels=np.stack([gray[yy+dy,xx+dx] for dy in range(-radius,radius+1)
                         for dx in range(-radius,radius+1)],axis=1)
        for threshold in (128,180,220):
            support=float((pixels<threshold).any(axis=1).mean())
            if support<.9:break
            checks.append({'threshold':threshold,'support':support})
        if len(checks)==3:arms.append({'slope':slope,'offset':offset,'distance':distance,
            'points_pixels':points.tolist(),'threshold_evidence':checks,
            'source_pixels_sha256':hashlib.sha256(pixels.tobytes()).hexdigest()})
    best=None
    for first in arms:
        for second in arms:
            if first['slope']*second['slope']>=0:continue
            d=(second['offset']-first['offset'])/(first['slope']-second['slope'])
            side=first['slope']*d+first['offset']
            if abs(d)>glyph_height*.5 or abs(side)>glyph_height*.4:continue
            if any(a['distance'].min()<d-radius*3 or a['distance'].max()<d+glyph_height*.3 for a in (first,second)):continue
            score=min(float(a['distance'].max()-a['distance'].min()) for a in (first,second))
            if best is not None and score<=best['score']:continue
            best={'tip_pixels':(endpoint-forward*d+perpendicular*side).tolist(),'direction':direction,
                  'arms':[{key:value for key,value in arm.items() if key!='distance'} for arm in (first,second)],
                  'band_half_width':radius,'score':score}
    return best


def detect(image,blocks):
    """Find independently boxed Block labels and source-proved direct/long routes."""
    import cv2
    import numpy as np
    gray=np.asarray(image.convert('L'));height,width=gray.shape
    headings=[(i,b) for i,b in enumerate(blocks) if re.fullmatch(r'Block\s+[0-9]+',b['t'])]
    if not 3<=len(headings)<=30:return None
    glyph=statistics.median((b['b'][3]-b['b'][1])*height for i,b in headings)
    band=max(2,round(glyph*.1));ink=(gray<180).astype(np.uint8);rules={}
    for axis in ('horizontal','vertical'):
        close=(1,max(3,round(glyph*.2))) if axis=='horizontal' else (max(3,round(glyph*.2)),1)
        opening=(1,max(20,round(glyph*1.5))) if axis=='horizontal' else (max(30,round(glyph*2)),1)
        mask=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones(close,np.uint8))
        mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones(opening,np.uint8))
        _,_,stats,_=cv2.connectedComponentsWithStats(mask)
        rules[axis]=[list(map(int,row[:4])) for row in stats[1:] if
            (row[2]>glyph*2 and row[3]<glyph*.4 if axis=='horizontal' else row[3]>glyph*2 and row[2]<glyph*.4)]
    nodes=[]
    for index,label in headings:
        x,y,u,v=[a*(width if axis%2==0 else height) for axis,a in enumerate(label['b'])]
        top_rules=[r for r in rules['horizontal'] if r[0]<(x+u)/2<r[0]+r[2]
                   and y-glyph*3<r[1]<y-glyph*.3 and r[2]>u-x]
        if not top_rules:return None
        top=max(top_rules,key=lambda r:r[1]);left=top[0];right=top[0]+top[2]-1
        vertical=[]
        for edge in (left,right):
            candidates=[r for r in rules['vertical'] if abs(r[0]+r[2]/2-edge)<=band*3
                        and r[1]<=top[1]+band*3 and r[1]+r[3]>v+glyph]
            if not candidates:return None
            vertical.append(min(candidates,key=lambda r:abs(r[0]+r[2]/2-edge)))
        left,right=[r[0]+(r[2]-1)/2 for r in vertical]
        bottom_rules=[r for r in rules['horizontal'] if abs(r[0]-left)<=band*3
                      and abs(r[0]+r[2]-right)<=band*3 and r[1]>v+glyph
                      and all(abs(r[1]-(a[1]+a[3]))<=band*4 for a in vertical)]
        if not bottom_rules:return None
        bottom=min(bottom_rules,key=lambda r:r[1]);top_y=top[1]+(top[3]-1)/2;bottom_y=bottom[1]+(bottom[3]-1)/2
        box=[left,top_y,right,bottom_y]
        outline=[[left,top_y],[right,top_y],[right,bottom_y],[left,bottom_y],[left,top_y]]
        evidence=stroke(gray,outline,band)
        if evidence is None:return None
        members=[i for i,b in enumerate(blocks) if left<=(b['b'][0]+b['b'][2])/2*width<=right
                 and top_y<=(b['b'][1]+b['b'][3])/2*height<=bottom_y]
        members.sort(key=lambda i:((blocks[i]['b'][1]+blocks[i]['b'][3])/2,blocks[i]['b'][0]))
        nodes.append({'id':f'n{index}','label_block':index,'blocks':members,'box_pixels':box,
                      'box':[left/width,top_y/height,right/width,bottom_y/height],'outline_evidence':evidence})
    nodes.sort(key=lambda n:n['box'][0])
    all_members=[i for node in nodes for i in node['blocks']]
    if len(all_members)!=len(set(all_members)):return None
    edges=[];seen=set();review=[]
    def add(first,last,path):
        key=(first['id'],last['id'])
        if key in seen:return
        if any(min(abs(a-c),abs(b-d))>1e-6 for (a,b),(c,d) in zip(path,path[1:])):return
        direction=('right' if path[-1][0]>path[-2][0] else 'left') if path[-1][1]==path[-2][1] else (
            'down' if path[-1][1]>path[-2][1] else 'up')
        head=arrowhead(gray,path[-1],direction,glyph)
        proof=stroke(gray,path,band)
        if proof is None:return
        if head is None:
            review.append({'code':'flow-route-arrowhead-unresolved','from':first['id'],'to':last['id'],
                           'path_pixels':path});return
        edges.append({'from':first['id'],'to':last['id'],'arrow_direction':direction,
            'path':[[x/width,y/height] for x,y in path],'path_pixels':path,
            'evidence':{**proof,'arrowhead':head}});seen.add(key)
    for x,y,w,h in rules['horizontal']:
        if w<glyph*2:continue
        u=x+w-1;middle=y+(h-1)/2
        # A direct inter-box rule has endpoints on distinct box sides.
        for first in nodes:
            a,b,c,d=first['box_pixels']
            if abs(x-c)>band*3 or not b<middle<d:continue
            for last in nodes:
                lo,top,hi,bottom=last['box_pixels']
                if abs(u-lo)<=band*3 and top<middle<bottom and last is not first:
                    add(first,last,[[c,middle],[lo,middle]])
        # Long orthogonal rules are traced as whole paths. Crossing a vertical
        # rule does not produce another edge or an implied graph junction.
        for first in nodes:
            a,b,c,d=first['box_pixels']
            if not a+band<x<c-band:continue
            for last in nodes:
                lo,top,hi,bottom=last['box_pixels']
                if first is last or not lo+band<u<hi-band:continue
                if middle<min(b,top):
                    add(first,last,[[x,b],[x,middle],[u,middle],[u,top]])
                elif middle>max(d,bottom):
                    add(first,last,[[x,d],[x,middle],[u,middle],[u,bottom]])
    # Repeated vertical points below the last text line are diagram marks,
    # not permission to fill the unfinished sentence or silently omit dots.
    for node in nodes:
        x,y,u,v=map(round,node['box_pixels'])
        last=max(blocks[i]['b'][3]*height for i in node['blocks'])
        a,b,c,d=x+band,round(last)+band,u-band,v-band
        if d<=b:continue
        _,_,stats,_=cv2.connectedComponentsWithStats((gray[b:d,a:c]<180).astype(np.uint8))
        dots=[list(map(int,s)) for s in stats[1:] if 3<=s[4] and
              .03*glyph<=s[2]<=.35*glyph and .03*glyph<=s[3]<=.35*glyph]
        groups=[]
        for dot in sorted(dots,key=lambda s:s[0]):
            if groups and abs((dot[0]+dot[2]/2)-statistics.median(s[0]+s[2]/2 for s in groups[-1]))<=band:
                groups[-1].append(dot)
            else:groups.append([dot])
        marks=[]
        for group in groups:
            group.sort(key=lambda s:s[1]);centers=[s[1]+s[3]/2 for s in group]
            if len(group)!=3:continue
            gaps=[e-f for f,e in zip(centers,centers[1:])]
            median=statistics.median(gaps)
            if not glyph*.2<=median<=glyph or any(abs(gap-median)>median*.3 for gap in gaps):continue
            boxes=[[a+s[0],b+s[1],a+s[0]+s[2],b+s[1]+s[3]] for s in group]
            checks=[]
            for threshold in (128,180,220):
                observed=[]
                for x,y,u,v in boxes:
                    _,_,components,_=cv2.connectedComponentsWithStats((gray[y:v,x:u]<threshold).astype(np.uint8))
                    significant=[s for s in components[1:] if s[4]>=3]
                    if len(significant)!=1:break
                    dot=significant[0]
                    if max(abs(int(dot[i])-target) for i,target in enumerate((0,0,u-x,v-y)))>1:break
                    observed.append(list(map(int,dot)))
                if len(observed)!=3:break
                checks.append({'threshold':threshold,'components':observed})
            if len(checks)!=3:continue
            marks.append({'kind':'vertical-dots','boxes_pixels':boxes,
                'boxes':[[value/(width if axis%2==0 else height) for axis,value in enumerate(box)] for box in boxes],
                'threshold_evidence':checks,
                'source_pixels_sha256':hashlib.sha256(b''.join(gray[y:v,x:u].tobytes() for x,y,u,v in boxes)).hexdigest()})
        if marks:node['marks']=marks
    return {'profile':PROFILE,'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),
        'box':[min(n['box'][0] for n in nodes),min(n['box'][1] for n in nodes),
               max(n['box'][2] for n in nodes),max(n['box'][3] for n in nodes)],
        'nodes':nodes,'edges':edges,'character_accuracy':'unreviewed','review':review,
        'topology_policy':'each whole source route; crossings are not junctions'}


def filter_detections(image,blocks,models,diagram):
    """Exclude only source ink on traced arrows, preserving every original byte."""
    import cv2
    import numpy as np
    width,height=image.size;gray=np.asarray(image.convert('L'));mask=np.zeros(gray.shape,np.uint8)
    for edge in diagram['edges']:
        band=edge['evidence']['band_half_width']
        cv2.polylines(mask,[np.rint(edge['path_pixels']).astype(np.int32)],False,1,band*2+1)
        head=edge['evidence']['arrowhead'];tip=np.asarray(head['tip_pixels'])
        for arm in head['arms']:
            points=np.asarray(arm['points_pixels']);end=max(points,key=lambda point:np.linalg.norm(point-tip))
            cv2.line(mask,tuple(np.rint(tip).astype(int)),tuple(np.rint(end).astype(int)),1,band*2+1)
    for node in diagram['nodes']:
        x,y,u,v=node['box_pixels'];band=node['outline_evidence']['band_half_width']
        cv2.polylines(mask,[np.rint([[x,y],[u,y],[u,v],[x,v]]).astype(np.int32)],True,1,band*2+1)
    inside={i for node in diagram['nodes'] for i in node['blocks']}
    excluded=[];retained=[];remap={}
    for index,block in enumerate(blocks):
        if index in inside:
            remap[index]=len(retained);retained.append(copy.deepcopy(block));continue
        x,y,u,v=[round(a*(width if axis%2==0 else height)) for axis,a in enumerate(block['b'])]
        x,y,u,v=max(0,x),max(0,y),min(width,u),min(height,v)
        if min(u-x,v-y)<1:
            remap[index]=len(retained);retained.append(copy.deepcopy(block));continue
        polygon=np.ones((v-y,u-x),np.uint8)
        if block.get('q'):
            polygon[:]=0
            points=np.rint(np.asarray(block['q'])*[width,height]-[x,y]).astype(np.int32)
            cv2.fillPoly(polygon,[points],1)
        checks=[]
        for threshold in (128,180,220):
            ink=(gray[y:v,x:u]<threshold)&polygon.astype(bool);total=int(ink.sum())
            covered=int((ink&mask[y:v,x:u].astype(bool)).sum())
            checks.append({'threshold':threshold,'ink_pixels':total,'route_pixels':covered})
        if all(c['ink_pixels']>=4 and c['route_pixels']>=c['ink_pixels']*.98 for c in checks):
            stream=io.BytesIO();image.crop((x,y,u,v)).save(stream,format='PNG');raw=stream.getvalue()
            excluded.append({'index':index,'original_block':copy.deepcopy(block),'checks':checks,
                'crop_sha256':hashlib.sha256(raw).hexdigest(),'crop_png_base64':base64.b64encode(raw).decode()})
        else:remap[index]=len(retained);retained.append(copy.deepcopy(block))
    result=copy.deepcopy(diagram)
    for node in result['nodes']:
        node['label_block']=remap[node['label_block']]
        node['blocks']=[remap[i] for i in node['blocks']]
        for mark in node.get('marks',[]):
            boxes=mark['boxes']
            bounds=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
            if any(max(bounds[0],b['b'][0])<min(bounds[2],b['b'][2]) and
                   max(bounds[1],b['b'][1])<min(bounds[3],b['b'][3]) for b in retained):
                result.setdefault('review',[]).append({'code':'flow-dots-overlap-original-text','node':node['id']})
                continue
            # This token denotes exactly the three source points. It is not
            # model text, completion of the list, or a replacement for ink.
            index=len(retained)
            retained.append({'t':'⋮','b':bounds,'c':1.,'s':'ocr',
                'confidence_source':'source-three-vertical-dots',
                'recognition_origin':'verified-graphic-geometry','source_mark_evidence':copy.deepcopy(mark)})
            node['blocks'].append(index);mark['operator_block']=index
    actual=copy.deepcopy(models);actual.setdefault('diagnostics',{})['flow_graphics']={
        'profile':PROFILE,'original_blocks':copy.deepcopy(blocks),'original_diagram':copy.deepcopy(diagram),
        'excluded':excluded,'mask_sha256':hashlib.sha256(mask.tobytes()).hexdigest()}
    actual['diagnostics']['recognized_regions']=len(retained)
    return retained,actual,result


def arrange(blocks,width,height,diagram,options=None):
    """Read each box completely, keeping exact printed line breaks and words."""
    try:
        from . import ocr_layout
    except ImportError:
        import ocr_layout
    text='';ordered=[];spans=[];boundaries=[];seen=set();working=[];mapping=[]
    members={i for node in diagram['nodes'] for i in node['blocks']}
    for index,block in enumerate(blocks):
        if index not in members:working.append(copy.deepcopy(block));mapping.append([index])
    for node in diagram['nodes']:
        working.append({'t':'\n'.join(blocks[i]['t'] for i in node['blocks']),
                        'b':node['box'],'c':min(blocks[i]['c'] for i in node['blocks']),'s':'ocr'})
        mapping.append(node['blocks'])
    result=ocr_layout.arrange(working,width,height,options,include_writing_modes=True)
    prior=0;previous_group=False
    for span in result['text_spans']:
        indices=mapping[span['block']];is_group=bool(set(indices)&members)
        gap=result['text'][prior:span['start']]
        if text and is_group and previous_group:gap='\n\n'
        if gap:
            boundaries.append({'offset':len(text),'kind':'region','separator':gap});text+=gap
        for number,index in enumerate(indices):
            if index in seen:raise ValueError('diagram source region duplicated')
            if number:text+='\n'
            start=len(text);text+=blocks[index]['t']
            spans.append({**span,'start':start,'end':len(text),'block':index,'box':blocks[index]['b']})
            ordered.append({**copy.deepcopy(blocks[index]),'id':index});seen.add(index)
        previous_group=is_group;prior=span['end']
    if seen!=set(range(len(blocks))):raise ValueError('diagram source region unassigned')
    return {'text':text,'blocks':ordered,'raw_blocks':copy.deepcopy(blocks),'text_spans':spans,
        'layout':{'version':ocr_layout.VERSION,'writing_mode':'horizontal-ltr','evidence':PROFILE,
                  'review':result['layout']['review'],'boundaries':boundaries,'offset_unit':'unicode-codepoint','mapping_precision':'block',
                  'flow_diagrams':[copy.deepcopy(diagram)]}}
