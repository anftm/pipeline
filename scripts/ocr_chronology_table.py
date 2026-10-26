"""Keep chronological tables' dates, country spans and wrapped events in source cells."""
from __future__ import annotations

import hashlib
import copy
import re
import statistics

PROFILE='chronology-source-dates-v1-country-event-spans'
DATE=re.compile(r'[0-9]{4}(?:[\-–—][0-9]{0,4})?')
EVENT=re.compile(r'(?:[A-Z][a-z]+\s+(?:[0-9]{1,2},\s*)?)?[0-9]{4}:\s+\S')


def detect(image,blocks):
    import cv2
    import numpy as np
    try:
        from . import ocr_aligned_table,ocr_grouped_table
    except ImportError:
        import ocr_aligned_table,ocr_grouped_table
    width,height=image.size;gray=np.asarray(image.convert('L'))
    headings=[(i,b) for i,b in enumerate(blocks) if re.fullmatch(r'TABLE\s+[0-9]+(?:\.[0-9]+)+',b['t'])]
    if len(headings)!=1:return None
    _,title=headings[0]
    footers=[b for b in blocks if b['t'].startswith('Note:') and b['b'][1]>title['b'][3]]
    if not footers:return None
    stop=min(b['b'][1] for b in footers)
    dates=[(i,b) for i,b in enumerate(blocks) if DATE.fullmatch(b['t']) and title['b'][3]<b['b'][1]<stop]
    if len(dates)<10:return None
    clusters=ocr_aligned_table.cluster([(b['b'][0]+b['b'][2])/2 for i,b in dates],.025)
    anchors=[statistics.median(c) for c in clusters if len(c)>=2]
    if not 2<=len(anchors)<=4:return None
    if sum(any(abs((b['b'][0]+b['b'][2])/2-x)<.025 for x in anchors) for i,b in dates)!=len(dates):return None
    first=min(b['b'][1] for i,b in dates)
    event_candidates=[(i,b) for i,b in enumerate(blocks) if re.match(r'[0-9]{4}:\s+\S',b['t'])
                      and b['b'][0]>anchors[-1]+.01 and first-.02<b['b'][1]<stop]
    if len(event_candidates)<5:return None
    if max(b['b'][0] for i,b in event_candidates)-min(b['b'][0] for i,b in event_candidates)>.02:return None
    event_left=statistics.median(b['b'][0] for i,b in event_candidates)
    selected=[(i,b) for i,b in enumerate(blocks) if b['b'][1]>=first-.005
              and (b['b'][1]+b['b'][3])/2<stop
              and b['b'][0]>title['b'][0]-.015]
    labels=[(i,b) for i,b in selected if b['b'][2]<anchors[0]-.025
            and re.fullmatch(r'[A-Za-z][A-Za-z ,’\-]*',b['t'])]
    if len(labels)<5:return None
    country_left=min(b['b'][0] for i,b in labels)
    country_right=max(b['b'][2] for i,b in labels)
    date_left=min(b['b'][0] for i,b in dates if abs((b['b'][0]+b['b'][2])/2-anchors[0])<.025)
    if country_right>=date_left:return None
    xs=[min(country_left,title['b'][0]),(country_right+date_left)/2]
    for a,b in zip(anchors,anchors[1:]):
        lo=max(p['b'][2] for i,p in dates if abs((p['b'][0]+p['b'][2])/2-a)<.025)
        hi=min(p['b'][0] for i,p in dates if abs((p['b'][0]+p['b'][2])/2-b)<.025)
        if lo>=hi:return None
        xs.append((lo+hi)/2)
    last_right=max(b['b'][2] for i,b in dates if abs((b['b'][0]+b['b'][2])/2-anchors[-1])<.025)
    xs.extend([(last_right+event_left)/2,max(b['b'][2] for i,b in selected)])
    if any(a>=b for a,b in zip(xs,xs[1:])):return None
    # Independently visible table rule and spanning-header underline delimit
    # header rows. Date vocabulary alone cannot establish a table.
    ink=cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,51,15)
    mask=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((1,max(3,round(width*.006))),np.uint8))
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((1,max(20,round(width*.1))),np.uint8))
    _,_,stats,_=cv2.connectedComponentsWithStats(mask)
    rules=[list(map(int,s[:4])) for s in stats[1:] if s[2]>(xs[-1]-xs[0])*width*.8
           and s[3]<height*.02 and title['b'][3]<s[1]/height<first]
    if len(rules)<2:return None
    top,bottom=min(rules,key=lambda r:r[1]),max(rules,key=lambda r:r[1])
    header_top=(top[1]+top[3]/2)/height;body_top=(bottom[1]+bottom[3])/height
    if body_top-header_top<.04:return None
    underlines=[]
    # Detect shared underlines relative to this table, not the width of a
    # two-page spread (a valid narrow header must not be filtered out).
    table_left=max(0,round(xs[0]*width));table_right=min(width,round(xs[-1]*width))
    for local_proof in ocr_grouped_table.source_underlines(gray[:,table_left:table_right]):
        proof=copy.deepcopy(local_proof)
        proof['line_pixels'][0]+=table_left;proof['line_pixels'][2]+=table_left
        for segment in proof['segments_pixels']:segment[0]+=table_left;segment[2]+=table_left
        x,y,u,v=proof['line_pixels'];mid=(y+v)/2/height
        if not header_top+.01<mid<body_top-.01:continue
        cols=[c+1 for c,anchor in enumerate(anchors) if x/width<=anchor<=u/width]
        if len(cols)<2:continue
        span=(min(cols),max(cols)+1)
        if cols!=list(range(*span)):continue
        members=[i for i,b in enumerate(blocks) if x/width<=(b['b'][0]+b['b'][2])/2<=u/width
                 and header_top<=(b['b'][1]+b['b'][3])/2<mid]
        if not members:continue
        if any(span==old[0] and members==old[2] and abs(mid-old[1])*height<=max(4,height*.003)
               for old in underlines):continue
        underlines.append((span,mid,members,proof))
    if len(underlines)!=1:return None
    span,split,shared,proof=underlines[0]
    # Country names span subsequent events; an indented wrapped label stays
    # its own entry rather than being filled with an assumed country name.
    typical_height=statistics.median(b['b'][3]-b['b'][1] for i,b in labels)
    country_anchors=[(i,b) for i,b in labels if abs(b['b'][0]-country_left)<.005]
    if len(country_anchors)<5:return None
    events=[(i,b) for i,b in selected if (b['b'][0]+b['b'][2])/2>=xs[-2]]
    if len(events)<10:return None
    # Source date-prefixed paragraphs give row boundaries, not an assumed
    # historical timeline or a blank line between tightly set events.
    event_groups=[]
    for i,b in sorted(events,key=lambda item:(item[1]['b'][1],item[1]['b'][0])):
        if EVENT.match(b['t']):
            event_groups.append([i])
        elif event_groups:event_groups[-1].append(i)
        else:return None
    row_tops=[min(blocks[i]['b'][1] for i in group) for group in event_groups]
    for i,b in dates+country_anchors:
        nearest=min(range(len(row_tops)),key=lambda n:abs(row_tops[n]-b['b'][1]))
        if abs(row_tops[nearest]-b['b'][1])>typical_height*.8:return None
    row_breaks=[(max(blocks[i]['b'][3] for i in a)+min(blocks[i]['b'][1] for i in b))/2
                for a,b in zip(event_groups,event_groups[1:])]
    ys=[header_top,split,body_top,*row_breaks,stop]
    if any(a>=b for a,b in zip(ys,ys[1:])):return None
    label_groups=[]
    for i,b in sorted(labels,key=lambda item:item[1]['b'][1]):
        if (label_groups and b['b'][0]>country_left+.005 and
                b['b'][1]-max(blocks[j]['b'][3] for j in label_groups[-1])<typical_height*.6):
            label_groups[-1].append(i)
        else:label_groups.append([i])
    label_start=[]
    for group in label_groups:
        center=(blocks[group[0]]['b'][1]+blocks[group[0]]['b'][3])/2
        row=next((n for n,(lo,hi) in enumerate(zip(ys,ys[1:])) if lo<=center<hi),None)
        if row is None or row<2:return None
        label_start.append((row,group))
    if len({r for r,g in label_start})!=len(label_start):return None
    cells=[];owned=set()
    for row in range(len(ys)-1):
        for col in range(len(xs)-1):
            if row==1 and not span[0]<=col<span[1]:continue
            if row==0 and span[0]<col<span[1]:continue
            if col==0 and row>=2:
                entry=next(((at,group) for at,(start,group) in enumerate(label_start) if start==row),None)
                if entry is None:continue
                at,members=entry;end_row=label_start[at+1][0] if at+1<len(label_start) else len(ys)-1
                end_col=1
            else:
                end_row=2 if row==0 and not span[0]<=col<span[1] else row+1
                end_col=span[1] if row==0 and col==span[0] else col+1
                members=[i for i,b in enumerate(blocks) if xs[col]<=(b['b'][0]+b['b'][2])/2<xs[end_col]
                         and ys[row]<=(b['b'][1]+b['b'][3])/2<ys[end_row]]
                if col==0:members=[]
            if owned.intersection(members):return None
            owned.update(members)
            cells.append({'row':row,'column':col,'rowspan':end_row-row,'colspan':end_col-col,
                'box':[xs[col],ys[row],xs[end_col],ys[end_row]],'blocks':members,
                'text':'\n'.join(blocks[i]['t'] for i in members),'boundary_status':'source-chronological-event-bands'})
    # Keep the Country header even though its body label cells are row spans.
    header_cell=next(c for c in cells if c['row']==0 and c['column']==0)
    header_cell['blocks']=[i for i,b in enumerate(blocks) if xs[0]<=(b['b'][0]+b['b'][2])/2<xs[1]
                           and header_top<=(b['b'][1]+b['b'][3])/2<body_top]
    header_cell['text']='\n'.join(blocks[i]['t'] for i in header_cell['blocks']);owned.update(header_cell['blocks'])
    expected={i for i,b in enumerate(blocks) if xs[0]<=(b['b'][0]+b['b'][2])/2<xs[-1]
              and header_top<=(b['b'][1]+b['b'][3])/2<stop}
    if owned!=expected:return None
    review=[]
    for cell in cells:
        if cell['row']<2 or cell['blocks']:continue
        x,y,u,v=[round(a*(width if axis%2==0 else height)) for axis,a in enumerate(cell['box'])]
        x,y,u,v=x+2,y+2,u-2,v-2
        if min(u-x,v-y)<3:continue
        _,_,components,_=cv2.connectedComponentsWithStats((gray[y:v,x:u]<180).astype(np.uint8))
        significant=[list(map(int,c)) for c in components[1:] if c[4]>=4 and c[2]>=3
                     and not(c[2]>=(u-x)*.8 and c[3]<=5)]
        if significant:review.append({'code':'chronology-empty-cell-with-ink','row':cell['row'],
                                      'column':cell['column'],'components':significant})
    return {'profile':PROFILE,'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),
        'box':[xs[0],ys[0],xs[-1],stop],'rows':ys,'columns':xs,'cells':cells,'review':review,
        'body_rows':[2,len(ys)-2],'character_accuracy':'unreviewed',
        'evidence':{'date_columns':anchors,'event_paragraph_blocks':event_groups,
            'country_label_blocks':label_groups,'shared_header':{'columns':list(span),'blocks':shared,'source_underline':proof},
            'header_rules':[top,bottom]}}


