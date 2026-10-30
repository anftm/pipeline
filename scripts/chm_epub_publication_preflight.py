#!/usr/bin/env python3
"""Prepare, but never apply, a narrowly scoped historical CHM EPUB withdrawal."""
from __future__ import annotations

import gzip
import hashlib
import json
import copy
from pathlib import Path
import re

try:
    from . import chm_epub_withdrawal_preview as preview, publish_reader_v3 as publication
    from . import reader_gc_collect as collector
except ImportError:
    import chm_epub_withdrawal_preview as preview
    import publish_reader_v3 as publication
    import reader_gc_collect as collector


ASSETS = preview.ASSETS
INDEX = 'chapters/ebook/chm/index.json'
INDEX_PREFIX = 'chapters/ebook/chm/'
SIDECAR = 'reader-index/reader_assets.json.gz'
CATALOG = 'reader-index/reader_catalog.json'
INVENTORY = Path('/tmp/opencode/reader-jxl-gc-20261025/chm-history-inventory-20261030.json')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_index_roots(store):
    paths = {path for path in store.list_roots(ASSETS, {INDEX_PREFIX})
             if path.startswith(INDEX_PREFIX) and path.rsplit('/', 1)[-1].startswith('index')
             and path.endswith('.json')}
    expected = {INDEX, *(f'{INDEX_PREFIX}index-0{number}.json' for number in range(8))}
    if paths != expected:
        raise ValueError('CHM root index inventory differs from the reviewed snapshot')
    raw = {path: store.read_bytes(ASSETS, path) for path in paths}
    return {path: json.loads(data) for path, data in raw.items()}, {path: digest(data) for path, data in raw.items()}


def stale_shard_merge_changes(canonical, shards):
    """Report outdated chapter routes that a later legacy shard merge could replay."""
    try:
        from .reader_stream_index import merge_shards
    except ImportError:
        from reader_stream_index import merge_shards
    entries = [(int(path.rsplit('-', 1)[1].split('.')[0]), value)
               for path, value in sorted(shards.items())]
    merged = merge_shards(canonical, entries, 'ebook-chapter-stream-index', {'chm'})
    before = {entry['key']: entry for entry in canonical['files']}
    after = {entry['key']: entry for entry in merged['files']}
    if set(before) != set(after) or merged.get('failures') != canonical.get('failures'):
        raise ValueError('legacy shard merge changes the CHM source or failure set')
    changed = [key for key in before if before[key] != after[key]]
    routes = [key for key in changed if before[key].get('manifest') != after[key].get('manifest')]
    return {'changed_entries': len(changed), 'changed_chapter_routes': len(routes)}


def synchronize_stale_shards(canonical, shards):
    """Preview replacing old shard rows with their exact current canonical rows."""
    canonical_by_key = {entry['key']: entry for entry in canonical['files']}
    updates, replaced = {}, 0
    for path, payload in shards.items():
        value = copy.deepcopy(payload)
        for i, entry in enumerate(value['files']):
            selected = canonical_by_key.get(entry.get('key'))
            if selected is None:
                raise ValueError('old shard contains an unknown current CHM source')
            if selected != entry:
                value['files'][i] = copy.deepcopy(selected)
                replaced += 1
        updates[path] = value
    replay = stale_shard_merge_changes(canonical, updates)
    if replay['changed_entries']:
        raise ValueError('synchronized shard would still replay a different CHM entry')
    return {'indexes': updates, 'replaced_entries': replaced,
            'changed_chapter_bytes': 0, 'remote_mutations': 0}


def sanitize_historical_v3_base(sidecar, targets):
    """Remove only exact CHM fallback routes from an old immutable base copy."""
    if sidecar.get('v') != 1 or not isinstance(sidecar.get('f'), dict):
        raise ValueError('invalid historical Reader sidecar')
    clean = copy.deepcopy(sidecar)
    removed = set()
    for key, mapping in clean['f'].items():
        if not isinstance(mapping, dict):
            raise ValueError('invalid historical sidecar entry')
        path = mapping.get('f')
        if not isinstance(path, str) or not path.startswith(preview.PREFIX):
            continue
        if (not key.lower().endswith('.chm') or path not in targets or mapping.get('m') != 'e'
                or mapping.get('b') != ASSETS or mapping.get('cb') != ASSETS
                or mapping.get('p') != mapping.get('c')
                or not preview.MANIFEST.fullmatch(str(mapping.get('c', '')))):
            raise ValueError('historical CHM fallback has an unverified chapter route')
        mapping.pop('f')
        if mapping.get('fb') is not None:
            raise ValueError('historical CHM fallback has an unexpected bucket')
        removed.add(path)
    if any(isinstance(v, str) and v.startswith(preview.PREFIX)
           for mapping in clean['f'].values() for v in mapping.values()):
        raise ValueError('another historical mapping still uses a CHM EPUB')
    return clean, removed


