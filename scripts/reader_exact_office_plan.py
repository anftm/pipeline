"""Preview the exact Office report replacement; this module never writes remote data."""
from hashlib import sha256
import gzip
import re
from xml.etree import ElementTree as ET

from . import publish_reader_v3 as v3, reader_route_control as control, reader_route_plan as routes
from .reader_web_exact_plan import _json, _rows

INDEX = 'chapters/ebook/epub/index.json'
TITLE = '白区党目前的中心任务'
REPOS = {'VoiceOfML/MLMRL-Hub', 'VoiceOfML/MLMRL-Library'}
PROFILE = 'office-epub-chapters-v1-flow-512'
PINS = {
    'rtf': ('399230332a4298771620cb84c9499d5f9d5ea5dab1cd0c843fcc1b6b9c43c52d', 'a3f30ce1631e2b90', '82e7b62c8cf5bd45',
            'dcb4613fbf02f54cf19891a7d5cc29947bf4618beed52e1a15560b6bb291f19c'),
    'docx': ('e2fec34b8929a7cc8faa959057c39e83683742134f788fd6a53bcbe5556385b3', 'ca25835742f6d979', '43e6d9cf3d2aa4e8',
             'b05408450c2e867f47a2babe11a62e904daae3e81681a4a5780241b7597a8868'),
}
FIELDS = ('root', 'manifest', 'search_index')
LIMIT = 16 * 1024 * 1024


def digest(raw):
    return sha256(raw).hexdigest()


def root(ext, successor):
    source, old, new, _ = PINS[ext]
    return f'chapters/ebook/epub/{source}/{new if successor else old}'


