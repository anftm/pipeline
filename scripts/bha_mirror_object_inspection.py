"""Read-only exact mirror-object metadata for bounded BHA source samples.

HF metadata hashes are observations, not proof of downloaded bytes. Optional
bounded full reads verify selected bytes without changing any dataset object.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import inspect_bha_source_mirror as source
except ImportError:
    import inspect_bha_source_mirror as source


MAX_SAMPLE_BYTES = 128 * 1024 * 1024
MAX_FULL = 4


def _metadata(entry, obj) -> dict:
    path = entry.get('path')
    digest, size = entry.get('sha256'), entry.get('bytes')
    if (not isinstance(path, str) or not re.fullmatch(r'archives(?:[0-9]|[12][0-9]|3[01])/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{1,12}', path)
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or type(size) is not int or size <= 0 or path.split('/')[-1].split('.')[0] != digest
            or path.split('/')[1] != digest[:2]):
        raise ValueError('invalid BHA source mirror manifest identity')
    lfs = getattr(obj, 'lfs', None)
    if (getattr(obj, 'path', None) != path or getattr(obj, 'size', None) != size or lfs is None
            or getattr(lfs, 'sha256', None) != digest or getattr(lfs, 'size', None) != size):
        raise ValueError('BHA mirror object metadata differs from its manifest')
    return {'path': path, 'bytes': size, 'sha256': digest, 'lfs_identity_matches': True}


def _fetch(url: str, expected: int) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'pipeline-bha-mirror-sample/1.0',
                                                   'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f'BHA mirror sample HTTP {response.status}')
        raw = response.read(expected + 1)
    if len(raw) != expected:
        raise ValueError('BHA mirror sample byte length differs')
    return raw


def inspect(api, *, archive: int, revision: str, manifest: dict, full_limit: int = 1, fetch=_fetch) -> dict:
    if (type(archive) is not int or not 0 <= archive <= 31 or not isinstance(revision, str)
            or not re.fullmatch(r'[0-9a-f]{40}', revision) or type(full_limit) is not int
            or not 0 <= full_limit <= MAX_FULL):
        raise ValueError('invalid bounded BHA mirror inspection scope')
    files = manifest.get('files') if isinstance(manifest, dict) and manifest.get('version') == 1 else None
    if not isinstance(files, dict):
        raise TypeError('invalid BHA mirror manifest')
    selected = [entry for entry in files.values() if isinstance(entry, dict) and entry.get('archive_id') == archive]
    if not selected:
        raise ValueError('BHA mirror manifest has no selected archive objects')
    by_path = {}
    for entry in selected:
        path = entry.get('path')
        if path in by_path and by_path[path] != entry:
            raise ValueError('BHA mirror manifest has conflicting object paths')
        by_path[path] = entry
    paths = sorted(by_path)
    if len(paths) > 10000:
        raise ValueError('BHA mirror inspection exceeds object count limit')
    found = api.get_paths_info(source.DATASET, paths, repo_type='dataset', revision=revision, expand=True)
    if not isinstance(found, list) or len(found) != len(paths):
        raise ValueError('BHA mirror object metadata is incomplete')
    actual = {getattr(item, 'path', None): item for item in found}
    if set(actual) != set(paths):
        raise ValueError('BHA mirror object paths differ from the manifest')
    rows = [_metadata(by_path[path], actual[path]) for path in paths]
    eligible = [row for row in rows if row['bytes'] <= MAX_SAMPLE_BYTES]
    full = []
    for row in eligible[:full_limit]:
        url = f'https://huggingface.co/datasets/{source.DATASET}/resolve/{revision}/' + urllib.parse.quote(row['path'], safe='/')
        data = fetch(url, row['bytes'])
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('BHA mirror fully downloaded bytes differ from manifest')
        full.append(row['path'])
    if len(full) < min(full_limit, len(paths)):
        raise ValueError('not enough bounded BHA mirror objects for requested full verification')
    if api.repo_info(source.DATASET, repo_type='dataset').sha != revision:
        raise RuntimeError('BHA mirror dataset changed during object inspection')
    return {'version': 1, 'kind': 'bha-mirror-object-inspection', 'archive': archive,
            'dataset_revision': revision, 'objects': len(rows), 'metadata_verified': len(rows),
            'fully_downloaded_verified': full, 'remote_writes': 0, 'publication_ready': False,
            'scope': 'LFS metadata for selected archive; full byte verification only for listed samples'}


def run(api, archive: int, *, full_limit: int = 1) -> dict:
    info = api.repo_info(source.DATASET, repo_type='dataset')
    revision = info.sha
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('BHA mirror dataset revision is not pinned')
    path = Path(api.hf_hub_download(source.DATASET, 'manifest.json', repo_type='dataset', revision=revision))
    if path.stat().st_size > source.MAX_MANIFEST_BYTES:
        raise ValueError('BHA mirror manifest exceeds limit')
    return inspect(api, archive=archive, revision=revision, manifest=json.loads(path.read_bytes()), full_limit=full_limit)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=int)
    parser.add_argument('--full-limit', type=int, default=1)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('BHA mirror report output must not exist')
    from huggingface_hub import HfApi
    result = run(HfApi(token=os.environ.get('HF_TOKEN')), args.archive, full_limit=args.full_limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
