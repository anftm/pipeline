"""Recover horizontal-rule statistical tables from independent repeated column rows."""
from __future__ import annotations

import copy
import hashlib
import re
import statistics

PROFILE='aligned-statistical-tables-v2-preserved-cell-ownership'


def numeric(text):
    return bool(re.fullmatch(r'[\d.,%\-—–\s⁰¹²³⁴⁵⁶⁷⁸⁹ᵃᵇᶜᵈᵉᶠa-fª°]+',text) and any(c.isdigit() for c in text))


def cluster(values,tolerance):
    groups=[]
    for position in sorted(values):
        if groups and position-statistics.median(groups[-1])<=tolerance:groups[-1].append(position)
        else:groups.append([position])
    return groups


def detect(image,blocks):
    import cv2
    import numpy as np
    width,height=image.size
    gray=np.asarray(image.convert('L'))
    ink=cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY_INV,51,15)
    mask=cv2.morphologyEx(ink,cv2.MORPH_CLOSE,np.ones((1,max(3,round(width*.006))),np.uint8))
    mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((1,max(20,round(width*.20))),np.uint8))
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
    rules=[list(map(int,s[:4])) for s in stats[1:] if s[2]>width*.4 and s[3]<height*.04]
    headings=sorted((b for b in blocks if re.fullmatch(r'TABLE\s+\d+(?:\.\d+)+',b['t'])),key=lambda b:b['b'][1])
    tables=[]
    for heading in headings:
        start=heading['b'][3]
        next_heading=min((h['b'][1] for h in headings if h['b'][1]>start),default=1.)
        sources=[b for b in blocks if re.match(r'Source\s*:',b['t']) and start<b['b'][1]<next_heading]
        if not sources:continue
        stop=min(b['b'][1] for b in sources)
        selected=[(i,b) for i,b in enumerate(blocks) if start<(b['b'][1]+b['b'][3])/2<stop]
        numbers=[(i,b) for i,b in selected if numeric(b['t'])]
        if len(numbers)<24:continue
        # A table needs at least three independently recurring numeric columns.
        xgroups=cluster([(b['b'][0]+b['b'][2])/2 for i,b in numbers],.022)
        anchors=[statistics.median(g) for g in xgroups if len(g)>=5]
        if len(anchors)<3:continue
        rows=cluster([(b['b'][1]+b['b'][3])/2 for i,b in numbers],.008)
        good=[]
        for band in rows:
            center=statistics.median(band)
            present={min(range(len(anchors)),key=lambda c:abs((b['b'][0]+b['b'][2])/2-anchors[c]))
                     for i,b in numbers if abs((b['b'][1]+b['b'][3])/2-center)<.008}
            if len(present)>=3:good.append(center)
        if len(good)<5:continue
        serials=sorted((b for i,b in selected if re.fullmatch(r'\d{1,2}\.',b['t'])
                        and b['b'][0]<anchors[0]+.04),key=lambda b:b['b'][1])
        numbered=(len(serials)>=5 and [int(b['t'][:-1]) for b in serials]==list(range(1,len(serials)+1)))
        if numbered:
            good=[(b['b'][1]+b['b'][3])/2 for b in serials]
            last=serials[-1]['b'][3]
            extra=cluster([(b['b'][1]+b['b'][3])/2 for i,b in numbers if b['b'][1]>last+.003],.008)
            good.extend(statistics.median(band) for band in extra if len(band)>=2)
            body_start=serials[0]['b'][1]-.003
        else:body_start=good[0]-.012
        nearby=[r for r in rules if start<r[1]/height<body_start]
        if not nearby:continue
        top=min(nearby,key=lambda r:r[1])
        left,right=top[0]/width,(top[0]+top[2])/width
        if right-left<.5:continue
        body=[(i,b) for i,b in selected if body_start<(b['b'][1]+b['b'][3])/2<stop]
        # Non-numeric row labels (Country/Item) establish their own persistent
        # column. Do not infer missing labels or cells from expected vocabulary.
        labels_found=[b for i,b in body if not numeric(b['t']) and re.search('[A-Za-z]',b['t'])
                      and b['b'][2]-b['b'][0]<.18]
        for group in cluster([b['b'][0] for b in labels_found],.025):
            if len(group)<5:continue
            edge=statistics.median(group)
            items=[b for b in labels_found if abs(b['b'][0]-edge)<.025]
            center=statistics.median((b['b'][0]+b['b'][2])/2 for b in items)
            if all(abs(center-a)>.045 for a in anchors):anchors.append(center)
        anchors.sort()
        if not 4<=len(anchors)<=10:continue
        # Boundaries fall in repeatedly empty space, not arbitrary center gaps.
        xs=[left]
        for a,b in zip(anchors,anchors[1:]):
            first=[p['b'][2] for i,p in body if abs((p['b'][0]+p['b'][2])/2-a)<.04]
            second=[p['b'][0] for i,p in body if abs((p['b'][0]+p['b'][2])/2-b)<.04]
            lo=max(first,default=a);hi=min(second,default=b)
            xs.append((lo+hi)/2 if lo<hi else (a+b)/2)
        xs.append(right)
        if any(a>=b for a,b in zip(xs,xs[1:])):continue
        # Rows follow numeric baselines, allowing a scanned page-break gap and
        # wrapped prose labels without inventing zeroes for blank data cells.
        if numbered:
            boundaries=[b['b'][1]-.003 for b in serials[1:]]
            boundaries.extend((a+b)/2 for a,b in zip(good[len(serials)-1:],good[len(serials):]))
        else:boundaries=[(a+b)/2 for a,b in zip(good,good[1:])]
        ys=[top[1]/height,body_start,*boundaries,stop]
        if any(a>=b for a,b in zip(ys,ys[1:])):continue
        cells=[];review=[]
        for row,(a,b) in enumerate(zip(ys,ys[1:])):
            for col,(lo,hi) in enumerate(zip(xs,xs[1:])):
                members=[i for i,p in selected if lo<=(p['b'][0]+p['b'][2])/2<hi
                         and a<=(p['b'][1]+p['b'][3])/2<b]
                members.sort(key=lambda i:(round(blocks[i]['b'][1]*height/8),blocks[i]['b'][0]))
                cells.append({'row':row,'column':col,'rowspan':1,'colspan':1,'box':[lo,a,hi,b],
                              'blocks':members,'boundary_status':'pixel-rule-and-repeated-column',
                              'text':'\n'.join(blocks[i]['t'] for i in members)})
        # A wide heading separated above every individual column label forms
        # its own spanning header row. It is never repeated in child cells.
        spanning=[]
        for i,p in selected:
            if not ys[0]<=(p['b'][1]+p['b'][3])/2<body_start:continue
            if any(p['b'][0]+.15*(p['b'][2]-p['b'][0])<cut<p['b'][2]-.15*(p['b'][2]-p['b'][0]) for cut in xs[1:-1]):
                spanning.append(i)
        if not spanning:
            spanning=[i for i,p in selected if ys[0]<=(p['b'][1]+p['b'][3])/2<body_start
                      and p['b'][2]-p['b'][0]>.20 and len(p['t'])>20]
        if spanning:
            p=blocks[spanning[0]]
            center=(p['b'][1]+p['b'][3])/2
            following=[(b['b'][1]+b['b'][3])/2 for i,b in selected if i not in spanning
                       and p['b'][0]<(b['b'][0]+b['b'][2])/2<p['b'][2]
                       and center+.004<(b['b'][1]+b['b'][3])/2<body_start]
            if len(spanning)==1 and following:
                split=(center+min(following))/2
                ys.insert(1,split)
                first=max(0,next((c for c in range(len(xs)-1) if xs[c]<=p['b'][0]<xs[c+1]),0))
                last=min(len(xs)-1,next((c for c in range(first+1,len(xs)) if p['b'][2]<=xs[c]+.01),len(xs)-1))
                underline=[r for r in nearby if center<=(r[1]+r[3])/height<=split+.006
                           and r[0]/width<=p['b'][0]+.01 and (r[0]+r[2])/width>=p['b'][2]-.01]
                if underline:
                    rule=max(underline,key=lambda r:r[1])
                    first=min(range(len(xs)-1),key=lambda c:abs(xs[c]-rule[0]/width))
                    last=min(range(first+1,len(xs)),key=lambda c:abs(xs[c]-(rule[0]+rule[2])/width))
                if last<=first:last=first+1
                rebuilt=[]
                for row,(a,b) in enumerate(zip(ys,ys[1:])):
                    col=0
                    while col<len(xs)-1:
                        if row==1 and not first<=col<last:
                            col+=1
                            continue
                        end=last if row==0 and col==first else col+1
                        end_row=2 if row==0 and not first<=col<last else row+1
                        members=[i for i,p in selected if xs[col]<=(p['b'][0]+p['b'][2])/2<xs[end]
                                 and a<=(p['b'][1]+p['b'][3])/2<ys[end_row]]
                        members.sort(key=lambda i:(round(blocks[i]['b'][1]*height/8),blocks[i]['b'][0]))
                        rebuilt.append({'row':row,'column':col,'rowspan':end_row-row,'colspan':end-col,
                            'box':[xs[col],a,xs[end],ys[end_row]],'blocks':members,
                            'boundary_status':'supported-shared-heading' if end-col>1 else 'repeated-column',
                            'text':'\n'.join(blocks[i]['t'] for i in members)})
                        col=end
                cells=rebuilt
            else:
                review.extend({'code':'aligned-table-header-span-review','block':i} for i in spanning)
        body_first=len(ys)-len(good)-1
        # Group labels between numeric baselines are separate rows, not part
        # of the preceding entry (e.g. Food/Nonfood in price comparisons).
        if not numbered:
            for i,p in selected:
                center=(p['b'][1]+p['b'][3])/2
                if not (body_start-.02<center<stop and p['b'][0]<xs[1] and p['b'][2]<xs[1]
                        and re.fullmatch('[A-Za-z]+',p['t'])):continue
                nearest=min(good,key=lambda pos:abs(pos-center))
                step=statistics.median(b-a for a,b in zip(good,good[1:]))
                if abs(nearest-center)<step*.55:continue
                other=[blocks[index] for c in cells if c['column']>0 and c['row']>=body_first
                       for index in c['blocks'] if abs((blocks[index]['b'][1]+blocks[index]['b'][3])/2-center)<step*.35]
                if other:continue
                owner=next((c for c in cells if i in c['blocks']),None)
                if owner is None:continue
                # Keep ambiguous category bands explicitly reviewable; their
                # labels remain in the source blocks and are never erased.
                review.append({'code':'aligned-table-category-band-review','block':i})
        # Multi-year category labels occupy a spanning cell. Keep all original
        # word fragments once, including wrapped labels such as raw materials.
        year_labels=[blocks[i]['t'] for c in cells if c['column']==1 and c['row']>=len(ys)-len(good)-1
                     for i in c['blocks'] if re.fullmatch(r'\d{4}',blocks[i]['t'])]
        if not numbered and len(xs)>2 and len(year_labels)>=6 and len(set(year_labels))<=4:
            labels=[c for c in cells if c['row']>=1+(len(ys)-len(good)-2) and c['column']==0 and c['blocks']]
            active=[]
            for cell in labels:
                rows_between=cell['row']-active[-1]['row'] if active else 0
                if active and rows_between==1:
                    prior=active[-1]
                    prev_bottom=max(blocks[i]['b'][3] for i in prior['blocks'])
                    next_top=min(blocks[i]['b'][1] for i in cell['blocks'])
                    if next_top-prev_bottom<=.012:
                        prior['blocks'].extend(cell['blocks']);cell['blocks']=[]
                        continue
                active.append(cell)
            remove=set()
            for at,cell in enumerate(active):
                end=active[at+1]['row'] if at+1<len(active) else len(ys)-1
                if end-cell['row']<2:continue
                cell['rowspan']=end-cell['row'];cell['box'][3]=ys[end]
                remove.update((r,0) for r in range(cell['row']+1,end))
            cells=[c for c in cells if (c['row'],c['column']) not in remove]
        # Preserve missing detections as reviewable empty cells. Repeated ink
        # in a numeric column is not permission to synthesize dash/zero text.
        for cell in cells:
            if cell['row']<body_first or cell['column']<1 or cell['blocks']:continue
            a,b,c,d=[round(v*(width if i%2==0 else height)) for i,v in enumerate(cell['box'])]
            if min(c-a,d-b)<3:continue
            dark=(gray[b:d,a:c]<180).astype(np.uint8)
            if dark.sum()<4:continue
            count,_,components,_=cv2.connectedComponentsWithStats(dark)
            significant=[s for s in components[1:] if s[4]>=4 and s[2]>=3 and s[3]>=1]
            significant=[s for s in significant if not(s[2]>=(c-a)*.8 and s[3]<=5)]
            if significant:
                review.append({'code':'aligned-table-empty-cell-with-ink','row':cell['row'],'column':cell['column'],
                               'components':[list(map(int,s)) for s in significant]})
        tables.append({'profile':PROFILE,'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),
            'box':[left,ys[0],right,stop],'rows':ys,'columns':xs,'cells':cells,'body_rows':[len(ys)-len(good)-1,len(ys)-2],
            'review':review,'character_accuracy':'unreviewed','heading':heading['t'],
            'evidence':{'horizontal_rules':nearby,'numeric_columns':anchors,'numeric_row_centers':good}})
    return tables


