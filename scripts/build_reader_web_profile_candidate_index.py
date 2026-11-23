"""Qualify already saved Web documents and build a digest-pinned import locally.

The input manifest lists independently reviewed historical HTML hashes and
local files. Given exact current source revisions, require a byte-for-byte
match from the current converter using its normal dependency reads. HTMLs
that differ, are absent, or exceed production limits never enter this index.
The production writer verifies this index and reruns both Reader gates.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from urllib.parse import quote

from . import reader_web_managed_convert as convert, reader_web_profile_candidates as cache
from .reader_web_managed_review import MAX_DOC


def qualify(row, root, *, fetch=convert.anonymous_fetch):
    source = row['source']
    convert.validate_request({'version':1, 'items':[source], 'max_rows':1,
                              'max_source_bytes':convert.MAX_SOURCE_BYTES,
                              'max_output_bytes':convert.MAX_OUTPUT_BYTES})
    original = root / row['file']
    if root not in original.resolve().parents or not 0 < original.stat().st_size <= MAX_DOC:
        return 'outside-document-limit', None
    with original.open('rb') as stream:
        saved = stream.read(MAX_DOC + 1)
    if not 0 < len(saved) <= MAX_DOC or cache.digest(saved) != row['sha256']:
        return 'saved-hash-mismatch', None
    url = f"https://{convert.HF}/datasets/{source['repo']}/resolve/{source['source_revision']}/{quote(source['path'])}"
    dl = convert.Downloads(convert.MAX_SOURCE_BYTES, fetch)
    try:
        raw = dl.get(url, convert.MAX_ITEM_SOURCE)
        if cache.digest(raw) != source['source_sha256']:
            return 'source-hash-mismatch', None
        notes = {}
        result = convert.convert(raw, source['extension'], url, dl, MAX_DOC, omissions=notes)
    except convert.Hold as exc:
        return 'held:'+str(exc), None
    if result != saved:
        return 'different-current-output', None
    return 'matched', {'sha256':row['sha256'], 'bytes':len(saved), 'omissions':notes}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=4)
    a=p.parse_args(); root=a.input.resolve().parent
    with a.input.open('rb') as stream:
        raw=stream.read(2*1024**2+1)
    if len(raw)>2*1024**2:
        raise ValueError('candidate input exceeds size bound')
    rows=json.loads(raw,object_pairs_hook=convert.unique_object)
    if not 1<=len(rows)<=cache.MAX_ENTRIES or not 1<=a.workers<=12:
        raise ValueError('invalid candidate intake scope')
    keys=[cache.input_key(r['source']) for r in rows]
    if len(keys)!=len(set(keys)):
        raise ValueError('duplicate candidate source identity')
    a.output.mkdir(parents=True,exist_ok=False)
    (a.output/'html').mkdir()
    index={}; outcomes=Counter()
    with ThreadPoolExecutor(max_workers=a.workers) as workers:
        for key, row, (state, entry) in zip(keys, rows, workers.map(lambda r: qualify(r,root),rows)):
            outcomes[state]+=1
            if entry is not None:
                with (root/row['file']).open('rb') as stream: raw=stream.read(MAX_DOC+1)
                if (len(raw),cache.digest(raw))!=(entry['bytes'],entry['sha256']):
                    raise ValueError('local candidate changed after qualification')
                dest=a.output/'html'/(entry['sha256']+'.html')
                if not dest.exists(): dest.write_bytes(raw)
                index[key]=entry
    if index:
        raw=convert.encode({'version':1,'profile':cache.WEB_PROFILE,'converter':cache.converter_identity(),'entries':index})
        pin=cache.digest(raw); cache.load(lambda *_:raw,pin)
        (a.output/(pin+'.json')).write_bytes(raw)
    else:
        pin=None
    summary={'candidate_index':pin,'outcomes':dict(outcomes),'remote_writes':0,'dual_reader_verified':False}
    (a.output/'report.json').write_bytes(convert.encode(summary));print(summary,flush=True)


if __name__=='__main__':main()
