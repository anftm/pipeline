"""Conditionally stage exact CCRD generation objects, without mutable roots.

Every input and remote readback is verified. A lost write response is inspected
once and never blindly retried. This module cannot advance current.json, deploy
Space, delete objects, or assert external-writer isolation.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as metadata
    from .reader_bucket_store import S3BucketStore
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as metadata
    from reader_bucket_store import S3BucketStore


GEN = re.compile(r'ccrd-four-[0-9a-f]{64}\Z')
MAX_BYTES = 4 * 1024 * 1024 * 1024


class Store(Protocol):
    def inspect(self, key: str) -> dict | None: ...
    def create(self, key: str, path: Path, expected: dict) -> None: ...
    def parent_identity(self) -> str: ...


def _identity(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError('staged input must be a bounded ordinary file')
    size, digest = 0, hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk); digest.update(chunk)
    return {'sha256': digest.hexdigest(), 'bytes': size}


def _validate(plan: dict) -> dict:
    if (not isinstance(plan, dict) or plan.get('version') != 1
            or plan.get('kind') != 'ccrd-cw0-local-staging-plan' or plan.get('publication_ready') is not False
            or plan.get('remote_writes') != 0 or not isinstance(plan.get('manifest'), dict)):
        raise ValueError('invalid inert CCRD staging plan')
    generation, manifest, identity = plan.get('generation'), plan['manifest'], plan.get('identity')
    if (not isinstance(generation, str) or not GEN.fullmatch(generation)
            or not isinstance(identity, dict) or generation != 'ccrd-four-' + hashlib.sha256(lifecycle.canonical(identity)).hexdigest()
            or manifest.get('generation') != generation):
        raise ValueError('CCRD staging generation identity differs')
    refs = lifecycle.references(manifest, 'current.json')
    if hashlib.sha256(lifecycle.canonical(manifest)).hexdigest() != plan.get('manifest_sha256'):
        raise ValueError('CCRD staging manifest identity differs')
    entries = plan.get('objects')
    if not isinstance(entries, list) or not entries:
        raise ValueError('CCRD staging plan has no objects')
    target_refs = [item for item in refs if item['path'].startswith(f'generations/{generation}/')]
    expected_paths = {item['path']: (item['sha256'], item['bytes']) for item in target_refs}
    if len(expected_paths) != len(target_refs) or len(expected_paths) != len(entries):
        raise ValueError('CCRD staging object count differs from manifest')
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'kind', 'source', 'source_path', 'target', 'sha256', 'bytes'}:
            raise ValueError('invalid CCRD staging entry')
        target = entry['target']
        if not isinstance(target, str) or not target.startswith(f'generations/{generation}/'):
            raise ValueError('invalid CCRD staging target')
        lifecycle.safe_path(target)
        if (entry['kind'] not in {'metadata', 'archive', 'CW database'} or entry['source'] not in {'parent', 'candidate'}
                or type(entry['bytes']) is not int or entry['bytes'] < 1 or entry['bytes'] > MAX_BYTES
                or expected_paths.pop(target, None) != (entry['sha256'], entry['bytes'])):
            raise ValueError('CCRD staging entry is not an exact manifest member')
        lifecycle.sha(entry['sha256'])
    if expected_paths:
        raise ValueError('CCRD staging plan omits manifest members')
    return manifest


def _local_map(plan: dict, parent_root: Path, candidate_root: Path, candidate_cw: Path) -> dict[str, Path]:
    parent = plan['identity']['parent_generation']
    if not isinstance(parent, str) or not GEN.fullmatch(parent):
        raise ValueError('CCRD staging plan lacks an exact parent generation')
    prefix = f'generations/{parent}/'
    result = {}
    for entry in plan['objects']:
        source = entry['source_path']
        if not isinstance(source, str):
            raise TypeError('invalid CCRD staging source')
        lifecycle.safe_path(source)
        if entry['kind'] == 'CW database':
            if entry['source'] != 'candidate' or source != plan['identity']['cw_database']['parent_path']:
                raise ValueError('invalid CW database staging origin')
            path = candidate_cw
        else:
            if not source.startswith(prefix):
                raise ValueError('CCRD staging source is outside the parent generation')
            relative = source[len(prefix):]
            expected_source = 'parent' if entry['kind'] == 'archive' else (
                'candidate' if relative in metadata.PATHS else 'parent')
            if entry['source'] != expected_source:
                raise ValueError('CCRD staging origin is not permitted')
            path = metadata._input(candidate_root if expected_source == 'candidate' else parent_root, relative)
        result[entry['target']] = path
    return result


def stage(plan: dict, *, parent_root: Path, candidate_root: Path, candidate_cw: Path, store: Store) -> dict:
    manifest = _validate(plan)
    entries = plan['objects']
    local_files = _local_map(plan, parent_root, candidate_root, candidate_cw)
    if store.parent_identity() != plan['identity']['parent_pointer_sha256']:
        raise ValueError('CCRD current pointer differs before staging')
    if not isinstance(local_files, dict) or set(local_files) != {item['target'] for item in entries}:
        raise ValueError('local staging files do not exactly cover the generation')
    for item in entries:
        expected = {'sha256': item['sha256'], 'bytes': item['bytes']}
        if _identity(local_files[item['target']]) != expected:
            raise ValueError('CCRD staging local file differs: ' + item['target'])
    written = 0
    for item in entries:
        expected = {'sha256': item['sha256'], 'bytes': item['bytes']}
        key, path = item['target'], local_files[item['target']]
        observed = store.inspect(key)
        if observed is None:
            try:
                store.create(key, path, expected)
            except Exception:
                observed = store.inspect(key)
                if observed != expected:
                    raise
            else:
                observed = store.inspect(key)
            written += 1
        if observed != expected:
            raise ValueError('CCRD staged object readback differs: ' + key)
    if store.parent_identity() != plan['identity']['parent_pointer_sha256']:
        raise ValueError('CCRD current pointer changed while staging')
    return {'version': 1, 'kind': 'ccrd-cw0-staged-objects', 'generation': manifest['generation'],
            'objects_verified': len(entries), 'objects_created': written,
            'manifest_written': False, 'pointer_written': False, 'consumer_verified': False,
            'publication_ready': False, 'external_writers_verified': False}


class S3Store:
    """Streaming S3 adapter restricted to exact immutable CCRD generation members."""
    def __init__(self, *, generation: str, store=None):
        if not isinstance(generation, str) or not GEN.fullmatch(generation):
            raise ValueError('invalid CCRD staging generation')
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                                                       request_checksum_calculation='when_required',
                                                       response_checksum_validation='when_required'))
        self.generation, self.store = generation, store

    def parent_identity(self) -> str:
        raw = self.store.read_bytes(lifecycle.BUCKET, 'current.json')
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 2 * 1024 * 1024:
            raise ValueError('invalid CCRD current pointer bytes')
        lifecycle.references(json.loads(raw), 'current.json')
        return hashlib.sha256(raw).hexdigest()

    def _location(self, key: str) -> tuple[str, str]:
        if not isinstance(key, str) or not key.startswith(f'generations/{self.generation}/'):
            raise ValueError('CCRD staging key is outside the candidate generation')
        lifecycle.safe_path(key)
        if key.endswith('/manifest.json'):
            raise ValueError('CCRD manifest publication is a separate step')
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        return namespace, name

    def inspect(self, key: str) -> dict | None:
        namespace, name = self._location(key)
        try:
            response = self.store._client(namespace).get_object(Bucket=name, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise
        size = response.get('ContentLength')
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise ValueError('CCRD staged object size is missing or oversized')
        digest, count = hashlib.sha256(), 0
        body = response['Body']
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b''):
                count += len(chunk)
                if count > size:
                    raise ValueError('CCRD staged object exceeds declared size')
                digest.update(chunk)
        finally:
            body.close()
        if count != size:
            raise ValueError('CCRD staged object is truncated')
        return {'sha256': digest.hexdigest(), 'bytes': count}

    def create(self, key: str, path: Path, expected: dict) -> None:
        namespace, name = self._location(key)
        if _identity(path) != expected:
            raise ValueError('CCRD local staged object changed')
        with path.open('rb') as body:
            self.store._client(namespace).put_object(Bucket=name, Key=key, Body=body,
                                                      ContentLength=expected['bytes'],
                                                      ContentType='application/octet-stream', IfNoneMatch='*')
