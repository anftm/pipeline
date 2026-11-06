"""Offline experiment for reusing exact older CCRD generation resources.

The existing production lifecycle rejects cross-generation metadata/archive
paths. This is a *separate shadow validator*, not a replacement or extension
of that lifecycle. It models just one hop from an existing v2 same-generation
parent. No script uses it for publication, activation or GC; no IO is done.

Parent paths and declared hashes are not proof of actual bytes, external writer
isolation, GC safety, recovery, or permission to publish. Every result retains
publication_ready=False. Repeated updates need a separately reviewed contract.
"""
from __future__ import annotations

from copy import deepcopy
import re

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_single_source_manifest_draft as draft
    from . import ccrd_single_source_plan as planner
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_single_source_manifest_draft as draft
    import ccrd_single_source_plan as planner


PROFILE = 'ccrd-single-source-one-hop-reuse-shadow-v1'


def _metadata_at_prefix(manifest, prefix):
    result = {}
    for ref in manifest['metadata']:
        path = ref['path']
        if not path.startswith(prefix):
            raise ValueError('draft metadata is not in its own generation')
        name = path[len(prefix):]
        if name in result:
            raise ValueError('duplicate draft metadata path')
        result[name] = ref
    if set(result) != draft.METADATA_PATHS:
        raise ValueError('draft metadata path set changed')
    return result


def propose(parent_bytes: bytes, candidate: dict) -> dict:
    """Transform a checked local draft into a one-hop reuse experiment."""
    parent, old_metadata, old_archives = draft._parent_parts(parent_bytes)
    if (not isinstance(candidate, dict) or candidate.get('publication_ready') is not False
            or candidate.get('remote_writes') != 0 or not isinstance(candidate.get('manifest'), dict)):
        raise ValueError('a non-publishing manifest draft is required')
    original = candidate['manifest']
    lifecycle.references(original, 'current.json')
    if original.get('previous_index_manifest_sha256') != planner.sha(parent_bytes):
        raise ValueError('draft does not identify the exact supplied parent')
    if original.get('previous_base_metadata_sha256') != parent['base_metadata_sha256']:
        raise ValueError('draft does not identify the parent metadata')
    prefix = f'generations/{original["generation"]}/'
    metadata = _metadata_at_prefix(original, prefix)
    proposed_archive = draft._path_map(original['archives'], prefix)
    if set(proposed_archive) != set(old_archives):
        raise ValueError('archive set differs from the parent')
    for name, ref in proposed_archive.items():
        if {**ref, 'path': old_archives[name]['path']} != old_archives[name]:
            raise ValueError('archive identity differs from the parent')
    for source in ('CCRD', 'RMRB', 'PLA'):
        if original['databases'][source] != parent['databases'][source]:
            raise ValueError('non-target database identity changed')
    if (original['databases']['CW']['path'] != prefix + 'CW.sqlite3'
            or set(original) != draft.MANIFEST_KEYS):
        raise ValueError('unsupported CW draft path or manifest shape')

    # The actual reuse profile affects the new immutable generation name; a
    # copying draft and reuse draft must never claim the same manifest path.
    identity = {'kind': PROFILE, 'parent_sha256': planner.sha(parent_bytes),
                'copy_draft_sha256': planner.sha(lifecycle.canonical(original)),
                'reused_archive_refs': [deepcopy(old_archives[name]) for name in sorted(old_archives)],
                'reused_metadata_refs': [deepcopy(old_metadata[name]) for name in sorted(metadata)
                                         if (metadata[name]['sha256'], metadata[name]['bytes']) ==
                                            (old_metadata[name]['sha256'], old_metadata[name]['bytes'])]}
    generation = 'ccrd-four-' + planner.sha(lifecycle.canonical(identity))
    next_prefix = f'generations/{generation}/'
    value = deepcopy(original)
    value['generation'] = generation
    value['databases']['CW']['path'] = next_prefix + 'CW.sqlite3'
    value['archives'] = [deepcopy(old_archives[name]) for name in sorted(old_archives)]
    value['metadata'] = []
    for name, ref in sorted(metadata.items()):
        if (ref['sha256'], ref['bytes']) == (old_metadata[name]['sha256'], old_metadata[name]['bytes']):
            value['metadata'].append(deepcopy(old_metadata[name]))
        else:
            value['metadata'].append({**deepcopy(ref), 'path': next_prefix + name})
    refs = references(value, parent_bytes)
    serialized = lifecycle.canonical(value)
    return {'manifest': value, 'manifest_bytes': serialized, 'generation': generation, 'identity': identity,
            'references': refs, 'archive_paths_reused': len(value['archives']),
            'metadata_paths_reused': len(identity['reused_metadata_refs']),
            'archive_bytes_copied': 0, 'no_archive_copy_is_only_a_model': True,
            'existing_lifecycle_compatible': False, 'actual_objects_verified': False,
            'gc_safety_proven': False, 'publication_ready': False, 'remote_writes': 0}


