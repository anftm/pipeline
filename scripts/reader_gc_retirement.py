"""Format-independent retirement hints; only the current graph authorizes deletion.

Publishers persist removed references before advancing a catalog. A receipt is
immutable and does not pin its old objects. Incomplete publication is safe: the
old live roots still protect the objects when the collector checks them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

try:
    from . import shared
except ImportError:
    import shared

ASSETS = shared.READER_ASSETS_BUCKET
BUCKETS = {ASSETS, shared.PDF_PAGES_BUCKET, shared.PDF_OCR_INPUT_BUCKET}
PREFIX = 'reader-index/gc/retirements/'
CURSOR = 'reader-index/gc/automatic/current.json'
KINDS = {'catalog-replacement', 'v3-replacement', 'processing-handoff'}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def bundle_root(path):
    """Bound hints to an individual published bundle, never a format directory."""
    if (not isinstance(path, str) or '\\' in path or '\0' in path
            or any(p in {'', '.', '..'} for p in path.split('/'))):
        return None
    patterns = (
        r'(objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16})(?:/|$)',
        r'(objects/[0-9a-f]{2}/[0-9a-f]{64})(?:/|$)',
        r'((?:pages/image|chapters/ebook|documents/(?:office|spreadsheet|web|text|pdf)|native/ebook)/[a-z0-9_-]+/[0-9a-f]{64})(?:/|$)',
        r'(media/(?:audio|video|swf)/[a-z0-9_-]+/[0-9a-f]{64})(?:/|$)',
        r'(derived/[a-zA-Z0-9._-]+/[0-9a-f]{32})(?:/|$)',
    )
    for pattern in patterns:
        match = re.match(pattern, path)
        if match:
            return match[1]
    return None


def references(values):
    return {(r['bucket'], r['path']) for r in values if isinstance(r, dict)
            and r.get('bucket') in BUCKETS and bundle_root(r.get('path'))}


def record(previous, current, generation, kind):
    removed = references(previous) - references(current)
    if not removed:
        return None
    if kind not in KINDS or not re.fullmatch(r'[0-9a-f]{32}|[0-9a-f]{64}', str(generation)):
        raise ValueError('invalid retirement publisher identity')
    roots = sorted({(bucket, bundle_root(path)) for bucket, path in removed})
    value = {'version': 1, 'kind': 'reader-retirement-hints', 'reason': kind,
             'replacement_generation': generation,
             'roots': [{'bucket': b, 'root': p} for b, p in roots],
             'removed': [{'bucket': b, 'path': p} for b, p in sorted(removed)]}
    return PREFIX + hashlib.sha256(encode(value)).hexdigest() + '.json', value


def handoff(protection, generation):
    return record([{'bucket': r['bucket'], 'path': r['root'] + '/handoff'}
                   for r in protection['resources']], [], generation, 'processing-handoff')


def validate(path, value):
    if (value.get('version') != 1 or value.get('kind') != 'reader-retirement-hints'
            or value.get('reason') not in KINDS or not isinstance(value.get('removed'), list)
            or not isinstance(value.get('roots'), list)
            or path != PREFIX + hashlib.sha256(encode(value)).hexdigest() + '.json'):
        raise ValueError('invalid retirement receipt')
    expected = record(value['removed'], [], value['replacement_generation'], value['reason'])
    if expected is None or expected != (path, value):
        raise ValueError('retirement receipt contains an unbounded root')
    return value


def load(store):
    paths = sorted(store.list_files(ASSETS, (PREFIX,)))
    reader = getattr(store, 'read_cached_bytes', store.read_bytes)
    values = shared.ordered_reads(lambda path: validate(path, json.loads(reader(ASSETS, path))), paths, workers=4)
    return dict(zip(paths, values))


def targets(receipts):
    return {(r['bucket'], r['root']) for value in receipts.values() for r in value['roots']}


def within(bucket, path, roots):
    root = bundle_root(path)
    # A legacy source root can own several profile directories.
    while root:
        if (bucket, root) in roots:
            return True
        root = root.rpartition('/')[0]
    return False


def cursor(store):
    try:
        value = json.loads(store.read_bytes(ASSETS, CURSOR))
    except FileNotFoundError:
        return None
    if (value.get('version') != 1 or value.get('kind') != 'reader-gc-continuation'
            or not re.fullmatch(r'[1-9][0-9]*', str(value.get('run_id', '')))
            or value.get('scope') not in {'unreferenced', 'retired-assets'}
            or type(value.get('complete')) is not bool
            or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('plan_sha256', '')))):
        raise ValueError('invalid automatic collection checkpoint')
    return value


def pending_plan(store):
    saved = cursor(store)
    if not saved or saved['complete']:
        return None, None
    plan = json.loads(store.read_bytes(ASSETS, f"reader-index/gc/{saved['run_id']}/collection.json"))
    if saved['plan_sha256'] != plan.get('plan_sha256') or saved['scope'] != plan.get('scope'):
        raise ValueError('automatic deletion plan does not match its cursor')
    if plan.get('complete') is True:
        return None, None
    return saved, plan


def save_cursor(store, run, scope, result, complete):
    store.put_bytes(ASSETS, CURSOR, encode({'version': 1, 'kind': 'reader-gc-continuation',
        'run_id': str(run), 'scope': scope, 'plan_sha256': result['plan_sha256'],
        'complete': complete, 'updated_at': datetime.now(timezone.utc).isoformat()}))