def historical_v3_replacement(store, old_catalog, targets):
    """Construct a rollback generation with the old routes and no CHM EPUBs.

    The caller may publish the returned immutable objects, but this function is
    strictly read-only. It preserves the historical catalog's metadata rather
    than pretending that all formats were equal to the current release.
    """
    publication.validate_catalog(old_catalog)
    old_generation = old_catalog['generation']
    base = publication.pinned_base(store, old_catalog, None)
    sanitized, removed = sanitize_historical_v3_base(base, targets)
    if any(sanitized['f'][key] != value for key, value in base['f'].items()
           if not key.lower().endswith('.chm')):
        raise ValueError('historical non-CHM routing changed')
    packed = gzip.compress(publication.encode(sanitized), mtime=0)
    hashed = digest(packed)
    ref = publication.metadata(ASSETS, f'reader-index/snapshots/{hashed}/reader_assets.json.gz', packed)
    catalog = copy.deepcopy(old_catalog)
    catalog.pop('generation')
    catalog['base_sidecar'] = ref
    catalog['rollback_target'] = old_generation
    catalog['generation'] = publication.text.digest(catalog)
    publication.validate_catalog(catalog)
    if catalog['files'] != old_catalog['files']:
        raise ValueError('historical reading overrides changed')
    return {'old_generation': old_generation, 'generation': catalog['generation'], 'catalog': catalog,
            'snapshot': ref, 'snapshot_bytes': packed, 'removed_fallbacks': len(removed),
            'non_chm_routes': sum(not key.lower().endswith('.chm') for key in base['f']),
            'remote_mutations': 0}


def current_v3_replacement(store, old_catalog, sanitized_sidecar, targets, *, parent_generation,
                           document_reset=None):
    """Keep the exact current non-CHM projection while replacing its fallback base."""
    publication.validate_catalog(old_catalog)
    if old_catalog['generation'] != parent_generation:
        raise ValueError('current Reader generation changed')
    previous = publication.pinned_base(store, old_catalog, None)
    expected, _ = sanitize_historical_v3_base(previous, targets)
    if sanitized_sidecar != expected:
        raise ValueError('current sanitized base changes more than CHM fallback fields')
    packed = gzip.compress(publication.encode(sanitized_sidecar), mtime=0)
    hashed = digest(packed)
    ref = publication.metadata(ASSETS, f'reader-index/snapshots/{hashed}/reader_assets.json.gz', packed)
    result = {'version': 3, 'kind': 'reader-reading-catalog', 'files': copy.deepcopy(old_catalog['files']),
              'base_sidecar': ref, 'parent_generation': parent_generation}
    if document_reset:
        if not isinstance(document_reset, str) or not re.fullmatch(r'[a-z0-9-]{8,80}', document_reset):
            raise ValueError('invalid preserved document reset identity')
        result['document_reset'] = document_reset
    result['generation'] = publication.text.digest(result)
    publication.validate_catalog(result)
    return {'old_generation': parent_generation, 'generation': result['generation'], 'catalog': result,
            'snapshot': ref, 'snapshot_bytes': packed, 'remote_mutations': 0}


def rollback_alias_from_current(replacement):
    """Create a standalone immutable rollback catalog for the previous current base."""
    current = replacement['catalog']
    publication.validate_catalog(current)
    alias = {'version': 3, 'kind': 'reader-reading-catalog', 'files': copy.deepcopy(current['files']),
             'base_sidecar': copy.deepcopy(current['base_sidecar']),
             'parent_generation': current['parent_generation'],
             'rollback_target': replacement['old_generation']}
    if current.get('document_reset'):
        alias['document_reset'] = current['document_reset']
    alias['generation'] = publication.text.digest(alias)
    publication.validate_catalog(alias)
    return {'old_generation': replacement['old_generation'], 'generation': alias['generation'],
            'catalog': alias, 'snapshot': replacement['snapshot'], 'snapshot_bytes': replacement['snapshot_bytes'],
            'remote_mutations': 0}


def resolve_retired_rollback(store, target_ref):
    """Resolve a retired history reference to its verified sanitized successor."""
    publication.index_resource(target_ref)
    match = re.fullmatch(r'reader-index/v3/generations/([0-9a-f]{64})/catalog\.json', target_ref['path'])
    if not match:
        return target_ref
    try:
        raw = store.read_bytes(ASSETS, 'reader-index/v3/retirement.json')
    except FileNotFoundError:
        # If the dedicated retirement marker exists, disappearance of the
        # alias journal must fail closed instead of restoring stale EPUB pins.
        try:
            store.read_bytes(ASSETS, 'reader-index/chm-epub-withdrawal-retention.json')
        except FileNotFoundError:
            return target_ref
        raise ValueError('CHM withdrawal history journal is missing') from None
    state = json.loads(raw)
    if (state.get('version') != 1 or state.get('kind') != 'reader-v3-retirement'
            or not isinstance(state.get('retired_paths'), list)):
        raise ValueError('cannot verify retired Reader generations')
    generation = match[1]
    if target_ref['path'] not in state['retired_paths']:
        return target_ref
    aliases = state.get('rollback_replacements') or {}
    if generation not in aliases:
        # Other formats' ordinary expired histories retain their existing
        # rollback semantics; this guard only owns explicit CHM retirements.
        return target_ref
    successor = aliases[generation]
    if not isinstance(successor, dict):
        raise ValueError('rollback generation was retired; select its sanitized replacement')
    catalog = publication.validate_catalog(publication.read_index(store, successor))
    if catalog.get('rollback_target') != generation:
        raise ValueError('sanitized rollback replacement has the wrong original generation')
    base = publication.pinned_base(store, catalog, None)
    if any(isinstance(entry, dict) and any(isinstance(value, str) and value.startswith(preview.PREFIX)
            for value in entry.values()) for entry in base['f'].values()):
        raise ValueError('rollback replacement still references a CHM EPUB')
    return successor


