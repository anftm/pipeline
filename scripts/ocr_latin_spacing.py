"""Recover Latin word gaps only from independent readings and original whitespace."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile

PROFILE = 'latin-word-gaps-v1-source-pixels'


def recognize(path, blocks):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    try:
        from . import ocr_line_geometry
    except ImportError:
        import ocr_line_geometry
    trained=next((root/'eng.traineddata' for root in [Path(os.environ.get('OCR_SCRIPT_MODEL_DIR','/usr/share/tesseract-ocr/5/tessdata')),
        Path('/usr/share/tesseract-ocr/5/tessdata'),Path('/usr/share/tesseract-ocr/4.00/tessdata')]
        if (root/'eng.traineddata').is_file()),None)
    if trained is None:return blocks,None
    output=copy.deepcopy(blocks);reports=[]
    with Image.open(path) as image,tempfile.TemporaryDirectory(dir=Path(path).parent) as directory:
        pixels=np.array(image.convert('RGB'))
        for index,block in enumerate(output):
            original=block['t']
            if not re.fullmatch('[A-Z]{12,}',original):continue
            x,y,u,v=block['b']
            quad=block.get('q') or [[x,y],[u,y],[u,v],[x,v]]
            part=ocr_line_geometry.crop(pixels,[[a*image.width,b*image.height] for a,b in quad])
            gray=cv2.cvtColor(part,cv2.COLOR_RGB2GRAY)
            if part.shape[1]<part.shape[0]*3:continue
            empty=~(gray<180).any(axis=0);runs=[]
            for col in np.flatnonzero(empty):
                if runs and col==runs[-1][-1]+1:runs[-1].append(int(col))
                else:runs.append([int(col)])
            internal=[r for r in runs if r[0]>0 and r[-1]<part.shape[1]-1]
            if len(internal)<3:continue
            median=float(np.median([len(r) for r in internal]))
            if max(map(len,internal))<median*1.5:continue
            attempts=[]
            for scale in (2,3):
                selected=ImageOps.expand(Image.fromarray(part),10,fill='white')
                selected=selected.resize((selected.width*scale,selected.height*scale),Image.Resampling.LANCZOS)
                crop=Path(directory)/f'{index}-{scale}.png';selected.save(crop)
                try:
                    run=subprocess.run(['tesseract',str(crop),'stdout','--tessdata-dir',str(trained.parent),
                        '-l','eng','--psm','7','-c','load_system_dawg=0','-c','load_freq_dawg=0','tsv'],
                        env={**os.environ,'OMP_THREAD_LIMIT':'1'},capture_output=True,text=True,timeout=20)
                    words=[row for row in csv.DictReader(io.StringIO(run.stdout),delimiter='\t')
                           if row.get('level')=='5' and row.get('text','').strip()] if not run.returncode else []
                    positions=[{'text':r['text'],'confidence':float(r['conf']),
                        'box':[(int(r['left'])/scale-10),(int(r['top'])/scale-10),
                               ((int(r['left'])+int(r['width']))/scale-10),
                               ((int(r['top'])+int(r['height']))/scale-10)]} for r in words]
                    gaps=[]
                    for a,b in zip(positions,positions[1:]):
                        lo,hi=round(a['box'][2]),round(b['box'][0])
                        observed=[r for r in internal if r[0]>=lo-2 and r[-1]<hi+2]
                        gaps.append({'between':[a['text'],b['text']],'range':[lo,hi],
                                     'blank_columns':max(map(len,observed),default=0)})
                    text=' '.join(p['text'] for p in positions)
                    attempts.append({'scale':scale,'text':text,'words':positions,'gaps':gaps,
                        'accepted':len(positions)>=2 and text.replace(' ','')==original
                            and all(p['confidence']>=80 for p in positions)
                            and all(g['blank_columns']>=median*1.5 for g in gaps),
                        'returncode':run.returncode,'crop_sha256':hashlib.sha256(crop.read_bytes()).hexdigest()})
                except (OSError,subprocess.TimeoutExpired) as exc:
                    attempts.append({'scale':scale,'accepted':False,'error':f'{type(exc).__name__}: {exc}'[:500]})
            accepted=all(a['accepted'] for a in attempts) and attempts[0]['text']==attempts[1]['text']
            proof={'region':index,'original':original,'box':block['b'],'quad':quad,'attempts':attempts,
                   'pixels_sha256':hashlib.sha256(part.tobytes()).hexdigest(),'median_letter_gap':median,'accepted':accepted}
            reports.append(proof)
            if accepted:
                block['t']=attempts[0]['text'];block['latin_spacing_evidence']=proof
    return output,({'profile':PROFILE,'model':{'file':trained.name,'sha256':hashlib.sha256(trained.read_bytes()).hexdigest(),
        'bytes':trained.stat().st_size},'regions':reports} if reports else None)
