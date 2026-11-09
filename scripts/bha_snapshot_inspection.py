"""Read-only observation of BHA's durable index snapshot and sidecars.

The full-sidecar path is bounded and opt-in. Manifest-only inspection is not
proof of sidecar bytes or an Elasticsearch restore. Nothing is written remotely.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from . import bha_reindex_plan as planning
except ImportError:
    import bha_reindex_plan as planning


BASE = 'https://huggingface.co/buckets/vomebook/bha-data/resolve/'
MAX_MANIFEST = 256 * 1024
MAX_SIDECAR = 12 * 1024 * 1024 * 1024
GEN = re.compile(r'bha-[a-z0-9-]+\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _get(key: str, limit: int, *, opener=urlopen) -> bytes:
    request = Request(BASE + quote(key, safe='/'), headers={'User-Agent': 'pipeline-bha-snapshot-inspection/1.0',
                                                           'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    with opener(request, timeout=120) as response:
        if response.status != 200: raise ValueError('unexpected BHA snapshot response')
        raw = response.read(limit + 1)
    if not 0 < len(raw) <= limit: raise ValueError('BHA snapshot object is empty or oversized')
    return raw


def _hash(key: str, expected_bytes: int, *, opener=urlopen) -> str:
    if type(expected_bytes) is not int or not 0 < expected_bytes <= MAX_SIDECAR:
        raise ValueError('BHA snapshot sidecar size is invalid')
    request = Request(BASE + quote(key, safe='/'), headers={'User-Agent': 'pipeline-bha-snapshot-inspection/1.0',
                                                           'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    digest, count = hashlib.sha256(), 0
    with opener(request, timeout=120) as response:
        if response.status != 200: raise ValueError('unexpected BHA sidecar response')
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            count += len(chunk)
            if count > expected_bytes: raise ValueError('BHA snapshot sidecar exceeds its declared size')
            digest.update(chunk)
    if count != expected_bytes: raise ValueError('BHA snapshot sidecar is truncated')
    return digest.hexdigest()


def inspect(expected: dict[str, str], *, full_sidecars: bool = False, expected_generation: str | None = None,
            not_before: int | None = None, get=_get, hash_object=_hash) -> dict:
    target = planning._trees(expected, 'snapshot target revisions')
    if type(full_sidecars) is not bool: raise ValueError('BHA sidecar inspection mode must be explicit')
    if expected_generation is not None and (not isinstance(expected_generation, str) or not GEN.fullmatch(expected_generation)):
        raise ValueError('BHA expected snapshot generation is invalid')
    if not_before is not None and (type(not_before) is not int or not_before < 0):
        raise ValueError('BHA snapshot minimum time is invalid')
    raw = get('current.json', MAX_MANIFEST)
    value = json.loads(raw)
    metadata = value.get('index_metadata') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 3
            or not isinstance(metadata, dict) or planning._trees(metadata.get('archive_revisions'), 'snapshot revisions') != target
            or type(value.get('document_count')) is not int or value['document_count'] < 1):
        raise ValueError('BHA durable snapshot does not describe the target revisions')
    generation, snapshot = value.get('generation'), value.get('snapshot')
    if (not isinstance(generation, str) or not GEN.fullmatch(generation) or generation != snapshot
            or expected_generation is not None and generation != expected_generation
            or not_before is not None and (type(value.get('created_at')) is not int or value['created_at'] < not_before)
            or metadata.get('archive_start') != 0 or metadata.get('archive_end') != 31
            or not isinstance(metadata.get('version'), str) or not metadata['version']
            or not isinstance(metadata.get('source_fingerprint'), str) or not DIGEST.fullmatch(metadata['source_fingerprint'])
            or not isinstance(metadata.get('build_fingerprint'), str) or not DIGEST.fullmatch(metadata['build_fingerprint'])
            or not isinstance(value.get('index_name'), str) or not value['index_name']
            or not isinstance(value.get('physical_index'), str)
            or not value['physical_index'].startswith(value['index_name'] + '-')):
        raise ValueError('BHA durable snapshot identity is malformed')
    planning._trees(value.get('archive_commits'), 'snapshot commits')
    sidecars = value.get('sidecars')
    if not isinstance(sidecars, dict) or set(sidecars) != {'docs', 'facets'}:
        raise ValueError('BHA durable snapshot sidecars are incomplete')
    receipts = {}
    for name, filename in (('docs', 'docs.sqlite'), ('facets', 'facets.sqlite')):
        details = sidecars[name]
        if (not isinstance(details, dict) or details.get('filename') != filename
                or type(details.get('bytes')) is not int or not 0 < details['bytes'] <= MAX_SIDECAR
                or not isinstance(details.get('sha256'), str) or not DIGEST.fullmatch(details['sha256'])):
            raise ValueError('BHA durable snapshot sidecar identity differs')
        if full_sidecars:
            digest = hash_object(f'generations/{generation}/{filename}', details['bytes'])
            if digest != details['sha256']:
                raise ValueError('BHA durable snapshot sidecar bytes differ')
        receipts[name] = {'bytes': details['bytes'], 'sha256': details['sha256'], 'full_bytes_verified': full_sidecars}
    if get('current.json', MAX_MANIFEST) != raw:
        raise ValueError('BHA durable snapshot changed during inspection')
    return {'kind': 'bha-durable-snapshot-observation', 'generation': generation,
            'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'archive_revisions': target,
            'build_fingerprint': metadata['build_fingerprint'], 'source_fingerprint': metadata['source_fingerprint'],
            'document_count': value['document_count'], 'sidecars': receipts,
            'manifest_verified': True, 'sidecars_verified': full_sidecars, 'restore_verified': False,
            'remote_writes': 0, 'publication_ready': False, 'external_writers_verified': False,
            'scope': 'manifest and optional sidecar hashes only; Elasticsearch snapshot blobs not restored or checked'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full-sidecars', action='store_true')
    args = parser.parse_args(argv)
    if (args.output.exists() or args.output.is_symlink() or args.output.resolve() == args.baseline.resolve()
            or args.baseline.is_symlink() or not args.baseline.is_file() or args.baseline.stat().st_size > MAX_MANIFEST):
        raise ValueError('BHA snapshot report and baseline paths are invalid')
    result = inspect(json.loads(args.baseline.read_bytes()), full_sidecars=args.full_sidecars)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True); stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