def preserved_chapter_closure(sidecars):
    """List every exact CHM chapter manifest used by current or rollback bases."""
    manifests = set()
    for sidecar in sidecars:
        if sidecar.get('v') != 1 or not isinstance(sidecar.get('f'), dict):
            raise ValueError('invalid CHM Reader sidecar')
        for key, entry in sidecar['f'].items():
            if not isinstance(key, str) or not key.lower().endswith('.chm'):
                continue
            if (not isinstance(entry, dict) or entry.get('s') != 2 or entry.get('m') != 'e'
                    or entry.get('b') != ASSETS or entry.get('cb') != ASSETS
                    or entry.get('p') != entry.get('c')
                    or not preview.MANIFEST.fullmatch(str(entry.get('c', '')))):
                raise ValueError('CHM reading route is not a qualified chapter manifest')
            manifests.add(entry['c'])
    return manifests


def verify_preserved_chapter(store, path, *, hash_bodies=False):
    """Validate one manifest, complete bundle inventory, and optionally chapter bytes."""
    try:
        from . import reader_assets
    except ImportError:
        import reader_assets
    if not preview.MANIFEST.fullmatch(path):
        raise ValueError('chapter manifest outside CHM scope')
    raw = store.read_bytes(ASSETS, path)
    manifest = json.loads(raw)
    reader_assets.validate_chapter_manifest(manifest)
    if not isinstance(manifest.get('search_index'), dict):
        raise ValueError('preserved CHM search index is missing')
    for entry in [*manifest['chapters'], manifest['search_index']]:
        if type(entry.get('bytes')) is not int or entry['bytes'] < 1 or not re.fullmatch(
                r'[0-9a-f]{64}', str(entry.get('sha256', ''))):
            raise ValueError('invalid preserved CHM resource identity')
    root = path.rsplit('/', 1)[0]
    listed = store.list_files(ASSETS, (root + '/',))
    required = {path}
    for entry in [*manifest['chapters'], manifest['search_index']]:
        relative = entry.get('path')
        if (not isinstance(relative, str) or relative.startswith('/') or '\\' in relative
                or any(part in {'', '.', '..'} for part in relative.split('/'))):
            raise ValueError('invalid chapter resource path')
        full = root + '/' + relative
        required.add(full)
        if full not in listed:
            raise ValueError('preserved CHM chapter or search resource is missing')
        if hash_bodies:
            body = store.read_bytes(ASSETS, full)
            if len(body) != entry['bytes'] or digest(body) != entry['sha256']:
                raise ValueError('preserved CHM chapter or search checksum differs')
    if not required <= listed:
        raise ValueError('preserved CHM manifest is missing from bundle')
    return {'manifest': path, 'manifest_sha256': digest(raw), 'required': len(required),
            'bundle_objects': len(listed), 'other_resources': len(listed - required),
            'chapter_bodies_hashed': bool(hash_bodies), 'remote_mutations': 0}


def verify_chm_reading_closure(store, manifests, *, workers=4, hash_bodies=False, hash_bundle=False,
                               checkpoint=None):
    """Validate chapter identities and companion existence without modifying them."""
    from concurrent.futures import ThreadPoolExecutor
    if len(set(manifests)) != len(manifests) or not manifests:
        raise ValueError('invalid CHM reading-manifest inventory')
    checked = []
    def check(path):
        result = verify_preserved_chapter(store, path, hash_bodies=hash_bodies)
        if hash_bundle:
            result['immutable_bundle'] = verify_chm_immutable_bundle(store, path)
        return result
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        for result in pool.map(check, sorted(manifests)):
            checked.append(result)
            if checkpoint and len(checked) % 25 == 0:
                checkpoint(len(checked), len(manifests))
    return {'manifests': len(checked), 'required_objects': sum(row['required'] for row in checked),
            'companion_objects': sum(row['other_resources'] for row in checked),
            'chapter_bodies_hashed': bool(hash_bodies or hash_bundle),
            'all_bundle_resources_hashed': bool(hash_bundle), 'results': checked, 'remote_mutations': 0}


def verify_chm_immutable_bundle(store, manifest_path):
    """Recompute a complete chapter bundle's content-addressed 16-hex version."""
    if not preview.MANIFEST.fullmatch(manifest_path):
        raise ValueError('invalid CHM bundle identity')
    root = manifest_path.rsplit('/', 1)[0]
    paths = store.list_files(ASSETS, (root + '/',))
    if not paths or manifest_path not in paths:
        raise ValueError('CHM immutable bundle is missing')
    content = hashlib.sha256()
    total = 0
    for path in sorted(paths):
        relative = path[len(root) + 1:]
        if not relative or '\\' in relative or any(p in {'', '.', '..'} for p in relative.split('/')):
            raise ValueError('invalid CHM immutable bundle path')
        raw = store.read_bytes(ASSETS, path)
        total += len(raw)
        content.update(relative.encode('utf-8') + b'\0')
        content.update(hashlib.sha256(raw).digest())
    actual = content.hexdigest()[:16]
    if actual != root.rsplit('/', 1)[-1]:
        raise ValueError('CHM immutable chapter/resource bundle checksum differs')
    return {'manifest': manifest_path, 'bundle_version': actual, 'objects': len(paths),
            'bytes': total, 'remote_mutations': 0}


