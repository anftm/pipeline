"""Verify conditional writes on one fresh disposable Reader probe object.

Only ``probes/reader-s3-conditional-<random>.json`` is ever written/deleted.
This does not touch indexes, chapters, processing roots or Reader pointers.
On an unknown mutation outcome the exact key is read once and execution stops;
no request is repeated. This does not establish global writer isolation or
prevent ABA even when all per-object checks succeed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets

try:
    from .reader_bucket_store import HubBucketStore, S3BucketStore, S3NotFound, s3_error_code
    from .reader_page_cas import strong_etag
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    from reader_bucket_store import HubBucketStore, S3BucketStore, S3NotFound, s3_error_code
    from reader_page_cas import strong_etag
    from shared import READER_ASSETS_BUCKET


BUCKET = READER_ASSETS_BUCKET
PREFIX = 'probes/reader-s3-conditional-'
IDENTITY = re.compile(r'[0-9a-f]{32}\Z')


def _conflict(error):
    code, status = s3_error_code(error)
    return status in {409, 412} or str(code) in {'409', '412', 'PreconditionFailed', 'ConditionalRequestConflict'}


def _state(store, key, initial, updated):
    try:
        raw = store.read_bytes(BUCKET, key)
    except S3NotFound:
        return 'absent'
    if raw == initial:
        return 'initial'
    if raw == updated:
        return 'updated'
    return 'foreign'


def _mutate_once(store, key, phase, call, initial, updated):
    try:
        call()
    except Exception as error:
        state = _state(store, key, initial, updated)
        print(json.dumps({'kind': 'reader-bucket-probe-unknown', 'probe_key': key,
                          'phase': phase, 'observed': state, 'retry_attempted': False}), flush=True)
        raise RuntimeError('Reader probe mutation outcome requires inspection: ' + phase) from error


def probe(store, hub, probe_id):
    if not isinstance(probe_id, str) or not IDENTITY.fullmatch(probe_id):
        raise ValueError('Reader probe requires a fresh 32-hex ID')
    key = PREFIX + probe_id + '.json'
    namespace, bucket = store._location(BUCKET)
    if (namespace, bucket) != ('vomebook', 'reader-assets-v2'):
        raise ValueError('unexpected Reader probe bucket mapping')
    client = store._client(namespace)
    initial = json.dumps({'kind': 'reader-s3-conditional-probe', 'id': probe_id, 'step': 1},
                         sort_keys=True, separators=(',', ':')).encode()
    updated = json.dumps({'kind': 'reader-s3-conditional-probe', 'id': probe_id, 'step': 2},
                         sort_keys=True, separators=(',', ':')).encode()
    print(json.dumps({'kind': 'reader-bucket-probe', 'phase': 'before-write', 'probe_key': key}), flush=True)
    if _state(store, key, initial, updated) != 'absent':
        raise RuntimeError('Reader probe key already exists; refusing to overwrite')
    _mutate_once(store, key, 'create', lambda: client.put_object(
        Bucket=bucket, Key=key, Body=initial, ContentType='application/json', IfNoneMatch='*'), initial, updated)
    if store.read_bytes(BUCKET, key) != initial:
        raise ValueError('Reader initial S3 readback differs')
    first = strong_etag(store.read_etags.get((BUCKET, key)))
    if hub.read_bytes(BUCKET, key) != initial:
        raise ValueError('Reader initial Hub/S3 readback differs')
    try:
        client.put_object(Bucket=bucket, Key=key, Body=updated, ContentType='application/json', IfNoneMatch='*')
    except Exception as error:
        if not _conflict(error):
            _state(store, key, initial, updated)
            raise RuntimeError('Reader duplicate create outcome is unknown; inspect probe key') from error
    else:
        raise ValueError('Reader duplicate conditional create unexpectedly succeeded')
    if _state(store, key, initial, updated) != 'initial':
        raise ValueError('Reader probe changed after rejected duplicate create')
    _mutate_once(store, key, 'update', lambda: client.put_object(
        Bucket=bucket, Key=key, Body=updated, ContentType='application/json', IfMatch=first), initial, updated)
    if store.read_bytes(BUCKET, key) != updated:
        raise ValueError('Reader updated S3 readback differs')
    latest = strong_etag(store.read_etags.get((BUCKET, key)))
    if hub.read_bytes(BUCKET, key) != updated:
        raise ValueError('Reader updated Hub/S3 readback differs')
    try:
        client.put_object(Bucket=bucket, Key=key, Body=initial, ContentType='application/json', IfMatch=first)
    except Exception as error:
        if not _conflict(error):
            _state(store, key, initial, updated)
            raise RuntimeError('Reader stale update outcome is unknown; inspect probe key') from error
    else:
        raise ValueError('Reader stale conditional update unexpectedly succeeded')
    if _state(store, key, initial, updated) != 'updated':
        raise ValueError('Reader probe changed after rejected stale update')
    _mutate_once(store, key, 'delete', lambda: client.delete_object(
        Bucket=bucket, Key=key, IfMatch=latest), initial, updated)
    if _state(store, key, initial, updated) != 'absent':
        raise ValueError('Reader probe still exists after conditional delete')
    try:
        hub.read_bytes(BUCKET, key)
    except FileNotFoundError:
        pass
    else:
        raise ValueError('Reader probe still visible in Hub after conditional delete')
    return {'version': 1, 'kind': 'reader-bucket-compatibility-probe', 'bucket': BUCKET, 'probe_key': key,
            'if_none_match_verified': True, 'if_match_verified': True, 'stale_match_rejected': True,
            'hub_s3_reads_match': True, 'conditional_delete_verified': True,
            'updated_sha256': hashlib.sha256(updated).hexdigest(), 'production_roots_written': False,
            'external_writers_isolated': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='conditionally create and delete exactly one random probe')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'bucket': BUCKET, 'prefix': PREFIX, 'write_enabled': False,
                          'production_roots_written': False, 'external_writers_isolated': False}))
        return 0
    from botocore.config import Config
    store = S3BucketStore()
    if store._namespace != 'vomebook':
        raise ValueError('Reader probe requires the vomebook S3 namespace')
    store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                                               request_checksum_calculation='when_required',
                                               response_checksum_validation='when_required'))
    result = probe(store, HubBucketStore(), secrets.token_hex(16))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
