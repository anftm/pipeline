"""Fresh positive source and running Reader evidence for exact hold admission."""
from hashlib import sha256
from urllib.parse import quote

import httpx

from . import publish_reader_v3 as v3
from . import reader_format_backlog as backlog
from . import reader_web_hold_admission as admission
from . import reader_web_managed_convert as convert
from . import reader_web_profile_replay as replay


def pins(hub, parent, search_raw, space_commit):
    if hub.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)!=search_raw:
        raise ValueError('hold admission Search pointer changed')
    if v3.current(hub)[0]!=parent:
        raise ValueError('hold admission Reader pointer changed')
    with httpx.Client(timeout=20,follow_redirects=True,headers={'Cache-Control':'no-cache'}) as http:
        if replay.space_revision(http)!=space_commit:
            raise ValueError('hold admission Space revision changed')


def prove(review,data,parents,hub,pages,*,prior=None):
    registry=admission.parse(data[admission.MEMBER],review[admission.DIGEST_FIELD])
    admission.validate_review(registry,review,parents)
    start=hub.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)
    search_raw,_,corpus,revisions,_=backlog.capture_search(hub)
    if start!=search_raw or backlog.search_publication_pointer.decode_pointer(search_raw)['desired']!=registry['search_generation']:
        raise ValueError('hold admission Search generation differs')
    if pages['search_generation']!=registry['search_generation'] or pages['code_sha']!=registry['pages_commit']:
        raise ValueError('hold admission Pages proof differs')
    records=backlog.source_records(corpus,revisions)
    for row in registry['rows']:
        hold,candidate=row['failure'],row['candidate']; item=records.get(hold['key'])
        if (not item or item['extension']!='html' or item['revision']!=candidate['source_revision']
                or any(item[k]!=hold[k] for k in ('repo','path'))):
            raise ValueError('hold admission source absent or preparation revision changed')
        url=f"https://{convert.HF}/datasets/{item['repo']}/resolve/{item['revision']}/{quote(item['path'])}"
        reader=convert.Downloads(convert.MAX_ITEM_SOURCE,convert.anonymous_fetch)
        raw=reader.get(url,convert.MAX_ITEM_SOURCE)
        if sha256(raw).hexdigest()!=candidate['source_sha256']:
            raise ValueError('hold admission source bytes differ')
    pointer,catalog=v3.current(hub)[:2]
    view=v3.project_sidecar(v3.pinned_base(hub,catalog,{}),catalog)
    routes={r['old']['key']:view['f'].get(r['old']['key']) for r in review['rows']}
    documents={r['new']['sha256']:data[r['id']+'/document.html'] for r in review['rows']}
    rendered=replay.verify(documents,pages['code_sha'])
    report={dst:{'sha':rendered[src]['code'],'documents':rendered[src]['documents']}
            for src,dst in (('pages','search'),('space','hf-search'))}
    pins(hub,review['parent_pointer'],search_raw,registry['space_commit'])
    opts={} if prior is None else {'prior_proof':prior}
    proof=admission.validate_proof(registry,review,parents,search_generation=pages['search_generation'],
        pages_commit=pages['code_sha'],space_commit=rendered['space']['code'],reader_pointer=pointer,routes=routes,report=report,**opts)
    return proof,report
