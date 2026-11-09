"""Audit the search publication journal before/after daily staging.

The journal is read-only here: unfinished transitions, altered history or an
exact parent mismatch fail closed. The head is not a global lock and a stable
ETag cannot prove external writers are isolated or exclude an ABA change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from . import search_snapshot, search_transition as tx
except ImportError:
    import search_snapshot
    import search_transition as tx


MAX_RECEIPT = 16 * 1024


def inspect(store):
    before = tx.load(store)
    pointer_raw = store.read_pointer()
    pointer = tx.decode_pointer(pointer_raw)
    if before is None:
        raise ValueError('search publication journal is absent; inspect before daily staging')
    if before.record['state'] not in {'complete', 'rolled-back'} or before.record['pending'] is not None:
        raise ValueError('search publication journal has unfinished work; reconcile before daily staging')
    count = tx.audit_history(store, before)
    record = before.record
    if record['state'] == 'complete':
        intended = tx.make_pointer(record['candidate'], record['parent_pointer']['desired'], record['transition'])
    else:
        intended = search_snapshot.canonical(record['parent_pointer'])
    if pointer_raw != intended:
        raise ValueError('search terminal journal does not match the exact current pointer')
    after = tx.load(store)
    if (after is None or after.head_raw != before.head_raw or after.etag != before.etag
            or store.read_pointer() != pointer_raw):
        raise ValueError('search publication journal or pointer changed during inspection')
    return {'version': 1, 'kind': 'search-daily-journal-observation', 'state': record['state'],
            'sequence': record['sequence'], 'transition': record['transition'], 'records_audited': count,
            'head_sha256': hashlib.sha256(before.head_raw).hexdigest(),
            'head_etag_sha256': hashlib.sha256(before.etag.encode('utf8')).hexdigest(),
            'pointer_sha256': hashlib.sha256(pointer_raw).hexdigest(), 'generation': pointer['desired'],
            'pointer_transition': pointer['transition'], 'remote_writes': 0,
            'external_writers_verified': False}


def verify_unchanged(expected, actual):
    if not isinstance(expected, dict) or expected != actual:
        raise ValueError('search journal or exact pointer changed during the daily run')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--expected', type=Path)
    args = parser.parse_args(argv)
    if (args.output is None) == (args.expected is None):
        raise ValueError('provide exactly one output or expected receipt')
    if args.output and (args.output.exists() or args.output.is_symlink()):
        raise ValueError('search journal observation output must be new')
    expected = None
    if args.expected:
        if args.expected.is_symlink() or not args.expected.is_file() or args.expected.stat().st_size > MAX_RECEIPT:
            raise ValueError('expected search journal observation is missing or oversized')
        expected = json.loads(args.expected.read_bytes())
    result = inspect(tx.S3TransitionStore())
    if expected is not None:
        verify_unchanged(expected, result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf8') as output:
            json.dump(result, output, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
