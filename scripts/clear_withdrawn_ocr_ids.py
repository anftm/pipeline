#!/usr/bin/env python3
"""Clear only the exact 781 retired OCR ID mappings, preserving audit bytes.

The original OCR retirement deliberately left these IDs in a withdrawn
registry. This command does not delete OCR data, change OCR code, publish an
OCR release, touch Reader routes, or resolve the CHM maintenance callback.
"""
import hashlib
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config

try:
    from . import ccrd_lifecycle as lifecycle, chm_subset_locked_preflight as preflight
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import ccrd_lifecycle as lifecycle
    import chm_subset_locked_preflight as preflight
    from reader_gc_fence import GitHubProducerFence

BUCKET = 'vomebook/ccrd-index'
IDS = 'control/ocr-ids.json'
CURRENT = 'control/ocr-current.json'
CONSUMER = 'control/ocr-consumer.json'
PLAN = 'reports/ccrd-ocr-reset/ccrd-ocr-exact-20261030/exact-plan.json'
SNAPSHOT = 'reports/ocr-id-cleanup/20261102/ocr-ids-before.json'
INTENT = 'reports/ocr-id-cleanup/20261102/intent.json'
RECEIPT = 'reports/ocr-id-cleanup/20261102/receipt.json'
BEFORE_SHA = '34a56d07b9c63f1603393ef7b83a0309ec1d626166cdb17e3d6378f356981d0a'
AFTER_SHA = 'cb8c9e7d5e2d90329b380b049ac75384f4d63aaf8ffd303f53b799a1881b2c85'
PLAN_SHA = '9238b91c4efdfef7fb0087098dd6341256dc152f4d214aed643c0e9be9bd88f1'
CONSUMER_SHA = '560a34a8728c2df38355fbf49357ad065ec8c5ca8bd790bacf79454e8e0c9a35'
GENERATION = 'fb1abf531f3a630d65e77ae0167bd049ddf9a6c46b8758ac4ed0672eb992819e'
WORKFLOW_REF = 'anftm/pipeline/.github/workflows/clear-withdrawn-ocr-ids.yml@refs/heads/main'
OUTPUT = Path('output/clear-withdrawn-ocr-ids/result.json')


class Unknown(RuntimeError):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def inspect(before, plan_raw, consumer_raw):
    if sha(before) != BEFORE_SHA or sha(plan_raw) != PLAN_SHA or sha(consumer_raw) != CONSUMER_SHA:
        raise ValueError('withdrawn OCR controls changed')
    registry, plan, consumer = json.loads(before), json.loads(plan_raw), json.loads(consumer_raw)
    if (registry.get('version') != 1 or registry.get('kind') != 'ccrd-ocr-id-registry'
            or registry.get('status') != 'withdrawn' or registry.get('resources') != []
            or registry.get('reset_id') != 'ccrd-ocr-exact-20261030' or len(registry.get('ids', {})) != 781
            or plan.get('kind') != 'ccrd-exact-ocr-retirement-plan' or plan.get('generation') != GENERATION
            or plan.get('updates', {}).get('ids', {}).get('ids') != registry['ids']
            or consumer.get('kind') != 'ccrd-ocr-consumer' or consumer.get('status') != 'consumer-withdrawn'
            or consumer.get('resources') != []):
        raise ValueError('withdrawn OCR ID ownership differs')
    lifecycle.references(registry, IDS)
    replacement = {**registry, 'ids': {}}
    lifecycle.references(replacement, IDS)
    raw = encode(replacement)
    if sha(raw) != AFTER_SHA:
        raise ValueError('withdrawn OCR ID replacement differs')
    return raw


def optional(reader, path):
    try:
        return reader.read_bytes(BUCKET, path)
    except FileNotFoundError:
        return None


