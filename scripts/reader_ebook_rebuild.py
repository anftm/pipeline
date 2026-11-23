"""Build a read-only plan for rebuilding published chapter books in pinned Search."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import httpx
from huggingface_hub import HfApi

from scripts import publish_reader_v3 as publication, reader_format_backlog as backlog
from scripts.pilot_epub_chapters import TARGET_BUCKET, shard_for

REPO = 'VoiceOfML/MLMRL-Library'
ALL_SCOPE = 'all-search-repositories'
EXTENSIONS = frozenset({'epub', 'mobi', 'azw3', 'fb2', 'chm'})
SHARDS = 8
MAX_PER_SHARD = 200


def require(ok, message):
    if not ok:
        raise ValueError(message)


def _rows(records, routes, *, revisions, allow_unmapped):
    rows, unmapped = [], []
    keys = set()
    for key, item in sorted(records.items()):
        ext = item['extension']
        repo = item['repo']
        if repo not in revisions or ext not in EXTENSIONS:
            continue
        require(item['revision'] == revisions[repo], 'source differs from pinned rebuild input')
        route = routes.get(key, {})
        if route.get('s') != 2 or route.get('m') != 'e':
            require(allow_unmapped, f'pinned source has no published chapter route: {item["path"]}')
            unmapped.append({**item, 'reason': 'no_published_chapter_route'})
            continue
        require(type(item['bytes']) is int and item['bytes'] > 0, 'invalid rebuild source size')
        old = route.get('c') or route.get('p')
        old_bucket = (route.get('cb') or route.get('b')) if route.get('c') else route.get('b')
        shape = rf'chapters/ebook/{ext}/([0-9a-f]{{64}})/[0-9a-f]{{16}}/chapter-manifest\.json'
        identity = re.fullmatch(shape, old) if isinstance(old, str) else None
        require(identity and old_bucket == TARGET_BUCKET, f'unexpected old chapter route: {item["path"]}')
        require(key not in keys, 'duplicate source key')
        keys.add(key)
        rows.append({**item, 'source_sha256': identity[1], 'old_manifest': old, 'shard': shard_for(key, SHARDS)})
    require(bool(rows), 'no chapter books found for the exact dataset revision')
    count = Counter((row['repo'], row['extension'], row['shard']) for row in rows)
    require(max(count.values()) <= MAX_PER_SHARD, 'rebuild exceeds per-shard limit; refusing a partial census')
    return rows, unmapped


def planned_rows(records, routes, *, revision):
    return _rows(records, routes, revisions={REPO: revision}, allow_unmapped=False)[0]


def capture(*, generation, reader_generation, store, api, client, scope=REPO, revision=None):
    all_repos = scope == ALL_SCOPE
    require(scope in {REPO, ALL_SCOPE} and backlog.SHA.fullmatch(generation)
            and (revision is None if all_repos else isinstance(revision, str) and backlog.REV.fullmatch(revision))
            and backlog.SHA.fullmatch(reader_generation), 'rebuild requires full immutable pins')
    search_pointer, raw_manifest, corpus, revisions, _ = backlog.capture_search(store)
    require(json.loads(raw_manifest)['generation'] == generation and (all_repos or revisions.get(REPO) == revision),
            'rebuild Search generation or dataset revision changed')
    selected_revisions = revisions if all_repos else {REPO: revision}
    pointer, catalog = publication.current(store)
    require(pointer and pointer['generation'] == reader_generation and catalog.get('base_sidecar'),
            'rebuild Reader generation changed or has no pinned projection')
    routes = publication.project_sidecar(publication.pinned_base(store, catalog, {}), catalog)['f']
    rows, unmapped = _rows(backlog.source_records(corpus, revisions), routes,
                            revisions=selected_revisions, allow_unmapped=all_repos)
    metadata = backlog._metadata(api, {(row['repo'], row['revision'], row['path']): row for row in rows})
    for row in rows:
        item = metadata.get((row['repo'], row['revision'], row['path']))
        require(item is not None and backlog._item_field(item, 'size') == row['bytes'],
                f'missing identity or different byte length: {row["path"]}')
        sha, blob = backlog._lfs_identity(item), backlog._blob_identity(item)
        require((sha == row['source_sha256']) if sha else bool(blob), f'source identity differs: {row["path"]}')
        if not sha:
            row['source_git_blob'] = blob  # verify full content SHA before accepting a local conversion
    for site in ('pages', 'hf'):
        observed = publication.observe_consumer(store, site, client)
        require(observed.get('generation') == reader_generation and observed.get('projection_verified') is True,
                f'{site} Reader differs from pinned generation')
    parents = {'repo': REPO, 'source_revision': revision}
    if all_repos:
        heads = {repo: api.repo_info(repo, repo_type='dataset', timeout=30).sha for repo in sorted(selected_revisions)}
        parents = {'scope': ALL_SCOPE, 'source_revisions': selected_revisions, 'unmapped': unmapped,
                   'source_heads_changed': sorted(repo for repo, sha in heads.items() if sha != selected_revisions[repo])}
    else:
        require(api.repo_info(REPO, repo_type='dataset', timeout=30).sha == revision, 'dataset head changed')
    require(store.read_bytes(backlog.SEARCH_BUCKET, backlog.CURRENT) == search_pointer
            and publication.current(store)[0] == pointer, 'build parents changed during capture')
    return {'version': 1, 'kind': 'reader-ebook-full-rebuild-plan', **parents,
            'search_generation': generation, 'reader_generation': reader_generation,
            'shards': SHARDS, 'max_attempts_per_shard': MAX_PER_SHARD, 'rows': rows, 'remote_writes': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', default=REPO, choices=(REPO, ALL_SCOPE))
    parser.add_argument('--search-generation', required=True)
    parser.add_argument('--source-revision')
    parser.add_argument('--reader-generation', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('output must be a new path')
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        plan = capture(generation=args.search_generation, revision=args.source_revision, scope=args.scope,
                       reader_generation=args.reader_generation, store=backlog.HubBucketStore(),
                       api=HfApi(), client=client)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
    print(json.dumps({'rows': len(plan['rows']), 'formats': Counter(row['extension'] for row in plan['rows']),
                      'unmapped': len(plan.get('unmapped', [])), 'source_heads_changed': plan.get('source_heads_changed', []),
                      'remote_writes': 0}, sort_keys=True))


if __name__ == '__main__':
    main()
