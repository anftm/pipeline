"""Recover ruled table cells and reading order without rewriting recognized text."""
from __future__ import annotations

import hashlib
import statistics

PROFILE = 'ruled-table-geometry-v3-sparse-rows-rotated'


def clusters(values, tolerance):
    groups = []
    for position, weight in sorted(values):
        if groups and position-groups[-1][-1][0] <= tolerance:
            groups[-1].append((position,weight))
        else:
            groups.append([(position,weight)])
    return [sum(p*w for p,w in group)/sum(w for _,w in group) for group in groups]


def detect(image, blocks):
    return detect_dense(image, blocks) or detect_sparse(image, blocks)


def detect_dense(image, blocks):
    """Admit a regular ruled grid only with independently visible long columns.

    Every boundary records pixel support; open edges are inferred from repeated
    rows and long outer rules. Merged cells require missing internal rules and a
    rectangular union. Ambiguous nonrectangular unions stay explicit for review.
    """
    import cv2
    import numpy as np
    gray = np.asarray(image.convert('L'))
    height,width = gray.shape
    segments = cv2.createLineSegmentDetector().detect(gray)[0]
    if segments is None:
        return None
    vertical = []
    for x0,y0,x1,y1 in segments.reshape(-1,4):
        # Intersections split an otherwise continuous rule into cell-height
        # segments. Keep those segments; the full repeated grid below provides
        # the independent evidence that they are table rules.
        if abs(y1-y0) >= height*.04 and abs(x1-x0) <= width*.012:
            vertical.append([float(x0),float(y0),float(x1),float(y1)])
    if len(vertical) < 6:
        return None
    columns = clusters([((s[0]+s[2])/2,abs(s[3]-s[1])) for s in vertical],max(4,width*.008))
    columns = [x for x in columns if sum(abs(s[3]-s[1]) for s in vertical
                                        if abs((s[0]+s[2])/2-x)<=width*.012)>=height*.15]
    if len(columns) < 6:
        return None
    # An apparatus drawing has several short pipes but no repeated column
    # boundaries running through the body. Require independent long support.
    if sum(sum(abs(s[3]-s[1]) for s in vertical if abs((s[0]+s[2])/2-x)<=width*.012)
           >=height*.35 for x in columns)<4:
        return None
    ink = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,31,15)
    hmask = cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((1,max(3,round(width*.01))),np.uint8))
    hmask = cv2.morphologyEx(hmask,cv2.MORPH_OPEN,np.ones((1,max(20,round(width*.055))),np.uint8))
    vmask = cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((max(3,round(height*.007)),1),np.uint8))
    vmask = cv2.morphologyEx(vmask,cv2.MORPH_OPEN,np.ones((max(20,round(height*.035)),1),np.uint8))
    # Short dashed cell borders do not survive the long kernels used to locate
    # the overall grid. Use separate short segments to decide individual joins.
    hcell = cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(6,round(width*.012))),np.uint8))
    vcell = cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(6,round(height*.008)),1),np.uint8))
    for block in blocks:
        x0,y0,x1,y1=[round(v*(width if i%2==0 else height)) for i,v in enumerate(block['b'])]
        hcell[max(0,y0):min(height,y1),max(0,x0):min(width,x1)]=0
        vcell[max(0,y0):min(height,y1),max(0,x0):min(width,x1)]=0
    left,right = round(columns[0]),round(columns[-1])
    projection = (hmask[:,left:right]>0).sum(axis=1)
    peaks = clusters([(float(i),float(projection[i])) for i in np.flatnonzero(projection>(right-left)*.12)],6)
    if len(peaks) < 8:
        return None
    best = None
    # Repeated row spacing rejects text strokes that survived line extraction.
    for i,anchor in enumerate(peaks):
        for other in peaks[i+1:]:
            step = other-anchor
            if not height*.018 <= step <= height*.09:
                continue
            aligned = [(round((p-anchor)/step),p) for p in peaks
                       if abs((p-anchor)/step-round((p-anchor)/step))*step <= step*.1]
            by_row = dict(aligned)
            if len(by_row) < 8:
                continue
            runs=[]
            for number in sorted(by_row):
                if runs and number == runs[-1][-1]+1:
                    runs[-1].append(number)
                else:
                    runs.append([number])
            run=max(runs,key=len)
            if len(run)<8:
                continue
            found=[by_row[n] for n in run]
            score=(len(found),sum(projection[round(p)] for p in found))
            if best is None or score>best[0]:
                best=(score,found)
    if best is None:
        return None
    ys=best[1]
    # The top header can precede the repeated body by a different-height band.
    preceding=[p for p in peaks if 0<ys[0]-p<1.4*statistics.median(np.diff(ys))]
    if preceding:
        ys=[max(preceding),*ys]
    y0,y1=round(ys[0]),round(ys[-1])
    edge_pixels=np.flatnonzero((hmask[max(0,y0-4):y0+5]>0).any(axis=0))
    if not len(edge_pixels):
        return None
    xs=columns
    if edge_pixels[0]<xs[0]-5:
        xs=[float(edge_pixels[0]),*xs]
    if edge_pixels[-1]>xs[-1]+5:
        xs=[*xs,float(edge_pixels[-1])]
    # Require repeated rule intersections, not just aligned prose columns.
    if len(xs)<7 or len(ys)<9:
        return None
    nr,nc=len(ys)-1,len(xs)-1
    def at(row,col):
        return [b for b in blocks if xs[col]/width<=(b['b'][0]+b['b'][2])/2<xs[col+1]/width
                and ys[row]/height<=(b['b'][1]+b['b'][3])/2<ys[row+1]/height]
    numeric_rows=[r for r in range(nr) if any(b['t'].strip().isdigit() for b in at(r,0))]
    body_start=min(numeric_rows) if len(numeric_rows)>=4 else nr
    body_end=max(numeric_rows) if numeric_rows else -1
    # A differently partitioned result/calculation area below the numbered
    # body is its own region, not an extension of the repeated numeric grid.
    if body_end>=0 and body_end+2<len(ys):
        ys=ys[:body_end+2]
        nr=len(ys)-1
    parents=list(range(nr*nc))
    def find(n):
        while parents[n]!=n:
            parents[n]=parents[parents[n]];n=parents[n]
        return n
    def merge(a,b):
        parents[find(a)]=find(b)
    edges=[]
    def crosses(axis,position,start,stop):
        return any(block['b'][axis]+.25*(block['b'][axis+2]-block['b'][axis])<position
                   <block['b'][axis+2]-.25*(block['b'][axis+2]-block['b'][axis])
                   and start<(block['b'][1-axis]+block['b'][3-axis])/2<stop for block in blocks)
    def occupied(row,col):
        return bool(at(row,col))
    for row in range(nr):
        for col in range(nc):
            here=row*nc+col
            if col+1<nc:
                x=round(xs[col+1]);lo,hi=round(ys[row]+4),round(ys[row+1]-4)
                support=float(((vcell[lo:hi,max(0,x-6):x+7]>0)|(vmask[lo:hi,max(0,x-6):x+7]>0)).any(axis=1).mean()) if hi>lo else 1.
                edges.append({'row':row,'column':col+1,'axis':'vertical','support':round(support,4)})
                bridging=crosses(0,x/width,ys[row]/height,ys[row+1]/height)
                separate_text=occupied(row,col) and occupied(row,col+1)
                if support<.12 and (bridging or not separate_text):
                    merge(here,here+1)
            if row+1<nr:
                y=round(ys[row+1]);lo,hi=round(xs[col]+4),round(xs[col+1]-4)
                support=float(((hcell[max(0,y-5):y+6,lo:hi]>0)|(hmask[max(0,y-5):y+6,lo:hi]>0)).any(axis=0).mean()) if hi>lo else 1.
                edges.append({'row':row+1,'column':col,'axis':'horizontal','support':round(support,4)})
                bridging=crosses(1,y/height,xs[col]/width,xs[col+1]/width)
                separate_text=occupied(row,col) and occupied(row+1,col)
                inside_body=body_start<=row<body_end
                if support<.12 and not inside_body and (bridging or not separate_text or row<body_start):
                    merge(here,here+nc)
    unions={}
    for row in range(nr):
        for col in range(nc):unions.setdefault(find(row*nc+col),[]).append((row,col))
    cells,review=[],[]
    for members in unions.values():
        a,b=min(r for r,c in members),min(c for r,c in members)
        c,d=max(r for r,c in members)+1,max(c for r,c in members)+1
        rectangular=len(members)==(c-a)*(d-b)
        if not rectangular:
            review.append({'code':'nonrectangular-cell-union','members':[list(p) for p in members]})
            parts=[(r,col,r+1,col+1) for r,col in members]
        else:parts=[(a,b,c,d)]
        for a,b,c,d in parts:
            cells.append({'row':a,'column':b,'rowspan':c-a,'colspan':d-b,
                          'box':[xs[b]/width,ys[a]/height,xs[d]/width,ys[c]/height],
                          'blocks':[],'boundary_status':'supported' if rectangular else 'review'})
    cells.sort(key=lambda c:(c['row'],c['column']))
    for index,block in enumerate(blocks):
        x=(block['b'][0]+block['b'][2])/2;y=(block['b'][1]+block['b'][3])/2
        cell=next((c for c in cells if c['box'][0]<=x<c['box'][2] and c['box'][1]<=y<c['box'][3]),None)
        if cell is not None:cell['blocks'].append(index)
    for cell in cells:
        cell['blocks'].sort(key=lambda i:(round(blocks[i]['b'][1]*height/8),blocks[i]['b'][0]))
        cell['text']='\n'.join(blocks[i]['t'] for i in cell['blocks'])
    actual=[int(next(b['t'].strip() for b in at(row,0) if b['t'].strip().isdigit())) for row in numeric_rows]
    expected=list(range(actual[0],actual[0]+len(actual))) if actual else []
    if actual!=expected:
        review.append({'code':'nonsequential-table-row-labels','labels':actual})
    # A table header crossed by an oblique rule has its own text directions.
    diagonal=[]
    for x0,y0,x1,y1 in segments.reshape(-1,4):
        if (abs(x1-x0)>width*.012 and abs(y1-y0)>height*.012
                and min(y0,y1)>=ys[0] and max(y0,y1)<=ys[min(body_start,len(ys)-1)]+5
                and min(x0,x1)>=xs[0] and max(x0,x1)<=xs[-1]):
            diagonal.append([float(x0)/width,float(y0)/height,float(x1)/width,float(y1)/height])
    if len(diagonal)>=2:
        review.append({'code':'diagonal-table-header','segments':diagonal})
    return {'profile':PROFILE,'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),
            'box':[xs[0]/width,ys[0]/height,xs[-1]/width,ys[-1]/height],
            'columns':[x/width for x in xs],'rows':[y/height for y in ys],
            'cells':cells,'edges':edges,'body_rows':[body_start,body_end],
            'review':review,'character_accuracy':'unreviewed'}