class Writer:
    def __init__(self):
        access, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('OCR ID cleanup credentials unavailable')
        self.client = boto3.client('s3', endpoint_url='https://s3.hf.co/vomebook',
                                   aws_access_key_id=access, aws_secret_access_key=secret,
                                   config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                                 retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=30,
                                                 request_checksum_calculation='when_required',
                                                 response_checksum_validation='when_required'))

    def put(self, path, raw, *, if_match=None):
        if path not in {SNAPSHOT, INTENT, RECEIPT, IDS} or not isinstance(raw, bytes):
            raise ValueError('OCR ID writer scope differs')
        if path == IDS:
            if sha(raw) != AFTER_SHA or not isinstance(if_match, str) or not if_match:
                raise ValueError('OCR ID replacement or observed ETag differs')
            condition = {'IfMatch': if_match}
        else:
            if if_match is not None:
                raise ValueError('OCR audit key cannot be overwritten')
            condition = {'IfNoneMatch': '*'}
        try:
            response = self.client.put_object(Bucket='ccrd-index', Key=path, Body=raw,
                                              ContentType='application/json', **condition)
        except Exception as error:
            raise Unknown('OCR ID write outcome unknown; read back before retry') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise Unknown('OCR ID write response ambiguous')


def ensure_audit(reader, writer, path, raw):
    current = optional(reader, path)
    if current is None:
        writer.put(path, raw)
        current = optional(reader, path)
    if current != raw:
        raise ValueError('OCR audit key differs: ' + path)


def clear(reader, writer, fence):
    preflight._identity(reader)
    fence.check()
    if optional(reader, CURRENT) is not None:
        raise ValueError('OCR release is active again')
    plan_raw, consumer_raw = reader.read_bytes(BUCKET, PLAN), reader.read_bytes(BUCKET, CONSUMER)
    registry = reader.read_bytes(BUCKET, IDS)
    if sha(registry) == BEFORE_SHA:
        before = registry
    elif sha(registry) == AFTER_SHA:
        before = optional(reader, SNAPSHOT)
        if before is None:
            raise ValueError('cleaned registry lacks its saved before image')
    else:
        raise ValueError('OCR ID registry is neither reviewed before nor after')
    replacement = inspect(before, plan_raw, consumer_raw)
    intent = encode({'kind': 'withdrawn-ocr-id-cleanup-intent', 'version': 1, 'before_sha256': BEFORE_SHA,
                     'after_sha256': AFTER_SHA, 'retirement_plan_sha256': PLAN_SHA,
                     'consumer_sha256': CONSUMER_SHA, 'removed_ids': 781, 'snapshot_path': SNAPSHOT})
    receipt = encode({'kind': 'withdrawn-ocr-id-cleanup-receipt', 'version': 1, 'before_sha256': BEFORE_SHA,
                      'after_sha256': AFTER_SHA, 'intent_sha256': sha(intent), 'removed_ids': 781,
                      'ocr_release_active': False})
    ensure_audit(reader, writer, SNAPSHOT, before)
    ensure_audit(reader, writer, INTENT, intent)
    preflight._identity(reader)
    fence.check()
    if optional(reader, CURRENT) is not None or reader.read_bytes(BUCKET, CONSUMER) != consumer_raw:
        raise ValueError('OCR consumer or current pointer changed')
    live = reader.read_bytes(BUCKET, IDS)
    if live == before:
        etag = reader.reader.read_etags.get((BUCKET, IDS))
        writer.put(IDS, replacement, if_match=etag)
    elif live != replacement:
        raise ValueError('OCR ID registry changed before cleanup')
    if reader.read_bytes(BUCKET, IDS) != replacement:
        raise Unknown('OCR ID registry readback differs')
    if optional(reader, CURRENT) is not None or reader.read_bytes(BUCKET, CONSUMER) != consumer_raw:
        raise ValueError('OCR consumer changed after cleanup')
    preflight._identity(reader)
    fence.check()
    ensure_audit(reader, writer, RECEIPT, receipt)
    return {'kind': 'withdrawn-ocr-id-cleanup', 'removed_ids': 781, 'remaining_ids': 0,
            'registry_before_sha256': BEFORE_SHA, 'registry_after_sha256': AFTER_SHA,
            'snapshot_path': SNAPSHOT, 'intent_path': INTENT, 'receipt_path': RECEIPT,
            'ocr_release_active': False, 'original_chm_complete': False,
            'producer_restore_authorized': False, 'ocr_data_deleted': 0,
            'caveats': ['shared Actions groups do not fence external Hub/S3 writers',
                        'historical audit plan retains the old ID mapping as evidence']}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('OCR ID cleanup requires its central workflow')
    preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('OCR ID cleanup output already exists')
    result = clear(preflight.ReadOnlyStore(), Writer(), GitHubProducerFence(api=preflight.github_get))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, sort_keys=True, indent=2)
        file.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
