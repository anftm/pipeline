"""Pure planner for a bounded exact web layout migration; no remote mutation."""
from collections import Counter
from hashlib import sha256
import gzip
import re

from . import publish_reader_v3 as v3, reader_image_history_catalog as catalog
from . import reader_lifecycle, reader_route_control as control
from . import reader_web_exact_plan as web, reader_web_exact_legacy as single
from .reader_web_index_guard import _encode

QUOTAS = {'html': 7, 'htm': 2, 'mht': 1}
CHANGED = ('object', 'manifest', 'bytes', 'sha256')


def digest(raw):
    return sha256(raw).hexdigest()


MAX_MANAGED_ROWS = 200


def canonical(parents, pairs, operation, *, managed=False):
    if managed is not True and managed is not False:
        raise ValueError('invalid web batch mode')
    if (not isinstance(operation, str) or re.fullmatch('[0-9a-f]{32}', operation) is None
            or (managed and (len(parents)!=1 or not set(parents)<=web.EXTENSIONS or not 1<=len(pairs)<=MAX_MANAGED_ROWS))
            or (not managed and (set(parents)!=set(QUOTAS) or len(pairs)!=10))):
        raise ValueError('invalid web batch index set or operation')
    old_by_key, new_by_key = {}, {}
    for old, new in pairs:
        web._web_row(old, old=True)
        web._web_row(new, old=False)
        key = old['key']
        if (key in old_by_key or key != new['key'] or old['extension'] != new['extension']
                or old['source_sha256'] != new['source_sha256'] or old['source_revision'] != new['source_revision']
                or old['object'] == new['object'] or old['sha256'] == new['sha256']):
            raise ValueError('candidate is not an exact same-source web successor')
        old_by_key[key] = old
        new_by_key[key] = new
    quotas = {next(iter(parents)):len(pairs)} if managed else QUOTAS
    if Counter(row['extension'] for row in old_by_key.values()) != quotas:
        raise ValueError('unexpected exact web batch composition')
    result = {}
    for ext in sorted(parents):
        parent = web._json(parents[ext], 'canonical '+ext)
        if (type(parent.get('version')) is not int or parent['version'] != 1 or parent.get('kind') != web.KIND
                or parent.keys()-{'version','kind','files','failures','checkpoint_operation'}
                or parent.get('checkpoint_operation') == operation):
            raise ValueError('invalid canonical web parent')
        files = web._rows(parent, 'files')
        web._rows(parent, 'failures')
        keys = {k for k,o in old_by_key.items() if o['extension'] == ext}
        if not keys <= files.keys() or any(not web._json_equal(files[k], old_by_key[k]) for k in keys):
            raise ValueError('reviewed old row absent or differs from parent index')
        # The historical processing-root record is preserved at its own object,
        # not attached to a new derivative. All unrelated rows stay identical.
        def replacement(row):
            key = row['key']
            if key not in keys:
                return row
            next_row = ({k:v for k,v in row.items() if k != 'processing_roots'}
                        | {k:new_by_key[key][k] for k in CHANGED})
            if 'profile' in new_by_key[key]:
                next_row['profile'] = new_by_key[key]['profile']
            return next_row
        result[ext] = _encode({**parent, 'checkpoint_operation': operation,
                               'files': [replacement(row) for row in parent['files']]})
    return result


def project(store, before_packed, pairs):
    before = catalog._sidecar(before_packed)
    expected = {**before, 'f': dict(before['f'])}
    for old, new in pairs:
        if before['f'].get(old['key']) != single.route(old):
            raise ValueError('old canonical route differs from legacy sidecar')
        expected['f'][old['key']] = single.route(new)
    packed = gzip.compress(v3.encode(expected), compresslevel=9, mtime=0)
    pointer, reading = v3.current(store)
    keys = {old['key'] for old,_ in pairs}
    if (not pointer or not reading.get('base_sidecar') or reading.get('derived_pdf_redirects')
            or keys & set(reading.get('files', {})) or keys & set(reading.get('suppressed_sources', []))):
        raise ValueError('v3 custom overlays prevent exact reviewed routes')
    prior = v3.project_sidecar(v3.pinned_base(store, reading, {}), reading)
    for old, new in pairs:
        if prior['f'].get(old['key']) != single.route(old):
            raise ValueError('old route differs from live Reader v3')
        prior['f'][old['key']] = single.route(new)
    next_base,_ = control.prepare_base(reading,packed)
    if prior != v3.project_sidecar(catalog._sidecar(next_base),reading):
        raise ValueError('v3 delta not restricted to reviewed web keys')
    class Target:
        def read_bytes(self,bucket,path):
            if (bucket,path)==(web.BUCKET,'reader-index/reader_assets.json.gz'):
                return packed
            return store.read_bytes(bucket,path)
    next_pointer=v3.unify(Target(), apply=False, expected_canonical=digest(packed), expected_pointer=pointer)['pointer']
    return pointer, next_pointer, packed


def plan_legacy(src,out,*,before,after,pairs,timestamp,parent_sha):
    left,right=map(catalog._sidecar,(before,after))
    want={**left,'f':dict(left['f'])}
    for old,new in pairs:
        if left['f'].get(old['key']) != single.route(old):
            raise ValueError('legacy parent route differs')
        want['f'][old['key']]=single.route(new)
    if v3.encode(want)!=v3.encode(right):
        raise ValueError('legacy successor differs outside the exact web batch')
    def refs(x):
        return {(r['bucket'],r['path']) for r in reader_lifecycle.sidecar_references(x)}
    a,b=refs(left),refs(right)
    return catalog.plan_route_transition(src,out,now=timestamp,
        removed=frozenset((web.BUCKET,old['object']) for old,_ in pairs),
        verified_delta=(digest(v3.encode(left)),digest(v3.encode(right)),a,b),
        after_sidecar=after,expected_parent_sha256=parent_sha)