def prepare(image,blocks):
    """Refine independently detected grids without replacing any OCR text."""
    try:
        from . import ocr_grouped_table
    except ImportError:
        import ocr_grouped_table
    return [ocr_grouped_table.detect(image,blocks,grid) or grid for grid in detect(image,blocks)]


def arrange(blocks,width,height,tables,options=None):
    try:
        from . import ocr_layout,ocr_table_geometry
    except ImportError:
        import ocr_layout,ocr_table_geometry
    payload={'text':'','blocks':[],'raw_blocks':copy.deepcopy(blocks),'text_spans':[],
             'layout':{'version':ocr_layout.VERSION,'writing_mode':'horizontal-ltr','evidence':'aligned-table-pixels',
                       'review':[],'boundaries':[],'offset_unit':'unicode-codepoint','mapping_precision':'block','tables':copy.deepcopy(tables)}}
    seen=set()
    def append(indices,table=None):
        if not indices:return
        remap={index:i for i,index in enumerate(indices)}
        chosen=[blocks[i] for i in indices]
        if table is not None:
            grid=copy.deepcopy(table)
            for cell in grid['cells']:cell['blocks']=[remap[i] for i in cell['blocks']]
            result=ocr_table_geometry.arrange(chosen,width,height,grid,options,include_writing_modes=True)
        else:result=ocr_layout.arrange(chosen,width,height,options,include_writing_modes=True)
        prefix='\n\n' if payload['text'] else '';offset=len(payload['text'])+len(prefix)
        payload['text']+=prefix+result['text']
        for span in result['text_spans']:
            original=indices[span['block']]
            payload['text_spans'].append({**span,'start':span['start']+offset,'end':span['end']+offset,'block':original})
            payload['blocks'].append({**copy.deepcopy(blocks[original]),'id':original})
        seen.update(indices)
    owned=set()
    for table in tables:
        inside=[i for c in table['cells'] for i in c['blocks']]
        if (any(type(i) is not int or not 0<=i<len(blocks) for i in inside)
                or len(set(inside))!=len(inside) or owned.intersection(inside)):
            raise ValueError('aligned table block ownership is duplicated or invalid')
        owned.update(inside)
    for table in tables:
        before=[i for i,b in enumerate(blocks) if i not in seen and (b['b'][1]+b['b'][3])/2<table['box'][1]]
        append(before)
        inside=sorted(i for c in table['cells'] for i in c['blocks'])
        append(inside,table)
    append([i for i in range(len(blocks)) if i not in seen])
    payload['layout']['regions']=len(payload['text_spans'])
    return payload


