"""Local legacy-catalog successor for one exact static-image route.

This is not a publication transaction. Use the existing serialized Reader
writer workflow/fence to revalidate the exact roots, coordinate the sidecar,
snapshot and catalog, and verify the consumers. A retirement hint is not
permission to retire a generation or delete anything.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import gzip
import hashlib
import io
import json
import re
from tempfile import TemporaryFile
import zlib

import ijson

from . import reader_gc_retirement as retirement, reader_image_history as image
from . import reader_lifecycle as lifecycle

MAX_CATALOG_BYTES = 512 * 1024 * 1024
MAX_ENTRY_BYTES = 48 * 1024 * 1024
MAX_SIDECAR_BYTES = 50 * 1024 * 1024
BLOCK = 64 * 1024
GENERATION = re.compile(r'[0-9a-f]{32}')
ESCAPED_SURROGATE = re.compile(rb'\\u[dD][89a-fA-F][0-9a-fA-F]{2}')


def _encode(value, *, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                      sort_keys=sort_keys, allow_nan=False).encode('utf-8')


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate sidecar field')
    return result


def _sidecar_float(token):
    value = float(token)
    if Decimal(token) != Decimal(str(value)):
        raise ValueError('sidecar number loses precision under canonical encoding')
    return value


def _sidecar(packed):
    if not isinstance(packed, bytes) or not 0 < len(packed) <= MAX_SIDECAR_BYTES:
        raise ValueError('invalid or oversized packed sidecar')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            body = stream.read(MAX_SIDECAR_BYTES + 1)
    except (EOFError, OSError, zlib.error) as error:
        raise ValueError('malformed packed sidecar') from error
    if len(body) > MAX_SIDECAR_BYTES:
        raise ValueError('expanded sidecar exceeds limit')
    value = json.loads(body.decode('utf-8'), object_pairs_hook=_unique, parse_float=_sidecar_float)
    if (not isinstance(value, dict) or type(value.get('v')) is not int or value['v'] != 1
            or not isinstance(value.get('f'), dict)
            or not all(isinstance(route, dict) for route in value['f'].values())):
        raise ValueError('invalid Reader sidecar')
    return value


def _refs(items):
    return {(ref['bucket'], ref['path']) for ref in items}


def _route_delta(before_packed, after_packed, key, webp_path, avif_path):
    if not isinstance(key, str):
        raise ValueError('expected an exact static-image key')
    repo, sep, path = key.partition('\0')
    extension = path.rpartition('.')[-1].lower()
    if (not sep or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*', repo)
            or any(part in {'', '.', '..'} for part in path.split('/')) or '\\' in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path) or extension not in image.IMAGE_EXTENSIONS):
        raise ValueError('expected an exact static-image key')
    roots = []
    for member, suffix in ((webp_path, 'webp'), (avif_path, 'avif')):
        if not isinstance(member, str) or not re.fullmatch(
                rf'pages/image/{extension}/[0-9a-f]{{64}}/document\.{suffix}', member):
            raise ValueError('invalid exact image member path')
        roots.append(member.rsplit('/', 1)[0])
    if roots[0] == roots[1]:
        raise ValueError('replacement requires a distinct AVIF bundle')

    before, after = _sidecar(before_packed), _sidecar(after_packed)
    old_route, new_route = ({'s': 2, 'm': 'i', 'b': image.BUCKET, 'p': path}
                            for path in (webp_path, avif_path))
    if (_encode(before['f'].get(key), sort_keys=True) != _encode(old_route, sort_keys=True)
            or _encode(after['f'].get(key), sort_keys=True) != _encode(new_route, sort_keys=True)):
        raise ValueError('sidecar does not bind the exact WebP and AVIF routes')
    before_sha = image._sha(_encode(before, sort_keys=True))
    after_sha = image._sha(_encode(after, sort_keys=True))
    before_refs = _refs(lifecycle.sidecar_references(before))
    before['f'][key] = new_route  # The decoded copy is private; no second copy of all routes.
    if image._sha(_encode(before, sort_keys=True)) != after_sha:
        raise ValueError('sidecars differ outside the admitted image route')
    return before_sha, after_sha, before_refs, _refs(lifecycle.sidecar_references(after))


class _Reader:
    def __init__(self, stream, limit, entry_limit):
        self.stream, self.limit, self.entry_limit = stream, limit, entry_limit
        self.size = self.progress = 0
        self.hash = hashlib.sha256()
        self.tail = b''

    def read(self, size):
        # Also bound bytes read without an event, e.g. an unterminated string.
        pending = self.entry_limit + BLOCK - (self.size - self.progress)
        n = min(size, self.limit - self.size + 1, pending + 1)
        raw = self.stream.read(n)
        if not isinstance(raw, bytes) or len(raw) > n:
            raise ValueError('catalog requires a bounded binary reader')
        self.size += len(raw)
        if self.size > self.limit:
            raise ValueError('catalog source exceeds byte limit')
        if len(raw) > pending:
            raise ValueError('catalog token exceeds entry limit')
        # Some ijson backends silently replace malformed surrogate escapes.
        # Legacy writers use UTF-8, so fail closed on any such escape spelling.
        if raw:
            window = self.tail + raw
            if ESCAPED_SURROGATE.search(window):
                raise ValueError('catalog uses an unsupported Unicode surrogate escape')
            self.tail = window[-5:]
        self.hash.update(raw)
        return raw

    def events(self):
        try:
            for item in ijson.basic_parse(self, buf_size=BLOCK):
                self.progress = self.size
                yield item
        except ijson.JSONError as error:
            raise ValueError('malformed catalog JSON') from error


class _UniqueMap(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise ValueError('duplicate catalog field')
        super().__setitem__(key, value)


def _value(events, first, limit):
    builder = ijson.ObjectBuilder(map_type=_UniqueMap)
    depth, size, count = 0, 0, 0
    event, value = first
    while True:
        # Charge structure as well as scalars before building the whole entry.
        size += 1 + (len(value.encode('utf-8')) if isinstance(value, str) else
                     len(str(value)) if value is not None else 0)
        count += 1
        depth += (event in ('start_map', 'start_array')) - (event in ('end_map', 'end_array'))
        if size > limit or depth > 64 or count > 2_000_000:
            raise ValueError('catalog entry exceeds size or depth limit')
        builder.event(event, value)
        if not depth:
            return builder.value
        event, value = next(events)


def _catalog_json(value):
    # ijson's default Decimal mode avoids rounding opaque historical numbers.
    try:
        return _encode(value)
    except TypeError:
        if isinstance(value, Decimal):
            return str(value).encode('ascii')
        if isinstance(value, dict):
            return b'{' + b','.join(_encode(k) + b':' + _catalog_json(v) for k, v in value.items()) + b'}'
        if isinstance(value, list):
            return b'[' + b','.join(map(_catalog_json, value)) + b']'
        raise


class _Writer:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.size = stream, limit, 0
        self.hash = hashlib.sha256()

    def write(self, raw):
        if self.size + len(raw) > self.limit:
            raise ValueError('catalog target exceeds byte limit')
        if self.stream.write(raw) != len(raw):
            raise OSError('short catalog target write')
        self.hash.update(raw)
        self.size += len(raw)


def _member(name, value, limit):
    raw = _catalog_json(value)
    if len(raw) > limit:
        raise ValueError('encoded catalog entry exceeds byte limit')
    return _encode(name) + b':' + raw


def _map(events, limit):
    if next(events)[0] != 'start_map':
        raise ValueError('expected a catalog object')
    names = set()
    name_bytes = 0
    for event, name in events:
        if event == 'end_map':
            return
        if event != 'map_key' or name in names:
            raise ValueError('invalid or duplicate catalog map field')
        size = len(name.encode('utf-8'))
        name_bytes += size
        if len(names) >= 65536 or size > min(limit, BLOCK) or name_bytes > 8 * 1024 * 1024:
            raise ValueError('catalog map exceeds field limit')
        names.add(name)
        yield name
    raise ValueError('incomplete catalog object')


def _generation(name):
    return isinstance(name, str) and GENERATION.fullmatch(name) is not None


def _entry(name, value):
    if (not isinstance(value, dict) or value.get('generation') != name or 'gc_stub' in value
            or value.get('status') not in ('current', 'superseded')
            or not isinstance(value.get('references'), list)
            or any(not isinstance(ref, dict) or not all(isinstance(ref.get(k), str) and ref[k]
                       for k in ('bucket', 'path')) for ref in value['references'])):
        raise ValueError('invalid or incomplete catalog generation')


def plan(source, target, *, key: str, webp_path: str, avif_path: str,
         before_sidecar: bytes, after_sidecar: bytes, now: str,
         expected_parent_sha256: str | None = None, max_source_bytes: int = MAX_CATALOG_BYTES,
         max_target_bytes: int = MAX_CATALOG_BYTES, max_entry_bytes: int = MAX_ENTRY_BYTES) -> dict:
    """Read a plain JSON binary stream and emit one compact JSON successor.

    The sidecars are exact packed gzip bytes; the new snapshot references the
    supplied after-sidecar bytes (no snapshot write). Catalog output remains
    local. A bounded temporary stream lets us validate a nonseekable parent
    before touching the caller's target and always emit the current pointer
    first, as required by existing streaming readers. Discard the target on a
    write failure. The caller owns both streams.
    """
    if source is target or any(type(n) is not int or n < 1
                               for n in (max_source_bytes, max_target_bytes, max_entry_bytes)):
        raise ValueError('invalid catalog streams or limits')
    if expected_parent_sha256 is not None:
        image._hex(expected_parent_sha256)
    if not isinstance(now, str) or datetime.fromisoformat(now.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('catalog timestamp must have a timezone')
    before_sha, after_sha, before_refs, after_refs = _route_delta(
        before_sidecar, after_sidecar, key, webp_path, avif_path)

    reader = _Reader(source, max_source_bytes, max_entry_bytes)
    events = reader.events()
    identities = set()
    head = {}
    previous = successor = previous_published = hint = None
    removed = (image.BUCKET, webp_path)
    with TemporaryFile() as spool:
        body = _Writer(spool, max_target_bytes)
        try:
            for field in _map(events, max_entry_bytes):
                if field == 'redirect_sidecar_pending':
                    raise ValueError('catalog sidecar reconciliation is pending')
                if field in ('kind', 'version', 'current'):
                    head[field] = _value(events, next(events), max_entry_bytes)
                    continue
                body.write(b',' + _encode(field) + b':' if field in ('generations', 'retired_generations') else b',')
                if field not in ('generations', 'retired_generations'):
                    body.write(_member(field, _value(events, next(events), max_entry_bytes), max_entry_bytes))
                    continue

                body.write(b'{')
                comma = b''
                for name in _map(events, max_entry_bytes):
                    if not _generation(name) or name in identities:
                        raise ValueError('invalid or duplicate catalog generation ID')
                    identities.add(name)
                    value = _value(events, next(events), max_entry_bytes)
                    if not isinstance(value, dict):
                        raise ValueError('invalid catalog history entry')
                    if field == 'generations':
                        _entry(name, value)
                        if value['status'] == 'current':
                            if previous is not None or value.get('sidecar_sha256') != before_sha:
                                raise ValueError('current catalog generation does not uniquely bind the before-sidecar')
                            prior_refs = _refs(value['references'])
                            if not before_refs <= prior_refs:
                                raise ValueError('current catalog does not cover the before-sidecar references')
                            acks = value.get('replacement_acks')
                            if isinstance(acks, dict) and any(acks.get(surface) is True for surface in ('hf', 'pages')):
                                raise ValueError('current catalog generation already has replacement acknowledgments')
                            previous, previous_published = name, value.get('published_at')
                            refs = sorted((prior_refs - {removed}) | after_refs)
                            signature = lifecycle._generation_id({'sidecar_sha256': after_sha, 'references': refs}, None)
                            successor = lifecycle._generation_id({'signature': signature}, name)
                            snapshot = {'bucket': image.BUCKET,
                                        'path': f'reader-index/generations/{successor}/reader_assets.json.gz',
                                        'sha256': image._sha(after_sidecar), 'bytes': len(after_sidecar)}
                            entry = {'generation': successor, 'status': 'current', 'published_at': now,
                                     'signature': signature, 'sidecar_sha256': after_sha, 'parent_generation': name,
                                     'references': [{'bucket': b, 'path': p} for b, p in refs],
                                     'acks': {'hf': False, 'pages': False}, 'snapshot': snapshot}
                            del prior_refs, refs
                            if removed not in after_refs:
                                hint = retirement.record([{'bucket': removed[0], 'path': removed[1]}], [],
                                                         successor, 'catalog-replacement')
                            value.update(status='superseded', superseded_at=now)
                    body.write(comma + _member(name, value, max_entry_bytes))
                    del value
                    comma = b','
                if field == 'generations' and successor is not None:
                    body.write(comma + _member(successor, entry, max_entry_bytes))
                    del entry
                body.write(b'}')
            if next(events, None) is not None:
                raise ValueError('trailing catalog JSON')
        except StopIteration as error:
            raise ValueError('incomplete catalog JSON') from error

        parent_sha = reader.hash.hexdigest()
        if expected_parent_sha256 is not None and parent_sha != expected_parent_sha256:
            raise ValueError('exact catalog parent digest differs')
        current = head.get('current')
        if (type(head.get('version')) is not int or head['version'] != lifecycle.CATALOG_VERSION
                or head.get('kind') != 'reader-catalog' or previous is None or not isinstance(current, dict)
                or current.get('generation') != previous or not isinstance(previous_published, str)
                or current.get('published_at') != previous_published):
            raise ValueError('invalid catalog header or current pointer')
        if successor in identities:
            raise ValueError('successor catalog generation already exists')

        current.update(generation=successor, published_at=now)
        header = b'{' + b','.join(_member(field, head[field], max_entry_bytes)
                                 for field in ('version', 'kind', 'current'))
        if len(header) + body.size + 2 > max_target_bytes:
            raise ValueError('catalog target exceeds byte limit')
        writer = _Writer(target, max_target_bytes)
        writer.write(header)
        spool.seek(0)
        while chunk := spool.read(BLOCK):
            writer.write(chunk)
        writer.write(b'}\n')
    return {'parent_sha256': parent_sha, 'parent_bytes': reader.size,
            'target_sha256': writer.hash.hexdigest(), 'target_bytes': writer.size,
            'before_sidecar_sha256': before_sha, 'after_sidecar_sha256': after_sha,
            'parent_generation': previous, 'generation': successor, 'snapshot': snapshot,
            'retirement_hint': hint, 'deletion_authorized': False}
