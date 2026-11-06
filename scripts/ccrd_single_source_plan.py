#!/usr/bin/env python3
"""Offline plan for one CCRD TXT change; this module cannot publish.

The caller supplies synthetic or independently read snapshots. The returned
plan is not an authorization, a writer lock, or a deployment receipt. In
particular, no Space commit, bucket upload, index build, or OCR action is made.
The candidate metadata is illustrative: derived caches, unknown schema fields,
and all original resources still require independent review before publication.
"""

from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
import re

try:
    from .ccrd_source_override import CORPUS_PATH
    from .ccrd_strict_json import loads as strict_json_loads
except ImportError:
    from ccrd_source_override import CORPUS_PATH
    from ccrd_strict_json import loads as strict_json_loads


SOURCES = frozenset({'CCRD', 'CW', 'RMRB', 'PLA'})
TARGET_PATH = CORPUS_PATH.as_posix()
TARGET_RELATIVE_PATH = TARGET_PATH.removeprefix('CW/')
HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_update(*, space_revision: str, metadata_gz: bytes, pointer_bytes: bytes,
                activation_bytes: bytes, current_txt: bytes, next_txt: bytes) -> dict:
    """Build a conservative plan and candidate metadata, without IO or writes."""
    if not isinstance(space_revision, str) or not HEX40.fullmatch(space_revision):
        raise ValueError('an exact Space commit is required')
    if not all(isinstance(value, bytes) for value in
               (metadata_gz, pointer_bytes, activation_bytes, current_txt, next_txt)):
        raise ValueError('all inputs must be complete bytes')
    if not current_txt or not next_txt:
        raise ValueError('current and upstream target TXT must not be empty')
    try:
        next_txt.decode('utf-8')
        pointer = strict_json_loads(pointer_bytes)
        activation = strict_json_loads(activation_bytes)
        metadata = strict_json_loads(gzip.decompress(metadata_gz))
    except (UnicodeError, ValueError, EOFError, OSError) as error:
        raise ValueError('invalid TXT, manifest, activation or compressed metadata') from error
    if not isinstance(pointer, dict) or not isinstance(activation, dict) or not isinstance(metadata, dict):
        raise ValueError('manifest, activation and metadata must be objects')
    generation = pointer.get('generation')
    databases = pointer.get('databases')
    if (type(pointer.get('format')) is not int or pointer['format'] != 2
            or pointer.get('kind') != 'ccrd-four-source-generation'
            or not isinstance(generation, str) or not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', generation)
            or not isinstance(databases, dict) or set(databases) != SOURCES):
        raise ValueError('only the exact existing four-source manifest is supported')
    pointer_sha = sha(pointer_bytes)
    if activation != {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': 'vomebook/ccrd-index',
                      'path': f'generations/{generation}/manifest.json', 'sha256': pointer_sha}:
        raise ValueError('Space activation does not identify the exact current manifest')
    if pointer.get('base_metadata_sha256') != sha(metadata_gz):
        raise ValueError('Space metadata differs from the activated manifest')
    overrides = pointer.get('source_overrides')
    if not isinstance(overrides, dict) or not isinstance(overrides.get(TARGET_PATH), dict):
        raise ValueError('manifest does not identify the current target TXT')
    current_sha = sha(current_txt)
    if overrides[TARGET_PATH].get('sha256') != current_sha:
        raise ValueError('current target TXT differs from the activated manifest')

    records, sources = metadata.get('records'), metadata.get('sources')
    if not isinstance(records, list) or not isinstance(sources, list):
        raise ValueError('metadata must include complete records and sources')
    counts = {source: 0 for source in SOURCES}
    ids = set()
    target_index = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError('invalid metadata record')
        source, doc_id = record.get('source'), record.get('doc_id')
        if (source not in SOURCES or not isinstance(doc_id, str) or not doc_id.startswith(source + ':')
                or len(doc_id) == len(source) + 1
                or doc_id in ids):
            raise ValueError('metadata has an unknown source or duplicate/invalid ID')
        ids.add(doc_id)
        counts[source] += 1
        if doc_id == 'CW:0':
            target_index = index
    declared = {}
    for source in sources:
        if (not isinstance(source, dict) or source.get('slug') not in SOURCES
                or source['slug'] in declared or type(source.get('count')) is not int):
            raise ValueError('metadata has invalid or duplicate source counts')
        declared[source['slug']] = source['count']
    if declared != counts:
        raise ValueError('metadata source counts do not match records')
    for source, count in counts.items():
        details = databases[source]
        if (not isinstance(details, dict) or type(details.get('documents')) is not int
                or details['documents'] != count or type(details.get('fts_rows')) is not int
                or details['fts_rows'] != count or not HEX64.fullmatch(str(details.get('sha256', '')))):
            raise ValueError('existing database identity or count does not match metadata')
    if target_index is None:
        raise ValueError('target CW:0 is missing')
    target = records[target_index]
    if (target.get('source') != 'CW' or target.get('storage_root') != 'CW'
            or target.get('storage_rel_path') != TARGET_RELATIVE_PATH
            or type(target.get('size')) is not int or target['size'] != len(current_txt)):
        raise ValueError('target identity, path or current size differs from metadata')
    if set(target) & {'sha256', 'text_sha256', 'source_sha256', 'content_sha256', 'summary', 'snippet'}:
        raise ValueError('target has additional content-derived metadata requiring a coordinated update')

    updated = deepcopy(metadata)
    updated['records'][target_index]['size'] = len(next_txt)
    metadata_next = gzip.compress(json.dumps(updated, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8'),
                                  compresslevel=9, mtime=0)
    changed = current_txt != next_txt
    metadata_changed = updated != metadata
    return {
        'version': 1,
        'kind': 'ccrd-single-txt-offline-plan',
        'target_path': TARGET_PATH,
        'target_doc_id': 'CW:0',
        'space_revision': space_revision,
        'base_manifest_sha256': pointer_sha,
        'base_metadata_sha256': sha(metadata_gz),
        'current_txt_sha256': current_sha,
        'next_txt_sha256': sha(next_txt),
        'next_txt_bytes': len(next_txt),
        'source_counts': counts,
        'unchanged_database_sha256': {name: databases[name]['sha256'] for name in ('CCRD', 'RMRB', 'PLA')},
        'content_changed': changed,
        'metadata_semantically_changed': metadata_changed,
        'candidate_metadata_sha256': sha(metadata_next) if metadata_changed else sha(metadata_gz),
        'candidate_metadata_gz': metadata_next if metadata_changed else metadata_gz,
        'required_actions': (['rebuild-cw-index', 'coordinate-space-and-index', 'verify-deployed-consumer']
                             if changed else []),
        'additional_metadata_review_required': changed,
        'remaining_gates': (['preserve and verify all remaining metadata and user resources',
                             'verify the resulting CW database and all existing source identities',
                             'independent writer isolation, race-safe publication and durable recovery',
                             'exact deployed activation and search/readback verification'] if changed else []),
        'explicit_deletions': [],
        'remote_writes': 0,
        'publication_ready': False,
    }