def recover_dashes(image,blocks,tables):
    """Recognize repeated empty-data dashes from stable original line components."""
    import cv2
    import numpy as np
    output=copy.deepcopy(blocks);grids=copy.deepcopy(tables);reports=[]
    width,height=image.size;gray=np.asarray(image.convert('L'))
    for table in grids:
        candidates=[]
        heights=[(b['b'][3]-b['b'][1])*height for b in blocks if numeric(b['t'])
                 and table['box'][1]<b['b'][1]<table['box'][3] and len(b['t'])<=8]
        if not heights:continue
        glyph_height=statistics.median(heights)
        for cell in table['cells']:
            if cell['row']<table['body_rows'][0] or cell['column']<1:continue
            original=[blocks[i] for i in cell['blocks']]
            if original and (len(original)!=1 or original[0]['t'] not in {'一','-','—','–'}):continue
            data_rows=table['evidence'].get('numeric_row_indices')
            if data_rows is not None:
                if cell['row'] not in data_rows:continue
                offset=data_rows.index(cell['row'])
            else:offset=cell['row']-table['body_rows'][0]
            baselines=table['evidence']['numeric_row_centers']
            if offset>=len(baselines):continue
            center=baselines[offset]*height
            x0=round(cell['box'][0]*width)+2;x1=round(cell['box'][2]*width)-2
            y0=max(0,round(center-glyph_height*.7));y1=min(height,round(center+glyph_height*.7))
            if x1<=x0 or y1<=y0:continue
            boxes=[];supports=[]
            for threshold in [128,180,220]:
                count,labels,stats,_=cv2.connectedComponentsWithStats((gray[y0:y1,x0:x1]<threshold).astype(np.uint8))
                strokes=[]
                for index in range(1,count):
                    x,y,w,h,area=map(int,stats[index])
                    if not (.4*glyph_height<=w<=1.4*glyph_height and 1<=h<=max(4,glyph_height*.13)
                            and w>=h*7 and area>=w*.8):continue
                    mask=labels[y:y+h,x:x+w]==index
                    support=float(mask.any(axis=0).mean())
                    if support>=.95:strokes.append(([x0+x,y0+y,x0+x+w,y0+y+h],support))
                if len(strokes)!=1:break
                box,support=strokes[0];boxes.append(box);supports.append(support)
            if len(boxes)!=3 or any(max(abs(a-b) for a,b in zip(boxes[1],box))>2 for box in boxes):continue
            candidates.append((cell,boxes,supports,original))
        # Repeated lengths in separate rows independently establish the printed
        # data-marker convention; a solitary horizontal scratch stays unresolved.
        if len(candidates)<2:continue
        median=statistics.median(boxes[1][2]-boxes[1][0] for c,boxes,s,o in candidates)
        for cell,boxes,supports,original in candidates:
            box=boxes[1]
            if abs((box[2]-box[0])-median)>max(2,median*.1):continue
            x,y,u,v=box
            proof={'kind':'repeated-table-data-dash','row':cell['row'],'column':cell['column'],
                   'thresholds':[128,180,220],'boxes':boxes,'column_support':supports,
                   'original_blocks':original,'original_cell_box':cell['box'],
                   'crop_pixels_sha256':hashlib.sha256(gray[y:v,x:u].tobytes()).hexdigest()}
            if cell['blocks']:
                index=cell['blocks'][0]
                output[index]={**output[index],'t':'—','dash_pixel_evidence':proof}
            else:
                index=len(output)
                output.append({'t':'—','b':[x/width,y/height,u/width,v/height],
                    's':'ocr','c':min(supports),'confidence_source':'printed-dash-pixel-support',
                    'dash_pixel_evidence':proof})
                cell['blocks']=[index]
            cell['text']='—';reports.append(proof)
            table['review']=[r for r in table['review'] if not(r['code']=='aligned-table-empty-cell-with-ink'
                and r['row']==cell['row'] and r['column']==cell['column'])]
    return output,grids,({'profile':PROFILE,'regions':reports,'original_blocks':copy.deepcopy(blocks)} if reports else None)


