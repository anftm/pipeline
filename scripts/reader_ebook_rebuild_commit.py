"""Ordered EPUB release coordinator; adapters own exact transport and readback.

The coordinator never retries a write. Recovery is deliberately a separate
operator path and must use the spent permits of the underlying stages.
"""
from __future__ import annotations

from scripts import publish_reader_v3 as v3
from scripts import reader_format_daily as daily


def commit(*, review, guard, stage, canonical, legacy, promote, publish_pages, verify):
    """Commit a separately sealed review with injected, guarded write adapters.

    `guard(phase)` must independently revalidate the exact authority and writer
    fence immediately before each phase. Adapters must repeat their respective
    parent, retention, and permit checks at the actual write boundary.
    """
    target = review.get('target_pointer', {})
    generation = target.get('generation')
    changes = review.get('changes', [])
    if (review.get('kind') != 'ebook-rebuild-review' or review.get('remote_writes') != 0
            or not isinstance(generation, str) or not daily.GENERATION.fullmatch(generation)
            or generation == review.get('reader_generation') or not 1 <= len(changes) <= 3
            or len({row['key'] for row in changes}) != len(changes)):
        raise ValueError('expected a bounded exact EPUB review')

    def invoke(phase, fn, *args):
        if guard(phase) is not True:
            raise RuntimeError(f'ebook release guard refused {phase}')
        return fn(*args)

    staged = invoke('stage', stage)
    if staged.get('members_verified') is not True or staged.get('both_roots_protected') is not True:
        raise RuntimeError('exact EPUB successors or prior roots not protected')
    index = invoke('canonical', canonical, staged)
    if index.get('sha256') != review['target_index_sha256']:
        raise RuntimeError('exact canonical EPUB successor not observed')
    sealed_legacy = invoke('legacy', legacy, staged, index)
    if (sealed_legacy.get('sha256') != review['target_sidecar_sha256']
            or sealed_legacy.get('catalog_sha256') != review['legacy']['target_sha256']):
        raise RuntimeError('exact legacy EPUB successor not observed')
    v3_result = invoke('v3', promote, staged, sealed_legacy)
    if v3_result.get('confirmed') is not True or v3.encode(v3_result.get('pointer')) != v3.encode(target):
        raise RuntimeError('exact EPUB Reader pointer not observed')
    pages = invoke('pages', publish_pages, target)
    receipt = invoke('verify', verify, generation, pages)
    daily.validate_verification(receipt, generation)
    return {'generation': generation, 'changed_rows': len(changes), 'verification': receipt}
