"""Exercise protected EPUB staging in memory; never access or mutate a bucket.

This offline experiment verifies exact source/bundle identities, creates
processing protections before content, handles interrupted conditional writes
by exact readback, and intentionally never writes a stream index or sidecar.
It is not an authorization to publish.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

try:
    from . import reader_epub_immutable as immutable, reader_epub_shadow as shadow, reader_epub_protection as protection, search_snapshot
except ImportError:
    import reader_epub_immutable as immutable
    import reader_epub_shadow as shadow
    import reader_epub_protection as protection
    import search_snapshot


class ResponseLost(TimeoutError):
    pass


class MemoryStore:
    """A deliberately isolated fake; no S3, Hub, filesystem mutation or delete."""
    def __init__(self, *, lose_once=frozenset(), fail_before=frozenset()):
        self.records = {}
        self.objects = {}
        self.loss = set(lose_once)
        self.fail_before = set(fail_before)
        self.events = []

    def _put(self, category, key, raw):
        identity = (category, key)
        target = self.records if category == 'protection' else self.objects
        self.events.append(('create', category, key))
        if identity in self.fail_before:
            self.fail_before.remove(identity)
            raise TimeoutError('simulated interruption before write')
        if key in target:
            raise ValueError('simulated conditional create conflict')
        target[key] = bytes(raw)
        if identity in self.loss:
            self.loss.remove(identity)
            raise ResponseLost('simulated response lost after write')

    def inspect_protection(self, key):
        self.events.append(('inspect', 'protection', key))
        return self.records.get(key)

    def create_protection(self, key, raw):
        self._put('protection', key, raw)

    def inspect_object(self, root, relative):
        key = immutable._key(root, relative)
        self.events.append(('inspect', 'object', key))
        raw = self.objects.get(key)
        return None if raw is None else {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}

    def create_object(self, root, relative, local, expected):
        key = immutable._key(root, relative)
        if immutable.local_identity(local) != expected:
            raise ValueError('local simulated object changed')
        self._put('object', key, local.read_bytes())


def _protect_record(plan, action, created_at):
    return protection.make_record(plan['operation'], plan['plan_sha256'], action['root'], created_at)


def _same_or_create(read, create, expected, *, label):
    actual = read()
    if actual is None:
        try:
            create()
        except Exception:
            # Only exact readback can resolve a lost response or create race.
            # If unchanged, missing or foreign, stop; no repeat in this call.
            if read() != expected:
                raise
            return 'written-response-unknown'
        actual = read()
    if actual != expected:
        raise ValueError('existing simulated ' + label + ' differs')
    return 'verified'


def simulate(plan, report, output: Path, store: MemoryStore, *, created_at):
    if type(store) is not MemoryStore:
        raise TypeError('EPUB staging simulation accepts only the isolated memory store')
    preview = shadow.staging_plan(plan, report, output)
    before = len(store.events)
    for action in preview['actions']:
        record = _protect_record(plan, action, created_at)
        raw = search_snapshot.canonical(record)
        _same_or_create(lambda: store.inspect_protection(action['protection_path']),
                        lambda: store.create_protection(action['protection_path'], raw), raw, label='protection')
    for number, action in enumerate(preview['actions']):
        for item in action['files']:
            expected = {'sha256': item['sha256'], 'bytes': item['bytes']}
            path = output / str(number) / 'bundle' / item['path']
            if immutable.local_identity(path) != expected:
                raise ValueError('local EPUB staged file changed')
            _same_or_create(lambda: store.inspect_object(action['root'], item['path']),
                            lambda: store.create_object(action['root'], item['path'], path, expected),
                            expected, label='object')
    return {'version': 1, 'kind': 'reader-epub-staging-simulation', 'operation': plan['operation'],
            'staging_sha256': preview['staging_sha256'], 'protected_sources': len(preview['actions']),
            'verified_objects': sum(len(action['files']) for action in preview['actions']),
            'simulated_new_creates': sum(event[0] == 'create' for event in store.events[before:]),
            'remote_writes': 0, 'index_written': False, 'publication_ready': False,
            'external_writers_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--created-at', required=True)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_bytes())
    report = json.loads(args.report.read_bytes())
    result = simulate(plan, report, args.output, MemoryStore(), created_at=args.created_at)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