def separate_category_rows(blocks,tables):
    """Give independently identified, unnumbered category labels their own row."""
    grids=copy.deepcopy(tables)
    for table in grids:
        wanted=[r['block'] for r in table['review'] if r['code']=='aligned-table-category-band-review']
        for index in wanted:
            block=blocks[index];center=(block['b'][1]+block['b'][3])/2
            old_ys=table['rows'];owner=next((c for c in table['cells'] if index in c['blocks']),None)
            if owner is None:continue
            row=next((r for r in range(len(old_ys)-1) if old_ys[r]<=center<old_ys[r+1]),None)
            if row is None:continue
            others=[blocks[i] for c in table['cells'] for i in c['blocks'] if i!=index
                    and old_ys[row]<=(blocks[i]['b'][1]+blocks[i]['b'][3])/2<old_ys[row+1]]
            if not others:continue
            above=[b for b in others if (b['b'][1]+b['b'][3])/2<center-.006]
            below=[b for b in others if (b['b'][1]+b['b'][3])/2>center+.006]
            if len(above)!=len(others) and len(below)!=len(others):continue
            if above:
                split=(max((b['b'][1]+b['b'][3])/2 for b in above)+center)/2
                label_row=row+1
            else:
                split=(center+min((b['b'][1]+b['b'][3])/2 for b in below))/2
                label_row=row
            if not old_ys[row]<split<old_ys[row+1]:continue
            ys=[*old_ys[:row+1],split,*old_ys[row+1:]];rebuilt=[]
            for cell in table['cells']:
                c=copy.deepcopy(cell)
                if c['row']+c['rowspan']<=row:
                    rebuilt.append(c);continue
                if c['row']>row:
                    c['row']+=1;rebuilt.append(c);continue
                end=c['row']+c['rowspan']+1
                for first,last in [(c['row'],row+1),(row+1,end)]:
                    members=[i for i in c['blocks'] if ys[first]<=(blocks[i]['b'][1]+blocks[i]['b'][3])/2<ys[last]]
                    rebuilt.append({**c,'row':first,'rowspan':last-first,
                                     'box':[c['box'][0],ys[first],c['box'][2],ys[last]],
                                     'blocks':members,'text':'\n'.join(blocks[i]['t'] for i in members)})
            else:
                table['rows']=ys;table['cells']=sorted(rebuilt,key=lambda c:(c['row'],c['column']));table['body_rows'][1]+=1
                if label_row<=table['body_rows'][0]:table['body_rows'][0]+=1
                table['evidence'].setdefault('separate_category_rows',[]).append({'block':index,'row':label_row})
                table['review']=[r for r in table['review'] if not(r['code']=='aligned-table-category-band-review' and r['block']==index)]
    return grids


