"""Select exact missing or content-changed EPUBs, without publishing.

The selection is tied to a verified backlog snapshot. An existing v8 chapter
bundle is not upgraded just because a repository commit or profile changed.
The resulting plan is for review/simulation; it does not authorize writes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

try:
    from . import pilot_epub_chapters as builder
    from . import reader_format_backlog as backlog
    from . import search_snapshot
except ImportError:
    import pilot_epub_chapters as builder
    import reader_format_backlog as backlog
    import search_snapshot


OP = re.compile(r'[0-9a-f]{32}\Z')
REV = re.compile(r'[0-9a-f]{40}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_BATCH = 3


def create(summary, details, corpus, revisions, canonical, shard_indexes, *, shard, operation, offset=0):
    if (not isinstance(summary, dict) or summary.get('kind') != 'reader-format-backlog'
            or summary.get('version') != 1 or summary.get('remote_writes') != 0
            or summary.get('publication_ready') is not False or summary.get('external_writers_verified') is not False):
        raise ValueError('exact read-only backlog summary required')
    if (not isinstance(details, dict) or details.get('kind') != 'reader-epub-content-backlog'
            or details.get('version') != 1 or details.get('remote_writes') != 0
            or details.get('publication_ready') is not False or not isinstance(details.get('rows'), list)
            or details.get('search_generation') != summary.get('search_generation')
            or details.get('reader_generation') != summary.get('reader_generation')):
        raise ValueError('EPUB detail snapshot differs from summary')
    if type(shard) is not int or not 0 <= shard < 8 or not isinstance(operation, str) or not OP.fullmatch(operation):
        raise ValueError('one valid shard and a unique operation ID are required')
    if type(offset) is not int or not 0 <= offset <= 100_000:
        raise ValueError('invalid EPUB selection offset')
    rows = [item for item in summary.get('formats', []) if isinstance(item, dict) and item.get('extension') == 'epub']
    if (len(rows) != 1 or rows[0].get('all_source_identities_checked') is not True
            or rows[0].get('threshold_exceeded') is not True
            or type(summary.get('threshold')) is not int
            or rows[0].get('definite_backlog', -1) <= summary['threshold']):
        raise ValueError('EPUB source identities are incomplete')
    if (rows[0].get('recorded_failure_current') != 0 or rows[0].get('pending_shard_checkpoint') != 0):
        raise ValueError('EPUB recovery has unresolved failures or unpublished shard checkpoints')
    records = backlog.source_records(corpus, revisions)
    if len([x for x in records.values() if x['extension'] == 'epub']) != rows[0].get('sources'):
        raise ValueError('EPUB source inventory differs from backlog summary')
    if (not isinstance(canonical, dict) or canonical.get('version') != 1
            or canonical.get('kind') != 'ebook-chapter-stream-index' or not isinstance(canonical.get('files'), list)):
        raise ValueError('EPUB canonical index is invalid')
    indexed = {row.get('key'): row for row in canonical['files'] if isinstance(row, dict)}
    if len(indexed) != len(canonical['files']):
        raise ValueError('EPUB canonical index contains duplicate or invalid keys')
    sources = {}
    for row in details['rows']:
        if not isinstance(row, dict) or not isinstance(row.get('key'), str) or row['key'] in sources:
            raise ValueError('EPUB detail rows contain invalid or duplicate source')
        key, state = row['key'], row.get('state')
        source = records.get(key)
        if (not source or source['extension'] != 'epub' or row.get('source_revision') != source['revision']
                or row.get('recorded_failure') is True or row.get('pending_shard_checkpoint') is True):
            raise ValueError('EPUB detail source differs from pinned corpus or needs recovery')
        if state == 'not_mapped':
            if key in indexed:
                raise ValueError('EPUB missing route already has an index entry; inspect handoff')
            if any(row.get(field) is not None for field in ('previous_revision', 'previous_source_sha256',
                                                             'previous_git_blob', 'current_git_blob')):
                raise ValueError('unmapped EPUB has unexpected previous identity')
            proof = row.get('proof')
            if proof is not None and (proof != 'unmapped-lfs-sha256'
                                      or not isinstance(row.get('current_source_sha256'), str)
                                      or not SHA.fullmatch(row['current_source_sha256'])):
                raise ValueError('invalid unmapped EPUB LFS proof')
            if proof is None and row.get('current_source_sha256') is not None:
                raise ValueError('unmapped EPUB digest has no source proof')
        elif state == 'content_changed':
            previous = indexed.get(key)
            if (not isinstance(previous, dict) or previous.get('source_revision') != row.get('previous_revision')
                    or previous.get('source_sha256') != row.get('previous_source_sha256')
                    or not REV.fullmatch(str(row.get('previous_revision', '')))
                    or not SHA.fullmatch(str(row.get('previous_source_sha256', '')))
                    or row['previous_revision'] == row['source_revision']):
                raise ValueError('EPUB canonical source identity changed')
            if row.get('proof') == 'current-lfs-sha256':
                if not SHA.fullmatch(str(row.get('current_source_sha256', ''))) or row['current_source_sha256'] == row['previous_source_sha256']:
                    raise ValueError('invalid changed EPUB LFS proof')
            elif row.get('proof') == 'exact-revision-git-blobs':
                if (not REV.fullmatch(str(row.get('previous_git_blob', '')))
                        or not REV.fullmatch(str(row.get('current_git_blob', '')))
                        or row['previous_git_blob'] == row['current_git_blob']):
                    raise ValueError('invalid changed EPUB Git blob proof')
            else:
                raise ValueError('EPUB changed content lacks exact proof')
        else:
            raise ValueError('EPUB details contain unresolved source identities')
        sources[key] = row
    missing = sum(row['state'] == 'not_mapped' for row in sources.values())
    changed = sum(row['state'] == 'content_changed' for row in sources.values())
    if (missing != rows[0].get('not_mapped') or changed != rows[0].get('source_content_changed')
            or missing + changed != rows[0].get('definite_backlog')):
        raise ValueError('EPUB detail counts differ from summary')
    if not isinstance(shard_indexes, dict) or set(shard_indexes) != set(range(8)):
        raise ValueError('all eight EPUB shard snapshots are required')
    for number, index in shard_indexes.items():
        if not isinstance(index, dict) or not isinstance(index.get('files', []), list):
            raise ValueError('invalid EPUB shard snapshot')
        if any(isinstance(row, dict) and row.get('key') in sources for row in index.get('failures', [])):
            raise ValueError('selected EPUB has a prior failure requiring explicit recovery')
    # Missing routes are a definite backlog, but only LFS identities can be
    # admitted by this staging protocol. Unproven / ordinary-Git missing
    # sources stay counted and are skipped rather than bound to arbitrary
    # downloaded bytes. A later protocol can add independent Git-blob proof.
    selected = [records[key] for key in sorted(sources)
                if builder.shard_for(key, 8) == shard
                and (sources[key]['state'] == 'content_changed'
                     or sources[key].get('proof') == 'unmapped-lfs-sha256')][offset:offset + MAX_BATCH]
    if not selected:
        if offset:
            raise ValueError('EPUB selection offset has no eligible sources')
        if any(builder.shard_for(key, 8) == shard for key in sources):
            raise ValueError('requested EPUB shard has backlog but no source with an admissible content proof')
        raise ValueError('requested EPUB shard has no eligible exact sources')
    selection = []
    for source in selected:
        detail = sources[source['key']]
        previous = indexed.get(source['key'])
        selection.append({'key': source['key'], 'repo': source['repo'], 'path': source['path'],
                          'source_revision': source['revision'], 'reason': detail['state'],
                          'proof': detail.get('proof'), 'previous_revision': detail.get('previous_revision'),
                          'previous_source_sha256': detail.get('previous_source_sha256'),
                          'current_source_sha256': detail.get('current_source_sha256'),
                          'previous_git_blob': detail.get('previous_git_blob'),
                          'current_git_blob': detail.get('current_git_blob'),
                          'previous_manifest': previous.get('manifest') if previous else None})
    body = {'version': 1, 'kind': 'reader-epub-exact-build-plan', 'operation': operation, 'shard': shard,
            'search_generation': summary['search_generation'], 'reader_generation': summary['reader_generation'],
            'canonical_index_sha256': hashlib.sha256(search_snapshot.canonical(canonical)).hexdigest(),
            'shard_index_sha256': hashlib.sha256(search_snapshot.canonical(shard_indexes[shard])).hexdigest(),
            'source_revisions_sha256': hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest(),
            'profile': builder.EPUB_PROFILE, 'max_attempts': MAX_BATCH, 'sources': selection,
            'remote_writes': 0, 'publication_ready': False, 'external_writers_verified': False}
    if offset:
        body['selection_offset'] = offset
    body['plan_sha256'] = hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    return body


def bind_unmapped_sources(details, records, api):
    """Bind each available unmapped LFS object at its pinned source revision.

    Missing/ordinary Git metadata does not change the backlog count; it just
    makes that candidate ineligible for this proof format. No bytes are written.
    """
    if not isinstance(details, dict) or not isinstance(details.get('rows'), list):
        raise ValueError('invalid EPUB detail snapshot')
    result = copy.deepcopy(details)
    requests = set()
    for row in result['rows']:
        if not isinstance(row, dict) or row.get('state') != 'not_mapped':
            continue
        source = records.get(row.get('key'))
        if not source or source.get('extension') != 'epub' or source.get('revision') != row.get('source_revision'):
            raise ValueError('unmapped EPUB detail differs from pinned source')
        if any(row.get(name) is not None for name in ('proof', 'current_source_sha256', 'current_git_blob')):
            raise ValueError('unmapped EPUB detail already contains source proof')
        requests.add((source['repo'], source['revision'], source['path']))
    metadata = backlog._metadata(api, requests)
    for row in result['rows']:
        if not isinstance(row, dict) or row.get('state') != 'not_mapped':
            continue
        source = records[row['key']]
        value = metadata.get((source['repo'], source['revision'], source['path']))
        if value is not None:
            digest = backlog._lfs_identity(value)
            if digest:
                row.update(proof='unmapped-lfs-sha256', current_source_sha256=digest)
    return result


def _inspect_selection(*, hub, api, fs, client, shard, selections, threshold=100, start=0, max_batches=12):
    """Capture one live snapshot and derive disjoint exact plan chunks."""
    summary, details = backlog.inspect(hub=hub, api=api, fs=fs, client=client, threshold=threshold, scope='epub')
    pointer, _manifest, corpus, revisions, _ = backlog.capture_search(hub)
    search_generation = backlog.search_publication_pointer.decode_pointer(pointer)['desired']
    if search_generation != summary['search_generation']:
        raise ValueError('search generation changed before exact EPUB plan')
    root = 'chapters/ebook/epub'
    canonical = backlog.matrix.read_json(fs, backlog.READER_BUCKET, root + '/index.json')
    shards = {number: backlog.matrix.read_json(fs, backlog.READER_BUCKET, f'{root}/index-{number:02d}.json')
              for number in range(8)}
    records = backlog.source_records(corpus, revisions)
    bound_details = bind_unmapped_sources(details, records, api)
    if selections is None:
        import secrets
        if (type(start) is not int or not 0 <= start <= 100_000
                or type(max_batches) is not int or not 1 <= max_batches <= 12):
            raise ValueError('invalid EPUB batch range')
        eligible = [row for row in bound_details['rows'] if isinstance(row, dict)
                    and isinstance(row.get('key'), str) and builder.shard_for(row['key'], 8) == shard
                    and (row.get('state') == 'content_changed'
                         or (row.get('state') == 'not_mapped' and row.get('proof') == 'unmapped-lfs-sha256'))]
        selections = [(offset, secrets.token_hex(16))
                      for offset in range(start, min(len(eligible), start + max_batches * MAX_BATCH), MAX_BATCH)]
        if not selections:
            raise ValueError('EPUB batch range has no eligible sources')
    if not isinstance(selections, list) or not selections or len(selections) > 12:
        raise ValueError('invalid EPUB batch selections')
    if len({op for _, op in selections}) != len(selections) or len({offset for offset, _ in selections}) != len(selections):
        raise ValueError('EPUB batch selections must have distinct operations and offsets')
    plans = [create(summary, bound_details, corpus, revisions, canonical, shards, shard=shard,
                    operation=operation, offset=offset) for offset, operation in selections]
    if len({row['key'] for plan in plans for row in plan['sources']}) != sum(len(p['sources']) for p in plans):
        raise ValueError('EPUB batch selections overlap')
    if hub.read_bytes(backlog.SEARCH_BUCKET, backlog.CURRENT) != pointer:
        raise ValueError('search pointer changed during exact EPUB planning')
    reader_pointer, _ = backlog.publication.current(hub)
    if reader_pointer is None or reader_pointer['generation'] != summary['reader_generation']:
        raise ValueError('Reader pointer changed during exact EPUB planning')
    if backlog.matrix.read_json(fs, backlog.READER_BUCKET, root + '/index.json') != canonical:
        raise ValueError('EPUB canonical index changed during exact planning')
    if any(backlog.matrix.read_json(fs, backlog.READER_BUCKET, f'{root}/index-{number:02d}.json') != snapshot
           for number, snapshot in shards.items()):
        raise ValueError('EPUB shard changed during exact planning')
    for repo, revision in sorted(revisions.items()):
        info = backlog.retry_read(lambda: api.repo_info(repo, repo_type='dataset', timeout=30))
        if getattr(info, 'sha', None) != revision:
            raise ValueError('dataset head changed during exact EPUB planning: ' + repo)
    return plans


def inspect(*, hub, api, fs, client, shard, operation, threshold=100, offset=0):
    """Recompute and bind a fresh plan; this function performs no remote writes."""
    return _inspect_selection(hub=hub, api=api, fs=fs, client=client, shard=shard,
                              selections=[(offset, operation)], threshold=threshold)[0]


def inspect_batches(*, hub, api, fs, client, shard, threshold=100, start=0, max_batches=12, selections=None):
    """Select or independently repeat up to 12 disjoint 3-source plans."""
    if type(shard) is not int or not 0 <= shard < 8:
        raise ValueError('invalid EPUB batch shard')
    return _inspect_selection(hub=hub, api=api, fs=fs, client=client, shard=shard,
                              selections=selections, threshold=threshold, start=start, max_batches=max_batches)


def inspect_one(*, hub, api, fs, client, shard, operation, threshold=100, offset=0):
    """Select one content-verified candidate; preserve the same identities."""
    kwargs={'offset':offset} if offset else {}
    plan=inspect(hub=hub,api=api,fs=fs,client=client,shard=shard,operation=operation,threshold=threshold,**kwargs)
    body={**plan,'sources':plan['sources'][:1],'max_attempts':1}
    body.pop('plan_sha256')
    body['plan_sha256']=hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    return body


def main(argv=None):
    import argparse
    import os
    import secrets
    import httpx
    from huggingface_hub import HfApi, HfFileSystem
    try:
        from .reader_bucket_store import HubBucketStore
    except ImportError:
        from reader_bucket_store import HubBucketStore
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('EPUB plan output must be a new file')
    token = os.environ.get('HF_TOKEN')
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        plan = inspect(hub=HubBucketStore(), api=HfApi(token=token), fs=HfFileSystem(token=token),
                       client=client, shard=args.shard, operation=secrets.token_hex(16))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf8') as stream:
        json.dump(plan, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps({'kind': plan['kind'], 'plan_sha256': plan['plan_sha256'], 'shard': plan['shard'],
                      'selected': len(plan['sources']), 'remote_writes': 0, 'publication_ready': False},
                     ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