def retirement_journal_for_replaced_history(state, replacements, current_pointer, acks, *, aliases=None):
    """Plan only the two old v3 roots, protected by exact replacement catalogs.

    The generic collector may delete retired roots, so every non-CHM route they
    carried must first be represented by the supplied immutable replacements.
    Existing retirement state and any DjVu/OCR input records remain untouched.
    """
    if acks != {'hf': True, 'pages': True} or not current_pointer.get('catalog'):
        raise ValueError('both consumers must acknowledge the new current catalog')
    if state is None:
        state = {'version': 1, 'kind': 'reader-v3-retirement', 'retired_paths': [], 'resources': []}
    if (state.get('version') != 1 or state.get('kind') != 'reader-v3-retirement'
            or not isinstance(state.get('retired_paths'), list) or not isinstance(state.get('resources'), list)):
        raise ValueError('unexpected existing v3 retirement journal')
    for generation in replacements:
        if generation == current_pointer.get('generation'):
            raise ValueError('cannot retire the active Reader generation')
    updated = copy.deepcopy(state)
    retired = set(updated['retired_paths'])
    resources = copy.deepcopy(updated['resources'])
    original_resources = {(r.get('bucket'), r.get('path')) for r in resources}
    for generation, replacement in replacements.items():
        if (not re.fullmatch(r'[0-9a-f]{64}', generation)
                or replacement.get('old_generation') != generation
                or replacement.get('catalog', {}).get('rollback_target') != generation):
            raise ValueError('invalid replacement history identity')
        publication.validate_catalog(replacement['catalog'])
        raw = publication.encode(replacement['catalog'])
        ref = publication.metadata(ASSETS,
                f"reader-index/v3/generations/{replacement['generation']}/catalog.json", raw)
        if replacement['generation'] == generation:
            raise ValueError('historical replacement may not reuse immutable identity')
        if (ref['bucket'], ref['path']) not in original_resources:
            resources.append(ref)
            original_resources.add((ref['bucket'], ref['path']))
        retired.update((f'reader-index/v3/generations/{generation}/catalog.json',
                        f'reader-index/v3/promotions/{generation}.json'))
    current = current_pointer['catalog']
    if (current['bucket'], current['path']) not in original_resources:
        resources.append(copy.deepcopy(current))
    updated.update(retired_paths=sorted(retired), resources=resources,
                   replacement_generation=current_pointer['generation'], replacement_acks=acks)
    if aliases is not None:
        old_aliases = dict(updated.get('rollback_replacements') or {})
        if set(aliases) != set(replacements):
            raise ValueError('every retired history needs a rollback replacement alias')
        for generation, ref in aliases.items():
            expected_path = f"reader-index/v3/generations/{replacements[generation]['generation']}/catalog.json"
            if ref.get('bucket') != ASSETS or ref.get('path') != expected_path:
                raise ValueError('rollback alias does not point to its sanitized generation')
            if generation in old_aliases and old_aliases[generation] != ref:
                raise ValueError('existing rollback replacement alias conflicts')
            old_aliases[generation] = copy.deepcopy(ref)
        updated['rollback_replacements'] = old_aliases
    return updated


def retire_collected_chm_processing_refs(canonical, missing, current_sidecar, *, expected_generation,
                                         acknowledged_generations):
    """Preview removing missing CHM-only processing links after consumer handoff.

    A missing processing root is not automatically safe to drop: the exact
    canonical chapter route must already be served by both named consumers.
    The current sidecar and every chapter path remain unchanged.
    """
    if (not isinstance(expected_generation, str) or not expected_generation
            or acknowledged_generations != {'hf': expected_generation, 'pages': expected_generation}):
        raise ValueError('both Reader surfaces must acknowledge the current generation')
    value = copy.deepcopy(canonical)
    removed = set()
    for entry in value.get('files', []):
        key = entry.get('key')
        mapping = current_sidecar.get('f', {}).get(key)
        if (not isinstance(key, str) or not key.lower().endswith('.chm')
                or not isinstance(mapping, dict) or mapping.get('p') != entry.get('manifest')
                or mapping.get('c') != entry.get('manifest') or mapping.get('b') != ASSETS
                or mapping.get('cb') != ASSETS or mapping.get('m') != 'e'):
            raise ValueError('current CHM chapter route was not acknowledged')
        paths = entry.get('processing_roots', [])
        if not isinstance(paths, list):
            raise ValueError('invalid CHM processing roots')
        for path in paths:
            if not isinstance(path, str) or not path.startswith('reader-index/processing/'):
                raise ValueError('invalid CHM processing root')
        retained = [path for path in paths if path not in missing]
        removed.update(path for path in paths if path in missing)
        if retained:
            entry['processing_roots'] = retained
        else:
            entry.pop('processing_roots', None)
    if removed != set(missing):
        raise ValueError('missing processing record set changed')
    return {'index': value, 'removed_processing_refs': len(removed), 'remote_mutations': 0}