def detect_sparse(image, blocks):
    """Recover narrow tables and unruled rows from long boundaries and text bands."""
    import cv2
    import numpy as np
    gray = np.asarray(image.convert('L'))
    height, width = gray.shape
    found = cv2.createLineSegmentDetector().detect(gray)[0]
    if found is None:
        return None
    segments = found.reshape(-1, 4)
    vertical = [s for s in segments if abs(s[1]-s[3])>=height*.06 and abs(s[0]-s[2])<=width*.012]
    horizontal = [s for s in segments if abs(s[0]-s[2])>=width*.06 and abs(s[1]-s[3])<=height*.018]
    xs = clusters([((s[0]+s[2])/2, abs(s[3]-s[1])) for s in vertical], max(4, width*.006))
    xs = [float(x) for x in xs if sum(abs(s[3]-s[1]) for s in vertical
                                     if abs((s[0]+s[2])/2-x)<=width*.008)>=height*.45]
    if len(xs)<2:
        return None
    ys = clusters([((s[1]+s[3])/2, abs(s[2]-s[0])) for s in horizontal], max(4, height*.007))
    rules = []
    for y in ys:
        lines = [s for s in horizontal if abs((s[1]+s[3])/2-y)<=height*.01]
        if sum(abs(s[0]-s[2]) for s in lines)<width*.35:
            continue
        rules.append((float(y), float(min(min(s[0],s[2]) for s in lines)),
                      float(max(max(s[0],s[2]) for s in lines))))
    wide = [r for r in rules if r[2]-r[1]>=width*.55]
    if len(wide)<3 or wide[-1][0]-wide[0][0]<height*.4:
        return None
    top, bottom = wide[0][0], wide[-1][0]
    left, right = min(wide[0][1],wide[-1][1]), max(wide[0][2],wide[-1][2])
    xs = [x for x in xs if left+width*.025<x<right-width*.025]
    if not 2<=len(xs)<=6:
        return None
    xs = [left, *xs, right]
    ys = [r[0] for r in rules if top<=r[0]<=bottom]
    if len(ys)<3:
        return None
    # Long unruled body bands need repeated independently positioned text in
    # multiple columns. Do not infer rows from a dictionary or expected values.
    inferred = []
    for lo, hi in zip(ys, ys[1:]):
        selected = [b for b in blocks if left/width<(b['b'][0]+b['b'][2])/2<right/width
                    and lo/height<(b['b'][1]+b['b'][3])/2<hi/height]
        # Printed columns can slope across a scan. Anchor candidate rows in a
        # single well-populated column, then require support from other columns.
        by_column = [[b for b in selected if xs[c]<(b['b'][0]+b['b'][2])*width/2<xs[c+1]]
                     for c in range(len(xs)-1)]
        def regularity(col):
            candidates = sorted((b['b'][1]+b['b'][3])*height/2 for b in col
                                if (b['b'][3]-b['b'][1])*height<height*.05)
            if len(candidates)<5:
                return (-1, 0)
            gaps = np.diff(candidates)
            step = float(np.median(gaps))
            return (-float(np.median(np.abs(gaps-step)) + np.max(np.abs(gaps-step)))/max(step,1), len(candidates))
        anchor = max(by_column, key=regularity, default=[])
        centers = clusters([((b['b'][1]+b['b'][3])*height/2, 1) for b in anchor
                            if (b['b'][3]-b['b'][1])*height<height*.05], max(3, height*.008))
        bands = []
        for center in centers:
            members = [b for b in selected if abs((b['b'][1]+b['b'][3])*height/2-center)<=height*.016]
            columns = {next((c for c in range(len(xs)-1)
                            if xs[c]<(b['b'][0]+b['b'][2])*width/2<xs[c+1]), -1) for b in members}
            if len(columns)>=3:
                bands.append(center)
        if hi-lo>height*.22 and len(bands)>=5:
            gaps = np.diff(bands)
            step = float(np.median(gaps))
            if max(gaps)>step*1.7 or min(gaps)<step*.5:
                return None
            inferred.extend((a+b)/2 for a,b in zip(bands,bands[1:]))
    if any(b-a>height*.3 for a,b in zip(ys,ys[1:])) and not inferred:
        return None
    ys = sorted([*ys, *inferred])
    if len(ys)<6:
        return None
    # Missing rules only join header slots. Inferred body rows still represent
    # separate logical cells even though the original has no horizontal lines.
    first_body = next((r for r in range(len(ys)-1) if ys[r+1] in inferred), len(ys)-1)
    ink = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,31,15)
    vmask = cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(8,round(height*.025)),1),np.uint8))
    hmask = cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,max(8,round(width*.025))),np.uint8))
    nr,nc=len(ys)-1,len(xs)-1
    parents=list(range(nr*nc))
    def find(slot):
        while parents[slot]!=slot:
            parents[slot]=parents[parents[slot]]
            slot=parents[slot]
        return slot
    def merge(a,b):
        parents[find(a)]=find(b)
    edges=[]
    if inferred:
        for row in range(first_body):
            for col in range(nc):
                if col+1<nc:
                    x=round(xs[col+1]);lo,hi=round(ys[row]+3),round(ys[row+1]-3)
                    support=float((vmask[lo:hi,max(0,x-6):x+7]>0).any(axis=1).mean()) if hi>lo else 1.
                    short_rules=[s for s in segments if abs(s[3]-s[1])>=height*.02
                                 and abs(s[2]-s[0])<=width*.012 and abs((s[0]+s[2])/2-x)<=width*.007]
                    support=float(max(support,min(1.,sum(max(0,min(hi,max(s[1],s[3]))-max(lo,min(s[1],s[3])))
                                                         for s in short_rules)/max(1,hi-lo))))
                    edges.append({'axis':'vertical','row':row,'column':col+1,'support':round(support,4)})
                    if support<.12:merge(row*nc+col,row*nc+col+1)
                if row+1<first_body:
                    y=round(ys[row+1]);lo,hi=round(xs[col]+4),round(xs[col+1]-4)
                    support=float((hmask[max(0,y-6):y+7,lo:hi]>0).any(axis=0).mean()) if hi>lo else 1.
                    edges.append({'axis':'horizontal','row':row+1,'column':col,'support':round(support,4)})
                    if support<.12:merge(row*nc+col,(row+1)*nc+col)
    unions={}
    for row in range(nr):
        for col in range(nc):unions.setdefault(find(row*nc+col),[]).append((row,col))
    cells = []
    review=[]
    for slots in unions.values():
        r0,c0=min(r for r,c in slots),min(c for r,c in slots)
        r1,c1=max(r for r,c in slots)+1,max(c for r,c in slots)+1
        rectangular=len(slots)==(r1-r0)*(c1-c0)
        if not rectangular:
            review.append({'code':'nonrectangular-cell-union','members':[list(p) for p in slots]})
        for row,col,end_row,end_col in ([(r0,c0,r1,c1)] if rectangular else [(r,c,r+1,c+1) for r,c in slots]):
            a,b=xs[col],xs[end_col]
            lo,hi=ys[row],ys[end_row]
            members = [i for i,block in enumerate(blocks)
                       if a/width<=(block['b'][0]+block['b'][2])/2<b/width
                       and lo/height<=(block['b'][1]+block['b'][3])/2<hi/height]
            members.sort(key=lambda i:(round(blocks[i]['b'][1]*height/8),blocks[i]['b'][0]))
            cells.append({'row':row,'column':col,'rowspan':end_row-row,'colspan':end_col-col,
                          'box':[a/width,lo/height,b/width,hi/height], 'blocks':members,
                          'text':'\n'.join(blocks[i]['t'] for i in members),
                          'boundary_status':'text-band' if lo in inferred or hi in inferred else 'supported'})
    cells.sort(key=lambda c:(c['row'],c['column']))
    return {'profile':PROFILE,'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),
            'box':[left/width,top/height,right/width,bottom/height],
            'columns':[x/width for x in xs],'rows':[y/height for y in ys],
            'cells':cells,'edges':edges, 'body_rows':[first_body if inferred else 1,len(ys)-2], 'review':review,
            'inferred_rows':[y/height for y in inferred], 'character_accuracy':'unreviewed'}


