"""Immutable, exact-scoped CHM retirement plan and resumable progress journal."""
import base64
import gzip
import json
from pathlib import Path
import re

try:
    from . import chm_epub_publication_safety as safety, chm_epub_publication_preflight as chm
    from . import publish_reader_v3 as publication
except ImportError:
    import chm_epub_publication_safety as safety
    import chm_epub_publication_preflight as chm
    import publish_reader_v3 as publication


PREFIX = 'reader-index/gc/chm-epub-withdrawal/20261030/'
PLAN = PREFIX + 'plan.json.gz'
PLAN_SHA = PREFIX + 'plan.sha256'
PROGRESS = PREFIX + 'progress.json'


def pack(plan, before, updates):
    """Serialize bytes without losing exact pre/post identities."""
    value = {'version': 1, 'kind': 'exact-chm-epub-retirement-recovery-plan',
             'inventory_sha256': chm.preview.INVENTORY_SHA256,
             'plan': {**plan, 'overrides': {p: base64.b64encode(raw).decode('ascii')
                                            for p, raw in plan['overrides'].items()},
                      'immutable': {p: base64.b64encode(raw).decode('ascii')
                                    for p, raw in plan['immutable'].items()}},
             'before': before,
             'processing_updates': {p: {**item, 'after': base64.b64encode(item['after']).decode('ascii')}
                                    for p, item in updates.items()}}
    return gzip.compress(publication.encode(value), mtime=0)


def unpack(raw, rows):
    value = json.loads(gzip.decompress(raw))
    if (value.get('version') != 1 or value.get('kind') != 'exact-chm-epub-retirement-recovery-plan'
            or value.get('inventory_sha256') != chm.preview.INVENTORY_SHA256):
        raise ValueError('invalid saved CHM recovery plan')
    plan = value['plan']
    for key in ('overrides', 'immutable'):
        plan[key] = {path: base64.b64decode(data, validate=True) for path, data in plan[key].items()}
    updates = {path: {**item, 'after': base64.b64decode(item['after'], validate=True)}
               for path, item in value['processing_updates'].items()}
    safety.validate_staged_plan(plan, rows)
    inventory = chm.processing_protection_inventory()
    if set(updates) != {entry['path'] for entry in inventory['records']}:
        raise ValueError('saved CHM processing updates differ from reviewed owners')
    before = value['before']
    if set(before) != set(plan['overrides']) | set(plan['immutable']):
        raise ValueError('saved CHM initial control inventory differs')
    for path, item in before.items():
        if (not isinstance(item, dict) or type(item.get('present')) is not bool
                or item.get('present') != (item.get('sha256') is not None)
                or (item['present'] and (not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256')))
                                         or type(item.get('bytes')) is not int or item['bytes'] < 0))
                or (not item['present'] and item.get('bytes') is not None)):
            raise ValueError('invalid saved CHM initial control identity: ' + path)
    if chm.digest(before[chm.CATALOG].get('raw', '').encode('utf-8')) != before[chm.CATALOG]['sha256']:
        raise ValueError('saved CHM initial catalog bytes do not match their digest')
    expected = {**plan['index_hashes_before'], chm.SIDECAR:plan['sidecar_sha256_before'],
                chm.CATALOG:plan['catalog_sha256_before']}
    if any(before[path]['sha256'] != digest for path, digest in expected.items()):
        raise ValueError('saved CHM initial controls do not match the reviewed plan')
    for path, after in plan['immutable'].items():
        if before[path]['present'] and before[path]['sha256'] != chm.digest(after):
            raise ValueError('saved CHM immutable identity conflicts')
    return plan, before, updates


def read_optional(store, path):
    try:
        return store.read_bytes(chm.ASSETS, path)
    except FileNotFoundError:
        safety.assert_all_buckets_available(store)
        return None


def save_plan(store, plan, before, updates, output):
    raw = pack(plan, before, updates)
    digest = chm.digest(raw)
    for path, payload in ((PLAN, raw), (PLAN_SHA, (digest + '\n').encode())):
        prior = read_optional(store, path)
        if prior is not None and prior != payload:
            raise ValueError('different CHM recovery plan already exists')
        if prior is None:
            store.put_bytes(chm.ASSETS, path, payload)
        if store.read_bytes(chm.ASSETS, path) != payload:
            raise ValueError('CHM recovery plan readback failed')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'plan.sha256').write_text(digest + '\n')
    return digest


