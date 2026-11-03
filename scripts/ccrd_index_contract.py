"""Fail closed when a legacy two-source writer would discard extra collections."""
from __future__ import annotations


LEGACY_SOURCES = frozenset({'CCRD', 'CW'})


def require_legacy_manifest(manifest: dict | None) -> None:
    if manifest is None:
        return
    databases = manifest.get('databases')
    if (manifest.get('format') == 2
            or isinstance(databases, dict) and set(databases) - LEGACY_SOURCES):
        raise ValueError('legacy CCRD publisher cannot replace a four-source/additive generation; use coordinated publication')


def require_legacy_metadata(payload: dict) -> None:
    record_sources = {record.get('source') for record in payload.get('records', [])}
    public_sources = {source.get('slug') for source in payload.get('sources', [])}
    if (record_sources | public_sources) - LEGACY_SOURCES:
        raise ValueError('legacy CW migration cannot rewrite additive newspaper metadata; use coordinated publication')
