"""Read back exact daily Web hold replacements, then retire matching notices."""
import argparse,json
from pathlib import Path

import httpx

from . import publish_reader_v3 as v3
from . import reader_web_batch_cli as batch
from . import reader_web_exact_plan as web
from . import reader_web_hold_cleanup as cleanup
from . import reader_web_hold_dispatch as handoff
from . import reader_web_managed_review as managed
from .reader_image_history_store import S3ClaimStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .reader_web_index_guard import S3Journal
from .reader_web_profile_readback import record_hint


def finish(resource, output):
    resource=handoff.parse_resource(resource)
    publication=json.loads((output/'publication.json').read_bytes())
    if resource[handoff.FIELD]!=publication.get(handoff.FIELD):raise ValueError('held publication owner digest differs')
    store=ConditionalCheckpointStore();client=store.store._client('vomebook'); claims=S3ClaimStore(client)
    proof=handoff.existing_apply_inputs(resource,claims)
    if proof['hold_admission']!=publication['hold_admission']:raise ValueError('hold readback authority differs')
    blob=batch.read(claims,batch.PREFIX+proof['bundle_sha256']+'.zip',managed.MAX_PACKED)
    review,data,_,_=managed.parse(blob,proof['bundle_sha256'],proof['review_sha256'])
    consumers=json.loads((output/'format-automation-verification.json').read_bytes())
    hub=v3.CentralHubStore(); active=handoff.owner(hub,resource)
    def guard():
        if v3.current(hub)[0]!=review['target_pointer']:
            raise RuntimeError('hold readback Reader target moved')
        return active()
    with httpx.Client(follow_redirects=True,timeout=30) as client_http:
        result=batch.live(review,consumers,publication,client_http,workers=batch.PUBLIC_WEB_READERS)
    result['retirement_hint']=record_hint(publication,review['rows'],guard=guard,hub=hub,claims=claims)
    with (output/'web-batch-object-readback.json').open('xb') as f:f.write(v3.encode(result))
    result=cleanup.cleanup(review,data,publication,result,store,hub,S3Journal(client),guard=guard)
    with (output/'web-hold-cleanup.json').open('xb') as f:f.write(v3.encode(result))


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--resource-json',required=True);cli.add_argument('--output',type=Path,required=True)
    a=cli.parse_args();finish(web._json(a.resource_json.encode('utf-8'),'hold resource'),a.output)

if __name__=='__main__':main()
