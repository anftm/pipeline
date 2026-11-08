"""Construct an inert, self-contained CCRD generation staging description.

This is a deterministic plan only. Nothing is uploaded, no pointer or Space is
changed, and copied archive identities still require byte verification.
"""
from __future__ import annotations

import hashlib

try:
    from . import ccrd_candidate_assembly as assembly
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_metadata_patch as patch
    from . import ccrd_source_override as source
except ImportError:
    import ccrd_candidate_assembly as assembly
    import ccrd_lifecycle as lifecycle
    import ccrd_metadata_patch as patch
    import ccrd_source_override as source


MAX_DESCRIPTOR = 2 * 1024 * 1024


def _validate_description(value: dict) -> None:
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('kind') != 'ccrd-cw0-local-four-source-candidate'
            or value.get('remote_writes') != 0 or value.get('publication_ready') is not False):
        raise ValueError('not an inert CCRD candidate description')
    expected = value.get('candidate_identity')
    lifecycle.sha(expected)
    audit = value.get('non_target_database_audit')
    if not isinstance(audit, dict):
        raise TypeError('CCRD candidate lacks complete non-target audit')
    normalized = {**value, 'non_target_database_audit': {k: v for k, v in audit.items() if k != 'seconds'}}
    normalized.pop('candidate_identity')
    if hashlib.sha256(lifecycle.canonical(normalized)).hexdigest() != expected:
        raise ValueError('CCRD candidate description identity differs')


def make(pointer: bytes, description: dict) -> dict:
    _validate_description(description)
    parent = assembly._parent(pointer, description.get('parent_pointer_sha256'))
    if description.get('parent_generation') != parent['generation']:
        raise ValueError('CCRD candidate parent generation differs')
    preserved = description.get('preserved_databases')
    if preserved != {name: parent['databases'][name] for name in ('CCRD', 'RMRB', 'PLA')}:
        raise ValueError('CCRD candidate changed unrelated source database identities')
    metadata = description.get('metadata')
    archives = description.get('archives')
    if not isinstance(metadata, dict) or set(metadata) != assembly.transport.METADATA_FILES:
        raise ValueError('CCRD candidate metadata set differs')
    if archives != parent['archives']:
        raise ValueError('CCRD candidate archive identities differ from parent')
    if description.get('metadata_target_files') != sorted(patch.PATHS):
        raise ValueError('CCRD candidate metadata target inventory differs')
    if (not isinstance(parent.get('source_overrides'), dict)
            or set(parent['source_overrides']) != {str(source.CORPUS_PATH)}):
        raise ValueError('CCRD parent has unsupported source overrides')
    parent_override = parent['source_overrides'][str(source.CORPUS_PATH)]
    old_text, new_text = description.get('old_text'), description.get('new_text')
    if (not isinstance(old_text, dict) or set(old_text) != {'bytes', 'sha256'}
            or old_text != {key: parent_override.get(key) for key in ('bytes', 'sha256')}
            or not isinstance(new_text, dict) or set(new_text) != {'bytes', 'sha256'}
            or type(new_text['bytes']) is not int or new_text['bytes'] < 1 or new_text == old_text):
        raise ValueError('CCRD text identity differs from parent or is unchanged')
    lifecycle.sha(new_text['sha256'])
    observed_source = description.get('source_observation')
    if (not isinstance(observed_source, dict) or observed_source.get('sha256') != new_text['sha256']
            or observed_source.get('bytes') != new_text['bytes'] or observed_source.get('source_stable') is not True):
        raise ValueError('CCRD candidate source observation differs')
    parent_refs = assembly._metadata_parent(parent)
    for name, item in metadata.items():
        old = parent_refs[name]
        if (not isinstance(item, dict) or set(item) != {'source', 'parent_path', 'sha256', 'bytes'}
                or item['source'] != ('candidate' if name in patch.PATHS else 'parent')
                or item['parent_path'] != old['path'] or type(item['bytes']) is not int or item['bytes'] < 1):
            raise ValueError('invalid CCRD candidate metadata member')
        lifecycle.sha(item['sha256'])
        if name not in patch.PATHS and (item['sha256'], item['bytes']) != (old['sha256'], old['bytes']):
            raise ValueError('unchanged CCRD metadata differs from parent')
    cw = description.get('cw_database')
    if (not isinstance(cw, dict) or set(cw) != {'parent_path', 'sha256', 'bytes'}
            or cw['parent_path'] != parent['databases']['CW']['path'] or type(cw['bytes']) is not int or cw['bytes'] < 1):
        raise ValueError('invalid CCRD CW candidate identity')
    lifecycle.sha(cw['sha256'])
    return _construct(parent, description)