def arrange(blocks, width, height, table, options=None, *, include_writing_modes=False):
    """Emit tab-separated cells with every recognized block and coordinate intact."""
    import copy
    try:
        from . import ocr_layout
    except ImportError:
        import ocr_layout
    inside={i for cell in table['cells'] for i in cell['blocks']}
    before=[i for i,b in enumerate(blocks) if i not in inside and (b['b'][1]+b['b'][3])/2<table['box'][1]]
    after=[i for i,b in enumerate(blocks) if i not in inside and i not in before]
    text,spans,boundaries,ordered='',[],[],[]
    def gap(separator):
        nonlocal text
        if text and separator:
            boundaries.append({'offset':len(text),'kind':'table-cell' if '\t' in separator else 'region',
                               'separator':separator})
            text+=separator
    def append(index,separator,region,mode='horizontal-ltr'):
        nonlocal text
        block=blocks[index]
        if not block.get('t'):
            return
        gap(separator)
        start=len(text);text+=block['t']
        spans.append({'start':start,'end':len(text),'block':index,'box':block['b'],
                      'precision':'block','region':region,
                      **({'writing_mode':mode} if include_writing_modes else {})})
        ordered.append({**copy.deepcopy(block),'id':index})
    def ordinary(indices,offset):
        if not indices:
            return
        result=ocr_layout.arrange([blocks[i] for i in indices],width,height,options,include_writing_modes=True)
        # Adjacent labels and formulas below a table share physical baselines.
        # Give the short result area a row order while retaining the normal
        # paragraph order of subsequent notes and folios.
        if offset==2000:
            row_area=[i for i in indices if (blocks[i]['b'][1]+blocks[i]['b'][3])/2<table['box'][3]+.09]
            rest=[i for i in indices if i not in row_area]
            if row_area and len(row_area)<len(indices):
                line_height=statistics.median(blocks[i]['b'][3]-blocks[i]['b'][1] for i in row_area)
                bands=[]
                for index in sorted(row_area,key=lambda i:blocks[i]['b'][1]):
                    if bands and blocks[index]['b'][1]-min(blocks[i]['b'][1] for i in bands[-1])<line_height*.6:
                        bands[-1].append(index)
                    else:bands.append([index])
                for band_no,band in enumerate(bands):
                    for n,index in enumerate(sorted(band,key=lambda i:blocks[i]['b'][0])):
                        append(index,'\t' if n else '\n\n',offset+band_no)
                ordinary(rest,offset+len(bands)+1)
                return
        last=0
        for span in result['text_spans']:
            separator=result['text'][last:span['start']] if last else '\n\n'
            append(indices[span['block']],separator,offset+span['region'],span['writing_mode'])
            last=span['end']
    ordinary(before,0)
    previous_row=None
    previous_column=0
    for cell_no,cell in enumerate(table['cells']):
        if cell['row']!=previous_row:
            if previous_row is not None:
                gap('\t'*(len(table['columns'])-2-previous_column))
            gap('\n\n')
            # A row-spanning header leaves empty positions in subsequent rows.
            # Leading empty cells are part of the text, even on the first row.
            text+='\t'*cell['column']
        else:
            gap('\t'*(cell['column']-previous_column))
        for number,index in enumerate(cell['blocks']):
            # Separators carry geometry only, never synthesized words or zeros.
            separator='\n' if number else ''
            if number:
                previous=blocks[cell['blocks'][number-1]]
                current=blocks[index]
                overlap=min(previous['b'][3],current['b'][3])-max(previous['b'][1],current['b'][1])
                line_height=min(previous['b'][3]-previous['b'][1],current['b'][3]-current['b'][1])
                if (overlap>=.5*line_height and current['b'][0]>=previous['b'][2]-.01):
                    separator='' if ocr_layout.cjk(previous['t'][-1:]) and ocr_layout.cjk(current['t'][:1]) else ' '
            if number and blocks[index].get('table_header_side'):
                previous=blocks[cell['blocks'][number-1]]
                separator='' if previous.get('table_header_side')==blocks[index]['table_header_side'] else '\n'
            append(index,separator,1000+cell_no)
        previous_row=cell['row']
        previous_column=cell['column']
    if previous_row is not None:
        gap('\t'*(len(table['columns'])-2-previous_column))
    ordinary(after,2000)
    return {'text':text,'blocks':ordered,'raw_blocks':copy.deepcopy(blocks),'text_spans':spans,
            'layout':{'version':ocr_layout.VERSION,'writing_mode':'horizontal-ltr','evidence':'ruled-table-pixels',
                      'review':[],'regions':len(table['cells'])+len(before)+len(after),'boundaries':boundaries,
                      'offset_unit':'unicode-codepoint','mapping_precision':'block','tables':[copy.deepcopy(table)]}}
