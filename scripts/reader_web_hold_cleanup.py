"""Clear only full sealed failure rows after exact dual-site readback."""
from hashlib import sha256

from . import publish_reader_v3 as v3
from . import reader_web_hold_admission as admission
from . import reader_web_index_guard as index

B=v3.ASSETS
PATH='documents/web/html/index.json'


def cleanup(review,data,publication,readback,cps,hub,journal,*,guard):
    digest=review[admission.DIGEST_FIELD]
    registry=admission.parse(data[admission.MEMBER],digest)
    proof=publication['hold_admission']
    generation=review['target_pointer']['generation']
    if (publication.get('generation')!=generation or publication.get('failure_notices_retained') is not True
            or proof.get(admission.DIGEST_FIELD)!=digest or proof.get('selected')!=review['selected']
            or readback.get('generation')!=generation or readback.get('both_projections_verified') is not True
            or readback.get('deleted_objects')!=0 or readback.get('old_and_new_verified')!=review['selected']):
        raise ValueError('exact sealed hold publication and readback required')
    sealed_target=data['target-html.json']; expected={r['failure']['key']:r['failure'] for r in registry['rows']}
    prior,_=index._index(sealed_target)
    found={f['key']:f for f in prior['failures'] if f['key'] in expected}
    if found!=expected:
        raise ValueError('exact sealed failure notice set differs')
    binding={'hold_admission':digest,'public_readback':sha256(v3.encode(readback)).hexdigest()}
    op=sha256(v3.encode(binding)).hexdigest()[:32]
    payload={**prior,'checkpoint_operation':op,'failures':[f for f in prior['failures'] if f['key'] not in expected]}
    target=index._encode(payload); after=sha256(target).hexdigest(); before=sha256(sealed_target).hexdigest()
    seal=index._permit(PATH,before,after,binding)

    def check(_=None):
        if guard() is not True or v3.current(hub)[0]!=review['target_pointer']:
            raise RuntimeError('hold cleanup target or writer changed')
        return True

    check()
    snapshot=cps.read(B,PATH)
    if snapshot.raw != hub.read_bytes(B,PATH):
        raise RuntimeError('hold cleanup S3/resolver disagree')
    existing=journal.read(before,guard=check)
    if snapshot.raw==target and existing==seal:
        outcome='observed'
    else:
        if snapshot.raw!=sealed_target or existing is not None:
            raise RuntimeError('hold cleanup parent changed or permit spent; observe only')
        if not journal.claim(before,seal,guard=check):
            raise RuntimeError('hold cleanup permit not acquired; observe only')
        check()
        if cps.read(B,PATH)!=snapshot or hub.read_bytes(B,PATH)!=snapshot.raw:
            raise RuntimeError('hold cleanup parent moved after permit; observe only')
        outcome=cps.write_once(B,PATH,payload,snapshot.etag,operation_id=op)
        if cps.read(B,PATH).raw!=target or hub.read_bytes(B,PATH)!=target:
            raise RuntimeError('hold cleanup write not read back; observe only')
    return {'generation':generation,'removed_count':len(found),'removed':list(found.values()),'outcome':outcome,
            'canonical_sha256':after,'hold_admission_sha256':digest}
