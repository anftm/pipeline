#!/usr/bin/env python3
"""Serialized four-source rollout with exact Space pins and a full OCR rebase.

Only the existing central publish-ccrd-ocr workflow may mutate storage. Public
Release transport is input, not an active index. No deletion or OCR/model call.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

try:
    from . import auto_ccrd_ocr as auto, build_ccrd_ocr_release as builder
    from . import ccrd_lifecycle as lifecycle, ccrd_ocr_release as contract
    from . import ccrd_newspaper_transport as transport, publish_ccrd_ocr as ocr, shared
    from .reader_bucket_store import HubBucketStore, retry_read
except ImportError:
    import auto_ccrd_ocr as auto
    import build_ccrd_ocr_release as builder
    import ccrd_lifecycle as lifecycle
    import ccrd_ocr_release as contract
    import ccrd_newspaper_transport as transport
    import publish_ccrd_ocr as ocr
    import shared
    from reader_bucket_store import HubBucketStore, retry_read


JOURNAL = 'control/newspaper-rollout.json'
SPACE = 'vomebook/Search'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def reference(path, sha256, size):
    return {'bucket': contract.BUCKET, 'path': path, 'sha256': sha256, 'bytes': size, 'role': 'runtime'}


def remote_identity(store, ref):
    lifecycle.resource(ref)
    if store.file_identity(ref['bucket'], ref['path']) != {key: ref[key] for key in ['sha256', 'bytes']}:
        raise ValueError('complete immutable remote readback mismatch: ' + ref['path'])


def stage_file(store, local, ref):
    if transport.identity(local) != {key: ref[key] for key in ['sha256', 'bytes']}:
        raise ValueError('local rollout file changed after admission')
    try:
        remote_identity(store, ref)
    except FileNotFoundError:
        store.put_file(ref['bucket'], ref['path'], local)
        remote_identity(store, ref)
    # A conflicting immutable object is never overwritten, even for a rerun.


class PublicationStore(HubBucketStore):
    def file_identity(self, bucket, path):
        from huggingface_hub.utils import get_session, hf_raise_for_status
        token = self.token(bucket)
        def read():
            self.pace_resolver()
            digest, size = hashlib.sha256(), 0
            with get_session().stream('GET',
                    f'https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe="/")}',
                    headers={'Authorization': f'Bearer {token}'} if token else {},
                    follow_redirects=True, timeout=300) as response:
                if response.status_code == 404:
                    raise FileNotFoundError(path)
                hf_raise_for_status(response)
                # httpx strips Authorization on cross-origin signed redirects.
                for block in response.iter_bytes(chunk_size=1024 * 1024):
                    digest.update(block)
                    size += len(block)
            return {'sha256': digest.hexdigest(), 'bytes': size}
        return retry_read(read)

    def put_file(self, bucket, path, local):
        shared.batch_bucket_files_with_retry(bucket, [(str(local), path)], self.token(bucket))


class SpaceCommitter:
    def __init__(self):
        from huggingface_hub import HfApi
        self.api = HfApi(token=os.environ.get('HF_TOKEN'))

    def head(self):
        return self.api.repo_info(SPACE, repo_type='space').sha

    def read(self, path, revision):
        from huggingface_hub import hf_hub_download
        return Path(hf_hub_download(SPACE, path, repo_type='space', revision=revision,
                                   token=os.environ.get('HF_TOKEN'))).read_bytes()

    def runtime_revision(self):
        runtime = self.api.repo_info(SPACE, repo_type='space').runtime
        # The SDK exposes the complete server payload separately from typed
        # hardware fields. Production acceptance requires the running image SHA.
        raw = runtime.raw if runtime is not None else {}
        return raw.get('sha') if raw.get('stage') == 'RUNNING' else None

    def commit(self, parent, files):
        from huggingface_hub import CommitOperationAdd
        operations = [CommitOperationAdd(path_in_repo=path,
            path_or_fileobj=io.BytesIO(value) if isinstance(value, bytes) else str(value))
            for path, value in sorted(files.items())]
        return self.api.create_commit(SPACE, repo_type='space', parent_commit=parent,
            commit_message='Activate verified newspapers and coherently rebased OCR', operations=operations).oid


def validate_evidence(value, root):
    evidence = {name: json.loads((root / 'evidence' / name).read_text()) for name in transport.EVIDENCE_FILES}
    counts = {source: value['manifest']['databases'][source]['documents'] for source in ['RMRB', 'PLA']}
    oracle = evidence['oracle-receipt.json']
    app_sha = transport.identity(root / 'runtime/app.py')['sha256']
    if (evidence['oracle-process-receipt.json'].get('exit_code') != 0
            or evidence['oracle-process-receipt.json'].get('runtime_sha256_unchanged') is not True
            or evidence['oracle-process-receipt.json'].get('runtime_sha256') != app_sha
            or oracle.get('queries') != 22 or oracle.get('runtime_sha256') != app_sha
            or {key: item['documents'] for key, item in oracle.get('sources', {}).items()} != counts
            or any(len(item.get('results', [])) != 22 or item.get('tail_phrase_checks') != 8
                   for item in oracle.get('sources', {}).values())):
        raise ValueError('full independent original-TXT oracle evidence is incomplete')
    runtime = evidence['full-runtime-acceptance-receipt.json']
    if (runtime.get('runtime_sha256') != app_sha or len(runtime.get('browser', [])) != 4
            or any(item.get('page_errors') for item in runtime['browser'])
            or runtime.get('http', {}).get('mixed_zip_complete_original_bytes_and_CRC') is not True):
        raise ValueError('full runtime/browser/TXT acceptance evidence is incomplete')
    declared = {key: item['documents'] for key, item in value['manifest']['databases'].items()}
    if (evidence['full-package-acceptance-receipt.json'].get('metadata', {}).get('source_counts') != declared
            or any(item.get('remote_writes') != 0 for item in evidence.values())):
        raise ValueError('full-package acceptance differs from the admitted generation')


def check_base(space, revision, value, old_base):
    if (contract.digest(space.read('data/search_data.json.gz', revision)) != old_base
            or contract.digest(space.read('build_fulltext_db.py', revision)) != value['builder_sha256']
            or space.read('ocr_release.py', revision) != Path(contract.__file__).read_bytes()):
        raise ValueError('Space builder/base/shared OCR contract differs from the rollout baseline')
    for name, digest in value['previous_runtime_sha256'].items():
        if contract.digest(space.read(name, revision)) != digest:
            raise ValueError('Space runtime changed outside the approved newspaper update')


def exact_space(space, revision, files):
    for name, content in files.items():
        expected = content if isinstance(content, bytes) else content.read_bytes()
        if space.read(name, revision) != expected:
            raise ValueError('Space coherent commit readback differs: ' + name)


def validate_generation(value, root, previous_metadata):
    manifest = value['manifest']
    old = json.loads(gzip.decompress(previous_metadata))
    prefix = f'generations/{manifest["generation"]}/'
    raw = (root / prefix / 'data/search_data.json.gz').read_bytes()
    if contract.digest(raw) != manifest['base_metadata_sha256']:
        raise ValueError('four-source compressed base metadata SHA differs')
    data = json.loads(gzip.decompress(raw))
    by_source = {source: [] for source in manifest['databases']}
    for record in data['records']:
        by_source[record['source']].append(record)
    if ([record for record in data['records'] if record['source'] in {'CCRD', 'CW'}] != old['records']
            or {row['slug']: row['count'] for row in data['sources']} !=
               {source: detail['documents'] for source, detail in manifest['databases'].items()}):
        raise ValueError('four-source metadata changed legacy records or declared counts')
    for source, details in manifest['databases'].items():
        ids = {row['doc_id'] for row in by_source[source]}
        if len(ids) != len(by_source[source]) or len(ids) != details['documents']:
            raise ValueError('four-source metadata contains duplicate IDs or incomplete counts')
        if source not in {'RMRB', 'PLA'}:
            continue
        path = root / details['path']
        with sqlite3.connect(f'file:{path.resolve()}?mode=ro&immutable=1', uri=True) as db:
            metadata = dict(db.execute('SELECT key,value FROM metadata'))
            if (metadata.get('build_complete') != 'true' or metadata.get('index_profile') != 'newspaper-hybrid-texts-v1'
                    or metadata.get('tokenizer_version') != contract.TOKENIZER
                    or db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]):
                raise ValueError('newspaper database is incomplete or has invalid SQLite integrity')
            for table, column in [('documents', 'doc_id'), ('texts', 'doc_id'), ('content_fts', 'rowid')]:
                actual = {row[0] if column == 'doc_id' else f'{source}:{row[0]}'
                          for row in db.execute(f'SELECT {column} FROM {table}')}
                if actual != ids:
                    raise ValueError('newspaper full metadata/document/text/FTS IDs differ')


def prepare_rebase(store, root, consumer, previous, value):
    """Freeze the full original export set even when resuming after OCR promotion."""
    class Snapshot:
        def read_bytes(self, bucket, path):
            if bucket == contract.BUCKET and path == ocr.CURRENT:
                return contract.canonical(previous)
            return store.read_bytes(bucket, path)
    _pointer, refs, exports = auto.current_exports(Snapshot(), root)
    if not exports:
        raise ValueError('newspaper rollout cannot omit the live OCR export set')
    base_sha = value['manifest']['base_metadata_sha256']
    sorted_refs = sorted(refs.values(), key=lambda ref: (ref.get('bucket', ''), ref.get('path', '')))
    identity = {'version': 1, 'kind': 'ccrd-ocr-search-release', 'base_metadata_sha256': base_sha,
                'builder_sha256': value['builder_sha256'], 'tokenizer_version': contract.TOKENIZER,
                'exports': sorted_refs}
    generation = contract.digest(contract.canonical(identity))
    path = f'releases/ocr-{generation}/manifest.json'
    local = root / path
    if local.exists():
        manifest = json.loads(local.read_text())
        contract.load_release(root, path, contract.digest(local.read_bytes()), base_sha)
        result = {'manifest': manifest, 'path': path, 'sha256': contract.digest(local.read_bytes())}
    else:
        result = builder.build_release(root, sorted_refs, consumer, value['builder_sha256'], base_sha)
    if (result['manifest']['generation'] != generation or result['manifest']['exports'] != sorted_refs
            or result['manifest']['documents'] != len(exports)):
        raise ValueError('OCR base rebind changed the complete live export set')
    (root / 'release-receipt.json').write_bytes(contract.canonical(result))
    return result, sorted(exports.values(), key=lambda item: item['doc_id'])


def rollout(store, space, value, root, *, apply=False):
    """No global pointer moves until immutable members and OCR have been checked."""
    manifest = value['manifest']
    generation = manifest['generation']
    base_sha = manifest['base_metadata_sha256']
    old_base = manifest['previous_base_metadata_sha256']
    transport.validate(value, value['files'][0]['parts'][0]['url'].rsplit('/', 1)[0] + '/transport.json')
    for entry in value['files']:
        if transport.identity(root / entry['path']) != {key: entry[key] for key in ['sha256', 'bytes']}:
            raise ValueError('complete materialized rollout member differs from admitted transport')
    validate_evidence(value, root)
    approved = auto.configuration(json.loads(Path('ccrd-ocr-consumer.json').read_text()))
    if approved['builder_sha256'] != value['builder_sha256']:
        raise ValueError('pipeline OCR builder pin differs from the newspaper candidate')
    prior_journal, _raw = ocr.optional_json(store, JOURNAL)
    if prior_journal and prior_journal['generation'] != generation:
        raise ValueError('another newspaper rollout already owns the durable journal')
    if apply:
        ocr.assert_writer()
    current, current_raw = ocr.optional_json(store, ocr.CURRENT)
    if current is None:
        raise ValueError('expected a complete currently published OCR release')
    previous = prior_journal['previous_ocr_pointer'] if prior_journal else current
    if previous.get('base_metadata_sha256') != old_base:
        raise ValueError('live OCR baseline does not match the previous newspaper base')
    old_pointer_raw = store.read_bytes(contract.BUCKET, 'current.json')
    if (contract.digest(old_pointer_raw) != manifest['previous_index_manifest_sha256']
            and (not prior_journal or json.loads(old_pointer_raw) != manifest)):
        raise ValueError('base index pointer changed after the approved full plan')
    old_pointer = json.loads(old_pointer_raw)
    if not prior_journal:
        lifecycle.references(old_pointer, 'current.json')
        if old_pointer.get('format') != 1 or any(old_pointer['databases'].get(source) != manifest['databases'][source]
                                                  for source in ['CCRD', 'CW']):
            raise ValueError('newspaper plan cannot replace the exact published legacy database identities')
    head = space.head()
    old_revision = prior_journal['previous_space_revision'] if prior_journal else head
    if not prior_journal:
        if approved['base_metadata_sha256'] != old_base:
            raise ValueError('pipeline approved consumer does not match the previous base')
        check_base(space, head, value, old_base)
    consumer = root / 'consumer'
    consumer.mkdir(exist_ok=True)
    old_metadata = space.read('data/search_data.json.gz', old_revision)
    if contract.digest(old_metadata) != old_base:
        raise ValueError('frozen Space baseline metadata changed')
    validate_generation(value, root, old_metadata)
    for name in ['build_fulltext_db.py', 'ocr_release.py']:
        (consumer / name).write_bytes(space.read(name, old_revision))
    if (transport.identity(consumer / 'build_fulltext_db.py')['sha256'] != value['builder_sha256']
            or (consumer / 'ocr_release.py').read_bytes() != Path(contract.__file__).read_bytes()):
        raise ValueError('frozen old consumer does not match the approved builder/contract')
    rebase_root = root / 'ocr-rebase'
    rebase_root.mkdir(exist_ok=True)
    rebase, exports = prepare_rebase(store, rebase_root, consumer, previous, value)
    release_ref = ocr.resource(rebase['path'], (rebase_root / rebase['path']).read_bytes())
    allowed_ocr = {previous['generation'], rebase['manifest']['generation']}
    if current['generation'] not in allowed_ocr:
        raise ValueError('OCR advanced independently of the frozen rollout parent')
    manifest_raw = contract.canonical(manifest)
    manifest_path = f'generations/{generation}/manifest.json'
    manifest_ref = ocr.resource(manifest_path, manifest_raw)
    local_manifest = root / manifest_path
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(manifest_raw)
    index_activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': contract.BUCKET,
                        'path': manifest_path, 'sha256': manifest_ref['sha256']}
    ocr_activation = {'version': 1, 'kind': 'ccrd-ocr-activation', 'bucket': contract.BUCKET,
                      'path': rebase['path'], 'sha256': rebase['sha256']}
    files = {name: root / 'runtime' / name for name in transport.RUNTIME_FILES}
    prefix = f'generations/{generation}/'
    files.update({ref['path'][len(prefix):]: root / ref['path'] for ref in manifest['metadata']})
    files.update({'data/index-release.json': contract.canonical(index_activation),
                  'data/ocr-release.json': contract.canonical(ocr_activation)})
    if head != old_revision:
        exact_space(space, head, files)
    else:
        check_base(space, head, value, old_base)
    report = {'version': 1, 'kind': 'ccrd-newspaper-rollout-receipt', 'generation': generation,
              'applied': apply, 'base_metadata_sha256': base_sha, 'OCR_generation': rebase['manifest']['generation'],
              'OCR_documents': len(exports), 'OCR_exports_unchanged': True, 'model_calls': 0,
              'deleted_objects': 0, 'previous_space_revision': old_revision, 'remote_writes': 0}
    if not apply:
        return report
    deployment_refs = [reference(f'{prefix}runtime/{name}', transport.identity(root / 'runtime' / name)['sha256'],
                                (root / 'runtime' / name).stat().st_size)
                       for name in sorted(transport.RUNTIME_FILES)]
    resources = [*lifecycle.references(manifest, 'current.json'), manifest_ref, release_ref, *deployment_refs,
                 rebase['manifest']['database']]
    if not prior_journal or prior_journal['status'] != 'committed':
        resources.append(previous['release'])
    journal = {'version': 1, 'kind': 'ccrd-newspaper-rollout', 'generation': generation,
               'status': 'staging', 'previous_space_revision': old_revision, 'previous_ocr_pointer': previous,
               'index_activation': index_activation, 'ocr_activation': ocr_activation,
               'base_metadata_sha256': base_sha, 'resources': resources, 'updated_at': stamp()}
    if prior_journal:
        if any(prior_journal.get(key) != journal[key] for key in
               ['generation', 'previous_space_revision', 'previous_ocr_pointer', 'index_activation', 'ocr_activation']):
            raise ValueError('retry differs from the protected newspaper rollout identity')
        if prior_journal['status'] == 'committed':
            exact_space(space, head, files)
            report.update(space_revision=head, accepted_consumer={
                'version': 1, 'consumer_revision': head, 'builder_sha256': value['builder_sha256'],
                'base_metadata_sha256': base_sha},
                OCR_publication={'generation': rebase['manifest']['generation'], 'release': release_ref},
                unchanged=True)
            return report
    lifecycle.references(journal, JOURNAL)
    ocr.put_checked(store, JOURNAL, contract.canonical(journal))
    # Reused legacy DBs are streamed and hashed, not trusted from HEAD metadata.
    for source in ['CCRD', 'CW']:
        detail = manifest['databases'][source]
        remote_identity(store, reference(detail['path'], detail['sha256'], detail['bytes']))
    for entry in value['files']:
        if entry['path'].startswith(('runtime/', 'evidence/')):
            continue
        stage_file(store, root / entry['path'], reference(entry['path'], entry['sha256'], entry['bytes']))
    for ref in deployment_refs:
        stage_file(store, root / 'runtime' / ref['path'].rsplit('/', 1)[1], ref)
    stage_file(store, local_manifest, manifest_ref)
    if (store.read_bytes(contract.BUCKET, 'current.json') != old_pointer_raw or space.head() != head
            or ocr.optional_json(store, ocr.CURRENT)[1] != current_raw):
        raise ValueError('Space/base index changed during full staging')
    # The original publisher still owns all OCR ID, ancestry, text and rollback
    # checks. It is idempotent if a retry already promoted this exact rebase.
    publication = ocr.publish(store, rebase_root, rebase['path'], rebase['sha256'], base_sha,
                              previous['generation'], apply=True)
    commit_head = space.head()
    if commit_head not in {old_revision, head}:
        raise ValueError('Space changed immediately before the coherent commit')
    if commit_head == old_revision:
        revision = space.commit(old_revision, files)
    else:
        revision = head
    exact_space(space, revision, files)
    # The new Space selects the immutable manifest in its own coherent commit;
    # the mutable pointer can safely follow only after that commit's readback.
    pointer_now = store.read_bytes(contract.BUCKET, 'current.json')
    if pointer_now != old_pointer_raw and json.loads(pointer_now) != manifest:
        raise ValueError('base index pointer changed before final promotion')
    ocr.put_checked(store, 'current.json', manifest_raw)
    consumer_pin = {'version': 1, 'consumer_revision': revision,
                    'builder_sha256': value['builder_sha256'], 'base_metadata_sha256': base_sha}
    auto.configuration(consumer_pin)
    (root / 'accepted-consumer.json').write_bytes(contract.canonical(consumer_pin))
    consumer_control = {**consumer_pin, 'kind': 'ccrd-ocr-consumer',
                        'resources': [manifest_ref, release_ref]}
    ocr.put_checked(store, 'control/ocr-consumer.json', contract.canonical(consumer_control))
    # Keep rollback and the writer/GC fence until production acceptance AND the
    # pipeline consumer-pin commit both succeed in the following workflow steps.
    journal.update(status='activated', space_revision=revision, updated_at=stamp())
    ocr.put_checked(store, JOURNAL, contract.canonical(journal))
    report.update(space_revision=revision, accepted_consumer=consumer_pin, remote_writes='canonical-writer',
                  OCR_publication=publication, journal=JOURNAL,
                  production_acceptance='required separately before pipeline consumer pin update')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport-url', required=True)
    parser.add_argument('--transport-sha256', required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.apply:
        ocr.assert_writer()
    report = {'kind': 'ccrd-newspaper-rollout-process', 'started_at': stamp(), 'applied': args.apply,
              'phase': 'reading-approved-transport', 'deleted_objects': 0, 'model_calls': 0}
    try:
        value = transport.fetch_descriptor(args.transport_url, args.transport_sha256)
        report.update(generation=value['manifest']['generation'], phase='materializing-complete-transport')
        root = transport.materialize(value, args.root)
        report['phase'] = 'serialized-staging-and-coherent-activation'
        report = rollout(PublicationStore(), SpaceCommitter(), value, root, apply=args.apply)
    except Exception as error:
        # A failed run still leaves local evidence and the durable remote journal
        # (if staging began); a new runner resumes only that exact identity.
        report.update(phase='failed', error_type=type(error).__name__)
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(contract.canonical(report))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
