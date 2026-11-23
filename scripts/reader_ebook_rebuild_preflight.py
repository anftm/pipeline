"""Calculate checked canonical, legacy and v3 EPUB successor identities; no PUTs."""
from __future__ import annotations

from dataclasses import asdict
import json
from tempfile import TemporaryFile

from scripts import reader_ebook_rebuild_review as review, reader_ebook_canonical_guard as canonical
from scripts import reader_exact_office_stage as legacy_preview
from scripts import reader_image_history_publish as legacy
from scripts import publish_reader_v3 as v3


def preflight(store, plan, bundles, *, operation, timestamp, open_catalog, capture=None):
    """Compute a streamed proposal from current inputs; not a publication permit.

    open_catalog(stream) obtains the latest Reader catalog through a bounded
    exact streaming reader and returns its SHA-256 and byte length.
    """
    parents = {}
    proposal = review.preview(store, plan, bundles, capture_parents=parents)
    prior = {entry['key']: entry for entry in json.loads(parents['index'])['files']}
    after = parents['canonical_updates']
    specs = []
    for row in proposal['changes']:
        key = row['key']
        original, successor = prior[key], after[key]
        manifest_sha = next(member['sha256'] for member in row['members'] if member['path'] == canonical.MANIFEST)
        specs.append(canonical.Replacement(key=key, source_sha256=row['source_sha256'],
            old_root=original['root'], old_manifest=row['prior_manifest'],
            old_search_index=original.get('search_index'), old_manifest_sha256=row['prior_manifest_sha256'],
            new_root=successor['root'], new_manifest=successor['manifest'],
            new_search_index=successor['search_index'], new_manifest_sha256=manifest_sha,
            chapter_count=successor['chapter_count']))
    target = canonical.build_successor(parents['index'], reviewed=specs, operation_id=operation)
    old = {spec.key: prior[spec.key] for spec in specs}
    with TemporaryFile() as source, TemporaryFile() as destination:
        live_sha, size = open_catalog(source)
        source.seek(0)
        legacy_plan = legacy_preview.plan_legacy(source, destination, before=parents['sidecar'],
            after=parents['target_sidecar'], old=old, new=after, now=timestamp, parent_sha=live_sha)
    review.require(legacy_plan['parent_bytes'] == size, 'legacy catalog parent length changed')
    review.require(store.digest(v3.ASSETS, legacy.CATALOG) == live_sha
                   and store.read_bytes(v3.ASSETS, legacy.SIDECAR) == parents['sidecar']
                   and store.read_bytes(v3.ASSETS, canonical.INDEX) == parents['index']
                   and v3.current(store)[0] == parents['pointer'], 'release parents moved during preflight')
    if capture is not None:
        capture.update(parents)
    return {**proposal, 'operation': operation, 'timestamp': timestamp,
            'target_index_sha256': review.sha(target), 'legacy': legacy_plan,
            'canonical_replacements': [asdict(item) for item in specs],
            'remote_writes': 0, 'publication_ready': False}