def member(bundle, relative):
    if not isinstance(relative, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', relative):
        raise ValueError('invalid exact Office member name')
    if set(relative.split('/')) & {'.', '..'}:
        raise ValueError('relative Office traversal')
    file = bundle / relative
    if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(bundle.resolve()):
        raise ValueError('missing/nonregular exact Office member')
    with file.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('office member exceeds budget')
    return raw


def validate_bundle(ext, bundle, old_manifest):
    raw = member(bundle, 'chapter-manifest.json')
    if digest(raw) != PINS[ext][3]:
        raise ValueError('reviewed Office manifest changed')
    value = _json(raw, 'Office manifest')
    if value['toc'] != old_manifest['toc'] or len(value['toc']) != 7 or len(value['chapters']) != 2:
        raise ValueError('Office chapter/TOC count or targets changed')
    members = {'chapter-manifest.json': {'sha256': digest(raw), 'bytes': len(raw)}}
    ids = {}
    for item in (*value['chapters'], value['search_index']):
        content = member(bundle, item['path'])
        info = {'sha256': digest(content), 'bytes': len(content)}
        if info != {key: item[key] for key in info}:
            raise ValueError('Office manifest member mismatch')
        if item['path'] in members:
            raise ValueError('duplicate Office member')
        members[item['path']] = info
        if item in value['chapters']:
            ids[item['index']] = {node.get('id') for node in ET.fromstring(content).iter()}
    if not all(item['fragment'] in ids[item['chapter']] for item in value['toc']):
        raise ValueError('Office TOC fragment missing')
    # The version binds all files, including resource members not individually
    # listed in the manifest. Recheck the exact directories, not only XHTML.
    for path in sorted(bundle.rglob('*')):
        rel = path.relative_to(bundle).as_posix()
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError('unexpected Office bundle object')
        if path.is_file() and rel not in members:
            if not rel.startswith('resources/'):
                raise ValueError('unexpected Office bundle member')
            content = member(bundle, rel)
            members[rel] = {'sha256': digest(content), 'bytes': len(content)}
    identity = sha256()
    for path, member_hash in sorted(members.items()):
        identity.update(path.encode('utf-8') + b'\0' + bytes.fromhex(member_hash['sha256']))
    if identity.hexdigest()[:16] != PINS[ext][2]:
        raise ValueError('reviewed Office bundle version changed')
    from io import BytesIO
    with gzip.open(BytesIO(member(bundle, value['search_index']['path'])), 'rt', encoding='utf8') as stream:
        text = stream.read(LIMIT + 1)
    if len(text) > LIMIT or '指刘少奇' not in text or any(x in text for x in ('圾癣应哆间揭', '洁葵丽饲掐澎琶')):
        raise ValueError('Office search text does not match reviewed cleanup')
    return value, members


def exact_rows(raw, parent_sha, replacements):
    if len(raw) > LIMIT or digest(raw) != parent_sha or set(replacements) != set(PINS):
        raise ValueError('exact Office parent SHA or candidate formats differ')
    index = _json(raw, 'chapter index')
    if index.get('kind') != 'ebook-chapter-stream-index' or index.get('version') != 1:
        raise ValueError('not the canonical chapter index')
    before = _rows(index, 'files')
    failures = _rows(index, 'failures')
    after = {}
    observed = set()
    for key, row in before.items():
        ext = row.get('source_extension')
        if ext not in PINS or row.get('source_sha256') != PINS[ext][0]:
            continue
        if (row['repo'] not in REPOS or key != row['repo']+'\0'+row['path'] or TITLE not in row['path']
                or not row['path'].lower().endswith('.'+ext) or row['manifest'] != root(ext, False)+'/chapter-manifest.json'
                or row['root'] != root(ext, False) or row.get('profile') != PROFILE
                or row.get('source_revision') is None or key in failures or (ext,row['repo']) in observed):
            raise ValueError('reviewed Office row drift')
        observed.add((ext,row['repo']))
        successor = replacements[ext]
        if (successor['root'] != root(ext, True) or successor['manifest'] != root(ext, True)+'/chapter-manifest.json'
                or successor['search_index'] != root(ext, True)+'/epub-search-index.json.gz'):
            raise ValueError('unreviewed Office successor route')
        updated = {**row, **{name: successor[name] for name in FIELDS}}
        if {name for name in row if row[name] != updated[name]} != set(FIELDS):
            raise ValueError('incorrect Office mutable fields')
        after[key] = updated
    if observed != {(ext, repo) for ext in PINS for repo in REPOS}:
        raise ValueError('expected exactly four reviewed Office rows')
    result = {**index, 'files': [after.get(row['key'], row) for row in index['files']]}
    return result, {key: before[key] for key in after}, after


def compact(row):
    target = row['manifest']
    return {'s':2, 'm':'e', 'p':target, 'b':v3.ASSETS, 'c':target, 'cb':v3.ASSETS}


def project(store, before, old, new):
    pointer, catalog = v3.current(store)
    if not pointer or catalog.get('derived_pdf_redirects') or not catalog.get('base_sidecar'):
        raise ValueError('unsupported v3 publication state')
    if set(old) & (set(catalog.get('files', {})) | set(catalog.get('suppressed_sources', []))):
        raise ValueError('reviewed Office targets are shadowed')
    base = routes.decode_sidecar(before)
    for key, row in old.items():
        if base['f'].get(key)!=compact(row):
            raise ValueError('Office current sidecar differs from reviewed old rows')
    target = {**base, 'f': {**base['f'], **{key:compact(row) for key,row in new.items()}}}
    packed = gzip.compress(v3.encode(target), compresslevel=9, mtime=0)
    pinned = v3.project_sidecar(v3.pinned_base(store,catalog,{}), catalog)
    packed_target, _ = control.prepare_base(catalog, packed)
    projected = v3.project_sidecar(routes.decode_sidecar(packed_target),catalog)
    if any(pinned['f'].get(key)!=compact(row) for key,row in old.items()):
        raise ValueError('Office current v3 targets differ')
    wanted = {**pinned, 'f':{**pinned['f'],**{key:compact(row) for key,row in new.items()}}}
    if wanted != projected:
        raise ValueError('v3 projection drift beyond the reviewed four Office routes')
    return pointer, packed


def retention(old, members):
    # Keep old destinations and staged successors visible to the same GC graph.
    values = sorted({r['manifest'] for r in old.values()} | {root(ext, True)+'/chapter-manifest.json' for ext in members})
    return {'version':1, 'kind':'reader-office-retained-bundles',
            'resources':[{'bucket':v3.ASSETS,'path':path} for path in values]}
