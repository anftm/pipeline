"""Recognize complete, nonoverlapping source regions and restore original pixels."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import tempfile

try:
    from . import ocr_layout
except ImportError:
    import ocr_layout

PROFILE='region-recognition-v4-source-grouped-numeric-tables'


def recognize(path,regions,language,backend,ocr):
    from PIL import Image
    ocr_layout.validate_options({'regions':regions})
    if not any(region.get('rotation') for region in regions):
        raise ValueError('region recognition requires at least one rotated region')
    blocks=[];reports=[];empty=[];unresolved=[];identities={};detection_regions=[]
    with Image.open(path) as original,tempfile.TemporaryDirectory(dir=Path(path).parent) as temporary:
        width,height=original.size
        for index,region in enumerate(regions):
            bounds=[round(v*(width if axis%2==0 else height)) for axis,v in enumerate(region['box'])]
            x,y,u,v=bounds
            if min(u-x,v-y)<1:raise ValueError('region has no original pixels')
            rotation=region.get('rotation',0)
            crop=original.crop(bounds).convert('RGB')
            if rotation:crop=crop.rotate(-rotation,expand=True)
            target=Path(temporary)/f'region-{index}.png';crop.save(target)
            try:
                recognized=ocr.ocr_page(target,crop.width,crop.height,language,backend)
                models=copy.deepcopy(ocr.actual_ocr_models(language,backend))
                local_tables=[]
                if language=='en':
                    try:
                        from . import ocr_symbolic_table,ocr_aligned_table
                    except ImportError:
                        import ocr_symbolic_table,ocr_aligned_table
                    table=ocr_symbolic_table.detect(crop,recognized)
                    if table:local_tables.append(table)
                    else:
                        local_tables=ocr_aligned_table.prepare(crop,recognized)
                        local_tables=ocr_aligned_table.separate_category_rows(recognized,local_tables)
                        local_tables=ocr_aligned_table.align_wrapped_labels(recognized,local_tables)
            finally:crop.close()
            def point(a,b):
                a,b=ocr_layout.map_point_back(a,b,rotation)
                return [(x+a*(u-x))/width,(y+b*(v-y))/height]
            def restore(block):
                restored=copy.deepcopy(block);a,b,c,d=block['b']
                corners=[point(a,b),point(c,b),point(c,d),point(a,d)]
                restored['b']=[min(p[0] for p in corners),min(p[1] for p in corners),
                               max(p[0] for p in corners),max(p[1] for p in corners)]
                if block.get('q'):restored['q']=[point(a,b) for a,b in block['q']]
                restored['recognition_region']=index
                restored['recognition_rotation_clockwise']=rotation
                return restored
            output=[{**restore(block),'recognition_local_block':i} for i,block in enumerate(recognized)]
            diagnostics=models.get('diagnostics',{})
            empty.extend(f'{index}:{number}' for number in diagnostics.get('empty_regions',[]))
            for row in diagnostics.get('detection_regions',[]):
                converted=restore(row);converted['region']=f'{index}:{row["region"]}'
                detection_regions.append(converted)
            for row in diagnostics.get('unresolved_regions',[]):
                converted=restore({'b':row['box']}) if row.get('box') else None
                unresolved.append({**row,'recognition_region':index,
                    **({'box':converted['b']} if converted else {})})
            for name,identity in models.get('models',{}).items():
                if name in identities and identities[name]!=identity:raise ValueError('region model identity changed')
                identities[name]=identity
            reports.append({'region':index,'original_box':region['box'],'crop_pixels':bounds,
                'rotation_clockwise':rotation,'recognition_size':[u-x,v-y] if rotation in (0,180) else [v-y,u-x],
                'input_png_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                'original_recognition_blocks':copy.deepcopy(recognized),'restored_blocks':output,'models':models,
                'local_tables':local_tables})
            blocks.extend(output)
    # Every original pixel belongs to a crop. Failure or an empty detection is
    # retained with its own region identity, never replaced by a blank page.
    actual={'backend':backend,'models':identities,'diagnostics':{
        'empty_regions':empty,'detection_regions':detection_regions,
        'detected_regions':sum(r['models'].get('diagnostics',{}).get('detected_regions',len(r['restored_blocks'])) for r in reports),
        'recognized_regions':len(blocks),'unresolved_regions':unresolved,
        'regional_recognition':{'profile':PROFILE,'complete_source_coverage':True,'regions':reports}}}
    for check in ('secondary_check','mixed_script'):
        issues=[issue for r in reports for issue in r['models'].get('diagnostics',{}).get(check,{}).get('issues',[])]
        if issues:actual['diagnostics'][check]={'issues':issues}
    return blocks,actual


def arrange(blocks,width,height,options,regional_recognition=None):
    """Order each crop upright, then emit only original-page geometry/offsets."""
    options=ocr_layout.validate_options(options)
    text='';spans=[];ordered=[];boundaries=[];used=set();review=[];tables=[]
    for number,region in enumerate(options['regions']):
        x,y,u,v=[round(a*(width if i%2==0 else height)) for i,a in enumerate(region['box'])]
        rotation=region.get('rotation',0)
        indices=[i for i,b in enumerate(blocks) if b.get('recognition_region')==number]
        local=[]
        for i in indices:
            block=copy.deepcopy(blocks[i]);a,b,c,d=block['b']
            bounds=[(a*width-x)/(u-x),(b*height-y)/(v-y),(c*width-x)/(u-x),(d*height-y)/(v-y)]
            block['b']=ocr_layout.map_box_back(bounds,(360-rotation)%360)
            block.pop('q',None)
            local.append(block)
        rw,rh=(v-y,u-x) if rotation in (90,270) else (u-x,v-y)
        writing=region.get('writing_mode','auto')
        report=next((r for r in (regional_recognition or {}).get('regions',[]) if r['region']==number),{})
        grids=copy.deepcopy(report.get('local_tables',[]))
        local_options={'writing_mode':writing,'join_soft_lines':options.get('join_soft_lines',True)}
        if grids:
            try:
                from . import ocr_aligned_table
            except ImportError:
                import ocr_aligned_table
            # Source reviews may remove a preceding non-table block. Restore
            # the saved table ownership by stable crop-local identity instead
            # of silently shifting every later cell's member index.
            local_ids={block.get('recognition_local_block',i):i for i,block in enumerate(local)}
            if len(local_ids)!=len(local):raise ValueError('duplicate regional block identity')
            for grid in grids:
                for cell in grid['cells']:
                    if any(index not in local_ids for index in cell['blocks']):
                        raise ValueError('regional table lost a source member after review')
                    cell['blocks']=[local_ids[index] for index in cell['blocks']]
            result=ocr_aligned_table.arrange(local,rw,rh,grids,local_options)
            def original_box(bounds):
                mapped=ocr_layout.map_box_back(bounds,rotation)
                return [(x+mapped[0]*(u-x))/width,(y+mapped[1]*(v-y))/height,
                        (x+mapped[2]*(u-x))/width,(y+mapped[3]*(v-y))/height]
            for grid in result['layout'].get('tables',[]):
                actual=copy.deepcopy(grid)
                actual['grid_box']=grid['box'];actual['grid_rotation_clockwise']=rotation
                actual['grid_region_box']=copy.deepcopy(region['box'])
                actual['box']=original_box(grid['box'])
                for cell in actual['cells']:
                    cell['blocks']=[indices[i] for i in cell['blocks']]
                    cell['box']=original_box(cell['box'])
                tables.append(actual)
        else:
            result=ocr_layout.arrange(local,rw,rh,local_options,include_writing_modes=True)
        review.extend(flag for flag in result['layout']['review'] if flag not in review)
        offset=len(text)
        if text and result['text']:
            boundaries.append({'offset':len(text),'kind':'region','separator':'\n\n'})
            text+='\n\n';offset=len(text)
        text+=result['text']
        for span in result['text_spans']:
            index=indices[span['block']]
            spans.append({**span,'start':span['start']+offset,'end':span['end']+offset,
                          'block':index,'box':blocks[index]['b'],'region':number})
            ordered.append({**copy.deepcopy(blocks[index]),'id':index});used.add(index)
        boundaries.extend({**b,'offset':b['offset']+offset} for b in result['layout']['boundaries'])
    if used!=set(range(len(blocks))):
        raise ValueError('region recognition has unassigned source blocks')
    return {'text':text,'blocks':ordered,'raw_blocks':copy.deepcopy(blocks),'text_spans':spans,
            'layout':{'version':ocr_layout.VERSION,'writing_mode':'horizontal-ltr','evidence':PROFILE,
                'review':review,'regions':len(options['regions']),'boundaries':boundaries,
                'offset_unit':'unicode-codepoint','mapping_precision':'block',
                'recognition_regions':copy.deepcopy(options['regions']),
                **({'tables':tables} if tables else {})}}
