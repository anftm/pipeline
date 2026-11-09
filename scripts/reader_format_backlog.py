"""Read-only Reader format backlog with exact source-content comparisons.

A route or an old repository revision is not content identity.  The report
separates unmapped, content-changed, unchanged and unknown sources.  It neither
publishes data nor authorizes dispatch: current workflows need independent
write/recovery controls and known writers must be coordinated first.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re

import httpx
from huggingface_hub import HfApi, HfFileSystem

try:
    from . import plan_reader_stream_matrix as matrix
    from . import publish_reader_v3 as publication
    from . import reader_assets, reader_lifecycle, reader_route_plan, reader_stream_index
    from . import search_daily, search_generation, search_publication_pointer, search_snapshot
    from .reader_bucket_store import HubBucketStore, retry_read
except ImportError:
    import plan_reader_stream_matrix as matrix
    import publish_reader_v3 as publication
    import reader_assets
    import reader_lifecycle
    import reader_route_plan
    import reader_stream_index
    import search_daily
    import search_generation
    import search_publication_pointer
    import search_snapshot
    from reader_bucket_store import HubBucketStore, retry_read


SEARCH_BUCKET = search_publication_pointer.BUCKET
READER_BUCKET = reader_assets.READER_ASSETS_BUCKET
CURRENT = 'published/current.json'
MAX_COMPRESSED = 32 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
REV = re.compile(r'[0-9a-f]{40}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
BATCH = 100

# Only categories with an existing bounded non-OCR converter are listed.
# PDFs/DjVu remain dataset-primary and are not mistaken for unprocessed pages.
FAMILIES = {
    'image': (('jpg', 'jpeg', 'png', 'bmp', 'webp', 'gif', 'tif', 'tiff'), 'reader-image-stream.yml', 'image'),
    'ebook': (('epub', 'mobi', 'azw3', 'fb2'), 'reader-ebook-stream.yml', 'ebook'),
    'chm': (('chm',), 'reader-chm-repair.yml', 'chm'),
    'office-chapters': (('doc', 'docx', 'odt', 'rtf'), 'reader-office-chapters.yml', 'ebook'),
    'spreadsheet': (('xls', 'xlsx', 'csv', 'ods'), 'reader-spreadsheet-stream.yml', 'html'),
    'web': (('html', 'htm', 'mht', 'mhtml'), 'reader-web-stream.yml', 'html'),
    'static-pdf': (('ppt', 'pptx', 'pps', 'wps', 'ps'), 'reader-static-pdf-stream.yml', 'pdf'),
    'media': (('mp3', 'wav', 'm4a', 'flac', 'mpga', 'ape', 'wma', 'amr', 'aac',
               'mp4', 'mov', 'asx', 'flv', 'f4v', 'rm', 'rmvb', 'mkv', 'avi',
               'mpg', 'mpeg', 'mts', 'ts', 'wmv', 'swf'), 'reader-media-stream.yml', 'media'),
}
EXTENSIONS = {extension: (family, workflow, mode)
              for family, (extensions, workflow, mode) in FAMILIES.items() for extension in extensions}


def _json(raw: bytes, limit: int) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ValueError('invalid or oversized backlog JSON')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('backlog JSON must be an object')
    return value


def _inflate(raw: bytes, limit: int) -> bytes:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_COMPRESSED:
        raise ValueError('invalid compressed backlog input')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise ValueError('backlog input expands beyond its limit')
    return result


def capture_search(store, *, observe=search_daily.observe_consumers, get=search_daily.bounded_get):
    pointer_raw = store.read_bytes(SEARCH_BUCKET, CURRENT)
    pointer = search_publication_pointer.decode_pointer(pointer_raw)
    generation = pointer['desired']
    manifest_raw = store.read_bytes(SEARCH_BUCKET, f'generations/{generation}/manifest.json')
    manifest = _json(manifest_raw, search_daily.MAX_JSON)
    excluded = {'generation', 'remote_writes', 'publication_ready', 'object_layout', 'objects', 'additional_checks'}
    if (manifest.get('version') != 1 or manifest.get('kind') != 'search-snapshot-local'
            or manifest.get('generation') != generation
            or manifest.get('object_layout') != 'objects/sha256/<first-two>/<sha256>'
            or hashlib.sha256(search_snapshot.canonical({k: v for k, v in manifest.items() if k not in excluded})).hexdigest() != generation):
        raise ValueError('published search manifest identity differs')
    member = manifest.get('objects', {}).get('pages', {}).get('search_data.json.gz')
    if not isinstance(member, dict) or set(member) != {'sha256', 'bytes', 'object'}:
        raise ValueError('search corpus object metadata missing')
    digest, size = member['sha256'], member['bytes']
    if (not isinstance(digest, str) or not SHA.fullmatch(digest) or type(size) is not int
            or not 0 < size <= MAX_COMPRESSED or member['object'] != f'objects/sha256/{digest[:2]}/{digest}'
            or manifest.get('files', {}).get('pages', {}).get('search_data.json.gz') != {'sha256': digest, 'bytes': size}):
        raise ValueError('search corpus identity invalid')
    raw = store.read_bytes(SEARCH_BUCKET, member['object'])
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('search corpus bytes differ')
    corpus = _json(_inflate(raw, MAX_EXPANDED), MAX_EXPANDED)
    revisions = manifest.get('source_revisions')
    rows = search_generation._validate_corpus(corpus, revisions)
    if len(rows) != manifest.get('record_count'):
        raise ValueError('search record count differs')
    observed = observe(generation, manifest, get=get)
    if store.read_bytes(SEARCH_BUCKET, CURRENT) != pointer_raw:
        raise ValueError('search pointer changed during capture')
    return pointer_raw, manifest_raw, corpus, revisions, observed


def source_records(corpus: dict, revisions: dict) -> dict:
    rows = search_generation._validate_corpus(corpus, revisions)
    decoded = reader_assets.decode_search_payload(corpus)
    if len(rows) != len(decoded):
        raise ValueError('search decoder dropped source records')
    result = {}
    for record in decoded:
        repo = record.get('Repo')
        path = reader_assets.relative_path(record)
        key = reader_route_plan.source_key(repo, path)
        if key in result:
            raise ValueError('duplicate search source key')
        extension = str(record.get('Extension') or '').lower().lstrip('.')
        result[key] = {'key': key, 'repo': repo, 'path': path, 'extension': extension,
                       'revision': revisions[repo], 'bytes': int(record.get('Size') or 0)}
    return result


def _item_field(value, field):
    return value.get(field) if isinstance(value, dict) else getattr(value, field, None)


def _lfs_identity(item):
    lfs = _item_field(item, 'lfs')
    if lfs is None:
        return None
    digest = _item_field(lfs, 'sha256')
    if not isinstance(digest, str) or not SHA.fullmatch(digest):
        raise ValueError('malformed source LFS identity')
    return digest


def _blob_identity(item):
    value = _item_field(item, 'blob_id')
    return value if isinstance(value, str) and REV.fullmatch(value) else None


def _metadata(api, requests):
    groups = defaultdict(list)
    for repo, revision, path in requests:
        if not isinstance(revision, str) or not REV.fullmatch(revision):
            raise ValueError('source metadata requires an immutable revision')
        groups[(repo, revision)].append(path)
    result = {}
    for (repo, revision), paths in sorted(groups.items()):
        unique = sorted(set(paths))
        for offset in range(0, len(unique), BATCH):
            batch = unique[offset:offset + BATCH]
            values = retry_read(lambda: api.get_paths_info(repo, batch, repo_type='dataset', revision=revision, expand=True))
            if not isinstance(values, list):
                raise ValueError('source metadata response is invalid')
            seen = set()
            for value in values:
                path = _item_field(value, 'path')
                if not isinstance(path, str) or path not in batch or path in seen:
                    raise ValueError('source metadata contains duplicate or foreign paths')
                seen.add(path)
                result[(repo, revision, path)] = value
    return result


def _matches_route(entry, route):
    if not isinstance(entry, dict) or not isinstance(route, dict):
        return False
    if entry.get('bucket') != READER_BUCKET:
        return False
    refs = {(ref['bucket'], ref['path']) for ref in reader_lifecycle.sidecar_references({'f': {'entry': route}})}
    for field in ('manifest', 'object'):
        path = entry.get(field)
        if isinstance(path, str) and (READER_BUCKET, path) in refs:
            return True
    return False


def classify_epub(records, routes, canonical, shards, api):
    """Return key-level states and old/new metadata, never compare revision alone."""
    source = [value for value in records.values() if value['extension'] == 'epub']
    if not isinstance(canonical, dict) or canonical.get('version') != 1 or canonical.get('kind') != 'ebook-chapter-stream-index':
        raise ValueError('canonical EPUB index is invalid')
    reader_stream_index.validate_index(canonical, 'EPUB canonical index')
    for field in ('files', 'failures'):
        keys = [entry['key'] for entry in canonical.get(field, [])]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate canonical EPUB ' + field)
    seen_shards = set()
    for number, shard in shards:
        if type(number) is not int or not 0 <= number < 8 or number in seen_shards:
            raise ValueError('invalid or duplicate EPUB shard')
        seen_shards.add(number)
        if shard.get('version') != 1 or shard.get('kind') != 'ebook-chapter-stream-index':
            raise ValueError('invalid EPUB shard schema')
        reader_stream_index.validate_index(shard, 'EPUB shard')
        for field in ('files', 'failures'):
            keys = [entry['key'] for entry in shard.get(field, [])]
            if len(keys) != len(set(keys)):
                raise ValueError('duplicate EPUB shard ' + field)
    # Do not mistake an index checkpoint not yet merged into the published
    # sidecar for a completed update; report it separately.
    merged = reader_stream_index.merge_shards(canonical, shards, 'ebook-chapter-stream-index', {'epub'}, 8)
    entries = {entry['key']: entry for entry in canonical['files']}
    merged_entries = {entry['key']: entry for entry in merged.get('files', [])}
    # Keep each failure observation separately. Merging an old success with a
    # newer failure can intentionally suppress one in legacy checkpoints; a
    # diagnostic must not turn that suppression into proof of success.
    failure_rows = defaultdict(list)
    for entry in canonical.get('failures', []):
        failure_rows[entry['key']].append(entry)
    for number, shard in shards:
        for entry in shard.get('failures', []):
            if reader_stream_index.shard_for(entry['key'], 8) == number:
                failure_rows[entry['key']].append(entry)
    rows, current_requests = {}, set()
    for item in source:
        key, route = item['key'], routes.get(item['key'])
        entry = entries.get(key)
        staged = merged_entries.get(key)
        failure_current = any(failure.get('source_revision') == item['revision']
                              for failure in failure_rows.get(key, []))
        row = {'key': key, 'source_revision': item['revision'], 'recorded_failure': failure_current}
        if route is None:
            row['state'] = 'not_mapped'
        elif (not isinstance(route, dict) or route.get('s') != 2 or route.get('m') != 'e'
              or not _matches_route(entry, route)):
            row['state'] = 'route_or_index_mismatch'
        elif not isinstance(entry.get('source_revision'), str) or not REV.fullmatch(entry['source_revision']):
            row['state'] = 'source_identity_unknown'
        elif entry['source_revision'] == item['revision']:
            row['state'] = 'same_revision'
            row['proof'] = 'same-pinned-revision-and-path'
        else:
            row['state'] = 'compare_source'
            row['previous_revision'] = entry['source_revision']
            row['previous_source_sha256'] = entry.get('source_sha256')
            current_requests.add((item['repo'], item['revision'], item['path']))
        if staged and staged != entry:
            row['pending_shard_checkpoint'] = True
        rows[key] = row
    current = _metadata(api, current_requests)
    old_requests = set()
    for item in source:
        row = rows[item['key']]
        if row['state'] != 'compare_source':
            continue
        value = current.get((item['repo'], item['revision'], item['path']))
        if value is None:
            row['state'] = 'source_identity_unknown'
            continue
        lfs = _lfs_identity(value)
        old_sha = row.get('previous_source_sha256')
        if lfs and isinstance(old_sha, str) and SHA.fullmatch(old_sha):
            row['state'] = 'content_unchanged' if lfs == old_sha else 'content_changed'
            row['proof'] = 'current-lfs-sha256'
            if row['state'] == 'content_changed':
                row['current_source_sha256'] = lfs
        elif not lfs and _blob_identity(value):
            old_requests.add((item['repo'], row['previous_revision'], item['path']))
        else:
            row['state'] = 'source_identity_unknown'
    old = _metadata(api, old_requests)
    for item in source:
        row = rows[item['key']]
        if row['state'] != 'compare_source':
            continue
        before = old.get((item['repo'], row['previous_revision'], item['path']))
        after = current.get((item['repo'], item['revision'], item['path']))
        before_blob, after_blob = _blob_identity(before), _blob_identity(after)
        # An LFS pointer is a Git blob, not file bytes. Only compare ordinary
        # files by Git blob identity; switching LFS/ordinary stays unknown.
        old_lfs = _lfs_identity(before)
        old_sha = row.get('previous_source_sha256')
        if old_lfs and isinstance(old_sha, str) and SHA.fullmatch(old_sha) and old_lfs != old_sha:
            row['state'] = 'source_identity_unknown'
        elif before_blob and after_blob:
            # Equal Git blob IDs prove equal stored bytes, including an LFS
            # pointer. A differing old/new blob proves content drift only
            # when both are ordinary files; format switches remain unknown.
            if before_blob == after_blob:
                row['state'] = 'content_unchanged'
            elif not old_lfs and not _lfs_identity(after):
                row['state'] = 'content_changed'
            else:
                row['state'] = 'source_identity_unknown'
            if row['state'] != 'source_identity_unknown':
                row['proof'] = 'exact-revision-git-blobs'
                if row['state'] == 'content_changed':
                    row.update(previous_git_blob=before_blob, current_git_blob=after_blob)
        else:
            row['state'] = 'source_identity_unknown'
    return rows


def summarize(records: dict, routes: dict, epub_rows: dict, *, threshold: int = 100) -> list[dict]:
    if type(threshold) is not int or threshold < 0:
        raise ValueError('threshold must be nonnegative')
    grouped = defaultdict(list)
    for item in records.values():
        if item['extension'] in EXTENSIONS:
            grouped[item['extension']].append(item)
    output = []
    for extension in sorted(EXTENSIONS):
        family, workflow, _mode = EXTENSIONS[extension]
        counts = Counter()
        for item in grouped[extension]:
            route = routes.get(item['key'])
            if extension == 'epub':
                row = epub_rows[item['key']]
                state = row['state']
                counts[state] += 1
                if row.get('recorded_failure'):
                    counts['recorded_failure_current'] += 1
                if row.get('pending_shard_checkpoint'):
                    counts['pending_shard_checkpoint'] += 1
            elif route is None:
                counts['not_mapped'] += 1
            elif not isinstance(route, dict) or route.get('s') != 2:
                counts['route_unverified'] += 1
            else:
                counts['mapped_content_not_checked'] += 1
        actionable = counts['not_mapped'] + counts['content_changed']
        unverified = counts['source_identity_unknown'] + counts['route_or_index_mismatch'] + counts['route_unverified']
        verified = (extension == 'epub' and unverified == 0
                    and counts['pending_shard_checkpoint'] == 0 and counts['recorded_failure_current'] == 0)
        output.append({'extension': extension, 'family': family, 'workflow': workflow,
                       'sources': len(grouped[extension]), 'not_mapped': counts['not_mapped'],
                       'source_content_changed': counts['content_changed'],
                       'source_content_unchanged': counts['content_unchanged'] + counts['same_revision'],
                       'source_identity_unknown': unverified,
                       'mapped_content_not_checked': counts['mapped_content_not_checked'],
                       'recorded_failure_current': counts['recorded_failure_current'],
                       'pending_shard_checkpoint': counts['pending_shard_checkpoint'],
                       'definite_backlog': actionable, 'threshold_exceeded': actionable > threshold,
                       'all_source_identities_checked': verified, 'trigger_ready': False})
    return output


def inspect(*, hub, api, fs, client, threshold=100, observe_search=search_daily.observe_consumers,
            get=search_daily.bounded_get):
    pointer_raw, manifest_raw, corpus, revisions, _observed = capture_search(hub, observe=observe_search, get=get)
    records = source_records(corpus, revisions)
    for repo, expected in sorted(revisions.items()):
        info = retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30))
        if getattr(info, 'sha', None) != expected:
            raise ValueError('published source revision is not the current dataset head: ' + repo)
    reader_pointer, catalog = publication.current(hub)
    if reader_pointer is None or not catalog.get('base_sidecar'):
        raise ValueError('Reader has no pinned v3 sidecar')
    base = publication.pinned_base(hub, catalog, {})
    projection = publication.project_sidecar(base, catalog)
    routes = projection['f']
    receipts = {}
    for surface in ('pages', 'hf'):
        receipt = publication.observe_consumer(hub, surface, client)
        if receipt.get('generation') != reader_pointer['generation'] or receipt.get('projection_verified') is not True:
            raise ValueError('Reader consumer differs from the pinned projection')
        receipts[surface] = {'generation': receipt['generation'], 'projection_verified': True}
    root = 'chapters/ebook/epub'
    canonical = matrix.read_json(fs, READER_BUCKET, root + '/index.json')
    shards = []
    for number in range(8):
        value = matrix.read_json(fs, READER_BUCKET, f'{root}/index-{number:02d}.json')
        if value:
            shards.append((number, value))
    epub = classify_epub(records, routes, canonical, shards, api)
    summaries = summarize(records, routes, epub, threshold=threshold)
    # Fail closed if the exact shared inputs changed. This is a point-in-time
    # observation only: it cannot fence independent writers or ABA changes.
    if hub.read_bytes(SEARCH_BUCKET, CURRENT) != pointer_raw:
        raise ValueError('search pointer changed during backlog inspection')
    if hub.read_bytes(SEARCH_BUCKET, f"generations/{search_publication_pointer.decode_pointer(pointer_raw)['desired']}/manifest.json") != manifest_raw:
        raise ValueError('search manifest changed during backlog inspection')
    if publication.current(hub)[0] != reader_pointer:
        raise ValueError('Reader pointer changed during backlog inspection')
    if matrix.read_json(fs, READER_BUCKET, root + '/index.json') != canonical:
        raise ValueError('EPUB canonical index changed during backlog inspection')
    for number in range(8):
        latest = matrix.read_json(fs, READER_BUCKET, f'{root}/index-{number:02d}.json')
        if latest != dict(shards).get(number, {}):
            raise ValueError('EPUB shard index changed during backlog inspection')
    for repo, expected in sorted(revisions.items()):
        info = retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30))
        if getattr(info, 'sha', None) != expected:
            raise ValueError('dataset head changed during backlog inspection: ' + repo)
    for surface in ('pages', 'hf'):
        receipt = publication.observe_consumer(hub, surface, client)
        if receipt.get('generation') != reader_pointer['generation'] or receipt.get('projection_verified') is not True:
            raise ValueError('Reader consumer changed during backlog inspection')
    if publication.current(hub)[0] != reader_pointer or hub.read_bytes(SEARCH_BUCKET, CURRENT) != pointer_raw:
        raise ValueError('Reader or search pointer changed at final acceptance')
    summary = {'version': 1, 'kind': 'reader-format-backlog',
               'search_generation': search_publication_pointer.decode_pointer(pointer_raw)['desired'],
               'reader_generation': reader_pointer['generation'], 'threshold': threshold,
               'reader_consumers': receipts, 'formats': summaries, 'remote_writes': 0,
               'dispatches': 0, 'publication_ready': False, 'external_writers_verified': False,
               'scope': 'published search and pinned Reader projection; exact content drift checked for EPUB only'}
    details = {'version': 1, 'kind': 'reader-epub-content-backlog',
               'search_generation': summary['search_generation'], 'reader_generation': summary['reader_generation'],
               'rows': [row for row in sorted(epub.values(), key=lambda x: x['key'])
                        if row['state'] in {'content_changed', 'not_mapped', 'source_identity_unknown', 'route_or_index_mismatch'}
                        or row.get('pending_shard_checkpoint') or row.get('recorded_failure')],
               'remote_writes': 0, 'publication_ready': False}
    return summary, details


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--epub-details', required=True, type=Path)
    parser.add_argument('--threshold', type=int, default=100)
    args = parser.parse_args(argv)
    if args.output.resolve() == args.epub_details.resolve() or any(
            path.exists() or path.is_symlink() for path in (args.output, args.epub_details)):
        raise ValueError('backlog outputs must be two new distinct paths')
    token = os.environ.get('HF_TOKEN')
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        summary, details = inspect(hub=HubBucketStore(), api=HfApi(token=token), fs=HfFileSystem(token=token),
                                   client=client, threshold=args.threshold)
    for path, result in ((args.output, summary), (args.epub_details, details)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf8') as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