def _construct(parent: dict, description: dict) -> dict:
    # Timing and local observation metadata are excluded so rebuilding the same
    # inputs yields the same generation. This does not assert provenance.
    identity = {'version': 1, 'kind': 'ccrd-cw0-four-source-stage-identity',
                'parent_generation': parent['generation'], 'parent_pointer_sha256': description['parent_pointer_sha256'],
                'source_revision': description['source_observation']['revision'],
                'source_url': description['source_observation']['url'], 'new_text': description['new_text'],
                'cw_database': description['cw_database'], 'metadata': description['metadata'],
                'archives': description['archives'], 'preserved_databases': description['preserved_databases']}
    generation = 'ccrd-four-' + hashlib.sha256(lifecycle.canonical(identity)).hexdigest()
    prefix = f'generations/{generation}/'
    new_metadata, copies = [], []
    for name, value in sorted(description['metadata'].items()):
        original = next(item for item in parent['metadata'] if item['path'] == value['parent_path'])
        ref = {**original, 'path': prefix + name, 'sha256': value['sha256'], 'bytes': value['bytes']}
        new_metadata.append(ref)
        copies.append({'kind': 'metadata', 'source': value['source'], 'source_path': value['parent_path'],
                       'target': ref['path'], 'sha256': ref['sha256'], 'bytes': ref['bytes']})
    new_archives = []
    old_prefix = f"generations/{parent['generation']}/"
    for old in parent['archives']:
        target = prefix + old['path'][len(old_prefix):]
        ref = {**old, 'path': target}
        new_archives.append(ref)
        copies.append({'kind': 'archive', 'source': 'parent', 'source_path': old['path'],
                       'target': target, 'sha256': old['sha256'], 'bytes': old['bytes']})
    cw = {**parent['databases']['CW'], 'path': prefix + 'CW.sqlite3',
          'sha256': description['cw_database']['sha256'], 'bytes': description['cw_database']['bytes']}
    databases = {**description['preserved_databases'], 'CW': cw}
    override = {**parent['source_overrides'][str(source.CORPUS_PATH)],
                **description['new_text'], 'url': description['source_observation']['url']}
    manifest = {**parent, 'generation': generation, 'previous_index_manifest_sha256': description['parent_pointer_sha256'],
                'previous_base_metadata_sha256': parent['base_metadata_sha256'],
                'base_metadata_sha256': description['metadata']['data/search_data.json.gz']['sha256'],
                'databases': databases, 'metadata': new_metadata, 'archives': new_archives,
                'source_overrides': {str(source.CORPUS_PATH): override}}
    lifecycle.references(manifest, 'current.json')
    copies.append({'kind': 'CW database', 'source': 'candidate', 'source_path': description['cw_database']['parent_path'],
                   'target': cw['path'], 'sha256': cw['sha256'], 'bytes': cw['bytes']})
    return {'version': 1, 'kind': 'ccrd-cw0-local-staging-plan', 'generation': generation,
            'identity': identity, 'manifest': manifest, 'objects': copies,
            'manifest_sha256': hashlib.sha256(lifecycle.canonical(manifest)).hexdigest(),
            'remote_writes': 0, 'publication_ready': False,
            'scope': 'local staging plan only; remote bytes, activation, writer isolation and recovery unverified'}