def align_wrapped_labels(blocks,tables):
    """Pair wrapped labels to the row's unit baselines without merging entries."""
    grids=copy.deepcopy(tables)
    for table in grids:
        if any(blocks[i]['t'].endswith('.') and blocks[i]['t'][:-1].isdigit()
               for c in table['cells'] if c['column']==0 for i in c['blocks']):continue
        labels=[c for c in table['cells'] if c['column']==0 and c['row']>=table['body_rows'][0] and c['rowspan']==1]
        numeric_rows=[]
        for cell in table['cells']:
            if cell['column']!=1 or cell['row']<table['body_rows'][0] or not cell['blocks']:continue
            if any(numeric(blocks[i]['t']) for i in cell['blocks']):break
            last=max((blocks[i]['b'][1]+blocks[i]['b'][3])/2 for i in cell['blocks'])
            numeric_rows.append((cell['row'],last))
        else:
            if len(numeric_rows)<5:continue
            # A units column has one short entry per data row. Year/category
            # tables do not enter this path.
            if any(len(blocks[i]['t'])>8 for c in table['cells'] if c['column']==1 and c['row']>=table['body_rows'][0]
                   for i in c['blocks']):continue
            category_rows={r['row'] for r in table['evidence'].get('separate_category_rows',[])}
            categories={r['block'] for r in table['evidence'].get('separate_category_rows',[])}
            members=sorted({i for c in labels for i in c['blocks'] if i not in categories},key=lambda i:blocks[i]['b'][1])
            original_owners={i:c for c in labels for i in c['blocks']}
            for cell in labels:cell['blocks']=[i for i in cell['blocks'] if i in categories]
            by_row={c['row']:c for c in labels if c['row'] not in category_rows}
            median=statistics.median(b-a for (r,a),(s,b) in zip(numeric_rows,numeric_rows[1:]))
            for index in members:
                p=blocks[index];center=(p['b'][1]+p['b'][3])/2
                choices=[(row,base) for row,base in numeric_rows if p['b'][1]<=base+.003
                         and base-center<=median*.9 and base-center>=-median*.25]
                if not choices:
                    target=min(numeric_rows,key=lambda v:abs(v[1]-center))[0]
                else:target=min(choices,key=lambda v:abs(v[1]-center))[0]
                if target in by_row:by_row[target]['blocks'].append(index)
                else:
                    original_owners[index]['blocks'].append(index)
                    table['review'].append({'code':'aligned-table-wrapped-label-unresolved','block':index})
            for cell in labels:
                cell['blocks'].sort(key=lambda i:blocks[i]['b'][1])
                cell['text']='\n'.join(blocks[i]['t'] for i in cell['blocks'])
    return grids
