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


def verified_remote(store, ref):
    lifecycle.resource(ref)
    raw = store.read_bytes(ref['bucket'], ref['path'])
    if len(raw) != ref['bytes'] or contract.digest(raw) != ref['sha256']:
        raise ValueError('OCR remote resource checksum mismatch')
    return raw


def verify_text_upgrade(store, previous, replacement):
    """Only an accepted descendant of the same complete source may replace text."""
    fields = ('book_id', 'doc_id', 'readable_rel_path', 'origin_repo', 'origin_rel_path',
              'origin_revision', 'origin_sha256', 'source_sha256', 'page_count')
    if any(previous.get(key) != replacement.get(key) for key in fields):
        raise ValueError('OCR update changes stable source identity')
    if (replacement['accepted_pages'] < previous['accepted_pages']
            or replacement['quality'] != 'partially-reviewed'
            or any(old['quality'] != 'unreviewed' and new['quality'] == 'unreviewed'
                   for old, new in zip(previous['pages'], replacement['pages']))
            or any(old['text_generation'] != new['text_generation'] and new['quality'] == 'unreviewed'
                   for old, new in zip(previous['pages'], replacement['pages']))):
        raise ValueError('OCR update cannot downgrade accepted text')
    from_refs = replacement['retained_reader_resources']
    try:
        from . import pdf_reading_v3 as v3
    except ImportError:
        import pdf_reading_v3 as v3
    for ref in from_refs:
        if not ref['path'].endswith('/text-layer-manifest.json'):
            continue
        manifest = v3.validate_text_manifest(v3.decode(verified_remote(store, ref)))
        if manifest['generation'] != replacement['text_generation']:
            continue
        seen = set()
        for _ in range(128):
            if (manifest['source_sha256'] != replacement['source_sha256']
                    or manifest['page_count'] != replacement['page_count']):
                raise ValueError('OCR update ancestry changes source or page coverage')
            if manifest['generation'] == previous['text_generation']:
                return
            parent = manifest.get('parent')
            if not parent or (parent['bucket'], parent['path']) in seen:
                break
            seen.add((parent['bucket'], parent['path']))
            manifest = v3.validate_text_manifest(v3.decode(verified_remote(store, parent)))
    raise ValueError('OCR update is not a verified accepted text descendant')


def verify_additive_update(store, previous, candidate):
    old_release = json.loads(verified_remote(store, previous['release']))
    lifecycle.references(old_release, previous['release']['path'])
    new = {export['book_id']: (ref, export) for ref, export in
           zip(candidate['manifest']['exports'], candidate['exports'])}
    for old_ref in old_release['exports']:
        old = json.loads(verified_remote(store, old_ref))
        if old['book_id'] not in new:
            raise ValueError('additive OCR promotion cannot drop an existing book')
        new_ref, replacement = new[old['book_id']]
        if new_ref != old_ref:
            verify_text_upgrade(store, old, replacement)


def optional_json(store, path):
    try:
        raw = store.read_bytes(contract.BUCKET, path)
    except FileNotFoundError:
        return None, None
    payload = json.loads(raw)
    lifecycle.references(payload, path)
    return payload, raw


