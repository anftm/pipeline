#!/usr/bin/env python3
"""Publish one verified OCR release under the CCRD writer; never delete or run OCR."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path

try:
    from . import ccrd_ocr_release as contract, ccrd_lifecycle as lifecycle
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import ccrd_ocr_release as contract
    import ccrd_lifecycle as lifecycle
    from reader_bucket_store import HubBucketStore


PROTOCOL = 'central-ccrd-ocr-publication-v1'
WORKFLOW = 'anftm/pipeline/.github/workflows/publish-ccrd-ocr.yml@refs/heads/main'
CURRENT = 'control/ocr-current.json'
IDS = 'control/ocr-ids.json'


def assert_writer():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW or not os.environ.get('GITHUB_RUN_ID')
            or os.environ.get('CCRD_OCR_WRITE_PROTOCOL') != PROTOCOL):
        raise RuntimeError('OCR-library mutation requires the actual central CCRD publication workflow')


def resource(path, raw, role='runtime'):
    return {'bucket': contract.BUCKET, 'path': path, 'sha256': contract.digest(raw), 'bytes': len(raw), 'role': role}


def optional_json(store, path):
    try:
        raw = store.read_bytes(contract.BUCKET, path)
    except FileNotFoundError:
        return None, None
    payload = json.loads(raw)
    lifecycle.references(payload, path)
    return payload, raw


def put_checked(store, path, raw, *, immutable=False):
    if immutable:
        try:
            previous = store.read_bytes(contract.BUCKET, path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous != raw:
                raise ValueError('immutable OCR-library object conflict')
            return
    store.put_bytes(contract.BUCKET, path, raw)
    if store.read_bytes(contract.BUCKET, path) != raw:
        raise ValueError('OCR-library upload readback mismatch')


def plan(root, path, sha256, base_metadata_sha256):
    candidate = contract.load_release(root, path, sha256, base_metadata_sha256)
    files = {path: (root / path).read_bytes()}
    refs = [*candidate['manifest']['exports'], candidate['manifest']['database']]
    refs += [export['text'] for export in candidate['exports']]
    for ref in refs:
        files[ref['path']] = contract.read_resource(root, ref)
    return candidate, files


def publish(store, root, path, sha256, base_metadata_sha256, expected_parent, *, apply=False):
    candidate, files = plan(root, path, sha256, base_metadata_sha256)
    manifest = candidate['manifest']
    previous, previous_raw = optional_json(store, CURRENT)
    parent = previous['generation'] if previous else None
    if previous and parent == manifest['generation']:
        if previous['release']['sha256'] != sha256:
            raise ValueError('same generation has a different OCR release manifest')
        for key, value in files.items():
            if store.read_bytes(contract.BUCKET, key) != value:
                raise ValueError('existing promoted OCR release is incomplete')
        return {'unchanged': True, 'generation': parent, 'release': previous['release'], 'applied': apply}
    if parent != expected_parent:
        raise ValueError('stale expected OCR-library parent')
    registry, ids_raw = optional_json(store, IDS)
    registry = registry or {'version': 1, 'kind': 'ccrd-ocr-id-registry', 'ids': {}, 'resources': []}
    ids = dict(registry['ids'])
    used = {item['doc_id']: book for book, item in ids.items()}
    for export in candidate['exports']:
        book, number = export['book_id'], export['doc_id']
        old = ids.get(book)
        if old and (old['doc_id'] != number or old['readable_rel_path'] != export['readable_rel_path']):
            raise ValueError('stable OCR ID/path cannot be reassigned')
        if number in used and used[number] != book:
            raise ValueError('OCR document ID is owned by another book')
        if any(value['readable_rel_path'] == export['readable_rel_path'] and key != book for key, value in ids.items()):
            raise ValueError('readable TXT path collision')
        ids[book] = {'doc_id': number, 'readable_rel_path': export['readable_rel_path']}
        used[number] = book
    if previous:
        old_release = json.loads(store.read_bytes(contract.BUCKET, previous['release']['path']))
        if not {ref['path'] for ref in old_release['exports']} <= {ref['path'] for ref in manifest['exports']}:
            raise ValueError('first-stage additive promotion cannot silently drop or replace existing exports')
    release_ref = resource(path, files[path])
    retained = [resource(key, value) for key, value in sorted(files.items())]
    for export in candidate['exports']:
        retained.extend(export['retained_reader_resources'])
    intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': manifest['generation'],
              'status': 'uploading', 'expected_parent': parent, 'resources': retained}
    intent_path = f'control/intents/{manifest["generation"]}.json'
    report = {'applied': apply, 'generation': manifest['generation'], 'parent': parent, 'documents': len(candidate['records']),
              'release': release_ref, 'objects': len(files), 'model_calls': 0, 'deleted_objects': 0}
    if not apply:
        return report
    assert_writer()
    # Protect candidate members/provenance before the first upload. An incomplete
    # intent is deliberately a GC blocker, never an expiration-based delete grant.
    put_checked(store, intent_path, contract.canonical(intent))
    for key, value in sorted(files.items()):
        put_checked(store, key, value, immutable=True)
    # Provenance must still exist with its exact identity, including accepted text.
    seen = set()
    for ref in retained:
        identity = ref['bucket'], ref['path']
        if identity in seen:
            continue
        seen.add(identity)
        raw = store.read_bytes(*identity)
        if len(raw) != ref['bytes'] or contract.digest(raw) != ref['sha256']:
            raise ValueError('retained OCR provenance differs from the staged export')
    if optional_json(store, CURRENT)[1] != previous_raw or optional_json(store, IDS)[1] != ids_raw:
        raise ValueError('OCR registry/current changed while candidate was uploaded')
    # Keep previous release reachable before publishing a replacement pointer.
    if previous:
        history = {'version': 1, 'kind': 'ccrd-ocr-retention', 'resources': [previous['release']],
                   'reason': 'explicit rollback retention; publication never deletes history'}
        put_checked(store, f'control/retained/{parent}.json', contract.canonical(history), immutable=True)
    updated_ids = {**registry, 'ids': ids, 'resources': [release_ref]}
    put_checked(store, IDS, contract.canonical(updated_ids))
    pointer = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': manifest['generation'],
               'release': release_ref, 'resources': [release_ref], 'parent': parent,
               'base_metadata_sha256': base_metadata_sha256}
    put_checked(store, CURRENT, contract.canonical(pointer))
    intent.update(status='committed')
    put_checked(store, intent_path, contract.canonical(intent))
    report['pointer'] = pointer
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--release-path', required=True)
    parser.add_argument('--release-sha256', required=True)
    parser.add_argument('--base-metadata-sha256', required=True)
    parser.add_argument('--expected-parent', default='none')
    parser.add_argument('--activate-space', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    expected = None if args.expected_parent == 'none' else contract.sha(args.expected_parent)
    if args.apply:
        assert_writer()
    store = HubBucketStore()
    report = publish(store, args.root, args.release_path, args.release_sha256,
                     args.base_metadata_sha256, expected, apply=args.apply)
    if args.activate_space:
        if not args.apply:
            raise ValueError('Space activation requires a published verified release')
        from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download
        token = os.environ.get('HF_TOKEN')
        api = HfApi(token=token)
        info = api.repo_info('vomebook/Search', repo_type='space')
        base = Path(hf_hub_download('vomebook/Search', 'data/search_data.json.gz', repo_type='space', revision=info.sha, token=token))
        runtime = Path(hf_hub_download('vomebook/Search', 'app.py', repo_type='space', revision=info.sha, token=token)).read_text()
        if contract.digest(base.read_bytes()) != args.base_metadata_sha256 or 'ocr-release.json' not in runtime:
            raise ValueError('Space is not the compatible prepared OCR consumer')
        activation = {'version': 1, 'kind': 'ccrd-ocr-activation', 'bucket': contract.BUCKET,
                      'path': args.release_path, 'sha256': args.release_sha256}
        commit = api.create_commit('vomebook/Search', repo_type='space', parent_commit=info.sha,
                                  commit_message='Activate verified additive OCR text release',
                                  operations=[CommitOperationAdd(path_in_repo='data/ocr-release.json',
                                                                 path_or_fileobj=io.BytesIO(contract.canonical(activation)))])
        report['space_revision'] = commit.oid
        report['space_activation'] = 'requested; production identity/search acceptance is separate'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(contract.canonical(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
