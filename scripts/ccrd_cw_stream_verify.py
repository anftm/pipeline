"""Read-only streaming digest of the activated CW index; no local copy.

This tool never writes to the bucket, the filesystem, a cache, or a workflow.
It reads current.json twice and performs a single If-Match GET for the exact CW
object, hashing bounded chunks without retaining them. It requires S3 read
credentials and transfers the complete database (currently about 796 MB).

Even a matching full digest is not a writer fence, a history/ABA proof, SQLite
semantic validation, consumer verification, a receipt or publication approval.
Run explicitly with --live; importing or unit-testing never initiates network IO.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time

try:
    from .ccrd_strict_json import loads as strict_json_loads
except ImportError:
    from ccrd_strict_json import loads as strict_json_loads


BUCKET = 'ccrd-index'
NAMESPACE = 'vomebook'
MAX_POINTER_BYTES = 1024 * 1024
MAX_DATABASE_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 256 * 1024


def _read_pointer(client):
    response = client.get_object(Bucket=BUCKET, Key='current.json')
    body = response['Body']
    try:
        raw = body.read(MAX_POINTER_BYTES + 1)
    finally:
        body.close()
    length = response.get('ContentLength')
    if type(length) is not int or not 0 < length <= MAX_POINTER_BYTES or len(raw) != length:
        raise ValueError('current manifest is incomplete or outside its byte limit')
    return raw


def _cw_identity(raw):
    pointer = strict_json_loads(raw)
    if not isinstance(pointer, dict):
        raise ValueError('current manifest must be an object')
    generation, databases = pointer.get('generation'), pointer.get('databases')
    if (type(pointer.get('format')) is not int or pointer['format'] != 2
            or pointer.get('kind') != 'ccrd-four-source-generation'
            or not isinstance(generation, str) or not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', generation)
            or not isinstance(databases, dict) or set(databases) != {'CCRD', 'CW', 'RMRB', 'PLA'}
            or not isinstance(databases['CW'], dict)):
        raise ValueError('current manifest is not the supported four-source generation')
    item = databases['CW']
    path, size, digest = item.get('path'), item.get('bytes'), item.get('sha256')
    if (not isinstance(path, str) or not re.fullmatch(r'generations/ccrd-[^/]+/CW\.sqlite3', path)
            or type(size) is not int or not 100 <= size <= MAX_DATABASE_BYTES
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('unsupported CW database identity or transfer size')
    return generation, path, size, digest


def _head(client, path, size):
    response = client.head_object(Bucket=BUCKET, Key=path)
    etag = response.get('ETag')
    if (response.get('ContentLength') != size or not isinstance(etag, str)
            or not re.fullmatch(r'"[^"\r\n]+"', etag)):
        raise ValueError('CW object HEAD lacks the declared size or strong ETag')
    return etag, response.get('VersionId')


def verify(client, *, clock=time.monotonic):
    """One read-only bounded-memory pass using an already-created S3 client."""
    started = clock()
    before = _read_pointer(client)
    generation, path, size, expected = _cw_identity(before)
    etag, version = _head(client, path, size)
    response = client.get_object(Bucket=BUCKET, Key=path, IfMatch=etag)
    body = response['Body']
    total, digest, header = 0, hashlib.sha256(), b''
    try:
        if response.get('ContentLength') != size or response.get('ETag') != etag:
            raise ValueError('conditional CW GET identity differs from HEAD')
        while True:
            chunk = body.read(CHUNK_BYTES)
            if not chunk:
                break
            if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                raise ValueError('invalid streaming response chunk')
            if len(header) < 100:
                header += chunk[:100 - len(header)]
            total += len(chunk)
            if total > size:
                raise ValueError('CW object exceeds its declared length')
            digest.update(chunk)
    finally:
        body.close()
    if total != size or digest.hexdigest() != expected:
        raise ValueError('full CW object length or SHA-256 differs from its manifest')
    if len(header) < 100 or header[:16] != b'SQLite format 3\0':
        raise ValueError('verified bytes do not begin with a SQLite header')
    page_size = int.from_bytes(header[16:18], 'big') or 65536
    if (page_size < 512 or page_size > 65536 or page_size & (page_size - 1)
            or header[20] != 0 or int.from_bytes(header[28:32], 'big') * page_size != size):
        raise ValueError('unsupported SQLite page size, reserved bytes or page count')
    final_etag, final_version = _head(client, path, size)
    if (etag, version) != (final_etag, final_version) or _read_pointer(client) != before:
        raise ValueError('CW object or current manifest changed during verification')
    return {'read_only': True, 'generation': generation, 'pointer_sha256': hashlib.sha256(before).hexdigest(),
            'cw_sha256': expected, 'bytes_streamed': total, 'sqlite_page_size': page_size,
            'seconds': round(clock() - started, 2), 'object_version_id_present': bool(version),
            'entire_object_sha256_verified': True, 'full_sqlite_semantics_verified': False,
            'external_writers_fenced': False, 'aba_excluded': False, 'saved_to_disk': False,
            'remote_writes': 0, 'publication_ready': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='stream the current full CW database')
    parser.parse_args()
    key, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
    if not key or not secret:
        raise RuntimeError('HF S3 read credentials are required')
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', endpoint_url=f'https://s3.hf.co/{NAMESPACE}',
                          aws_access_key_id=key, aws_secret_access_key=secret,
                          config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                        connect_timeout=10, read_timeout=60, retries={'max_attempts': 2}))
    print(json.dumps(verify(client), sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