def references(value: dict, parent_bytes: bytes) -> list[dict]:
    """Validate one-hop references against an exact supplied parent only.

    This function is deliberately not installed into the real GC graph. It
    requires an independent parent whose manifest digest is in the child and
    only reuses exact parent members, never a caller-selected arbitrary path.
    """
    parent, old_metadata, old_archives = draft._parent_parts(parent_bytes)
    if not isinstance(value, dict) or set(value) != draft.MANIFEST_KEYS:
        raise ValueError('unknown child manifest shape')
    if (value.get('previous_index_manifest_sha256') != planner.sha(parent_bytes)
            or value.get('previous_base_metadata_sha256') != parent['base_metadata_sha256']):
        raise ValueError('child does not pin its exact parent')
    generation = value.get('generation')
    if not isinstance(generation, str) or not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', generation):
        raise ValueError('invalid child generation')
    prefix = f'generations/{generation}/'
    for source in ('CCRD', 'RMRB', 'PLA'):
        if value.get('databases', {}).get(source) != parent['databases'][source]:
            raise ValueError('non-target database changed')
    if value.get('databases', {}).get('CW', {}).get('path') != prefix + 'CW.sqlite3':
        raise ValueError('child CW path outside its generation')
    if (not isinstance(value.get('archives'), list)
            or len(value['archives']) != len(old_archives)
            or sorted(value['archives'], key=lambda ref: ref['path']) !=
               sorted(old_archives.values(), key=lambda ref: ref['path'])):
        raise ValueError('all archive references must exactly match the parent')

    by_old_path = {ref['path']: name for name, ref in old_metadata.items()}
    seen, normalized = set(), deepcopy(value)
    normalized['metadata'] = []
    for ref in value.get('metadata', []):
        if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
            raise ValueError('invalid child metadata reference')
        path = ref['path']
        if path in by_old_path:
            name = by_old_path[path]
            if ref != old_metadata[name]:
                raise ValueError('reused metadata differs from exact parent reference')
        elif path.startswith(prefix):
            name = path[len(prefix):]
            if name not in old_metadata:
                raise ValueError('unexpected new metadata path')
        else:
            raise ValueError('metadata points outside parent and child generations')
        if name in seen:
            raise ValueError('duplicate metadata reference')
        seen.add(name)
        normalized['metadata'].append({**ref, 'path': prefix + name})
    if seen != draft.METADATA_PATHS:
        raise ValueError('incomplete metadata reference set')
    normalized['archives'] = [{**old_archives[name], 'path': prefix + name} for name in sorted(old_archives)]
    # Run the existing full structural/count contract on the normalized shadow
    # copy, then substitute the actual one-hop object references in its result.
    lifecycle.references(normalized, 'current.json')
    db_refs = [{'bucket': lifecycle.BUCKET, 'path': item['path'], 'sha256': item['sha256'],
                'bytes': item['bytes'], 'role': 'runtime'} for item in value['databases'].values()]
    refs = [*db_refs, *value['metadata'], *value['archives']]
    paths = [(ref['bucket'], ref['path']) for ref in refs]
    if len(paths) != len(set(paths)):
        raise ValueError('child reuses a physical reference more than once')
    return [lifecycle.resource(deepcopy(ref)) for ref in refs]
