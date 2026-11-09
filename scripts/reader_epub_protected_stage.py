"""Stage exact EPUB shadow objects only after durable protection, no promotion.

No CLI or workflow invokes this adapter yet. The caller must re-read the
pinned sources, both Reader consumers and every writer before each invocation;
``revalidate`` is an I/O boundary, not evidence supplied by a user. These
single-object checks do not isolate external writers or permit ABA assumptions.
This module never writes an index, sidecar, Reader pointer or deletion record.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

try:
    from . import reader_epub_immutable as immutable, reader_epub_protection as protection
    from . import reader_epub_shadow as shadow, search_snapshot
except ImportError:
    import reader_epub_immutable as immutable
    import reader_epub_protection as protection
    import reader_epub_shadow as shadow
    import search_snapshot


class UnknownOutcome(RuntimeError):
    """Readback could not prove the exact conditional mutation outcome."""


def _digest(value):
    return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


def _exact_or_create(inspect, create, expected, label):
    actual = inspect()
    if actual is None:
        try:
            result = create()
        except Exception as error:
            # Exactly one read, never repeat the conditional write. An absence
            # is still unknown: an earlier request could have been delayed.
            if inspect() == expected:
                return 'written-response-unknown'
            raise UnknownOutcome(label + ' outcome unknown; inspect before recovery') from error
        actual = inspect()
        if actual != expected:
            raise UnknownOutcome(label + ' readback differs')
        return result
    if actual != expected:
        raise ValueError('existing ' + label + ' conflicts with the exact plan')
    return 'already-present'


def _verify_stage_objects(preview, objects):
    for action in preview['actions']:
        for item in action['files']:
            if objects.inspect(action['root'], item['path']) != {'sha256': item['sha256'], 'bytes': item['bytes']}:
                raise ValueError('EPUB object changed before staging acceptance')


def stage(plan, report, directory: Path, *, created_at: str, protections, objects, revalidate):
    """Perform an explicit exact-plan stage, given independently wired adapters.

    The same operation/time/plan may be resumed after exact reconciliation.
    No automatic workflow is attached; this is not a cross-object transaction.
    """
    if not callable(revalidate):
        raise ValueError('fresh independent revalidation is required')
    preview = shadow.staging_plan(plan, report, directory)
    if _digest({k: v for k, v in preview.items() if k != 'staging_sha256'}) != preview['staging_sha256']:
        raise ValueError('EPUB staging preview identity differs')
    # The revalidator performs reads/raises on mismatch. Its return value is
    # deliberately ignored; a caller-supplied Boolean is not a receipt.
    revalidate(plan, preview)
    outcomes = []
    # Protect every bundle root before writing any one of its members.
    for action in preview['actions']:
        record = protection.make_record(plan['operation'], plan['plan_sha256'], action['root'], created_at)
        raw = search_snapshot.canonical(record)
        path = action['protection_path']
        protection.validate(path, raw)
        state = _exact_or_create(lambda: protections.inspect(path), lambda: protections.create_once(path, raw),
                                 raw, 'EPUB protection')
        outcomes.append({'kind': 'protection', 'path': path, 'status': state})
    # Before content transfer, revalidate roots and confirm every protection
    # remains the precise original record. No cleanup/release is performed.
    revalidate(plan, preview)
    for action in preview['actions']:
        raw = search_snapshot.canonical(protection.make_record(
            plan['operation'], plan['plan_sha256'], action['root'], created_at))
        if protections.inspect(action['protection_path']) != raw:
            raise ValueError('EPUB protection changed before object staging')
    for number, action in enumerate(preview['actions']):
        for item in action['files']:
            expected = {'sha256': item['sha256'], 'bytes': item['bytes']}
            immutable._expected(expected)
            if immutable._key(action['root'], item['path']) != item['remote']:
                raise ValueError('EPUB remote member path differs from staging preview')
            local = directory / str(number) / 'bundle' / item['path']
            if immutable.local_identity(local) != expected:
                raise ValueError('EPUB local object changed before staging')
            state = _exact_or_create(lambda: objects.inspect(action['root'], item['path']),
                                     lambda: objects.create_once(action['root'], item['path'], local, expected),
                                     expected, 'EPUB immutable object')
            outcomes.append({'kind': 'object', 'path': item['remote'], 'status': state})
    _verify_stage_objects(preview, objects)
    # Bracket the final source/consumer check with complete object and
    # protection reads. These are still observations, not a global fence.
    revalidate(plan, preview)
    for action in preview['actions']:
        expected = search_snapshot.canonical(protection.make_record(
            plan['operation'], plan['plan_sha256'], action['root'], created_at))
        if protections.inspect(action['protection_path']) != expected:
            raise ValueError('EPUB protection changed before staging acceptance')
    _verify_stage_objects(preview, objects)
    return {'version': 1, 'kind': 'reader-epub-protected-staging', 'operation': plan['operation'],
            'plan_sha256': plan['plan_sha256'], 'staging_sha256': preview['staging_sha256'],
            'protected_sources': len(preview['actions']),
            'verified_objects': sum(len(action['files']) for action in preview['actions']),
            'outcomes': outcomes, 'index_written': False, 'pointer_written': False,
            'publication_ready': False, 'external_writers_verified': False}
