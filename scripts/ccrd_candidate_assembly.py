"""Assemble a local CCRD four-source replacement description without publishing.

The result describes every object that would need validation or staging. It is
not a currently valid production manifest and never grants publication rights.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

try:
    from . import ccrd_candidate_preflight as preflight
    from . import ccrd_cw_candidate as cw
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as patch
    from . import ccrd_newspaper_transport as transport
except ImportError:
    import ccrd_candidate_preflight as preflight
    import ccrd_cw_candidate as cw
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as patch
    import ccrd_newspaper_transport as transport


GENERATION = re.compile(r'ccrd-four-[0-9a-f]{64}\Z')
MAX_PLAN = 2 * 1024 * 1024


def _parent(pointer: bytes, approved_sha256: str) -> dict:
    lifecycle.sha(approved_sha256)
    if not isinstance(pointer, bytes) or not 0 < len(pointer) <= MAX_PLAN:
        raise ValueError('CCRD parent pointer is missing or oversized')
    if hashlib.sha256(pointer).hexdigest() != approved_sha256:
        raise ValueError('CCRD parent pointer identity differs from the approved plan')
    value = json.loads(pointer)
    lifecycle.references(value, 'current.json')
    if not isinstance(value.get('generation'), str) or not GENERATION.fullmatch(value['generation']):
        raise ValueError('invalid CCRD parent generation')
    return value


def _metadata_parent(manifest: dict) -> dict:
    prefix = f"generations/{manifest['generation']}/"
    refs = {item['path'][len(prefix):]: item for item in manifest['metadata']}
    if len(refs) != len(manifest['metadata']) or set(refs) != transport.METADATA_FILES:
        raise ValueError('CCRD parent does not contain exactly the supported metadata members')
    return refs


def _local_metadata(refs: dict, root: Path, manifest: dict) -> dict:
    try:
        from . import ccrd_metadata_inventory as inventory
    except ImportError:
        import ccrd_metadata_inventory as inventory
    result = inventory.inventory(manifest, root)
    inventory.classify(result)
    if set(result['files']) != set(refs):
        raise ValueError('CCRD metadata inventory is incomplete')
    return result


def _candidate_metadata(parent_root: Path, candidate_root: Path, refs: dict, old_text: str, new_text: str) -> dict:
    changed = {}
    for name in patch.PATHS:
        old_file = patch._input(parent_root, name)
        new_file = patch._input(candidate_root, name)
        expected = {'sha256': refs[name]['sha256'], 'bytes': refs[name]['bytes']}
        if patch.identity(old_file) != expected:
            raise ValueError('CCRD parent metadata changed during candidate assembly')
        item = patch._selected(old_file, name)
        updates = patch._patch_fields(name, item, len(old_text.encode()), len(new_text.encode()), old_text, new_text)
        patch.verify(old_file, new_file, name, item, updates)
        changed[name] = patch.identity(new_file)
    return changed


def _cw_database(manifest: dict, source: Path, target: Path, old_text: str, new_text: str) -> tuple[dict, dict]:
    entry = manifest['databases']['CW']
    expected = {'sha256': entry['sha256'], 'bytes': entry['bytes']}
    if cw.file_identity(source, cw.MAX_DB_BYTES) != expected:
        raise ValueError('CCRD parent CW database identity differs')
    actual = cw.file_identity(target, cw.MAX_DB_BYTES)
    import sqlite3
    with sqlite3.connect(f'file:{source.resolve()}?mode=ro&immutable=1', uri=True) as database:
        cw._preflight(database, old_text, entry['documents'])
    with sqlite3.connect(f'file:{target.resolve()}?mode=ro&immutable=1', uri=True) as database:
        cw._preflight(database, new_text, entry['documents'])
    audit = cw.audit_unchanged(source, target, documents=entry['documents'])
    if cw.file_identity(source, cw.MAX_DB_BYTES) != expected or cw.file_identity(target, cw.MAX_DB_BYTES) != actual:
        raise ValueError('CCRD database changed during candidate assembly')
    return actual, audit


def assemble(pointer: bytes, old: bytes, new: bytes, consumer: dict, source_observation: dict, *,
             approved_pointer_sha256: str, parent_metadata: Path, candidate_metadata: Path,
             parent_cw: Path, candidate_cw: Path) -> dict:
    manifest = _parent(pointer, approved_pointer_sha256)
    if not isinstance(source_observation, dict):
        raise TypeError('pinned source observation is missing')
    preflight.plan(pointer, old, new, consumer, source_url=source_observation.get('url'),
                   source_revision=source_observation.get('revision'), source_observation=source_observation)
    refs = _metadata_parent(manifest)
    inventory = _local_metadata(refs, parent_metadata, manifest)
    old_text, new_text = old.decode('utf-8'), new.decode('utf-8')
    changed = _candidate_metadata(parent_metadata, candidate_metadata, refs, old_text, new_text)
    database, audit = _cw_database(manifest, parent_cw, candidate_cw, old_text, new_text)
    resources = {}
    for name in sorted(refs):
        entry = refs[name]
        resources[name] = {'source': 'candidate' if name in changed else 'parent',
                           'parent_path': entry['path'], **(changed.get(name) or
                                                            {'sha256': entry['sha256'], 'bytes': entry['bytes']})}
    old_ref, new_ref = preflight._text(old), preflight._text(new)
    description = {'version': 1, 'kind': 'ccrd-cw0-local-four-source-candidate',
                   'parent_generation': manifest['generation'], 'parent_pointer_sha256': approved_pointer_sha256,
                   'old_text': old_ref, 'new_text': new_ref, 'source_observation': source_observation,
                   'cw_database': {'parent_path': manifest['databases']['CW']['path'], **database},
                   'preserved_databases': {name: manifest['databases'][name] for name in ('CCRD', 'RMRB', 'PLA')},
                   'metadata': resources, 'archives': manifest['archives'], 'metadata_target_files': inventory['target_files'],
                   'non_target_database_audit': audit, 'publication_ready': False, 'remote_writes': 0,
                   'remaining': ['verify source and parent against remote at publication time',
                                 'stage immutable copies of all 17 metadata and 52 archive members',
                                 'construct and independently validate an allowed four-source manifest',
                                 'establish writer isolation, Space activation, durable recovery and live acceptance']}
    identity = {**description, 'non_target_database_audit': {key: value for key, value in audit.items() if key != 'seconds'}}
    description['candidate_identity'] = hashlib.sha256(lifecycle.canonical(identity)).hexdigest()
    return description
