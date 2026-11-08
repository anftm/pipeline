"""Verify CCRD bucket conditions on one disposable, exact random probe key.

The default CLI is read-only. An applied probe uses a random name under the
exclusive probes/ccrd-conditional- prefix and never touches production roots.
Each write is attempted once; an unknown outcome requires exact-state review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets

try:
    from .reader_bucket_store import (
        HubBucketStore,
        S3BucketStore,
        S3NotFound,
        s3_error_code,
    )
    from .reader_page_cas import strong_etag
except ImportError:
    from reader_bucket_store import (
        HubBucketStore,
        S3BucketStore,
        S3NotFound,
        s3_error_code,
    )
    from reader_page_cas import strong_etag


BUCKET = 'vomebook/ccrd-index'
PREFIX = 'probes/ccrd-conditional-'
PROBE_ID = re.compile(r'[0-9a-f]{32}\Z')


def _conflict(error: BaseException) -> bool:
    code, status = s3_error_code(error)
    return status in {409, 412} or str(code) in {'409', '412', 'PreconditionFailed', 'ConditionalRequestConflict'}


def probe(store, hub, probe_id: str) -> dict:
    if not isinstance(probe_id, str) or not PROBE_ID.fullmatch(probe_id):
        raise ValueError('CCRD probe ID must be 32 lowercase hexadecimal characters')
    key = PREFIX + probe_id + '.json'
    namespace, bucket = store._location(BUCKET)
    if (namespace, bucket) != ('vomebook', 'ccrd-index'):
        raise ValueError('unexpected CCRD bucket mapping')
    client = store._client(namespace)
    initial = json.dumps({'kind': 'ccrd-conditional-probe', 'id': probe_id, 'step': 1},
                         sort_keys=True, separators=(',', ':')).encode()
    updated = json.dumps({'kind': 'ccrd-conditional-probe', 'id': probe_id, 'step': 2},
                         sort_keys=True, separators=(',', ':')).encode()
    print(json.dumps({'kind': 'ccrd-conditional-probe', 'phase': 'before-write', 'probe_key': key}), flush=True)
    try:
        store.read_bytes(BUCKET, key)
    except S3NotFound:
        pass
    else:
        raise RuntimeError('CCRD probe key already exists; refusing to overwrite')
    client.put_object(Bucket=bucket, Key=key, Body=initial, ContentType='application/json', IfNoneMatch='*')
    raw = store.read_bytes(BUCKET, key)
    first = strong_etag(store.read_etags.get((BUCKET, key)))
    if raw != initial or hub.read_bytes(BUCKET, key) != initial:
        raise ValueError('CCRD initial Hub/S3 probe readback differs')
    try:
        client.put_object(Bucket=bucket, Key=key, Body=updated, ContentType='application/json', IfNoneMatch='*')
    except Exception as error:
        if not _conflict(error):
            raise
    else:
        raise ValueError('CCRD duplicate conditional create unexpectedly succeeded')
    client.put_object(Bucket=bucket, Key=key, Body=updated, ContentType='application/json', IfMatch=first)
    raw = store.read_bytes(BUCKET, key)
    latest = strong_etag(store.read_etags.get((BUCKET, key)))
    if raw != updated or hub.read_bytes(BUCKET, key) != updated:
        raise ValueError('CCRD updated Hub/S3 probe readback differs')
    try:
        client.put_object(Bucket=bucket, Key=key, Body=initial, ContentType='application/json', IfMatch=first)
    except Exception as error:
        if not _conflict(error):
            raise
    else:
        raise ValueError('CCRD stale conditional update unexpectedly succeeded')
    client.delete_object(Bucket=bucket, Key=key, IfMatch=latest)
    try:
        store.read_bytes(BUCKET, key)
    except S3NotFound:
        pass
    else:
        raise ValueError('CCRD probe still exists after conditional deletion')
    return {'version': 1, 'kind': 'ccrd-bucket-compatibility-probe', 'bucket': BUCKET,
            'probe_key': key, 'if_none_match_verified': True, 'if_match_verified': True,
            'stale_match_rejected': True, 'hub_s3_reads_match': True, 'conditional_delete_verified': True,
            'updated_sha256': hashlib.sha256(updated).hexdigest(), 'production_pointer_written': False,
            'external_writers_isolated': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='conditionally create and delete one random probe')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'bucket': BUCKET, 'prefix': PREFIX, 'write_enabled': False,
                          'production_pointer_written': False}))
        return 0
    from botocore.config import Config
    store = S3BucketStore()
    store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                                               request_checksum_calculation='when_required',
                                               response_checksum_validation='when_required'))
    result = probe(store, HubBucketStore(), secrets.token_hex(16))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