def synchronize_indexes(indexes, sidecar, catalog, targets, missing_processing, *, current_generation, receipts):
    """Plan all nine CHM indices without changing current or historical chapters."""
    if set(indexes) != {INDEX, *(f'{INDEX_PREFIX}index-0{i}.json' for i in range(8))}:
        raise ValueError('CHM index or shard is missing')
    updated = preview.preview(indexes[INDEX], sidecar, catalog, targets)
    shards = preview.preview_shards({p: v for p, v in indexes.items() if p != INDEX}, indexes[INDEX], targets)
    repaired = retire_collected_chm_processing_refs(updated['index'], missing_processing, updated['sidecar'],
                       expected_generation=current_generation, acknowledged_generations=receipts)
    synchronized = synchronize_stale_shards(repaired['index'], shards['indexes'])
    planned = {INDEX: repaired['index'], **synchronized['indexes']}
    old = {entry['key']: entry['manifest'] for entry in indexes[INDEX]['files']}
    new = {entry['key']: entry['manifest'] for entry in repaired['index']['files']}
    if old != new:
        raise ValueError('CHM index synchronization changed chapter routes')
    # The previous base snapshot can be independently pinned by a document
    # reset receipt, even after it ceases to be the current base generation.
    # Build an immutable equivalent that preserves every unrelated route.
    previous_id = updated['previous_generation']
    previous_entry = updated['catalog']['generations'][previous_id]
    previous_clean, _ = sanitize_historical_v3_base(sidecar, {row['path'] for row in targets})
    previous_raw = gzip.compress(publication.encode(previous_clean), mtime=0)
    previous_digest = digest(previous_raw)
    previous_snapshot = {'bucket': ASSETS,
        'path': f'reader-index/snapshots/{previous_digest}/reader_assets.json.gz',
        'sha256': previous_digest, 'bytes': len(previous_raw)}
    previous_entry['snapshot'] = previous_snapshot
    target_refs = {(ASSETS, row['path']) for row in targets}
    previous_entry['references'] = [ref for ref in previous_entry.get('references', [])
                                    if (ref.get('bucket'), ref.get('path')) not in target_refs]
    previous_entry['sidecar_sha256'] = hashlib.sha256(json.dumps(previous_clean, ensure_ascii=False,
                                 sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'indexes': planned, 'sidecar': updated['sidecar'], 'catalog': updated['catalog'],
            'generation': updated['generation'], 'retired_epub_fields': updated['entries_changed'],
            'previous_snapshot': previous_snapshot, 'previous_snapshot_bytes': previous_raw,
            'missing_processing_refs_removed': repaired['removed_processing_refs'],
            'shard_entries_synchronized': synchronized['replaced_entries'], 'remote_mutations': 0}


def retire_old_base_catalog_generation(catalog, previous, replacement, *, acknowledged):
    """Retire only the previous base generation after both surfaces confirm."""
    if not acknowledged or (catalog.get('current') or {}).get('generation') != replacement:
        raise ValueError('new base generation is not acknowledged')
    value = copy.deepcopy(catalog)
    entries = value.get('generations')
    if not isinstance(entries, dict) or previous == replacement or previous not in entries:
        raise ValueError('previous base generation identity changed')
    old = entries.pop(previous)
    if (not isinstance(old, dict) or old.get('generation') != previous
            or old.get('status') != 'superseded'):
        raise ValueError('previous base generation is not a superseded root')
    retired = value.setdefault('retired_generations', {})
    if previous in retired:
        raise ValueError('previous base generation already has a conflicting retirement')
    retired[previous] = {'replacement_generation': replacement, 'replacement_acks': {'hf': True, 'pages': True},
                         'sidecar_sha256': old.get('sidecar_sha256'), 'snapshot': old.get('snapshot'),
                         'retired_at': publication.reader_lifecycle.now_iso(),
                         'reason': 'explicit-chm-epub-withdrawal'}
    return value


def retained_reader_snapshot_refs(store, history_refs):
    """Build a GC root that continues to pin every retained CHM chapter bundle."""
    from concurrent.futures import ThreadPoolExecutor
    def read(path):
        if not preview.MANIFEST.fullmatch(path):
            raise ValueError('unexpected preserved chapter manifest path')
        raw = store.read_bytes(ASSETS, path)
        return publication.metadata(ASSETS, path, raw)
    with ThreadPoolExecutor(max_workers=6) as pool:
        refs = list(pool.map(read, sorted(set(history_refs))))
    if not refs:
        raise ValueError('no CHM chapters to retain')
    state = {'version': 1, 'kind': 'reader-chm-epub-withdrawal-retention', 'resources': refs,
             'inventory_sha256': preview.INVENTORY_SHA256}
    return state


def acknowledge_base_catalog(catalog, generation):
    """Mark only the accepted CHM replacement base and its predecessor."""
    value = copy.deepcopy(catalog)
    if (value.get('current') or {}).get('generation') != generation:
        raise ValueError('base catalog moved before consumer acknowledgment')
    entry = value.get('generations', {}).get(generation)
    if not isinstance(entry, dict) or entry.get('generation') != generation:
        raise ValueError('replacement base generation is missing')
    entry['acks'] = {'hf': True, 'pages': True}
    for previous in value['generations'].values():
        if previous.get('status') == 'superseded':
            previous['replacement_acks'] = {'hf': True, 'pages': True}
    return value


def preserved_v3_reading_refs(old_catalogs):
    """Retain v3 overrides but reject CHM overrides with hidden EPUB dependencies."""
    refs = {}
    for catalog in old_catalogs:
        publication.validate_catalog(catalog)
        for key, entry in catalog['files'].items():
            if isinstance(key, str) and key.lower().endswith('.chm'):
                raise ValueError('CHM v3 reading override needs separate dependency verification')
            ref = copy.deepcopy(entry['resource'])
            key = (ref['bucket'], ref['path'])
            if key in refs and refs[key] != ref:
                raise ValueError('historical Reader resource has conflicting identities')
            refs[key] = ref
    return [refs[key] for key in sorted(refs)]


def plan_chm_withdrawal(store, inventory_path, *, acknowledged_generations):
    """Construct immutable replacements and exact metadata updates, read-only."""
    inventory = preview.load_inventory(inventory_path)
    rows = inventory['targets']
    targets = {row['path'] for row in rows}
    if store.list_files(ASSETS, (preview.PREFIX,)) != targets:
        raise ValueError('current CHM EPUB inventory differs from the reviewed exact set')
    indexes, index_hashes = read_index_roots(store)
    side_raw = store.read_bytes(ASSETS, SIDECAR)
    catalog_raw = store.read_bytes(ASSETS, CATALOG)
    if digest(side_raw) != inventory['sidecar_sha256'] or index_hashes[INDEX] != inventory['index_sha256']:
        raise ValueError('canonical CHM index or sidecar changed since inventory review')
    sidecar, catalog = json.loads(gzip.decompress(side_raw)), json.loads(catalog_raw)
    pointer, current = publication.current(store)
    if not pointer or current.get('base_sidecar', {}).get('sha256') != digest(side_raw):
        raise ValueError('current v3 projection does not bind the canonical sidecar')
    if acknowledged_generations != {'hf': pointer['generation'], 'pages': pointer['generation']}:
        raise ValueError('both sites must serve the current Reader generation')
    old_generation = current.get('parent_generation')
    if not re.fullmatch(r'[0-9a-f]{64}', str(old_generation)):
        raise ValueError('current historical Reader generation is not identified')
    expected_v3_roots = {f'reader-index/v3/generations/{g}/catalog.json'
                         for g in (pointer['generation'], old_generation)}
    present_v3_roots = set(store.list_files(ASSETS, ('reader-index/v3/generations/',)))
    if present_v3_roots != expected_v3_roots:
        raise ValueError('historical Reader v3 root inventory differs from the reviewed two generations')
    expected_promotions = {f'reader-index/v3/promotions/{g}.json' for g in (pointer['generation'], old_generation)}
    if set(store.list_files(ASSETS, ('reader-index/v3/promotions/',))) != expected_promotions:
        raise ValueError('historical Reader promotion inventory differs from the reviewed two generations')
    for generation in (pointer['generation'], old_generation):
        promotion = json.loads(store.read_bytes(ASSETS, f'reader-index/v3/promotions/{generation}.json'))
        if (promotion.get('kind') != 'reader-reading-promotion' or promotion.get('generation') != generation
                or promotion.get('acks') != {'hf': True, 'pages': True}):
            raise ValueError('historical Reader promotion lacks both original acknowledgments')
    old_path = f'reader-index/v3/generations/{old_generation}/catalog.json'
    historical = json.loads(store.read_bytes(ASSETS, old_path))
    if historical.get('generation') != old_generation:
        raise ValueError('historical Reader catalog identity changed')
    history = historical_v3_replacement(store, historical, targets)
    receipt_path = 'reader-index/gc/document-reset/withdrawal.json'
    receipt = json.loads(store.read_bytes(ASSETS, receipt_path))
    if (receipt.get('kind') != 'reader-document-reset' or receipt.get('status') != 'withdrawn'
            or receipt.get('generation') != old_generation or current.get('files')
            or receipt.get('reset_id') != historical.get('document_reset')
            or receipt.get('base_snapshot', {}).get('sha256') != historical['base_sidecar']['sha256']):
        raise ValueError('document reset receipt no longer binds the reviewed historical generation')
    processing = {path for entry in indexes[INDEX]['files'] for path in entry.get('processing_roots', [])}
    missing = processing - store.existing_files(ASSETS, processing)
    synced = synchronize_indexes(indexes, sidecar, catalog, rows, missing,
                        current_generation=pointer['generation'], receipts=acknowledged_generations)
    current_new = current_v3_replacement(store, current, synced['sidecar'], targets,
                                         parent_generation=pointer['generation'],
                                         document_reset=receipt['reset_id'])
    current_alias = rollback_alias_from_current(current_new)
    replacements = {old_generation: history, pointer['generation']: current_alias}
    alias_refs = {}
    immutable = {}
    for generation, value in replacements.items():
        raw = publication.encode(value['catalog'])
        path = f"reader-index/v3/generations/{value['generation']}/catalog.json"
        alias_refs[generation] = publication.metadata(ASSETS, path, raw)
        immutable[path] = raw
        immutable[value['snapshot']['path']] = value['snapshot_bytes']
    new_raw = publication.encode(current_new['catalog'])
    new_path = f"reader-index/v3/generations/{current_new['generation']}/catalog.json"
    new_ref = publication.metadata(ASSETS, new_path, new_raw)
    immutable[new_path] = new_raw
    immutable[current_new['snapshot']['path']] = current_new['snapshot_bytes']
    immutable[synced['previous_snapshot']['path']] = synced['previous_snapshot_bytes']
    new_pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': current_new['generation'],
                   'catalog': new_ref, 'resources': [new_ref]}
    try:
        old_journal = json.loads(store.read_bytes(ASSETS, 'reader-index/v3/retirement.json'))
    except FileNotFoundError:
        old_journal = None
    journal = retirement_journal_for_replaced_history(old_journal, replacements, new_pointer,
                            {'hf': True, 'pages': True}, aliases=alias_refs)
    overrides = {p: publication.encode(value) for p, value in synced['indexes'].items()}
    overrides[SIDECAR] = current_new['snapshot_bytes']
    current_base_gen = synced['generation']
    new_base = synced['catalog']['generations'][current_base_gen]
    new_base_snapshot_path = f'reader-index/generations/{current_base_gen}/reader_assets.json.gz'
    immutable[new_base_snapshot_path] = current_new['snapshot_bytes']
    new_base['snapshot'] = {'bucket': ASSETS, 'path': new_base_snapshot_path,
                            'sha256': digest(current_new['snapshot_bytes']),
                            'bytes': len(current_new['snapshot_bytes'])}
    overrides[CATALOG] = publication.encode(acknowledge_base_catalog(synced['catalog'], current_base_gen))
    overrides['reader-index/v3/current.json'] = publication.encode(new_pointer)
    overrides['reader-index/v3/retirement.json'] = publication.encode(journal)
    old_base_gen = (catalog.get('current') or {}).get('generation')
    for retired in synced['catalog'].get('retired_generations', {}).values():
        if (isinstance(retired, dict) and retired.get('replacement_generation') == old_base_gen
                and retired.get('replacement_acks') != {'hf': True, 'pages': True}
                and retired.get('withdrawal_reset_id')):
            raise ValueError('an unacknowledged document-reset snapshot still pins a historical base')
    new_history = {'version': 1, 'kind': 'reader-reading-promotion', 'generation': new_pointer['generation'],
                   'parent_generation': pointer['generation'], 'resources': [new_ref],
                   'acks': {'hf': True, 'pages': True}, 'retention_days': 30,
                   'created_at': publication.reader_lifecycle.now_iso()}
    overrides[f"reader-index/v3/promotions/{new_pointer['generation']}.json"] = publication.encode(new_history)
    adapted_receipt = copy.deepcopy(receipt)
    adapted_receipt['generation'] = new_pointer['generation']
    adapted_receipt['base_generation'] = synced['generation']
    adapted_receipt['base_snapshot'] = current_new['snapshot']
    adapted_receipt['sidecar_sha256'] = digest(current_new['snapshot_bytes'])
    adapted_receipt['chm_epub_withdrawal'] = {'previous_generation': old_generation,
        'historical_replacement_generation': history['generation'], 'inventory_sha256': preview.INVENTORY_SHA256}
    overrides[receipt_path] = publication.encode(adapted_receipt)
    consumer_path = 'reader-index/reader_gc_consumers.json'
    consumers = json.loads(store.read_bytes(ASSETS, consumer_path))
    if consumers.get('kind') != 'reader-gc-consumers' or not isinstance(consumers.get('resources'), list):
        raise ValueError('current Reader consumer metadata is invalid')
    target_refs = {(ASSETS, p) for p in targets}
    consumers['resources'] = [r for r in consumers['resources'] if (r.get('bucket'), r.get('path'))
                              not in target_refs]
    overrides[consumer_path] = publication.encode(consumers)
    history_refs = preserved_chapter_closure([sidecar, publication.pinned_base(store, historical, None)])
    historical_reading_refs = preserved_v3_reading_refs([current, historical])
    # Replacement catalogs already protect these exact chapter manifests. A
    # second, independent current root ensures a later generic history pruning
    # cannot collect their bundles while the repaired rollback remains offered.
    retention = retained_reader_snapshot_refs(store, history_refs)
    retention['resources'].extend(historical_reading_refs)
    overrides['reader-index/chm-epub-withdrawal-retention.json'] = publication.encode(retention)
    return {'kind': 'chm-epub-withdrawal-plan', 'target_count': len(rows), 'target_bytes': inventory['logical_bytes'],
            'inventory_sha256': preview.INVENTORY_SHA256,
            'old_v3_generation': old_generation, 'current_v3_generation': pointer['generation'],
            'new_v3_generation': new_pointer['generation'], 'rollback_aliases': alias_refs,
            'base_generation': synced['generation'], 'index_hashes_before': index_hashes,
            'sidecar_sha256_before': digest(side_raw), 'catalog_sha256_before': digest(catalog_raw),
            'v3_pointer_before': pointer,
            'index_entries_retired': synced['retired_epub_fields'],
            'stale_shard_rows_aligned': synced['shard_entries_synchronized'],
            'missing_processing_refs_removed': synced['missing_processing_refs_removed'],
            'historical_v3_reading_override_count': len(historical_reading_refs),
            'chapter_manifests_to_preserve': sorted(history_refs), 'overrides': overrides,
            'immutable': immutable, 'remote_mutations': 0, 'deletion_authorized': False}


