"""Repeat real read-only source, two-site Reader and index checks before staging.

This adapter wraps the existing exact plan inspector. It never accepts caller
booleans as evidence, does not write, and cannot isolate external writers.
The protected staging coordinator has no production CLI/workflow wiring.
"""
from __future__ import annotations

import hashlib

try:
    from . import reader_epub_plan as planning, reader_epub_shadow as shadow, search_snapshot
except ImportError:
    import reader_epub_plan as planning
    import reader_epub_shadow as shadow
    import search_snapshot


class LivePlanRevalidator:
    def __init__(self, *, hub, api, fs, client):
        self.hub, self.api, self.fs, self.client = hub, api, fs, client

    def __call__(self, plan, preview):
        shadow._verify_plan(plan)
        if (not isinstance(preview, dict) or preview.get('kind') != 'reader-epub-immutable-staging-preview'
                or preview.get('operation') != plan['operation'] or preview.get('plan_sha256') != plan['plan_sha256']
                or preview.get('search_generation') != plan['search_generation']
                or preview.get('reader_generation') != plan['reader_generation']
                or preview.get('remote_writes') != 0 or preview.get('index_written') is not False
                or preview.get('publication_ready') is not False
                or preview.get('external_writers_verified') is not False):
            raise ValueError('EPUB preview is not the exact unpromoted plan')
        digest = hashlib.sha256(search_snapshot.canonical({k: v for k, v in preview.items() if k != 'staging_sha256'})).hexdigest()
        if preview.get('staging_sha256') != digest:
            raise ValueError('EPUB preview digest differs')
        current = planning.inspect(hub=self.hub, api=self.api, fs=self.fs, client=self.client,
                                   shard=plan['shard'], operation=plan['operation'])
        if current != plan:
            raise ValueError('EPUB source, consumers or checkpoint changed before staging')
        return None
