"""Hosted read-only observations of the four reviewed Office replacement parents."""
from dataclasses import asdict
from pathlib import Path
import json
import os

from . import reader_exact_office_plan as plan, publish_reader_v3 as v3
from .reader_image_history_publish_s3 import CATALOG, SIDECAR, LegacyRoots
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore

B = v3.ASSETS
PARENT_SHA = '6c38be89499ed375f5920cc39578e59888adcc99842565b09773b271147f37e0'


def report(index, pointer, legacy, hub, s3):
    before = index.read(B, plan.INDEX)
    if before.raw_sha256 != PARENT_SHA or hub.read_bytes(B, plan.INDEX) != before.raw:
        raise ValueError('Office index differs from the reviewed S3 and Hub parent')
    replacement = {ext: dict(root=plan.root(ext, True), manifest=plan.root(ext, True)+'/chapter-manifest.json',
                             search_index=plan.root(ext, True)+'/epub-search-index.json.gz') for ext in plan.PINS}
    _, old, new = plan.exact_rows(before.raw, PARENT_SHA, replacement)
    p_raw, p_etag = pointer.read_for_update(B, v3.POINTER)
    if hub.read_bytes(B, v3.POINTER) != p_raw:
        raise ValueError('Office pointer differs between reader and S3')
    side_raw, side_etag = pointer.read_for_update(B, SIDECAR)
    if hub.read_bytes(B, SIDECAR) != side_raw:
        raise ValueError('Office sidecar differs between reader and S3')
    live, successor = plan.project(hub, side_raw, old, new)
    if live != json.loads(p_raw):
        raise ValueError('Office v3 generation changed during proof')
    cat = legacy.observe(CATALOG)
    observed = {}
    for ext in sorted(plan.PINS):
        def exact(path):
            raw = s3.read_bytes(B, path)
            if len(raw) > plan.LIMIT or hub.read_bytes(B, path) != raw:
                raise ValueError('old Office member differs between S3 and reader')
            return raw
        manifest = exact(plan.root(ext, False)+'/chapter-manifest.json')
        obj = json.loads(manifest)
        if len(obj['toc']) != 7 or len(obj['chapters']) != 2:
            raise ValueError('unexpected reviewed Office bundle contents')
        for child in (*obj['chapters'],obj['search_index']):
            raw = exact(plan.root(ext, False)+'/'+child['path'])
            if len(raw)!=child['bytes'] or plan.digest(raw)!=child['sha256']:
                raise ValueError('old Office manifest disagrees with immutable member')
        observed[ext] = plan.digest(manifest)
    for path, size, etag in ((CATALOG, cat.size, cat.etag),(SIDECAR,len(side_raw),side_etag)):
        check = s3._client('vomebook').head_object(Bucket='reader-assets-v2',Key=path)
        if (check['ContentLength'],check['ETag']) != (size,etag):
            raise ValueError('Office legacy root changed during hosted collection')
    if index.read(B,plan.INDEX) != before or pointer.read_for_update(B,v3.POINTER) != (p_raw,p_etag):
        raise ValueError('Office canonical or v3 parent drift')
    return dict(version=1, changed_rows=4, reader_generation=live['generation'], index_sha256=PARENT_SHA,
                index_etag=before.etag, pointer_sha256=plan.digest(p_raw), pointer_etag=p_etag,
                catalog=asdict(cat), sidecar=dict(sha256=plan.digest(side_raw),etag=side_etag,size=len(side_raw)),
                planned_sidecar_sha256=plan.digest(successor), old_manifest_sha256=observed,
                remote_writes=0, publication_ready=False)


def main():
    if (os.environ.get('GITHUB_REPOSITORY'),os.environ.get('GITHUB_REF'),os.environ.get('GITHUB_EVENT_NAME')) != (
            'anftm/pipeline','refs/heads/main','workflow_dispatch'):
        raise RuntimeError('requires exact hosted central Office preflight')
    index = ConditionalCheckpointStore()
    s3 = index.store
    hub = v3.CentralHubStore()
    records = report(index, ConditionalPageStore(hub,s3=s3),LegacyRoots(s3._client('vomebook')),hub,s3)
    p = Path('output/office-s3-preflight.json');p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf8') as f:json.dump(records,f,indent=2)
    print('Office S3 and Hub: four exact routes only; zero remote writes')

if __name__=='__main__':main()
