"""Prove which parent metadata files contain the target CCRD document ID.

Every declared metadata file is checked against its manifest hash and streamed
under a decompression limit. The inventory never updates or publishes files.
"""
from __future__ import annotations

import gzip
from pathlib import Path

import ijson

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as patch
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as patch


MAX_FILES = 64
MAX_SCALARS = 25_000_000


def inventory(manifest: dict, root: Path) -> dict:
    lifecycle.references(manifest, 'current.json')
    if manifest.get('format') != 2:
        raise ValueError('metadata inventory requires four sources')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('metadata inventory root must be an ordinary directory')
    metadata = manifest.get('metadata')
    if not isinstance(metadata, list) or not 1 <= len(metadata) <= MAX_FILES:
        raise ValueError('invalid CCRD metadata inventory size')
    prefix = f"generations/{manifest['generation']}/"
    results = {}
    for entry in metadata:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not entry['path'].startswith(prefix):
            raise ValueError('metadata reference does not belong to parent generation')
        relative = entry['path'][len(prefix):]
        lifecycle.safe_path(relative)
        if relative in results or not relative.endswith(('.json', '.json.gz', '.tsv.gz')):
            raise ValueError('duplicate or unsupported CCRD metadata member')
        path = patch._input(root, relative)
        expected = {'sha256': entry['sha256'], 'bytes': entry['bytes']}
        if patch.identity(path) != expected:
            raise ValueError('CCRD metadata file identity mismatch: ' + relative)
        matches, scalars = [], 0
        if relative.endswith('.tsv.gz'):
            with gzip.open(path, 'rb') as source:
                scanned, tail = 0, b''
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    scanned += len(chunk)
                    if scanned > patch.MAX_EXPANDED:
                        raise ValueError('CCRD metadata TSV exceeds expanded limit')
                    if b'CW:0' in tail + chunk:
                        matches.append('tsv-byte-match')
                        break
                    tail = chunk[-3:]
            scalars = scanned
        else:
            with patch._open(path) as source:
                for prefix_value, event, value in ijson.parse(patch.LimitedReader(source, patch.MAX_EXPANDED), use_float=True):
                    if event in {'string', 'number', 'boolean', 'null'}:
                        scalars += 1
                        if scalars > MAX_SCALARS:
                            raise ValueError('CCRD metadata scalar count exceeds limit')
                    if event == 'string' and value == 'CW:0':
                        matches.append(prefix_value)
                        if len(matches) > 16:
                            raise ValueError('CCRD metadata has too many target references')
        results[relative] = {'identity': expected, 'target_references': matches, 'scalars': scalars}
    return {'version': 1, 'kind': 'ccrd-cw0-parent-metadata-inventory', 'generation': manifest['generation'],
            'files': results, 'target_files': sorted(name for name, value in results.items() if value['target_references']),
            'remote_writes': 0, 'publication_ready': False}


def classify(report: dict) -> None:
    if (not isinstance(report, dict) or report.get('kind') != 'ccrd-cw0-parent-metadata-inventory'
            or report.get('publication_ready') is not False or report.get('remote_writes') != 0
            or report.get('target_files') != sorted(patch.PATHS)):
        raise ValueError('CW:0 metadata references differ from the supported four-file layout')
    files = report.get('files')
    if not isinstance(files, dict):
        raise TypeError('missing CCRD metadata inventory files')
    expected_paths = {name: patch._item_prefix(name) + '.doc_id' for name in patch.PATHS}
    for name, prefix in expected_paths.items():
        if not isinstance(files.get(name), dict) or files[name].get('target_references') != [prefix]:
            raise ValueError('CW:0 metadata reference identity differs: ' + name)
