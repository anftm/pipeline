#!/usr/bin/env python3
"""Publish one fixed 20-EPUB protection root; this program cannot delete.

The caller must hold all three reviewed Actions concurrency groups. The root
and transaction identity are predetermined, and the original CHM journal and
all source, chapter, and Reader objects are read-only. S3's conditional PUT is
not a lock; the workflow groups serialize cooperating jobs, not external clients.
"""
import json
import os
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

try:
    from . import chm_subset_isolated_root as isolated, chm_subset_locked_graph as locked
    from . import chm_subset_locked_preflight as preflight, reader_gc_graph as graph
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_subset_isolated_root as isolated
    import chm_subset_locked_graph as locked
    import chm_subset_locked_preflight as preflight
    import reader_gc_graph as graph
    from reader_gc_fence import GitHubProducerFence

WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-subset-protect.yml@refs/heads/main'
OUTPUT = Path('output/chm-subset-protect/result.json')


class RootWriteUnknown(RuntimeError):
    """The single root PUT may have succeeded; re-read before doing anything."""


def candidate(rows, classification_raw):
    if preflight.digest(classification_raw) != preflight.CLASSIFICATION_SHA:
        raise ValueError('CHM classification bytes differ')
    items = json.loads(classification_raw)['targets']
    indexed = {row['path']: row for row in rows}
    withheld = sorted((indexed[item['path']] for item in items
                       if item['category'] in {'needs-independent-review', 'withheld-oversize'}),
                      key=lambda row: row['path'])
    root = isolated.build(withheld, locked.IDENTITY)
    if (root['objects'] != 20 or root['sha256'] != locked.ROOT_SHA
            or not graph.is_root(root['path'], preflight.chm.ASSETS)):
        raise ValueError('fixed 20-target protection root differs')
    return root


def read_optional(store, path):
    try:
        return store.read_bytes(preflight.chm.ASSETS, path)
    except FileNotFoundError:
        # ReadOnlyStore already HEADs the owning bucket before surfacing 404.
        return None


class SingleRootWriter:
    """One no-retry PUT to the fixed protection key, with no delete method."""
    def __init__(self):
        access = os.environ.get('HF_S3_ACCESS_KEY_ID')
        secret = os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('CHM protection writer credentials are unavailable')
        self.client = boto3.client('s3', endpoint_url='https://s3.hf.co/vomebook',
                                   aws_access_key_id=access, aws_secret_access_key=secret,
                                   config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                                 retries={'total_max_attempts': 1},
                                                 connect_timeout=10, read_timeout=30,
                                                 request_checksum_calculation='when_required',
                                                 response_checksum_validation='when_required'))

    def create(self, root):
        path = isolated.root_path(locked.IDENTITY['transaction_id'])
        raw = root.get('raw')
        if (root.get('path') != path or not isinstance(raw, bytes)
                or preflight.digest(raw) != locked.ROOT_SHA or root.get('objects') != 20):
            raise ValueError('root writer received a foreign key or bytes')
        try:
            response = self.client.put_object(Bucket='reader-assets-v2', Key=path, Body=raw,
                                              ContentType='application/json', IfNoneMatch='*')
        except Exception as error:
            raise RootWriteUnknown('protection PUT outcome unknown; do not blindly repeat') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise RootWriteUnknown('protection PUT response ambiguous; do not blindly repeat')


def protect(store, writer, fence, client, rows, classification_raw):
    root = candidate(rows, classification_raw)
    # The full existing check verifies all 972 objects, 358 mapped manifests,
    # both sites, original controls, five rollback aliases and producer fence.
    check = preflight.check(store, fence, client, rows, classification_raw)
    if (check.get('remote_mutations') != 0 or check.get('deletion_authorized') is not False
            or check.get('partition', {}).get('selected') != 352
            or check.get('partition', {}).get('withheld') != 20):
        raise ValueError('CHM protection preflight differs')
    existing = read_optional(store, root['path'])
    if existing is not None and existing != root['raw']:
        raise ValueError('fixed protection root already contains different bytes')
    status = 'already-exact'
    if existing is None:
        # The held workflow groups are the cooperative serialization mechanism.
        # If-None-Match is only an additional check, not an atomicity claim.
        fence.check()
        preflight._identity(store)
        writer.create(root)
        status = 'created'
    if store.read_bytes(preflight.chm.ASSETS, root['path']) != root['raw']:
        raise RootWriteUnknown('protection root readback differs; stop and reconcile')
    preflight._identity(store)
    fence.check()
    return {'kind': 'chm-subset-protection-publication', 'status': status,
            'root_path': root['path'], 'root_sha256': root['sha256'], 'protected_objects': root['objects'],
            'original_progress_sha256': preflight.PROGRESS_SHA,
            'preflight_sha256': preflight.digest(json.dumps(check, sort_keys=True, separators=(',', ':'),
                                                            ensure_ascii=False).encode()),
            'deleted_objects': 0, 'original_complete': False, 'producer_restore_authorized': False,
            'caveats': ['only the exact 20-item root is written; no EPUB is deleted',
                        'conditional PUT is not a distributed lock; external writers are not fenced',
                        'this publication alone does not authorize deletion or producer restoration']}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('CHM root publication requires its central main workflow')
    preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('CHM root publication output exists')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
    store, fence = preflight.ReadOnlyStore(), GitHubProducerFence(api=preflight.github_get)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        result = protect(store, SingleRootWriter(), fence, client, rows, preflight.CLASSIFICATION.read_bytes())
    with OUTPUT.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
