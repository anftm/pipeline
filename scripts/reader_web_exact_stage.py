"""Local review and immutable staging for one exact web replacement.

This module does not dispatch a workflow or change the mutable indexes. The
operator-provided guard must be checked in the real controlled writer window.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

from .publish_reader_v3 import encode
from .reader_bucket_store import s3_error_code
from .reader_stream_checkpoint_cas import strong_etag
from .reader_web_exact_plan import BUCKET, _web_row, _json_equal


def retention(old):
    root = _web_row(old, old=True)
    prefix = root.rsplit('/', 1)[1]
    raw = encode({'version': 1, 'kind': 'reader-web-retained-objects',
                  'resources': [{'bucket': BUCKET, 'path': old['manifest']}]})
    return f'reader-index/retained-web-{old["extension"]}-{prefix}.json', raw


def new_retention(candidate):
    root = _web_row(candidate, old=False)
    return ('reader-index/retained-web-new-' + root.rsplit('/', 1)[1] + '.json',
            encode({'version': 1, 'kind': 'reader-web-retained-objects',
                    'resources': [{'bucket': BUCKET, 'path': candidate['manifest']}]}))


def _read_optional(read_bytes, path):
    try:
        return read_bytes(BUCKET, path)
    except FileNotFoundError:
        return None


def assert_old(read_bytes, old, *, manifest_sha):
    html = read_bytes(BUCKET, old['object'])
    manifest = read_bytes(BUCKET, old['manifest'])
    if len(html) != old['bytes'] or sha256(html).hexdigest() != old['sha256'] or sha256(manifest).hexdigest() != manifest_sha:
        raise ValueError('old objects do not match their exact reviewed identities')


def stage(*, old, candidate, html, manifest, old_manifest_sha, new_manifest_sha,
          snapshot, snapshot_bytes, s3_read, resolver_read, put_once, guard):
    """Protect old and new immutable outputs before any routing mutation."""
    _web_row(old, old=True)
    _web_row(candidate, old=False)
    if (old['key'] != candidate['key'] or old['source_sha256'] != candidate['source_sha256']
            or old['object'] == candidate['object']):
        raise ValueError('staging requires the exact same original and a new path')
    if len(html) != candidate['bytes'] or sha256(html).hexdigest() != candidate['sha256']:
        raise ValueError('candidate HTML differs from preflight')
    expected_manifest = {'kind': 'web-document-stream', 'version': 1, 'path': 'document.html',
                         'source_extension': candidate['extension'], 'source_sha256': candidate['source_sha256'],
                         'bytes': candidate['bytes'], 'sha256': candidate['sha256']}
    if sha256(manifest).hexdigest() != new_manifest_sha or not _json_equal(json.loads(manifest), expected_manifest):
        raise ValueError('candidate manifest differs from preflight')
    entry_path = snapshot['path']
    if (not re.fullmatch(r'reader-index/generations/[0-9a-f]{32}/reader_assets\.json\.gz', entry_path)
            or snapshot.get('bucket') != BUCKET or len(snapshot_bytes) != snapshot['bytes']
            or sha256(snapshot_bytes).hexdigest() != snapshot['sha256']):
        raise ValueError('legacy snapshot differs from review')
    html.decode('utf-8', errors='strict')

    def check(phase):
        if guard(phase) is not True:
            raise RuntimeError('exact web writer window not verified')

    check('old-objects')
    assert_old(s3_read, old, manifest_sha=old_manifest_sha)
    assert_old(resolver_read, old, manifest_sha=old_manifest_sha)
    pin_path, pin = retention(old)
    # After both new members exist, keep the manifest protected through the
    # first canonical CAS and recovery. The caller holds GC paused throughout.
    stagepin_path, stagepin = new_retention(candidate)
    members = [(pin_path, pin, 'application/json'),
               (candidate['object'], html, 'text/html; charset=utf-8'),
               (candidate['manifest'], manifest, 'application/json'),
               (stagepin_path, stagepin, 'application/json'),
               (entry_path, snapshot_bytes, 'application/gzip')]
    for path, raw, mime in members:
        check('immutable:' + path)
        observed = _read_optional(s3_read, path)
        if observed is None:
            put_once(path, raw, mime)
        elif observed != raw:
            raise ValueError('immutable web staging conflict: ' + path)
        if s3_read(BUCKET, path) != raw or resolver_read(BUCKET, path) != raw:
            raise ValueError('immutable web staging readback differs: ' + path)
    check('staged')
    assert_old(resolver_read, old, manifest_sha=old_manifest_sha)
    return {'old_retention': pin_path, 'new_retention': stagepin_path,
            'new_html': candidate['object'], 'snapshot': entry_path}


def immutable_creator(client):
    if (client.meta.config.retries or {}).get('total_max_attempts') != 1:
        raise ValueError('exact web staging requires a single-attempt S3 client')

    def create(path, raw, mime):
        try:
            result = client.put_object(Bucket='reader-assets-v2', Key=path, Body=raw,
                                       ContentType=mime, IfNoneMatch='*')
        except Exception as error:
            if s3_error_code(error)[1] not in (409, 412):
                raise RuntimeError('immutable web PUT unconfirmed; inspect before recovery') from error
            # Definite conflict admits read-only comparison in stage().
            return
        meta = result['ResponseMetadata']
        if meta['HTTPStatusCode'] not in (200, 201) or meta.get('RetryAttempts', 0):
            raise RuntimeError('immutable web PUT unconfirmed; inspect before recovery')
        strong_etag(result.get('ETag'))
    return create
