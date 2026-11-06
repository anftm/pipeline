"""Model a four-source generation and relocation list without publishing.

Only in-memory inputs and outputs. Old immutable database references for CCRD,
RMRB and PLA are reused; the CW candidate and each metadata/archive object are
named in a proposed generation. Archive entries are references, NOT proof that
copies exist. lifecycle v2 currently requires all archives and metadata under
the new generation; changing that contract is outside this offline draft.

No remote IO, storage, writer fencing, receipts, garbage collection, OCR, or
activation is performed. All identities are caller-supplied/deterministic and
are forgeable. This is not a complete production publication plan.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_source_override as source_override
    from . import ccrd_single_source_offline_chain as chain
    from . import ccrd_single_source_plan as planner
    from .ccrd_strict_json import loads as strict_json_loads
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_source_override as source_override
    import ccrd_single_source_offline_chain as chain
    import ccrd_single_source_plan as planner
    from ccrd_strict_json import loads as strict_json_loads


MANIFEST_KEYS = {'format', 'kind', 'generation', 'tokenizer_version', 'base_metadata_sha256',
                 'previous_base_metadata_sha256', 'previous_index_manifest_sha256', 'databases',
                 'metadata', 'archives', 'builder', 'source_archive', 'source_overrides', 'corpus_migration'}
METADATA_PATHS = frozenset({'data/search_data.json.gz', 'data/sources.json.gz', 'data/folder_tree.json.gz',
                            'data/folder_browser.json.gz', 'data/newspaper-catalog.tsv.gz',
                            *[f'data/{kind}/{name}.json' for kind in ('initial', 'sidebar')
                              for name in ('global', 'manifest', 'sources/CCRD', 'sources/CW',
                                           'sources/RMRB', 'sources/PLA')]})
BASE_METADATA = 'data/search_data.json.gz'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _path_map(refs, prefix):
    result = {}
    for ref in refs:
        path = ref['path']
        if not path.startswith(prefix):
            raise ValueError('parent reference outside the pinned generation')
        suffix = path[len(prefix):]
        lifecycle.safe_path(suffix)
        if suffix in result:
            raise ValueError('duplicate parent generation member')
        result[suffix] = ref
    return result


def _parent_parts(pointer_bytes: bytes):
    if type(pointer_bytes) is not bytes:
        raise ValueError('parent manifest must be complete bytes')
    parent = strict_json_loads(pointer_bytes)
    if not isinstance(parent, dict) or set(parent) != MANIFEST_KEYS:
        raise ValueError('parent manifest has unknown or missing fields')
    if (not isinstance(parent.get('generation'), str)
            or not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', parent['generation'])):
        raise ValueError('unsupported parent generation identity')
    lifecycle.references(parent, 'current.json')
    parent_prefix = f'generations/{parent["generation"]}/'
    parent_metadata = _path_map(parent['metadata'], parent_prefix)
    parent_archives = _path_map(parent['archives'], parent_prefix)
    if set(parent_metadata) != METADATA_PATHS:
        raise ValueError('parent must identify the reviewed complete metadata path set')
    if (any(set(ref) != {'bucket', 'bytes', 'path', 'role', 'sha256'} or ref['role'] != 'runtime'
            for ref in parent_metadata.values())
            or any(set(ref) != {'bucket', 'bytes', 'documents', 'path', 'role', 'sha256', 'source', 'year'}
                   or ref['role'] != 'runtime' for ref in parent_archives.values())):
        raise ValueError('unknown or unsupported metadata/archive reference fields')
    return parent, parent_metadata, parent_archives


def inspect_parent(pointer_bytes: bytes) -> dict:
    """Summarize declared references only; does not read referenced objects."""
    parent, metadata, archives = _parent_parts(pointer_bytes)
    return {'parent_manifest_sha256': _sha(pointer_bytes), 'generation': parent['generation'],
            'metadata_count': len(metadata), 'metadata_declared_bytes': sum(ref['bytes'] for ref in metadata.values()),
            'archive_count': len(archives), 'archive_declared_bytes': sum(ref['bytes'] for ref in archives.values()),
            'databases': sorted(parent['databases']), 'referenced_bytes_verified': False,
            'remote_writes': 0, 'publication_ready': False}


def build(snapshot: chain.Snapshot, checkpoint: chain.Checkpoint, *, remaining_metadata: dict,
          next_source_url: str) -> dict:
    """Return a synthetic manifest, activation and members-to-copy description."""
    if not isinstance(snapshot, chain.Snapshot):
        raise ValueError('offline snapshot required')
    if not isinstance(next_source_url, str) or next_source_url not in source_override.source_urls():
        raise ValueError('upstream TXT provenance must identify the selected configured URL')
    parent, parent_metadata, parent_archives = _parent_parts(snapshot.pointer_bytes)

    cache_payloads = dict(snapshot.cache_payloads)
    expected_remaining = METADATA_PATHS - set(cache_payloads) - {BASE_METADATA}
    if (BASE_METADATA in cache_payloads or not set(cache_payloads) <= METADATA_PATHS
            or not isinstance(remaining_metadata, dict) or set(remaining_metadata) != expected_remaining
            or any(not isinstance(name, str) or type(raw) is not bytes for name, raw in remaining_metadata.items())):
        raise ValueError('all other metadata bytes must be supplied exactly once')
    originals = {**remaining_metadata, **cache_payloads, BASE_METADATA: snapshot.metadata_gz}
    for name, raw in originals.items():
        ref = parent_metadata[name]
        if len(raw) != ref['bytes'] or _sha(raw) != ref['sha256']:
            raise ValueError('metadata bytes differ from the pinned parent: ' + name)

    bundle = chain.materialize(snapshot, checkpoint)
    proposed_metadata = {**originals, **bundle['cache_payloads'], BASE_METADATA: bundle['metadata_gz']}
    cw_parent = parent['databases']['CW']
    counts = bundle['cw_counts']
    expected_cw_keys = {'path', 'sha256', 'bytes', 'documents', 'fts_rows', 'postings', 'snippet_anchors'}
    if (set(cw_parent) != expected_cw_keys or set(counts) != expected_cw_keys - {'path', 'sha256', 'bytes'}
            or any(type(value) is not int or value < 0 for value in counts.values())
            or counts['documents'] != cw_parent['documents'] or counts['fts_rows'] != cw_parent['fts_rows']):
        raise ValueError('unknown CW database identity or changed document count')
    overrides = parent.get('source_overrides')
    if (not isinstance(overrides, dict) or set(overrides) != {planner.TARGET_PATH}
            or not isinstance(overrides[planner.TARGET_PATH], dict)
            or set(overrides[planner.TARGET_PATH]) != {'bytes', 'sha256', 'url'}
            or overrides[planner.TARGET_PATH]['bytes'] != len(snapshot.current_txt)
            or overrides[planner.TARGET_PATH]['sha256'] != _sha(snapshot.current_txt)
            or not isinstance(overrides[planner.TARGET_PATH]['url'], str)):
        raise ValueError('unknown or stale source override identity')

    base_digest = _sha(snapshot.pointer_bytes)
    # Identity avoids self-referential generation paths; parent/archive
    # references and every content digest are retained exactly.
    identity = {
        'kind': 'ccrd-single-txt-generation-draft-v1', 'parent_manifest_sha256': base_digest,
        'parent_space_revision': snapshot.space_revision, 'target_txt_sha256': _sha(snapshot.next_txt),
        'target_txt_bytes': len(snapshot.next_txt), 'target_source_url': next_source_url,
        'cw_sha256': _sha(bundle['cw_database']),
        'cw_bytes': len(bundle['cw_database']), 'cw_counts': counts,
        'metadata': [{'path': name, 'bytes': len(raw), 'sha256': _sha(raw)}
                     for name, raw in sorted(proposed_metadata.items())],
        'archives': [deepcopy(parent_archives[name]) for name in sorted(parent_archives)],
        'unchanged_databases': {name: deepcopy(parent['databases'][name]) for name in ('CCRD', 'RMRB', 'PLA')},
    }
    generation = 'ccrd-four-' + _sha(lifecycle.canonical(identity))
    prefix = f'generations/{generation}/'
    metadata_refs, archive_refs, relocations = [], [], []
    for name, raw in sorted(proposed_metadata.items()):
        old = parent_metadata[name]
        ref = {**deepcopy(old), 'path': prefix + name, 'sha256': _sha(raw), 'bytes': len(raw)}
        metadata_refs.append(ref)
        relocations.append({'kind': 'metadata', 'source_path': old['path'], 'destination_path': ref['path'],
                            'old_sha256': old['sha256'], 'new_sha256': ref['sha256'],
                            'bytes': ref['bytes'], 'content_changed': raw != originals[name]})
    for name, old in sorted(parent_archives.items()):
        ref = {**deepcopy(old), 'path': prefix + name}
        archive_refs.append(ref)
        relocations.append({'kind': 'archive', 'source_path': old['path'], 'destination_path': ref['path'],
                            'old_sha256': old['sha256'], 'new_sha256': old['sha256'],
                            'bytes': old['bytes'], 'content_changed': False})
    proposed = deepcopy(parent)
    proposed.update(generation=generation, previous_index_manifest_sha256=base_digest,
                    previous_base_metadata_sha256=parent['base_metadata_sha256'],
                    base_metadata_sha256=_sha(bundle['metadata_gz']), metadata=metadata_refs, archives=archive_refs)
    proposed['databases']['CW'] = {'path': prefix + 'CW.sqlite3', 'sha256': _sha(bundle['cw_database']),
                                   'bytes': len(bundle['cw_database']), **counts}
    proposed['source_overrides'][planner.TARGET_PATH].update(bytes=len(snapshot.next_txt),
                                                              sha256=_sha(snapshot.next_txt), url=next_source_url)
    lifecycle.references(proposed, 'current.json')
    manifest_bytes = lifecycle.canonical(proposed)
    activation = {'version': 1, 'kind': 'ccrd-index-activation', 'bucket': lifecycle.BUCKET,
                  'path': prefix + 'manifest.json', 'sha256': _sha(manifest_bytes)}
    return {'manifest': proposed, 'manifest_bytes': manifest_bytes, 'activation': activation,
            'relocations': relocations, 'generation': generation, 'generation_identity': identity,
            'generated_artifact_roles': ['CW SQLite candidate', 'changed and unchanged metadata candidates',
                                         'manifest and activation candidates'],
            'generation_identity_profile_verified_by_production': False,
            'space_changed_paths': sorted({planner.TARGET_PATH, 'data/index-release.json'} |
                                          {name for name, raw in proposed_metadata.items() if raw != originals[name]}),
            'parent_database_bytes_verified': {'CW': True, 'CCRD': False, 'RMRB': False, 'PLA': False},
            'upstream_provenance_verified': False,
            'archive_bytes_verified': False, 'cache_inventory_complete_proven': False,
            'remote_writes': 0, 'explicit_deletions': [], 'publication_ready': False}
