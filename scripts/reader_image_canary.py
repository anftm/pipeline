"""Build and hand off exactly one new/changed image under the Reader writer lock.

An interrupted run is not retried; inspect the canonical index and the protected
bundle before attempting recovery. No historical profile or failure replay.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path

import httpx
from huggingface_hub import HfApi, HfFileSystem

from . import build_reader_assets_v2_sidecar as sidecar, finalize_image_index as finalizer
from . import pilot_image_page_stream as image, publish_reader_v3 as reader
from . import reader_format_backlog as backlog, reader_format_daily as daily
from . import reader_image_avif as avif, reader_route_plan, reader_stream_index
from .reader_bucket_store import HubBucketStore, retry_read, retry_s3
from .reader_gc_stream import catalog_identity
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .shared import hash_file


SIDECAR = 'reader-index/reader_assets.json.gz'
CATALOG = 'reader-index/reader_catalog.json'
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TIFF_PAGES = 128
WORKFLOW = 'reader-image-stream.yml'


def require_context(env, head):
    revision = env.get('GITHUB_SHA', '')
    run_id = env.get('GITHUB_RUN_ID', '')
    if (env.get('READER_IMAGE_AVIF_ENABLED') != 'true' or env.get('GITHUB_ACTIONS') != 'true'
            or env.get('GITHUB_REPOSITORY') != daily.REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_WORKFLOW_REF') != f'{daily.REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/main'
            or env.get('GITHUB_WORKFLOW_SHA') != revision or not daily.SHA.fullmatch(revision)
            or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or env.get('GITHUB_RUN_ATTEMPT') != '1'
            or not run_id.isascii() or not run_id.isdecimal() or run_id.startswith('0')
            or len(run_id) > 20 or head() != revision):
        raise ValueError('AVIF canary requires opt-in and exact first-attempt main workflow context')
    return revision, hashlib.sha256(f'{revision}\0{run_id}\0image-avif'.encode()).hexdigest()[:32]


def source_record(corpus, revisions, *, repo, path, api):
    key = reader_route_plan.source_key(repo, path)
    item = backlog.source_records(corpus, revisions).get(key)
    if not item or item['extension'] not in avif.FORMATS or Path(path).suffix.lower()[1:] != item['extension']:
        raise ValueError('canary source is not an exact supported image in pinned Search')
    request = (repo, item['revision'], path)
    metadata = backlog._metadata(api, {request}).get(request)
    size = backlog._item_field(metadata, 'size')
    lfs_sha = backlog._lfs_identity(metadata)
    blob = None if lfs_sha else backlog._blob_identity(metadata)
    if (type(size) is not int or not 0 < size <= MAX_SOURCE_BYTES
            or item['bytes'] not in (0, size) or not (lfs_sha or blob)):
        raise ValueError('canary source lacks a bounded immutable dataset identity')
    return {**item, 'bytes': size, 'source_sha256': lfs_sha, 'git_blob': blob}


def previous_source(item, canonical, shard, base):
    key, revision = item['key'], item['revision']
    number = reader_stream_index.shard_for(key, 8)
    # Keep the same checkpoint priority rules as the ordinary producer, but
    # never consume a newer unmerged checkpoint on the selected source.
    pending = image.shard_baseline(shard, canonical, number, 8)
    prior = next((row for row in canonical['files'] if row['key'] == key), None)
    staged = next((row for row in pending['files'] if row['key'] == key), None)
    if staged != prior or any(row['key'] == key and row.get('source_revision') == revision
                              for index in (canonical, pending) for row in index.get('failures', [])):
        raise ValueError('selected source has an unmerged checkpoint or a recorded failure')
    if prior is None:
        if key in base['f']:
            raise ValueError('selected source already has a historical Reader route')
        return None
    path = prior.get('path') if prior.get('mode') == 'image' else prior.get('manifest')
    prior_revision = prior.get('source_revision')
    if (base['f'].get(key) != {'s': 2, 'm': 'i', 'p': path, 'b': avif.BUCKET}
            or not isinstance(path, str) or prior.get('bucket') != avif.BUCKET
            or not isinstance(prior_revision, str) or not daily.SHA.fullmatch(prior_revision)
            or prior_revision == revision
            or not avif.digest(prior.get('source_sha256'))
            or item.get('source_sha256') == prior['source_sha256']):
        raise ValueError('canary requires a new source or changed bytes on an exact canonical image route')
    return prior['source_sha256']


def write_bundle(client, members, uploads):
    """Create immutable S3 objects with explicit MIME, then verify bytes and MIME."""
    bucket = avif.BUCKET.split('/', 1)[1]
    # Children first, then the manifest; no checkpoint until all exact reads pass.
    for path in sorted(members, key=lambda p: (p.endswith('/page-manifest.json'), p)):
        if avif.OBJECT.fullmatch(path):
            mime = 'image/avif'
        elif avif.PAGE_MANIFEST.fullmatch(path):
            mime = 'application/json'
        else:
            raise ValueError('unexpected AVIF upload path')
        with Path(uploads[path]).open('rb') as body:
            try:
                client.put_object(Bucket=bucket, Key=path, Body=body, ContentType=mime, IfNoneMatch='*')
            except Exception as error:
                # Conflict and uncertain transport outcome are both resolved
                # by exact readback, never by overwriting or retrying a PUT.
                try:
                    verify_object(client, bucket, path, mime, members[path])
                except FileNotFoundError:
                    raise RuntimeError(f'AVIF immutable object write unconfirmed: {path}') from error
            else:
                verify_object(client, bucket, path, mime, members[path])


def verify_object(client, bucket, path, mime, identity):
    def read():
        response = client.get_object(Bucket=bucket, Key=path)
        body = response['Body']
        try:
            expected_hash, size = identity
            if response.get('ContentLength') != size or response.get('ContentType') != mime:
                raise ValueError(f'AVIF object MIME or size differs: {path}')
            raw = body.read(size + 1)
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError(f'AVIF object bytes differ: {path}')
        finally:
            body.close()
    retry_s3(read)


def run(*, repo, path, revision, operation, search_generation, reader_parent, token,
        hub, api, github, fs, checkpoint, work, apply, http):
    if not avif.digest(search_generation) or not avif.digest(reader_parent):
        raise ValueError('canary requires exact Search and Reader generations')
    key = reader_route_plan.source_key(repo, path)
    if Path(path).suffix.lower()[1:] not in avif.FORMATS:
        raise ValueError('canary requires one supported non-GIF source path')
    github.check_main(revision)
    pointer, catalog = reader.current(hub)
    if (not pointer or pointer['generation'] != reader_parent or not catalog.get('base_sidecar')
            or catalog.get('derived_pdf_redirects') or catalog.get('suppressed_sources') or key in catalog['files']):
        raise ValueError('canary requires a pinned Reader parent with no redirects, suppressions or source override')
    base = reader.pinned_base(hub, catalog, {})
    base_raw = hub.read_bytes(avif.BUCKET, SIDECAR)
    if reader_route_plan.decode_sidecar(base_raw) != base:
        raise ValueError('canonical sidecar differs from the pinned Reader base')
    reader.verify_generation(hub, http, reader_parent)
    base_stamp, base_catalog_digest = hub.read_graph_json(avif.BUCKET, CATALOG, parser=catalog_identity)
    if (base_stamp['redirect_sidecar_pending']
            or base_stamp['bound_sidecar_sha256'] != hashlib.sha256(reader.encode(base)).hexdigest()):
        raise ValueError('legacy catalog does not bind the pinned Reader base')
    search_pointer, search_manifest, corpus, revisions, _ = backlog.capture_search(hub)
    if backlog.search_publication_pointer.decode_pointer(search_pointer)['desired'] != search_generation:
        raise ValueError('published Search generation differs from canary pin')
    item = source_record(corpus, revisions, repo=repo, path=path, api=api)
    shard_path = f'{finalizer.ROOT}/index-v2-{reader_stream_index.shard_for(key, 8):02d}.json'
    parent = checkpoint.read(avif.BUCKET, finalizer.CANONICAL)
    shard = checkpoint.read(avif.BUCKET, shard_path)
    canonical = parent.value
    if (not canonical or not canonical['files'] or canonical.get('kind') != 'image-stream-index'
            or (shard.value is not None and shard.value['kind'] != 'image-stream-index')):
        raise ValueError('missing canonical image baseline')
    previous_sha = previous_source(item, canonical, shard.value or {'files': []}, base)
    row, uploads = image.build_one(item, work / 'bundle', token, avif.BUCKET, codec='avif',
                                  previous_source_sha256=previous_sha, max_tiff_pages=MAX_TIFF_PAGES)
    route = avif.avif_route(row, avif.BUCKET)
    members = avif.bundle_members(row, avif.BUCKET, lambda _, member: Path(uploads[member]).read_bytes())
    if (members.keys() != uploads.keys() or sum(size for _, size in members.values()) > avif.MAX_BUNDLE_BYTES
            or any(hash_file(Path(local)) != members[member] for member, local in uploads.items())):
        raise ValueError('AVIF output does not match its bounded bundle')

    def rebuild():
        return sidecar.build(fs, avif.BUCKET, sidecar.PDF_BUCKET, 'reader-index/derived_pdf_manifest.json',
                             'reader-index/pdf_render_manifest.json', 'reader-index/pdf_ocr_manifest.json')

    def recheck():
        if (retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30)).sha != item['revision']
                or hub.read_bytes(backlog.SEARCH_BUCKET, backlog.CURRENT) != search_pointer
                or hub.read_bytes(backlog.SEARCH_BUCKET, f'generations/{search_generation}/manifest.json') != search_manifest
                or reader.current(hub)[0] != pointer
                or hub.read_bytes(avif.BUCKET, SIDECAR) != base_raw
                or checkpoint.read(avif.BUCKET, shard_path) != shard
                or hub.read_graph_json(avif.BUCKET, CATALOG, parser=catalog_identity)[1] != base_catalog_digest):
            raise ValueError('canary inputs changed before publication')
        github.check_main(revision)

    candidate = reader_stream_index.publish_checkpoint(
        avif.BUCKET, finalizer.CANONICAL, canonical, [row], [], uploads, work, token,
        apply=False, kind='image-stream-index')
    candidate['checkpoint_operation'] = operation
    if next((entry for entry in candidate['files'] if entry['key'] == key), None) != row:
        raise ValueError('canary checkpoint would not install the exact image row')
    expected = {**base, 'f': {**base['f'], key: route}}
    if rebuild() != base:
        raise ValueError('canary would publish more than the exact image route')
    recheck()
    if checkpoint.read(avif.BUCKET, finalizer.CANONICAL) != parent:
        raise ValueError('canonical image index changed before canary publication')
    if apply:
        # The standard processing root is created and read back before S3
        # upload; it remains active on any interrupted handoff.
        from .reader_bucket import publish_bytes, publish_catalog
        from .reader_processing_cas import ProcessingWriter
        store = checkpoint.store
        writer = ProcessingWriter(store)
        for root in row['processing_roots']:
            raw = (work / 'processing' / Path(root).name).read_bytes()
            # Do not reuse/overwrite a retained root from an interrupted run.
            writer.create(root, raw)
        namespace, _ = store._location(avif.BUCKET)
        write_bundle(store._client(namespace), members, uploads)
        recheck()
        checkpoint.write_once(avif.BUCKET, finalizer.CANONICAL, candidate, parent.etag, operation_id=operation)
        if rebuild() != expected:
            raise ValueError('canonical inputs changed after AVIF checkpoint; sidecar not published')
        recheck()
        packed = gzip.compress(reader.encode(expected), compresslevel=9, mtime=0)
        publish_bytes(SIDECAR, packed, token)
        if hub.read_bytes(avif.BUCKET, SIDECAR) != packed:
            raise ValueError('AVIF canonical sidecar readback differs')
        generation = publish_catalog(expected, token)
        stamp, _ = hub.read_graph_json(avif.BUCKET, CATALOG, parser=catalog_identity)
        snapshot = f'reader-index/generations/{generation}/reader_assets.json.gz'
        if (stamp != {'current': generation, 'redirect_sidecar_pending': False,
                      'bound_sidecar_sha256': hashlib.sha256(reader.encode(expected)).hexdigest()}
                or reader_route_plan.decode_sidecar(hub.read_bytes(avif.BUCKET, snapshot)) != expected):
            raise ValueError('AVIF legacy catalog handoff did not bind the exact sidecar')
        # The legacy catalog generation now owns the new route, and the prior
        # generation retains the WebP route. Release only this canary's root.
        sidecar.release_processing_roots([{'files': [row]}], generation, writer.read, writer.publish)
    return {'applied': apply, 'reader_parent': reader_parent, 'search_generation': search_generation,
            'key': key, 'source_revision': item['revision'], 'source_sha256': row['source_sha256'], 'route': route,
            **({'legacy_generation': generation, 'sidecar_sha256': hashlib.sha256(packed).hexdigest(),
                'central_unify_dispatched': False} if apply else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='JSON object with repo, path, and reader_parent')
    parser.add_argument('--search-generation', required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    revision, operation = require_context(os.environ, daily.local_head)
    if args.expected_sha != revision:
        raise ValueError('canary expected checkout differs from current SHA')
    source = json.loads(args.source)
    if (not isinstance(source, dict) or set(source) != {'repo', 'path', 'reader_parent'}
            or any(not isinstance(v, str) for v in source.values())):
        raise ValueError('canary requires one exact repo, path, and Reader parent')
    token = os.environ['HF_TOKEN']
    with tempfile.TemporaryDirectory(prefix='reader-image-canary-') as directory, \
            httpx.Client(timeout=90, follow_redirects=True) as http:
        report = run(**source, search_generation=args.search_generation, apply=args.apply,
                     revision=revision, operation=operation, token=token,
                     hub=HubBucketStore(), api=HfApi(token=token), fs=HfFileSystem(token=token, skip_instance_cache=True, use_listings_cache=False),
                     github=daily.GitHub(os.environ['GH_TOKEN'], local_head=daily.local_head),
                     checkpoint=ConditionalCheckpointStore(), work=Path(directory), http=http)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
