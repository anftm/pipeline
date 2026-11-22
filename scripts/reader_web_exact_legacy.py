"""One verified web route through the existing streaming legacy catalog transaction."""
from __future__ import annotations

import gzip
from hashlib import sha256

from . import publish_reader_v3 as publication
from . import reader_image_history_catalog as catalog
from . import reader_image_history_publish as legacy
from . import reader_lifecycle, reader_route_control as control
from .reader_web_exact_plan import BUCKET, _web_row


def route(row):
    return {'s': 2, 'm': 'h', 'p': row['object'], 'b': BUCKET}


def pack(before_packed, old, candidate):
    _web_row(old, old=True)
    _web_row(candidate, old=False)
    if (candidate['key'] != old['key'] or candidate['source_sha256'] != old['source_sha256']
            or candidate['object'] == old['object']):
        raise ValueError('not one new derivative of the exact same web source')
    original = catalog._sidecar(before_packed)
    if original['f'].get(old['key']) != route(old):
        raise ValueError('reviewed old route does not match the canonical sidecar')
    original['f'][old['key']] = route(candidate)
    return gzip.compress(publication.encode(original), compresslevel=9, mtime=0)


def verified_delta(before_packed, after_packed, old, candidate):
    before, after = map(catalog._sidecar, (before_packed, after_packed))
    _web_row(old, old=True)
    _web_row(candidate, old=False)
    key = old['key']
    if (candidate['key'] != key or candidate['source_sha256'] != old['source_sha256']
            or candidate['object'] == old['object'] or before['f'].get(key) != route(old)
            or after['f'].get(key) != route(candidate)):
        raise ValueError('sidecar does not bind the exact old and new web routes')
    before['f'][key] = route(candidate)
    if publication.encode(before) != publication.encode(after):
        raise ValueError('new legacy sidecar is not the exact one-route successor')
    before['f'][key] = route(old)
    def refs(value):
        return {(ref['bucket'], ref['path']) for ref in reader_lifecycle.sidecar_references(value)}
    def digest(value):
        return sha256(publication.encode(value)).hexdigest()
    return digest(before), digest(after), refs(before), refs(after)


def project_delta(store, before_packed, after_packed, old, candidate):
    verified_delta(before_packed, after_packed, old, candidate)
    pointer, reading = publication.current(store)
    key = old['key']
    if (not pointer or reading.get('derived_pdf_redirects') or not reading.get('base_sidecar')
            or key in reading.get('files', {}) or key in reading.get('suppressed_sources', [])):
        raise ValueError('ordinary v3 unification cannot expose this exact web route')
    before = publication.project_sidecar(publication.pinned_base(store, reading, {}), reading)
    projected, _ = control.prepare_base(reading, after_packed)
    after = publication.project_sidecar(catalog._sidecar(projected), reading)
    if before['f'].get(key) != route(old):
        raise ValueError('old v3 route differs from review')
    before['f'][key] = route(candidate)
    if before != after:
        raise ValueError('new pinned v3 projection changes more than the reviewed web route')
    return pointer


def plan(parent_catalog, target, *, before_packed, after_packed, old, candidate, now, parent_sha):
    delta = verified_delta(before_packed, after_packed, old, candidate)
    return catalog.plan_route_transition(parent_catalog, target, now=now, removed=(BUCKET, old['object']),
                                         verified_delta=delta, after_sidecar=after_packed,
                                         expected_parent_sha256=parent_sha)


def publish(parent_catalog, *, before_packed, after_packed, old, candidate, now, **controls):
    parent_sha = controls['parent'].sha256
    return legacy.publish_route_transition(
        before_sidecar=before_packed, after_sidecar=after_packed, key=old['key'],
        seal_kind='reader-web-legacy-publish-v1',
        plan_catalog=lambda target: plan(parent_catalog, target, before_packed=before_packed,
                                         after_packed=after_packed, old=old, candidate=candidate,
                                         now=now, parent_sha=parent_sha), **controls)