def put_checked(store, path, raw, *, immutable=False, replace_unpromoted=False):
    if immutable:
        try:
            previous = store.read_bytes(contract.BUCKET, path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous == raw:
                return
            if not replace_unpromoted:
                raise ValueError('immutable OCR-library object conflict')
            # Only a generation that was never promoted may be completed after
            # an interrupted upload. Its namespace is unreachable from CURRENT
            # and from every release manifest, so replacing the candidate copy
            # cannot rewrite published history.
            print(f'completing interrupted upload for {path}', flush=True)
    store.put_bytes(contract.BUCKET, path, raw)
    if store.read_bytes(contract.BUCKET, path) != raw:
        raise ValueError('OCR-library upload readback mismatch')


def unpromoted_candidate_paths(store, manifest, previous):
    """Return the two generation-addressed objects of an unpromoted candidate.

    Interrupting an upload leaves a never-promoted generation whose database and
    manifest may differ from a later rebuild. Those two paths are addressed by
    generation, not by content; every export and TXT path stays content-addressed
    and must never be replaced.
    """
    generation = manifest['generation']
    if previous and previous['generation'] == generation:
        return set()
    if optional_json(store, f'control/retained/{generation}.json')[0]:
        return set()
    intent = optional_json(store, f'control/intents/{generation}.json')[0]
    expected = previous['generation'] if previous else None
    if (not intent or intent.get('kind') != 'ccrd-ocr-intent'
            or intent.get('status') != 'uploading' or intent.get('generation') != generation
            or intent.get('expected_parent') != expected):
        return set()
    return {f'indexes/ocr/{generation}/OCR.sqlite3', f'releases/ocr-{generation}/manifest.json'}


def merge_unpromoted_intent(store, intent_path, intent, files):
    """Keep every object of an interrupted upload reachable.

    Replacing an ``uploading`` intent must not drop references to objects the
    interrupted run already uploaded, or a later GC could treat them as
    unreachable. New candidate members supersede the old copy at the same path;
    all other old members stay protected by the new intent.
    """
    old = optional_json(store, intent_path)[0]
    if (not old or old.get('kind') != 'ccrd-ocr-intent' or old.get('status') != 'uploading'
            or old.get('generation') != intent['generation']):
        return intent
    by_identity = {(ref['bucket'], ref['path']): ref for ref in intent['resources']}
    for ref in old.get('resources', []):
        if ref.get('path') not in files:
            by_identity.setdefault((ref.get('bucket'), ref.get('path')), ref)
    intent['resources'] = sorted(by_identity.values(), key=lambda ref: (ref['bucket'], ref['path']))
    return intent


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
        verify_additive_update(store, previous, candidate)
    release_ref = resource(path, files[path])
    retained = [resource(key, value) for key, value in sorted(files.items())]
    previous_exports = {}
    if previous:
        old_release = json.loads(verified_remote(store, previous['release']))
        for old_ref in old_release['exports']:
            old_export = json.loads(verified_remote(store, old_ref))
            previous_exports[old_export['book_id']] = old_ref
    for export_ref, export in zip(candidate['manifest']['exports'], candidate['exports']):
        # Unchanged exports were already fully protected and verified by the
        # live release. Rechecking thousands of old Reader page objects on every
        # additive batch made publication needlessly slow and could stall it.
        if previous_exports.get(export['book_id']) == export_ref:
            continue
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
    resumable = unpromoted_candidate_paths(store, manifest, previous)
    intent = merge_unpromoted_intent(store, intent_path, intent, files)
    put_checked(store, intent_path, contract.canonical(intent))
    for key, value in sorted(files.items()):
        put_checked(store, key, value, immutable=True, replace_unpromoted=key in resumable)
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


def activate_space(path, sha256, base_metadata_sha256, builder_sha256, *, apply=False):
    """Preflight before publication; idempotently reconcile activation after it."""
    from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download
    token = os.environ.get('HF_TOKEN')
    api = HfApi(token=token)
    info = api.repo_info('vomebook/Search', repo_type='space')
    def download(name):
        return Path(hf_hub_download('vomebook/Search', name, repo_type='space', revision=info.sha, token=token)).read_bytes()
    if (contract.digest(download('data/search_data.json.gz')) != base_metadata_sha256
            or contract.digest(download('build_fulltext_db.py')) != builder_sha256
            or download('ocr_release.py') != Path(contract.__file__).read_bytes()
            or b'ocr-release.json' not in download('app.py')):
        raise ValueError('Space is not the compatible prepared OCR consumer')
    activation = {'version': 1, 'kind': 'ccrd-ocr-activation', 'bucket': contract.BUCKET,
                  'path': path, 'sha256': sha256}
    from huggingface_hub.errors import EntryNotFoundError
    try:
        unchanged = json.loads(download('data/ocr-release.json')) == activation
    except EntryNotFoundError:
        unchanged = False
    report = {'space_revision': info.sha, 'space_activation_unchanged': unchanged}
    if apply and not unchanged:
        assert_writer()
        commit = api.create_commit('vomebook/Search', repo_type='space', parent_commit=info.sha,
            commit_message='Activate verified complete OCR text release',
            operations=[CommitOperationAdd(path_in_repo='data/ocr-release.json',
                                          path_or_fileobj=io.BytesIO(contract.canonical(activation)))])
        report['space_revision'] = commit.oid
    report['space_activation'] = 'production identity/search acceptance is separate'
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
    candidate, _files = plan(args.root, args.release_path, args.release_sha256, args.base_metadata_sha256)
    builder_sha256 = candidate['manifest']['builder_sha256']
    if args.activate_space:
        if not args.apply:
            raise ValueError('Space activation requires a published verified release')
        activate_space(args.release_path, args.release_sha256, args.base_metadata_sha256, builder_sha256)
    report = publish(store, args.root, args.release_path, args.release_sha256,
                      args.base_metadata_sha256, expected, apply=args.apply)
    if args.activate_space:
        report.update(activate_space(args.release_path, args.release_sha256, args.base_metadata_sha256,
                                     builder_sha256, apply=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(contract.canonical(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