def simulate_complete_withdrawal(store, overrides, rows, fence, *, workers=8, deadline_minutes=300,
                                 immutable=None):
    """Read-only four-bucket check of an explicit CHM-only publication plan."""
    import time
    try:
        from . import reader_gc_graph as graph, reader_gc_stream as stream
    except ImportError:
        import reader_gc_graph as graph
        import reader_gc_stream as stream
    immutable = immutable or {}
    overlay = {(ASSETS, p): raw for p, raw in {**overrides, **immutable}.items()}
    before = {}
    for path in set(overrides) | set(immutable):
        try:
            before[(ASSETS, path)] = store.digest(ASSETS, path)
        except FileNotFoundError:
            before[(ASSETS, path)] = None
        if path in immutable and before[(ASSETS, path)] not in {None, digest(immutable[path])}:
            raise ValueError('planned immutable CHM replacement conflicts with existing bytes: ' + path)

    class View:
        def __init__(self):
            self.object_sizes = store.object_sizes
            self.read_etags = {}
            self.root_etags = {}
        def list_files(self, bucket, prefixes):
            files = set(store.list_files(bucket, prefixes))
            files.update(path for owner, path in overlay if owner == bucket and path.startswith(prefixes))
            return files
        def list_roots(self, bucket, parents):
            roots = {path: etag for path, etag in store.list_roots(bucket, parents).items()
                     if graph.is_root(path, bucket)}
            roots.update({path: None for owner, path in overlay if owner == bucket and graph.is_root(path, bucket)})
            return roots
        def read_bytes(self, bucket, path):
            return overlay.get((bucket, path)) if (bucket, path) in overlay else store.read_bytes(bucket, path)
        def read_graph_json(self, bucket, path, parser=None):
            if (bucket, path) in overlay:
                return (parser or stream.graph_json)([overlay[(bucket, path)]], path.endswith('.gz'))
            return store.read_graph_json(bucket, path, parser=parser)
        def digest(self, bucket, path):
            return digest(overlay[(bucket, path)]) if (bucket, path) in overlay else store.digest(bucket, path)

    fence.check()
    graph_view = graph.ReferenceGraph(View(), workers=workers, max_rounds=40,
                                      deadline=time.monotonic() + deadline_minutes * 60)
    report = graph_view.build()
    if not report['graph_complete'] or report['blockers'] or report.get('ambiguous_paths'):
        raise ValueError('CHM graph is incomplete or ambiguous: ' + repr(report['blockers'][:5]))
    targets = {(ASSETS, row['path']) for row in rows}
    retained = targets & graph_view.references
    origins = [{'bucket': b, 'path': p, 'targets': len(refs & targets)}
               for (b, p), refs in graph_view.direct.items() if refs & targets]
    fence.check()
    collector.verify_roots(graph_view.store, graph_view)
    for ref, expected in before.items():
        try:
            actual = store.digest(*ref)
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise ValueError('CHM controls changed during simulated graph traversal')
    return {'graph_complete': True, 'target_count': len(targets), 'retained_targets': len(retained),
            'origins': origins, 'roots': len(report['roots']), 'remote_mutations': 0,
            'deletion_authorized': False}


