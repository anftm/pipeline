"""Hash-pinned, bounded GitHub Release transport for the newspaper candidate.

Transport is not publication: this module never changes a Bucket or Space.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from . import ccrd_lifecycle as lifecycle, shared
except ImportError:
    import ccrd_lifecycle as lifecycle
    import shared


KIND = 'ccrd-newspaper-release-transport'
MAX_PART = 1024 ** 3
RUNTIME_FILES = frozenset({'app.py', 'prepare_fulltext_index.py'})
EVIDENCE_FILES = frozenset({'oracle-receipt.json', 'oracle-process-receipt.json',
    'full-runtime-acceptance-receipt.json', 'full-package-acceptance-receipt.json'})
METADATA_FILES = frozenset({'data/search_data.json.gz', 'data/sources.json.gz',
    'data/folder_tree.json.gz', 'data/folder_browser.json.gz', 'data/newspaper-catalog.tsv.gz',
    *[f'data/{kind}/{name}.json' for kind in ['initial', 'sidebar'] for name in
      ['global', 'manifest', 'sources/CCRD', 'sources/CW', 'sources/RMRB', 'sources/PLA']]})


def release_prefix(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment
            or not re.fullmatch(r'/anftm/pipeline/releases/download/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+', parsed.path)):
        raise ValueError('transport must use a public release of anftm/pipeline, without credentials or query')
    return url.rsplit('/', 1)[0] + '/'


def identity(path):
    digest, size = shared.hash_file(path)
    return {'sha256': digest, 'bytes': size}


def validate(value, url):
    prefix = release_prefix(url)
    if value.get('version') != 1 or value.get('kind') != KIND or value.get('sample_only') is not False:
        raise ValueError('not a full approved newspaper transport')
    manifest = value.get('manifest')
    if (not isinstance(manifest, dict) or manifest.get('format') != 2
            or manifest.get('kind') != 'ccrd-four-source-generation'
            or not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', str(manifest.get('generation','')))):
        raise ValueError('newspaper transport requires an exact immutable four-source generation')
    refs = lifecycle.references(manifest, 'current.json')
    lifecycle.sha(manifest.get('previous_index_manifest_sha256'))
    lifecycle.sha(manifest.get('previous_base_metadata_sha256'))
    if manifest.get('builder', {}).get('sha256') != value.get('builder_sha256'):
        raise ValueError('transport builder differs from its manifest')
    lifecycle.sha(value.get('builder_sha256'))
    root = f'generations/{manifest["generation"]}/'
    metadata = {ref['path'][len(root):] for ref in manifest['metadata'] if ref['path'].startswith(root)}
    if metadata != METADATA_FILES or len(manifest['metadata']) != len(METADATA_FILES):
        raise ValueError('transport must include exactly the complete generated four-source metadata')
    files = value.get('files')
    if not isinstance(files, list):
        raise ValueError('transport lacks files')
    desired = {ref['path']: (ref['sha256'], ref['bytes']) for ref in refs
               if ref['path'] not in {manifest['databases'][source]['path'] for source in ['CCRD', 'CW']}}
    desired.update({f'runtime/{name}': None for name in RUNTIME_FILES})
    desired.update({f'evidence/{name}': None for name in EVIDENCE_FILES})
    seen = set()
    for entry in files:
        path = lifecycle.safe_path(entry.get('path'))
        lifecycle.sha(entry.get('sha256'))
        if (path not in desired or path in seen or type(entry.get('bytes')) is not int or entry['bytes'] < 1
                or desired[path] is not None and (entry['sha256'], entry['bytes']) != desired[path]):
            raise ValueError('transport file differs from its immutable resource allowlist')
        seen.add(path)
        parts = entry.get('parts')
        if not isinstance(parts, list) or not parts:
            raise ValueError('transport file lacks complete parts')
        for part in parts:
            digest = lifecycle.sha(part.get('sha256'))
            if (type(part.get('bytes')) is not int or not 1 <= part['bytes'] <= MAX_PART
                    or part.get('url') != prefix + f'part-{digest}.bin'):
                raise ValueError('invalid newspaper transport part')
        if sum(part['bytes'] for part in parts) != entry['bytes']:
            raise ValueError('transport part coverage differs from the complete file')
    if seen != set(desired):
        raise ValueError('transport omits a complete candidate member')
    old = value.get('previous_runtime_sha256', {})
    if set(old) != RUNTIME_FILES:
        raise ValueError('transport must pin the old versions of every changed runtime file')
    for digest in old.values():
        lifecycle.sha(digest)
    return value


def fetch_descriptor(url, sha256):
    release_prefix(url)
    lifecycle.sha(sha256)
    # Public Release URLs need no Authorization. urllib therefore never carries
    # HF/GitHub credentials to a cross-host signed download redirect.
    with urlopen(Request(url, headers={'User-Agent': 'CCRD-newspaper-transport/1'}), timeout=120) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('newspaper transport descriptor size/hash mismatch')
    return validate(json.loads(raw), url)


def materialize(value, root, *, open_url=urlopen):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    required = sum(entry['bytes'] for entry in value['files'] if not (root / entry['path']).is_file())
    if shutil.disk_usage(root).free < required + 2 * 1024 ** 3:
        raise RuntimeError('insufficient free disk for the complete transport and OCR rebase; refusing partial staging')
    for entry in value['files']:
        target = (root / lifecycle.safe_path(entry['path'])).resolve()
        if root not in target.parents:
            raise ValueError('transport output escapes the candidate root')
        expected = {key: entry[key] for key in ['sha256', 'bytes']}
        if target.exists():
            if identity(target) != expected:
                raise ValueError('existing complete transport file has different bytes')
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        digest, size = hashlib.sha256(), 0
        with temporary.open('wb') as output:
            for part in entry['parts']:
                part_digest, part_size = hashlib.sha256(), 0
                with open_url(Request(part['url'], headers={'User-Agent': 'CCRD-newspaper-transport/1'}), timeout=300) as response:
                    for block in iter(lambda: response.read(1024 * 1024), b''):
                        part_size += len(block)
                        if part_size > part['bytes']:
                            raise ValueError('transport part exceeds declared length')
                        part_digest.update(block)
                        digest.update(block)
                        size += len(block)
                        output.write(block)
                if part_size != part['bytes'] or part_digest.hexdigest() != part['sha256']:
                    raise ValueError('transport part readback differs from its full hash')
        if {'sha256': digest.hexdigest(), 'bytes': size} != expected:
            raise ValueError('complete transport file checksum mismatch')
        temporary.replace(target)
        print(f'materialized complete newspaper file: {entry["path"]} ({size} bytes)', flush=True)
    return root
