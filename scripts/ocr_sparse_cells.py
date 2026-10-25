"""Re-read complete glyph bands and headers bounded by visible table rules."""
from __future__ import annotations

import copy
import hashlib
import re
import statistics

PROFILE='sparse-cell-pixels-v2-leaders-headers-operators'


def broken_plus(gray):
    """Prove a cross with a lower stem and a separated upper ink fragment."""
    import cv2
    import numpy as np
    matches=[]
    for threshold in (128,180,220):
        count,_,stats,_=cv2.connectedComponentsWithStats((gray<threshold).astype(np.uint8))
        found=[]
        for index in range(1,count):
            x,y,w,h,area=map(int,stats[index])
            if not (5<=w<=gray.shape[0] and 4<=h<=w):continue
            ink=gray[y:y+h,x:x+w]<threshold
            row=int(ink.sum(axis=1).argmax())
            col=int(ink.sum(axis=0).argmax())
            if not (ink[row].sum()>=w*.7 and ink[:,col].sum()>=h*.7
                    and .25*w<=col<=.75*w and row<=h*.4 and h-row>=3):continue
            dots=[list(map(int,s[:4])) for s in stats[1:]
                  if 1<=s[2]<=w*.4 and 1<=s[3]<=h*.7 and s[4]>=3
                  and 0<=y-s[1]-s[3]<=max(2,gray.shape[0]*.1)
                  and abs(s[0]+s[2]/2-(x+col))<=max(1.5,w*.2)]
            if len(dots)==1:found.append({'cross':[x,y,w,h],'upper':dots[0]})
        if len(found)!=1:return None
        matches.append(found[0])
    if any(max(abs(a-b) for a,b in zip(matches[0]['cross'],p['cross']))>2 for p in matches[1:]):return None
    return {'thresholds':[128,180,220],'components':matches,
            'crop_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest()}


def recognize(image, table, blocks, ocr, language, backend):
    import cv2
    import numpy as np
    from PIL import Image,ImageOps
    if not table.get('inferred_rows'):
        return blocks,table,None
    engine=ocr.get_ocr_engine(language,backend)
    if not hasattr(engine,'recognize_txt'):
        return blocks,table,None
    width,height=image.size
    gray=np.asarray(image.convert('L'))
    first,last=table['body_rows']
    ys=[y*height for y in table['rows']]
    step=statistics.median(b-a for a,b in zip(ys[first+1:],ys[first+2:]))
    reports,replacements=[],[]
    def read_crop(part):
        attempts=[]
        padded=ImageOps.expand(part.convert('RGB'),6,fill='white')
        for scale in (2,3):
            scaled=padded.resize((padded.width*scale,padded.height*scale),Image.Resampling.LANCZOS)
            result=engine.recognize_txt([np.asarray(scaled)[:,:,::-1]])
            attempts.append({'scale':scale,'text':result.txts[0],'confidence':float(result.scores[0]),
                             'pixels_sha256':hashlib.sha256(np.asarray(scaled).tobytes()).hexdigest()})
        return attempts
    # Repeated dots identify a label column. Keep the original components as
    # cell geometry rather than converting a decorative leader to punctuation.
    for col in range(len(table['columns'])-1):
        a,b=[round(v*width) for v in table['columns'][col:col+2]]
        top,bottom=round(ys[first]),round(ys[last+1])
        x0,x1=a+4,b-4
        if x1<=x0:continue
        sub=gray[top:bottom,x0:x1]
        count,labels,stats,centers=cv2.connectedComponentsWithStats((sub<180).astype(np.uint8))
        large=[(i,*map(int,stats[i])) for i in range(1,count)
               if .35*step<=stats[i,3]<=1.4*step and 2<=stats[i,2]<=3*step and stats[i,4]>=12]
        bands=[]
        for item in sorted(large,key=lambda c:c[2]):
            _,x,y,w,h,area=item
            if bands and y<max(c[2]+c[4] for c in bands[-1])-2:
                bands[-1].append(item)
            else:bands.append([item])
        if len(bands)!=last-first+1:continue
        proposals=[]
        for offset,band in enumerate(bands):
            y0=min(c[2] for c in band);y1=max(c[2]+c[4] for c in band)
            if y1-y0>step*1.4:break
            glyph_end=max(c[1]+c[3] for c in band)
            dots=[tuple(map(int,stats[i])) for i in range(1,count)
                  if 1<=stats[i,2]<=step*.32 and 1<=stats[i,3]<=step*.32 and stats[i,4]>=3
                  and stats[i,0]>=glyph_end and y0<=centers[i,1]<=y1]
            dots.sort(key=lambda d:d[0])
            if len(dots)<6 or dots[-1][0]-dots[0][0]<(x1-x0)*.35:break
            gaps=[v[0]-u[0] for u,v in zip(dots,dots[1:])]
            # A few printed dots touch each other or a stain. The surviving
            # repeated run still proves a leader; retain the full source band.
            if max(gaps)>statistics.median(gaps)*4:break
            left=min(c[1] for c in band)
            box=[x0+max(0,left-2),max(top,top+y0-2),x0+glyph_end+2,min(bottom,top+y1+2)]
            attempts=read_crop(image.crop(box))
            same=attempts[0]['text']==attempts[1]['text'] and bool(attempts[0]['text'])
            row=first+offset
            center=(top+(y0+y1)/2)/height
            near=(ys[row]+ys[row+1])/2/height
            if abs(center-near)>step/height*.8:break
            proposals.append({'row':row,'column':col,'box':box,'attempts':attempts,'accepted':same,
                'leader_boxes':[[float(x0+x)/width,float(top+y)/height,float(x0+x+w)/width,float(top+y+h)/height]
                                for x,y,w,h,area in dots]})
        if len(proposals)!=len(bands):continue
        old_indices={i for cell in table['cells'] if cell['column']==col and cell['row']>=first for i in cell['blocks']}
        report={'column':col,'kind':'dot-leader-labels','original_blocks':[copy.deepcopy(blocks[i]) for i in sorted(old_indices)],
                'regions':proposals,'accepted':all(p['accepted'] for p in proposals)}
        reports.append(report)
        if not report['accepted']:continue
        new=[]
        for p in proposals:
            box=p['box']
            new.append({'t':p['attempts'][0]['text'],'s':'ocr','c':min(a['confidence'] for a in p['attempts']),
                        'b':[box[0]/width,box[1]/height,box[2]/width,box[3]/height],
                        'table_cell':{'row':p['row'],'column':col},'table_label_evidence':p})
        replacements.append((old_indices,new))
    # Re-read both sides of a crossed header together to retain every letter
    # from a detector region that straddles the visible rule.
    crossed=set()
    for index,block in enumerate(blocks):
        if (block['b'][1]+block['b'][3])/2>=table['rows'][first]:continue
        owners=[]
        for cell in table['cells']:
            if cell['row']>=first:continue
            a,b,c,d=cell['box'];x,y,u,v=block['b']
            overlap=max(0,min(c,u)-max(a,x))*max(0,min(d,v)-max(b,y))
            if overlap>.15*(u-x)*(v-y):owners.append((cell['row'],cell['column']))
        if len(owners)>1:crossed.update(owners)
    if crossed:
        selected=[c for c in table['cells'] if (c['row'],c['column']) in crossed]
        removed={i for c in selected for i in c['blocks']}
        proposals=[]
        for cell in selected:
            a,b,c,d=[round(v*(width if i%2==0 else height)) for i,v in enumerate(cell['box'])]
            box=[a+4,b+4,c-4,d-4]
            attempts=read_crop(image.crop(box))
            proposals.append({'row':cell['row'],'column':cell['column'],'box':box,'attempts':attempts,
                              'accepted':attempts[0]['text']==attempts[1]['text'] and bool(attempts[0]['text'])
                                  and min(a['confidence'] for a in attempts)>=.95})
        accepted=all(p['accepted'] for p in proposals)
        reports.append({'kind':'crossed-header-rules','regions':proposals,'accepted':accepted,
                        'original_blocks':[copy.deepcopy(blocks[i]) for i in sorted(removed)]})
        if accepted:
            new=[]
            for p in proposals:
                a,b,c,d=p['box']
                new.append({'t':p['attempts'][0]['text'],'s':'ocr','c':min(a['confidence'] for a in p['attempts']),
                            'b':[a/width,b/height,c/width,d/height],
                            'table_cell':{'row':p['row'],'column':p['column']}})
            replacements.append((removed,new))
    for cell in table['cells']:
        if cell['row']<first:continue
        for index in cell['blocks']:
            block=blocks[index]
            if block['t'].count('÷')!=1 or not re.fullmatch(r'[\d\s,+−\-÷()]+',block['t']):continue
            a,b,c,d=[round(v*(width if i%2==0 else height)) for i,v in enumerate(block['b'])]
            shape=broken_plus(gray[b:d,a:c])
            if shape is None:continue
            attempts=read_crop(image.crop((a,b,c,d)))
            expected=block['t'].replace('÷','+')
            accepted=all(''.join(p['text'].split())==''.join(expected.split()) and p['confidence']>=.9
                         for p in attempts)
            proof={'kind':'broken-cross-operator','original_blocks':[copy.deepcopy(block)],
                   'regions':[{'box':[a,b,c,d],'attempts':attempts,'geometry':shape}], 'accepted':accepted}
            reports.append(proof)
            if accepted:
                updated={**copy.deepcopy(block),'t':expected,'c':min(p['confidence'] for p in attempts),
                         'table_operator_evidence':proof,'table_cell':{'row':cell['row'],'column':cell['column']}}
                replacements.append(({index},[updated]))
    if not reports:return blocks,table,None
    removed=set().union(*(indices for indices,new in replacements)) if replacements else set()
    output=[copy.deepcopy(b) for i,b in enumerate(blocks) if i not in removed]
    output.extend(b for indices,new in replacements for b in new)
    grid=copy.deepcopy(table)
    for report in reports:
        if not report['accepted']:
            grid['review'].append({'code':'sparse-cell-recognition-disagreement','kind':report['kind']})
    for cell in grid['cells']:
        cell['blocks']=[]
        cell.pop('text',None)
    for index,block in enumerate(output):
        owner=block.get('table_cell')
        x,y=(block['b'][0]+block['b'][2])/2,(block['b'][1]+block['b'][3])/2
        cell=next((c for c in grid['cells'] if (owner and c['row']==owner['row'] and c['column']==owner['column'])
                   or not owner and c['box'][0]<=x<c['box'][2] and c['box'][1]<=y<c['box'][3]),None)
        if cell is not None:
            cell['blocks'].append(index)
            if block.get('table_label_evidence'):
                cell['dot_leader']={'source':'original-pixels','boxes':block['table_label_evidence']['leader_boxes']}
    for cell in grid['cells']:
        cell['blocks'].sort(key=lambda i:(round(output[i]['b'][1]*height/8),output[i]['b'][0]))
        cell['text']='\n'.join(output[i]['t'] for i in cell['blocks'])
    return output,grid,{'profile':PROFILE,'regions':reports,'original_blocks':copy.deepcopy(blocks),
                        'source_pixels_sha256':hashlib.sha256(gray.tobytes()).hexdigest()}