def processing_root_status(store, index):
    paths = {path for entry in index['files'] for path in entry.get('processing_roots', [])}
    if any(not isinstance(path, str) or not path.startswith('reader-index/processing/') for path in paths):
        raise ValueError('invalid CHM processing-root reference')
    present = store.existing_files(ASSETS, paths)
    return {'referenced': len(paths), 'present': len(present), 'missing': len(paths - present)}


def prepare(store, client, inventory_path=INVENTORY):
    """Use observed data only; preserve every existing rollback generation."""
    inventory = preview.load_inventory(inventory_path)
    targets = inventory['targets']
    keys = store.list_files(ASSETS, (preview.PREFIX,))
    if keys != {row['path'] for row in targets}:
        raise ValueError('CHM historical EPUB inventory changed')
    indexes, index_hashes = read_index_roots(store)
    sidecar_raw = store.read_bytes(ASSETS, SIDECAR)
    catalog_raw = store.read_bytes(ASSETS, CATALOG)
    if (index_hashes[INDEX] != inventory['index_sha256']
            or digest(sidecar_raw) != inventory['sidecar_sha256']):
        raise ValueError('CHM canonical index or sidecar changed')
    sidecar, catalog = json.loads(gzip.decompress(sidecar_raw)), json.loads(catalog_raw)
    pointer, reading_catalog = publication.current(store)
    if not pointer or reading_catalog.get('base_sidecar', {}).get('sha256') != digest(sidecar_raw):
        raise ValueError('current v3 Reader is not pinned to the canonical sidecar')
    identity, observations = collector.observe_projections(store, client)
    if identity.get('digest') != digest(catalog_raw):
        raise ValueError('base catalog changed during consumer verification')
    if not all(observations['receipts'].get(surface, {}).get('projection_verified')
               and observations['receipts'][surface].get('generation') == pointer['generation']
               for surface in ('hf', 'pages')):
        raise ValueError('both deployed Reader surfaces have not acknowledged the current v3 generation')
    result = preview.preview(indexes[INDEX], sidecar, catalog, targets)
    shards = preview.preview_shards({path: value for path, value in indexes.items() if path != INDEX},
                                   indexes[INDEX], targets)
    processing = processing_root_status(store, indexes[INDEX])
    if processing['missing']:
        raise ValueError(f"CHM index still names {processing['missing']} missing processing roots; "
                         'future sidecar handoff needs separately verified recovery')
    replay = stale_shard_merge_changes(result['index'], shards['indexes'])
    if replay['changed_entries']:
        raise ValueError('legacy CHM shard merge could replace current chapter routes; '
                         f"{replay['changed_entries']} entries and {replay['changed_chapter_routes']} routes; "
                         'publication must prevent stale shard replay')
    return {'kind': 'chm-historical-epub-publication-preflight', 'read_only': True,
            'target_count': len(targets), 'target_bytes': sum(row['bytes'] for row in targets),
            'index_sha256': index_hashes, 'sidecar_sha256': digest(sidecar_raw),
            'catalog_sha256': digest(catalog_raw), 'v3_pointer_before': pointer,
            'index_result': result, 'shard_result': shards,
            'consumer_receipts': observations['receipts'],
            'rollback_generations_preserved': True, 'remote_mutations': 0,
            'deletion_authorized': False}
