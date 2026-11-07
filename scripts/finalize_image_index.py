#!/usr/bin/env python3
"""Merge owned image shards without dropping or rolling back canonical routes."""
from __future__ import annotations

import argparse
import json
import os
import re

import boto3
from botocore.config import Config

try:
    from .reader_bucket_store import retry_s3
    from .reader_stream_index import shard_for, validate_index
except ImportError:
    from reader_bucket_store import retry_s3
    from reader_stream_index import shard_for, validate_index

ROOT = 'pages/image'
CANONICAL = ROOT + '/index-v2.json'


def merge(canonical, shards, shard_count=8):
    """Only newer, shard-owned checkpoints may replace a canonical mapping."""
    validate_index(canonical, CANONICAL)
    current = {entry['key']: entry for entry in canonical['files']}
    if not current:
        raise ValueError('canonical image index is empty')
    if len(current) != len(canonical['files']):
        raise ValueError('duplicate canonical image key')
    seen_shards = set()
    for number, shard in sorted(shards):
        if type(number) is not int or not 0 <= number < shard_count or number in seen_shards:
            raise ValueError('invalid or duplicate image shard')
        seen_shards.add(number)
        validate_index(shard, f'image shard {number}')
        seen = set()
        for entry in shard['files']:
            key = entry['key']
            if key in seen or shard_for(key, shard_count) != number:
                raise ValueError('duplicate or foreign key in image shard')
            seen.add(key)
            prior = current.get(key)
            if prior is not None and prior != entry:
                before, after = prior.get('checkpointed_at', ''), entry.get('checkpointed_at', '')
                # A timestamped new checkpoint may supersede an untimestamped
                # legacy row; a legacy or older shard can never roll it back.
                if not after:
                    continue
                if before and before == after:
                    raise ValueError('conflicting image checkpoint timestamps')
                if before and after < before:
                    continue
            current[key] = entry
    return {'version': 1, 'kind': 'image-stream-index',
            'files': [current[key] for key in sorted(current)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='publish after a successful merge')
    args = parser.parse_args()
    namespace, bucket = os.environ.get('HF_S3_BUCKET', 'vomebook/reader-assets-v2').rsplit('/', 1)
    client = boto3.client('s3', endpoint_url=f'https://s3.hf.co/{namespace}',
        aws_access_key_id=os.environ['HF_S3_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['HF_S3_SECRET_ACCESS_KEY'],
        config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                      retries={'mode': 'adaptive', 'max_attempts': 8}))

    def read(key):
        def load():
            response = client.get_object(Bucket=bucket, Key=key)
            try:
                return validate_index(json.loads(response['Body'].read()), key)
            finally:
                response['Body'].close()
        return retry_s3(load)

    # Existing canonical data must be readable. A missing or inaccessible index
    # is not an empty baseline and must never clear the active routes.
    canonical = read(CANONICAL)
    keys = []
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=ROOT + '/index-v2-'):
        keys.extend(item['Key'] for item in page.get('Contents', [])
                    if re.fullmatch(re.escape(ROOT) + r'/index-v2-0[0-7]\.json', item['Key']))
    shards = [(int(key.rsplit('-', 1)[1].split('.')[0]), read(key)) for key in sorted(keys)]
    result = merge(canonical, shards)
    print(f"image mappings={len(result['files'])} shard_indexes={len(shards)} apply={args.apply}")
    if args.apply and result != canonical:
        body = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        retry_s3(lambda: client.put_object(Bucket=bucket, Key=CANONICAL, Body=body,
                                          ContentType='application/json'))
        if read(CANONICAL) != result:
            raise ValueError('canonical image index readback failed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
