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
    from . import ccrd_staging_plan as staging_plan
    from .reader_bucket_store import S3BucketStore
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as metadata
    import ccrd_staging_plan as staging_plan
    from reader_bucket_store import S3BucketStore


GEN = re.compile(r'ccrd-four-[0-9a-f]{64}\Z')
MAX_BYTES = 4 * 1024 * 1024 * 1024
# Historical newspaper databases can exceed the candidate upload limit.
# This is a separate read-only streaming ceiling, not a writable allowance.
MAX_PRESERVED_DATABASE_BYTES = 12 * 1024 * 1024 * 1024


class Store(Protocol):
    def inspect(self, key: str) -> dict | None: ...
    def create(self, key: str, path: Path, expected: dict) -> None: ...
    def parent_pointer(self) -> bytes: ...


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


def stage(plan: dict, *, approved_description: dict, parent_root: Path, candidate_root: Path,
          candidate_cw: Path, store: Store) -> dict:
    manifest = _validate(plan)
    parent = store.parent_pointer()
    if staging_plan.make(parent, approved_description) != plan:
        raise ValueError('CCRD staging plan differs from the approved four-source candidate')
    entries = plan['objects']
    local_files = _local_map(plan, parent_root, candidate_root, candidate_cw)
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
    if store.parent_pointer() != parent:
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

    def parent_pointer(self) -> bytes:
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        response = self.store._client(namespace).get_object(Bucket=name, Key='current.json')
        length = response.get('ContentLength')
        body = response['Body']
        try:
            if type(length) is not int or not 0 < length <= 2 * 1024 * 1024:
                raise ValueError('invalid CCRD current pointer size')
            raw = body.read(length + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('invalid CCRD current pointer bytes')
        lifecycle.references(json.loads(raw), 'current.json')
        return raw

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

    def _inspect(self, key: str, namespace: str, name: str, *, limit: int | None = None) -> dict | None:
        if limit is None:
            limit = MAX_BYTES
        try:
            response = self.store._client(namespace).get_object(Bucket=name, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise
        size = response.get('ContentLength')
        body = response['Body']
        try:
            if type(size) is not int or not 0 < size <= limit:
                raise ValueError('CCRD staged object size is missing or oversized')
            digest, count = hashlib.sha256(), 0
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

    def inspect(self, key: str) -> dict | None:
        namespace, name = self._location(key)
        return self._inspect(key, namespace, name)

    def inspect_preserved(self, key: str) -> dict | None:
        """Hash only immutable CCRD index/archive paths already named in a plan."""
        lifecycle.safe_path(key)
        permitted = (re.fullmatch(r'generations/ccrd-[A-Za-z0-9._-]+/(?:CCRD|CW)\.sqlite3', key)
                     or re.fullmatch(r'indexes/newspapers/(?:RMRB|PLA)/[0-9a-f]{64}\.sqlite3', key)
                     or re.fullmatch(r'generations/ccrd-four-[0-9a-f]{64}/(?:data/.+|archives/(?:RMRB|PLA)/[^/]+\.tar\.gz)', key))
        if not permitted:
            raise ValueError('preserved CCRD member is outside the immutable allowlist')
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        limit = MAX_PRESERVED_DATABASE_BYTES if re.fullmatch(
            r'indexes/newspapers/(?:RMRB|PLA)/[0-9a-f]{64}\.sqlite3', key) else MAX_BYTES
        return self._inspect(key, namespace, name, limit=limit)

    def read_manifest(self) -> bytes | None:
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        key = f'generations/{self.generation}/manifest.json'
        try:
            response = self.store._client(namespace).get_object(Bucket=name, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise
        length, body = response.get('ContentLength'), response['Body']
        try:
            if type(length) is not int or not 0 < length <= 2 * 1024 * 1024:
                raise ValueError('invalid CCRD manifest object length')
            raw = body.read(length + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('CCRD manifest readback is truncated')
        return raw

    def create_manifest(self, raw: bytes) -> None:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 2 * 1024 * 1024:
            raise ValueError('invalid CCRD manifest bytes')
        value = json.loads(raw)
        key = f'generations/{self.generation}/manifest.json'
        if lifecycle.canonical(value) != raw:
            raise ValueError('CCRD manifest must use canonical bytes')
        lifecycle.references(value, key)
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        self.store._client(namespace).put_object(Bucket=name, Key=key, Body=raw,
                                                  ContentType='application/json', IfNoneMatch='*')

    def create(self, key: str, path: Path, expected: dict) -> None:
        namespace, name = self._location(key)
        if _identity(path) != expected:
            raise ValueError('CCRD local staged object changed')
        with path.open('rb') as body:
            self.store._client(namespace).put_object(Bucket=name, Key=key, Body=body,
                                                      ContentLength=expected['bytes'],
                                                      ContentType='application/octet-stream', IfNoneMatch='*')
