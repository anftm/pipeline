"""Publish exact content-addressed producer-retirement audit evidence only.

No deletion, mutable pointer, producer-scope activation, or GC authorization is
implemented here. An ambiguous conditional-write response is reconciled by a
read, never blindly retried. The storage/lock context is the caller's duty.
"""
from __future__ import annotations

import hashlib
import re

try:
    from . import reader_producer_migration_intent as intent
except ImportError:
    import reader_producer_migration_intent as intent


PATTERN = re.compile(re.escape(intent.PREFIX) + r'(receipts|writer-inventory|candidates|intents)/([0-9a-f]{64})\.json')


class Conflict(RuntimeError):
    """The fixed content-addressed key contains different bytes."""


class Uncertain(RuntimeError):
    """An outcome could not be conclusively observed; do not retry a PUT."""


def validate_objects(objects):
    if not isinstance(objects, dict) or len(objects) != 12:
        raise ValueError('invalid audit object inventory')
    counts = {'receipts': 0, 'writer-inventory': 0, 'candidates': 0, 'intents': 0}
    for path, raw in objects.items():
        match = PATTERN.fullmatch(path) if isinstance(path, str) else None
        if (not match or not isinstance(raw, bytes) or not raw
                or hashlib.sha256(raw).hexdigest() != match.group(2)):
            raise ValueError('audit object path or digest differs')
        counts[match.group(1)] += 1
    if counts != {'receipts': 9, 'writer-inventory': 1, 'candidates': 1, 'intents': 1}:
        raise ValueError('audit object category count differs')


def publish(objects, *, read, create):
    """Create exactly absent immutable objects, verify exact bytes afterward.

    `read(path)` returns bytes or raises FileNotFoundError only when the bucket
    is independently known to exist. `create(path, raw)` must use an If-None-
    Match condition and never retry an ambiguous operation. Validate every
    pre-existing object before writing the first missing one.
    """
    validate_objects(objects)
    observed = {}
    for path, raw in sorted(objects.items()):
        try:
            current = read(path)
        except FileNotFoundError:
            current = None
        if current is not None and current != raw:
            raise Conflict('existing audit object differs: ' + path)
        observed[path] = current
    created, existing, reconciled = 0, 0, 0
    for path, raw in sorted(objects.items()):
        if observed[path] is not None:
            existing += 1
            continue
        try:
            create(path, raw)
        except Exception as error:
            try:
                actual = read(path)
            except FileNotFoundError as missing:
                raise Uncertain('audit write outcome absent; reconcile before retry: ' + path) from missing
            except Exception as unavailable:
                raise Uncertain('audit write outcome unreadable; reconcile before retry: ' + path) from unavailable
            if actual != raw:
                raise Conflict('audit write produced conflicting bytes: ' + path) from error
            reconciled += 1
            continue
        try:
            actual = read(path)
        except Exception as error:
            raise Uncertain('audit write readback failed: ' + path) from error
        if actual != raw:
            raise Conflict('audit write readback differs: ' + path)
        created += 1
    # Recheck the whole set, including prior existing objects, to catch drift.
    for path, raw in sorted(objects.items()):
        if read(path) != raw:
            raise Conflict('audit object changed during publication: ' + path)
    return {'kind': 'reader-producer-migration-audit-publication', 'created': created,
            'existing': existing, 'reconciled': reconciled, 'object_count': len(objects),
            'scope_activated': False, 'writer_isolation_verified': False, 'gc_authorized': False}


def publish_candidate(candidate, receipt_raws, writer_inventory_raw, *, read, create, now=None):
    """Rebuild and validate every semantic input before the first remote write."""
    planned = intent.plan(candidate, receipt_raws, writer_inventory_raw, now=now)
    result = publish(planned['objects'], read=read, create=create)
    return {**result, 'candidate_sha256': planned['candidate_sha256'],
            'intent_sha256': planned['intent_sha256']}
