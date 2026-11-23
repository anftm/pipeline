"""Review same-source EPUB replacements without mutating canonical or Reader state."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from scripts import publish_reader_v3 as v3, reader_route_control as control, reader_route_plan as routes
from scripts import reader_epub_shadow as shadow, reader_kindle_native as native
from scripts.reader_chapter_profiles import EPUB_PROFILE

INDEX = 'chapters/ebook/epub/index.json'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_bundle(path: Path):
    manifest_file = path/'chapter-manifest.json'
    require(not path.is_symlink() and manifest_file.is_file() and not manifest_file.is_symlink(),
            'unexpected local chapter bundle path')
    with manifest_file.open('rb') as stream:
        raw = stream.read(8 * 1024**2 + 1)
    require(len(raw) <= 8 * 1024**2, 'oversized chapter manifest')
    manifest = json.loads(raw)
    require(manifest.get('conversion_profile') == EPUB_PROFILE, 'expected exact EPUB successor profile')
    members, version = shadow._bundle_index(path, manifest)
    chapters = manifest['chapters']
    require(len(chapters) <= 10000 and sum(ch['bytes'] for ch in chapters) <= 512 * 1024**2
            and max(ch['bytes'] for ch in chapters) <= 8 * 1024**2, 'candidate exceeds Reader chapter limits')
    toc = manifest.get('toc')
    require(isinstance(toc, list) and bool(toc), 'cannot replace with an empty table of contents')
    anchors = {}
    for row in toc:
        fragment = row['fragment']
        if not fragment:
            continue
        number = row['chapter']
        if number not in anchors:
            doc = path/chapters[number-1]['path']
            anchors[number] = native._document_anchors(doc.read_text(encoding='utf-8'))
        require(anchors[number][fragment] == 1, 'candidate TOC anchor missing or ambiguous')
    return manifest, members, version


def preview(store, plan, bundles, *, capture_parents=None):
    """Preview a tiny first cohort, binding all changes to old routes and exact bytes.

    Optionally collect exact parent bytes and projected bytes for an independent
    guarded legacy preview; no bucket mutation is available here.
    """
    require(1 <= len(bundles) <= 3, 'preview requires one to three exact EPUB candidates')
    require(plan.get('kind') == 'reader-ebook-full-rebuild-plan' and plan.get('remote_writes') == 0
            and not plan.get('source_heads_changed'), 'expected an exact read-only rebuild plan')
    pointer, catalog = v3.current(store)
    require(pointer and pointer['generation'] == plan['reader_generation'] and catalog.get('base_sidecar')
            and not catalog.get('derived_pdf_redirects'), 'Reader generation differs from plan')
    selected = {r['key']: r for r in plan['rows'] if r['key'] in bundles and r['extension'] == 'epub'}
    require(set(selected) == set(bundles), 'candidate outside the pinned EPUB rebuild source keys')
    prior_raw = store.read_bytes(v3.ASSETS, INDEX)
    prior = json.loads(prior_raw)
    require(len(prior_raw) <= 16 * 1024**2 and prior.get('kind') == 'ebook-chapter-stream-index'
            and prior.get('version') == 1, 'invalid canonical EPUB index')
    indexed = {r['key']: r for r in prior['files']}
    failed = {r['key'] for r in prior.get('failures', [])}
    require(len(indexed) == len(prior['files']) and not set(selected) & failed,
            'candidate has conflicting canonical rows or failure')
    sidecar_path = 'reader-index/reader_assets.json.gz'
    raw_sidecar = store.read_bytes(v3.ASSETS, sidecar_path)
    base = routes.decode_sidecar(raw_sidecar)
    pinned = v3.project_sidecar(v3.pinned_base(store, catalog, {}), catalog)
    require(not set(selected) & (set(catalog.get('files', {})) | set(catalog.get('suppressed_sources', []))),
            'EPUB routes are shadowed')
    changes, updates, route_updates = [], {}, {}
    for key, spec in sorted(selected.items()):
        old = indexed.get(key, {})
        prior_path = spec['old_manifest']
        require(old.get('repo') == spec['repo'] and old.get('path') == spec['path']
                and (old.get('source_extension') or old.get('extension')) == 'epub'
                and old.get('source_sha256') == spec['source_sha256']
                and old.get('manifest') == prior_path and old.get('bucket') == v3.ASSETS,
                'canonical source identity differs from rebuild plan')
        route = base['f'].get(key)
        require(route == pinned['f'].get(key) and route == {
            's': 2, 'm': 'e', 'p': prior_path, 'b': v3.ASSETS, 'c': prior_path, 'cb': v3.ASSETS},
                'prior chapter route drift or unsupported additional reference')
        manifest, members, version = read_bundle(bundles[key])
        old_raw = store.read_bytes(v3.ASSETS, prior_path)
        old_manifest = json.loads(old_raw)
        require([(c['source_path'], c['index']) for c in manifest['chapters']] ==
                [(c['source_path'], c['index']) for c in old_manifest['chapters']], 'candidate changed EPUB spine identity')
        root = f"chapters/ebook/epub/{spec['source_sha256']}/{version}"
        updated_path = root+'/chapter-manifest.json'
        require(updated_path != prior_path, 'candidate must have a new immutable route')
        updates[key] = {**old, 'root': root, 'manifest': updated_path,
                        'search_index': root+'/'+manifest['search_index']['path'], 'profile': EPUB_PROFILE,
                        'chapter_count': len(manifest['chapters'])}
        route_updates[key] = {**route, 'p': updated_path, 'c': updated_path}
        changes.append({'key': key, 'source_revision': spec['revision'], 'source_sha256': spec['source_sha256'],
                        'prior_manifest': prior_path, 'prior_manifest_sha256': sha(old_raw),
                        'new_manifest': updated_path, 'members': members,
                        'old_toc': len(old_manifest.get('toc', [])), 'new_toc': len(manifest['toc']),
                        'chapter_bytes_unchanged': all(a['sha256'] == b['sha256'] for a,b in zip(
                            old_manifest['chapters'], manifest['chapters'], strict=True))})
    next_index = {**prior, 'files': [updates.get(row['key'], row) for row in prior['files']]}
    next_base = {**base, 'f': {**base['f'], **route_updates}}
    compressed = gzip.compress(v3.encode(next_base), compresslevel=9, mtime=0)
    packed_target, _ = control.prepare_base(catalog, compressed)
    projected = v3.project_sidecar(routes.decode_sidecar(packed_target), catalog)
    require(projected == {**pinned, 'f': {**pinned['f'], **route_updates}}, 'unexpected changes outside reviewed routes')
    class Target:
        def read_bytes(self,bucket,path):
            return compressed if (bucket,path) == (v3.ASSETS,sidecar_path) else store.read_bytes(bucket,path)
    successor = v3.unify(Target(), expected_canonical=sha(compressed), expected_pointer=pointer, apply=False)
    require(v3.current(store)[0] == pointer and store.read_bytes(v3.ASSETS, INDEX) == prior_raw
            and store.read_bytes(v3.ASSETS, sidecar_path) == raw_sidecar, 'preview parent changed during readback')
    if capture_parents is not None:
        capture_parents.update(index=prior_raw, sidecar=raw_sidecar, target_sidecar=compressed,
                               pointer=pointer, canonical_updates=updates)
    return {'version': 1, 'kind': 'ebook-rebuild-review', 'search_generation': plan['search_generation'],
            'reader_generation': plan['reader_generation'], 'changes': changes,
            'canonical_parent_sha256': sha(prior_raw), 'target_index_sha256': sha(v3.encode(next_index)),
            'sidecar_parent_sha256': sha(raw_sidecar), 'target_sidecar_sha256': sha(compressed),
            'target_pointer': successor['pointer'], 'remote_writes': 0, 'publication_ready': False}