def load_plan(store, rows):
    safety.assert_all_buckets_available(store)
    raw = store.read_bytes(chm.ASSETS, PLAN)
    expected = store.read_bytes(chm.ASSETS, PLAN_SHA).decode('ascii').strip()
    if chm.digest(raw) != expected:
        raise ValueError('saved CHM recovery plan digest mismatch')
    return (*unpack(raw, rows), expected)


def save_progress(store, phase, state, output):
    state['phase'] = phase
    state['updated_at'] = publication.reader_lifecycle.now_iso()
    raw = publication.encode(state)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_bytes(raw)
    store.put_bytes(chm.ASSETS, PROGRESS, raw)
    if store.read_bytes(chm.ASSETS, PROGRESS) != raw:
        raise ValueError('CHM progress readback mismatch')
    print(json.dumps({'phase': phase, 'confirmed_deleted_objects':
                      state.get('confirmed_deleted_objects', 0)}, ensure_ascii=False), flush=True)


def load_progress(store, digest):
    state = json.loads(store.read_bytes(chm.ASSETS, PROGRESS))
    if (state.get('kind') != 'chm-epub-retirement' or state.get('plan_sha256') != digest
            or state.get('inventory_sha256') != chm.preview.INVENTORY_SHA256):
        raise ValueError('saved CHM progress does not match its exact plan')
    return state


def verify_initial_chapter_evidence(plan, updates, state):
    safety.require_chapter_evidence(plan, state['chapter_evidence'])
    rows = state['chapter_evidence']['results'] + [item['chapter'] for item in updates.values()]
    if len({row['manifest'] for row in rows}) != len(rows):
        raise ValueError('saved CHM chapter evidence has duplicate manifests')
    expected = [{'root':row['manifest'].rsplit('/',1)[0], 'count':row['bundle_objects'],
                 'paths_sha256':row['bundle_paths_sha256']} for row in rows]
    if state.get('chapter_bundles') != expected:
        raise ValueError('saved CHM chapter bundle inventory changed')
    return True


def classify_recovery_controls(store, plan, before, updates, state, *, allow_published=None):
    """Accept only planned bytes, and never an unacknowledged partial retirement."""
    staged = safety.staged_pre_ack_controls(plan, before)
    changes = set()
    def read_digest(path):
        raw = read_optional(store, path)
        return (chm.digest(raw) if raw is not None else None), raw
    for path, planned in plan['immutable'].items():
        digest, raw = read_digest(path)
        if raw is not None and raw != planned:
            raise ValueError('foreign CHM immutable appeared during recovery: ' + path)
        if digest != before[path]['sha256']:
            changes.add(path)
    for path, planned in plan['overrides'].items():
        digest, raw = read_digest(path)
        allowed = {before[path]['sha256'], chm.digest(planned)}
        if path in staged:
            allowed.add(chm.digest(staged[path]))
        if digest not in allowed:
            raise ValueError('foreign CHM control appeared during recovery: ' + path)
        if digest != before[path]['sha256']:
            changes.add(path)
        if (path not in staged and path not in (allow_published or set())
                and digest == chm.digest(planned) and digest != before[path]['sha256']
                and state.get('both_surfaces_accepted') is not True):
            raise ValueError('CHM control retired before recorded two-site acceptance: ' + path)
    for path, item in updates.items():
        digest, raw = read_digest(path)
        if digest not in {item['before_sha256'], chm.digest(item['after'])}:
            raise ValueError('foreign CHM processing control appeared during recovery: ' + path)
        if digest != item['before_sha256']:
            changes.add(path)
            if state.get('both_surfaces_accepted') is not True:
                raise ValueError('CHM processing root retired before two-site acceptance: ' + path)
    if state.get('both_surfaces_accepted') is True:
        accepted = set(staged) | set(plan['immutable'])
        for path in accepted:
            digest, _ = read_digest(path)
            possible = {chm.digest(plan['immutable'][path])} if path in plan['immutable'] else {
                chm.digest(staged[path]), chm.digest(plan['overrides'][path])}
            if digest not in possible:
                raise ValueError('accepted CHM staged control disappeared or reverted: ' + path)
    return {'changed_controls': len(changes), 'state_only': not changes}
