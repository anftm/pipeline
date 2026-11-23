"""Convert a pinned ebook rebuild shard to local candidates without publishing it."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
import hashlib
import json
from functools import partial
import os
import time
from pathlib import Path
import re
import shutil
import tempfile

from scripts import reader_ebook_rebuild as plan
from scripts import pilot_epub_chapters as pilot
from scripts.reader_assets import validate_chapter_manifest
from scripts.reader_route_plan import source_key


def require(ok, message):
    if not ok:
        raise ValueError(message)


def shard_rows(value, extension, shard, *, repo=None):
    require(value['kind'] == 'reader-ebook-full-rebuild-plan' and value['version'] == 1
            and value['shards'] == plan.SHARDS and value['remote_writes'] == 0
            and plan.backlog.SHA.fullmatch(value['search_generation'])
            and plan.backlog.SHA.fullmatch(value['reader_generation']), 'invalid rebuild plan')
    if value.get('scope') == plan.ALL_SCOPE:
        revisions = value['source_revisions']
        require(repo is not None and isinstance(revisions, dict) and repo in revisions, 'missing exact repository shard')
        for name, revision in revisions.items():
            source_key(name, 'probe.epub')
            require(isinstance(revision, str) and plan.backlog.REV.fullmatch(revision), 'invalid source revision')
    else:
        require(value.get('scope') is None and value['repo'] == plan.REPO and repo in (None, plan.REPO)
                and plan.backlog.REV.fullmatch(value['source_revision']), 'invalid single-dataset rebuild plan')
        repo, revisions = plan.REPO, {plan.REPO: value['source_revision']}
    require(extension in plan.EXTENSIONS and type(shard) is int and 0 <= shard < plan.SHARDS, 'invalid rebuild shard')
    selected = []
    seen = set()
    for row in value['rows']:
        key, ext = row['key'], row['extension']
        row_repo = row['repo']
        require(row_repo in revisions and row['revision'] == revisions[row_repo]
                and ext in plan.EXTENSIONS and row['path'].lower().endswith('.' + ext)
                and key == source_key(row_repo, row['path']) and key not in seen
                and type(row['shard']) is int and row['shard'] == pilot.shard_for(key, plan.SHARDS)
                and type(row['bytes']) is int and row['bytes'] > 0
                and plan.backlog.SHA.fullmatch(row['source_sha256']), 'unexpected source in rebuild plan')
        prefix = f"chapters/ebook/{ext}/{row['source_sha256']}/"
        old = row['old_manifest']
        require(isinstance(old, str) and old.startswith(prefix) and
                re.fullmatch(r'[0-9a-f]{16}/chapter-manifest\.json', old[len(prefix):]),
                'invalid prior chapter route')
        seen.add(key)
        if row_repo == repo and ext == extension and row['shard'] == shard:
            selected.append(row)
    require(0 < len(selected) <= plan.MAX_PER_SHARD, 'empty or oversized rebuild shard')
    return selected


def _attempt(row, extension, work, token, build):
    key = row['key']
    started = time.monotonic()
    result = {'key': key, 'old_manifest': row['old_manifest']}
    try:
        item = {'repo': row['repo'], 'path': row['path'], 'revision': row['revision'],
                'source_bytes': row['bytes'], 'source_sha256': row['source_sha256'], 'extension': extension}
        entry, _ = build(item, work, token, pilot.TARGET_BUCKET)
        require((entry['key'], entry['source_sha256'], entry['source_bytes'], entry['source_revision'])
                == (key, row['source_sha256'], row['bytes'], row['revision']), 'candidate source identity changed')
        bundle = work/'bundle'
        manifest = bundle/'chapter-manifest.json'
        require(manifest.stat().st_size <= 8 * 1024**2, 'candidate manifest exceeds Reader limit')
        data = manifest.read_bytes()
        checked = validate_chapter_manifest(json.loads(data))
        sizes = [chapter['bytes'] for chapter in checked['chapters']]
        require(len(sizes) <= 10000 and max(sizes) <= 8 * 1024**2 and sum(sizes) <= 512 * 1024**2,
                'candidate chapter bytes exceed Reader limits')
        result.update(state='candidate', entry=entry, manifest_sha256=hashlib.sha256(data).hexdigest(),
                      candidate_bytes=sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file()))
    except Exception as exc:
        # Never put transport URLs or converter stderr in shared receipts.
        result.update(state='held', error=type(exc).__name__)
    result['wall_seconds'] = round(time.monotonic() - started, 3)
    return result


def stage(raw, expected_sha256, extension, shard, output, *, repo=None, workers=4, token=None, build=pilot.build_one,
          max_retained_bytes=4 * 1024**3, process_workers=False):
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, 'rebuild plan SHA-256 mismatch')
    value = json.loads(raw)
    require(not value.get('source_heads_changed'), 'live source heads differed during plan capture')
    selected = shard_rows(value, extension, shard, repo=repo)
    require(1 <= workers <= 4 and max_retained_bytes > 0, 'invalid rebuild worker or storage budget')
    output.mkdir(mode=0o700)  # atomically require a new output directory
    retained = output / 'bundles'
    retained.mkdir(mode=0o700)
    retained_bytes = 0
    rows, pending = [], {}
    jobs = iter(selected)
    executor_type = ProcessPoolExecutor if process_workers else ThreadPoolExecutor
    with tempfile.TemporaryDirectory(dir=output, prefix='work-') as scratch, (output/'results.jsonl').open('x', encoding='utf-8') as stream:
        with executor_type(max_workers=workers) as executor:
            def submit():
                item = next(jobs, None)
                if item is None:
                    return
                identity = hashlib.sha256(item['key'].encode()).hexdigest()
                work = Path(scratch)/identity
                future = executor.submit(_attempt, item, extension, work, token, build)
                pending[future] = (work, identity)

            for _ in range(workers):
                submit()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    work, identity = pending.pop(future)
                    row = future.result()
                    if row['state'] == 'candidate':
                        size = row['candidate_bytes']
                        if size > max_retained_bytes - retained_bytes:
                            row.update(state='held', error='ShardRetentionBudget')
                            del row['entry'], row['manifest_sha256']
                        else:
                            shutil.move(work/'bundle', retained/identity)
                            retained_bytes += size
                            row['bundle'] = f'bundles/{identity}'
                    if work.exists():
                        shutil.rmtree(work)
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True)+'\n')
                    stream.flush()
                    submit()
    result = {'kind': 'reader-ebook-rebuild-shard-stage', 'version': 1, 'plan_sha256': expected_sha256,
              'repo': selected[0]['repo'], 'extension': extension, 'shard': shard, 'attempted': len(rows),
              'candidates': sum(row['state'] == 'candidate' for row in rows),
              'held': sum(row['state'] == 'held' for row in rows), 'retained_bytes': retained_bytes,
              'produced_bytes': sum(row.get('candidate_bytes', 0) for row in rows),
              'worker_type': 'process' if process_workers else 'thread',
              'remote_writes': 0, 'publication_ready': False}
    (output/'summary.json').write_text(json.dumps(result, sort_keys=True)+'\n', encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--extension', required=True, choices=sorted(plan.EXTENSIONS))
    parser.add_argument('--shard', type=int, required=True)
    parser.add_argument('--repo', help='exact dataset for one shard of an all-repository plan')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--max-retained-bytes', type=int, default=4 * 1024**3)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kindle-unpacker', type=Path,
                        help='exact local KindleUnpack script; required for native MOBI/AZW3 preview')
    args = parser.parse_args(argv)
    if not args.plan.is_file() or args.plan.is_symlink() or not 0 < args.plan.stat().st_size <= 8*1024**2:
        raise ValueError('plan file is missing or oversized')
    if args.extension in {'mobi', 'azw3'} and args.kindle_unpacker is None:
        raise ValueError('native Kindle preview requires a pinned unpacker; refusing legacy conversion')
    report = stage(args.plan.read_bytes(), args.plan_sha256, args.extension, args.shard, args.output,
                   repo=args.repo, workers=args.workers, token=os.environ.get('HF_TOKEN'),
                   build=partial(pilot.build_one, kindle_unpacker=args.kindle_unpacker, native_epub_profile=True),
                   max_retained_bytes=args.max_retained_bytes, process_workers=True)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
