"""Validate the exact CCRD staged generation with read-only remote calls.

The selected immutable members are streamed and hashed. This command never
writes to the bucket, changes a pointer or activates Space; a successful report
is not proof that external writers are isolated.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from . import ccrd_auto_stage as auto
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_source_override as source
    from . import ccrd_stage_manifest as manifest
    from . import ccrd_stage_objects as objects
except ImportError:
    import ccrd_auto_stage as auto
    import ccrd_lifecycle as lifecycle
    import ccrd_source_override as source
    import ccrd_stage_manifest as manifest
    import ccrd_stage_objects as objects


class ReadOnlyStore:
    """Read only the exact candidate and allowlisted preserved objects."""
    def __init__(self, generation: str, *, open_url=urlopen):
        if not isinstance(generation, str) or not objects.GEN.fullmatch(generation):
            raise ValueError('invalid CCRD candidate generation')
        self.generation, self.open_url = generation, open_url

    def parent_pointer(self) -> bytes:
        return auto._read_pointer()

    def _url(self, key: str) -> str:
        return auto.READ_ROOT + quote(key, safe='/')

    def _identity(self, key: str, limit: int) -> dict | None:
        request = Request(self._url(key), headers={'User-Agent': 'pipeline-ccrd-stage-readonly/1.0',
                                                  'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
        try:
            response = self.open_url(request, timeout=120)
        except HTTPError as error:
            if error.code == 404:
                error.close()
                return None
            raise
        import hashlib
        digest, count = hashlib.sha256(), 0
        with response:
            if response.status != 200:
                raise ValueError('unexpected CCRD staged object response')
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                count += len(chunk)
                if count > limit:
                    raise ValueError('CCRD staged object exceeds read-only bound')
                digest.update(chunk)
        if count == 0:
            raise ValueError('CCRD staged object is empty')
        return {'bytes': count, 'sha256': digest.hexdigest()}

    def inspect(self, key: str) -> dict | None:
        if not isinstance(key, str) or not key.startswith(f'generations/{self.generation}/') or key.endswith('/manifest.json'):
            raise ValueError('CCRD candidate member is outside the selected generation')
        lifecycle.safe_path(key)
        return self._identity(key, objects.MAX_BYTES)

    def inspect_preserved(self, key: str) -> dict | None:
        import re
        lifecycle.safe_path(key)
        newspaper = re.fullmatch(r'indexes/newspapers/(?:RMRB|PLA)/[0-9a-f]{64}\.sqlite3', key)
        permitted = (newspaper or re.fullmatch(r'generations/ccrd-[A-Za-z0-9._-]+/(?:CCRD|CW)\.sqlite3', key)
                     or re.fullmatch(r'generations/ccrd-four-[0-9a-f]{64}/(?:data/.+|archives/(?:RMRB|PLA)/[^/]+\.tar\.gz)', key))
        if not permitted:
            raise ValueError('CCRD preserved member is outside the immutable allowlist')
        return self._identity(key, objects.MAX_PRESERVED_DATABASE_BYTES if newspaper else objects.MAX_BYTES)

    def read_manifest(self) -> bytes | None:
        key = f'generations/{self.generation}/manifest.json'
        request = Request(self._url(key), headers={'User-Agent': 'pipeline-ccrd-stage-readonly/1.0',
                                                  'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
        try:
            response = self.open_url(request, timeout=120)
        except HTTPError as error:
            if error.code == 404:
                error.close()
                return None
            raise
        with response:
            if response.status != 200:
                raise ValueError('unexpected CCRD candidate manifest response')
            raw = response.read(manifest.MAX_MANIFEST + 1)
        if not 0 < len(raw) <= manifest.MAX_MANIFEST:
            raise ValueError('CCRD candidate manifest is empty or oversized')
        return raw

    def create_manifest(self, raw: bytes) -> None:
        raise RuntimeError('read-only CCRD stage store cannot create a manifest')


def inspect(plan: dict, description: dict, *, store, capture, observe) -> dict:
    result = manifest.stage_manifest(plan, approved_description=description, store=store,
                                     capture=capture, observe=observe, apply=False)
    if result['manifest_present'] is not True or result['manifest_written'] is not False:
        raise ValueError('CCRD candidate immutable manifest is not present')
    return {'kind': 'ccrd-staged-readonly-check', 'generation': result['generation'],
            'manifest_sha256': result['manifest_sha256'], 'references_verified': result['references_verified'],
            'remote_writes': 0, 'pointer_written': False, 'consumer_switched': False,
            'publication_ready': False, 'external_writers_verified': False,
            'scope': 'staged bytes and parent consumer/source at inspection time; no writer fencing'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink() or args.output.resolve().is_relative_to(args.bundle.resolve()):
        raise ValueError('CCRD inspection output must be a new path outside the candidate bundle')
    try:
        from . import ccrd_auto_activate as activation
    except ImportError:
        import ccrd_auto_activate as activation
    parent_raw, description, plan, _text = activation._bundle(args.bundle)
    parent = json.loads(parent_raw)
    text = parent['source_overrides'][str(source.CORPUS_PATH)]
    expected = {key: text[key] for key in ('bytes', 'sha256')}
    token = os.environ.get('GITHUB_TOKEN', '')
    result = inspect(plan, description, store=ReadOnlyStore(plan['generation']),
                     capture=lambda: auto._source(token)[1],
                     observe=lambda: auto.consumer.observe(parent['generation'], expected))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True); output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