def arrange(blocks,width,height,table,options=None):
    """Keep the complete table in its source-page column, with every span mapped."""
    try:
        from . import ocr_table_geometry,ocr_layout
    except ImportError:
        import ocr_table_geometry,ocr_layout
    members=sorted(i for cell in table['cells'] for i in cell['blocks'])
    if len(set(members))!=len(members):raise ValueError('chronology table source member duplicated')
    indices={index:i for i,index in enumerate(members)}
    grid=copy.deepcopy(table)
    for cell in grid['cells']:cell['blocks']=[indices[i] for i in cell['blocks']]
    local=ocr_table_geometry.arrange([blocks[i] for i in members],width,height,grid,options,include_writing_modes=True)
    others=[i for i in range(len(blocks)) if i not in indices]
    working=[copy.deepcopy(blocks[i]) for i in others]
    table_index=len(working)
    working.append({'t':local['text'],'b':table['box'],'s':'ocr','c':1})
    result=ocr_layout.arrange(working,width,height,options,include_writing_modes=True)
    spans=[];ordered=[]
    for span in result['text_spans']:
        if span['block']==table_index:
            for child in local['text_spans']:
                index=members[child['block']]
                spans.append({**child,'start':child['start']+span['start'],'end':child['end']+span['start'],'block':index})
                ordered.append({**copy.deepcopy(blocks[index]),'id':index})
        else:
            index=others[span['block']];spans.append({**span,'block':index})
            ordered.append({**copy.deepcopy(blocks[index]),'id':index})
    if sorted(s['block'] for s in spans)!=[i for i,b in enumerate(blocks) if b.get('t')]:
        raise ValueError('chronology table text dropped or duplicated')
    return {**result,'raw_blocks':copy.deepcopy(blocks),'blocks':ordered,'text_spans':spans,
        'layout':{**result['layout'],'evidence':PROFILE,'tables':[copy.deepcopy(table)]}}
