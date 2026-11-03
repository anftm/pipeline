"""Recognize sloping labels on both sides of a ruled header from glyph pixels."""
from __future__ import annotations

import copy
import hashlib

PROFILE='diagonal-header-crops-v1'


def recognize(image, table, blocks, ocr, language, backend):
    import cv2
    import numpy as np
    from PIL import Image
    if not any(row['code']=='diagonal-table-header' for row in table['review']):
        return blocks,table,None
    page_width,page_height=image.size
    replacements=[]
    reports=[]
    output=copy.deepcopy(blocks)
    for cell in table['cells']:
        if cell['row'] or cell['rowspan']<2 or cell['colspan']!=1:
            continue
        box=[round(v*(page_width if i%2==0 else page_height)) for i,v in enumerate(cell['box'])]
        # Skip border strokes, but keep glyph pixels within the cell intact.
        # Fitted vertical rules drift a few pixels with scan skew. Include the
        # neighboring border before classifying it, rather than clipping glyphs
        # at the mean x coordinate of that rule.
        box=[max(0,box[0]+3),max(0,box[1]+1),min(page_width,box[2]+4),min(page_height,box[3]+1)]
        crop=image.crop(box).convert('RGB')
        gray=np.asarray(crop.convert('L'))
        segments=cv2.createLineSegmentDetector().detect(gray)[0]
        if segments is None:
            continue
        segments=[s for s in segments.reshape(-1,4) if abs(s[2]-s[0])>8 and abs(s[3]-s[1])>12
                  and .45<abs((s[2]-s[0])/(s[3]-s[1]))<.95]
        if len(segments)<3:
            continue
        points=np.array([point for s in segments for point in [s[:2],s[2:]]])
        slope,intercept=np.polyfit(points[:,1],points[:,0],1)
        if np.abs(points[:,0]-(slope*points[:,1]+intercept)).max()>4:
            continue
        n,labels,stats,centers=cv2.connectedComponentsWithStats((gray<180).astype(np.uint8))
        components=[]
        for i in range(1,n):
            x,y,w,h,area=map(int,stats[i]);cx,cy=centers[i]
            if area<6 or x<2 or (h>2.5*w and w<=3) or y+h>gray.shape[0]-5:
                continue
            distance=cx-(slope*cy+intercept)
            if abs(distance)<5:
                continue
            components.append({'component':i,'side':'right' if distance>0 else 'left','box':[x,y,x+w,y+h]})
        sides=[]
        for side in ['left','right']:
            selected=sorted([c for c in components if c['side']==side],key=lambda c:c['box'][1])
            bands=[]
            for component in selected:
                if bands and component['box'][1]<max(b['box'][3] for b in bands[-1])-2:
                    bands[-1].append(component)
                else:bands.append([component])
            glyphs=[]
            for band in bands:
                a=min(c['box'][0] for c in band);b=min(c['box'][1] for c in band)
                c=max(c['box'][2] for c in band);d=max(c['box'][3] for c in band)
                if d-b<8:
                    continue
                glyphs.append([max(0,a-1),max(0,b-1),min(c+1,crop.width),min(d+1,crop.height)])
            if not 2<=len(glyphs)<=10:
                break
            band_height=max(g[3]-g[1] for g in glyphs)+12
            strip=Image.new('RGB',(sum(g[2]-g[0]+4 for g in glyphs)+8,band_height),'white')
            cursor=4
            for glyph in glyphs:
                part=crop.crop(glyph);strip.paste(part,(cursor,(band_height-part.height)//2));cursor+=part.width+4
            engine=ocr.get_ocr_engine(language,backend)
            if not hasattr(engine,'recognize_txt'):
                break
            attempts=[]
            for scale in [2,3]:
                scaled=strip.resize((strip.width*scale,strip.height*scale),Image.Resampling.LANCZOS)
                values=engine.recognize_txt([np.asarray(scaled)[:,:,::-1]])
                attempts.append({'scale':scale,'text':values.txts[0],'confidence':float(values.scores[0]),
                                 'pixels_sha256':hashlib.sha256(np.asarray(scaled).tobytes()).hexdigest()})
            accepted=(attempts[0]['text']==attempts[1]['text'] and len(attempts[0]['text'])==len(glyphs)
                      and min(a['confidence'] for a in attempts)>=.98)
            sides.append({'side':side,'glyphs':glyphs,'attempts':attempts,'accepted':accepted})
        report={'cell':{k:cell[k] for k in ['row','column','box','rowspan','colspan']},'crop_pixels':box,
                'source_pixels_sha256':hashlib.sha256(np.asarray(crop).tobytes()).hexdigest(),
                'line':[float(slope),float(intercept)],'sides':sides,
                'original_blocks':[copy.deepcopy(blocks[i]) for i in cell['blocks']]}
        reports.append(report)
        if len(sides)!=2 or not all(side['accepted'] for side in sides):
            continue
        replacement=[]
        for side in sides:
            value=side['attempts'][0]['text']
            for index,(char,glyph) in enumerate(zip(value,side['glyphs'])):
                a,b,c,d=glyph
                replacement.append({'t':char,'c':min(a['confidence'] for a in side['attempts']),'s':'ocr',
                    'b':[(box[0]+a)/page_width,(box[1]+b)/page_height,(box[0]+c)/page_width,(box[1]+d)/page_height],
                    'table_header_side':side['side'],'table_header_order':index})
        replacements.append((set(cell['blocks']),replacement))
        report['accepted']=True
    if replacements:
        removed=set().union(*(indices for indices,_ in replacements))
        output=[block for index,block in enumerate(blocks) if index not in removed]
        output.extend(block for _,replacement in replacements for block in replacement)
    if not reports:
        return blocks,table,None
    evidence={'profile':PROFILE,'regions':reports,'models':ocr.actual_ocr_models(language,backend),
              'original_blocks':copy.deepcopy(blocks)}
    if replacements:
        try:
            from . import ocr_table_geometry
        except ImportError:
            import ocr_table_geometry
        table=ocr_table_geometry.detect(image,output)
        if table is None:
            return blocks,None,{**evidence,'accepted':False,'reason':'grid-changed-after-header-recognition'}
        if all(report.get('accepted') for report in reports):
            table['review']=[row for row in table['review'] if row['code']!='diagonal-table-header']
        for cell in table['cells']:
            if any(output[i].get('table_header_side') for i in cell['blocks']):
                cell['blocks'].sort(key=lambda i:(output[i].get('table_header_side',''),output[i].get('table_header_order',0)))
                cell['text']=''.join(output[i]['t'] for i in cell['blocks'])
    return output,table,evidence
