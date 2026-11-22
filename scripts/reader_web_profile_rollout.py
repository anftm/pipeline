"""Plan a full, hosted and read-only reconversion for mapped older web profiles."""
from pathlib import Path
import argparse,json
from . import reader_format_backlog as backlog, reader_web_managed_convert as convert
from . import reader_web_exact_plan as web, publish_reader_v3 as v3
from .reader_web_profile import WEB_PROFILE

KIND=web.KIND
B=web.BUCKET
MAX_JOBS=100


def plan(records,indexes):
    by_key={}; holds=[]
    for ext,body in sorted(indexes.items()):
        if ext not in convert.EXTENSIONS or body.get('version')!=1 or body.get('kind')!=KIND:
            raise ValueError('invalid current canonical web index')
        for key,row in web._rows(body,'files').items():
            if key in by_key or row.get('extension')!=ext:
                raise ValueError('duplicate or foreign canonical web row')
            by_key[key]=row
    out={e:[] for e in sorted(convert.EXTENSIONS)}; counts={'current':0,'unmapped':0,'held':0,'queued':0}
    for key,src in sorted(records.items()):
        ext=src['extension']
        if ext not in out:continue
        row=by_key.get(key)
        if row is None:
            counts['unmapped']+=1;continue
        if row.get('profile')==WEB_PROFILE:
            counts['current']+=1;continue
        req={'repo':row.get('repo'),'path':row.get('path'),'extension':ext,
             'source_revision':row.get('source_revision'),'source_sha256':row.get('source_sha256')}
        try:
            web._web_row(row,old=True)
            if (req['repo'],req['path'],row['extension'])!=(src['repo'],src['path'],ext):
                raise ValueError('canonical source key differs')
            convert.validate_request(request([req]))
        except (ValueError,TypeError):
            counts['held']+=1;holds.append({'key':key,'reason':'invalid_pinned_web_identity'});continue
        out[ext].append(req);counts['queued']+=1
    groups=[]
    for ext,rows in out.items():
        for i in range(0,len(rows),convert.MAX_ROWS):
            groups.append((f'{ext}-{i//convert.MAX_ROWS:03d}',request(rows[i:i+convert.MAX_ROWS])))
    if len(groups)>MAX_JOBS:raise ValueError('web profile scope exceeds the hosted matrix bound')
    return groups,counts,holds


def request(items):
    return {'version':1,'max_rows':convert.MAX_ROWS,'max_source_bytes':convert.MAX_SOURCE_BYTES,
            'max_output_bytes':convert.MAX_OUTPUT_BYTES,'items':items}


def capture(store):
    from huggingface_hub.errors import HfHubHTTPError
    from urllib.error import HTTPError
    search,manifest,corpus,revisions,_=backlog.capture_search(store)
    records=backlog.source_records(corpus,revisions); idx={}; raw={}
    pointer=store.read_bytes(B,v3.POINTER)
    for ext in sorted(convert.EXTENSIONS):
        path=f'documents/web/{ext}/index.json'
        try:body=store.read_bytes(B,path)
        except (FileNotFoundError,HfHubHTTPError,HTTPError) as e:
            if not isinstance(e,FileNotFoundError) and getattr(e,'code',getattr(getattr(e,'response',None),'status_code',None))!=404:
                raise
            continue
        raw[path]=body;idx[ext]=web._json(body,'canonical '+ext)
    groups,counts,holds=plan(records,idx)
    if (pointer!=store.read_bytes(B,v3.POINTER) or search!=store.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)
            or any(store.read_bytes(B,p)!=b for p,b in raw.items())):
        raise ValueError('live Reader or Search roots moved while planning')
    result={'version':1,'profile':WEB_PROFILE,'reader_generation':web._json(pointer,'Reader pointer')['generation'],
            'search_generation':web._json(manifest,'Search manifest')['generation'],'counts':counts,
            'index_sha256':{p:convert.digest(b) for p,b in raw.items()},'holds':holds,'remote_writes':0,'publication_ready':False}
    return groups,result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    class Public(v3.HubBucketStore):
        def token(self,bucket):return False
    groups,report=capture(Public());a.output.mkdir(parents=True,exist_ok=False)
    for name,req in groups:
        (a.output/(name+'.json')).write_bytes(convert.encode(req))
    (a.output/'report.json').write_bytes(convert.encode(report))
    (a.output/'matrix.json').write_text(json.dumps({'include':[{'shard':name} for name,_ in groups]},separators=(',',':'))+'\n')
    print('Web profile rollout plan',report['profile'],report['counts'],len(groups),'shards',flush=True)

if __name__=='__main__':main()
