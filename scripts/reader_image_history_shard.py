"""Pure physical-shard splice and observation proof. No bucket I/O or writer.

A profiled canonical AVIF can win both ordinary reconciliation paths while an
old physical shard row independently keeps WebP reachable in Reader GC. Review
and seal that shard separately; never treat a logical merge as a physical edit.
The producer checkout below is code to deploy, not a deployment receipt.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import reader_image_history as history
from .reader_stream_checkpoint_cas import Snapshot, strong_etag
from .reader_stream_index import shard_for

PRODUCER = '8bb4d158abf737bcfa0990e563c2e5da9dc41795'
SHARDS = 8


@dataclass(frozen=True)
class ShardPin:
    path: str
    sha256: str
    bytes: int
    etag: str


@dataclass(frozen=True)
class Splice:
    key: str
    parent: ShardPin
    canonical_sha256: str
    target: bytes


def _snapshot(snapshot):
    strong_etag(snapshot.etag)
    index = history._parent_index(snapshot.raw)
    if snapshot.raw_sha256 != history._sha(snapshot.raw) or history._encode(snapshot.value) != history._encode(index):
        raise ValueError('snapshot does not bind the exact image index bytes')
    return index


def _owned(index, number, key):
    _, row = history._selected(index, key)
    if (any(shard_for(item['key'], SHARDS) != number
            for field in ('files', 'failures') for item in index.get(field, []))
            or any(item['key'] == key for item in index.get('failures', []))):
        raise ValueError('owning image shard has a foreign key or selected failure')
    return row


def splice(*, key: str, old_row_sha256: str, canonical_target: bytes, canonical_sha256: str,
           shard: Snapshot, pin: ShardPin) -> Splice:
    """Build a one-row splice against a separately reviewed physical parent.

    The canonical target may be the reviewed preview or a later exact sealed
    checkpoint with protection metadata. The caller supplies its trusted digest.
    """
    history._hex(old_row_sha256)
    history._hex(canonical_sha256)
    history._hex(pin.sha256)
    if not isinstance(key, str) or not key:
        raise ValueError('expected one exact image source key')
    number = shard_for(key, SHARDS)
    if (pin.path != f'pages/image/index-v2-{number:02d}.json'
            or not isinstance(shard.raw, bytes) or type(pin.bytes) is not int
            or (len(shard.raw), history._sha(shard.raw), shard.etag) != (pin.bytes, pin.sha256, pin.etag)):
        raise ValueError('exact owning image shard differs from its reviewed pin')
    parent = _snapshot(shard)
    target = history._parent_index(canonical_target)
    if history._sha(canonical_target) != canonical_sha256:
        raise ValueError('canonical image target differs from its reviewed digest')
    old, new = _owned(parent, number, key), history._selected(target, key)[1]
    if (new.get('profile') != history.AVIF_PROFILE or 'profile' in old
            or any(row['key'] == key for row in target.get('failures', []))):
        raise ValueError('splice requires a legacy WebP and a profiled static AVIF without a selected failure')
    # Reuse the core exact static identity, byte equality and one-row delta
    # validation. The splice changes only the selected row and operation marker.
    def source(row):
        return history.Source(row['repo'], row['source_path'], row['source_revision'],
                              row['source_sha256'], row['source_bytes'])

    def asset(row):
        path = row.get('path')
        return history.Asset(path.rpartition('/')[0] if isinstance(path, str) else '', path,
                             row.get('sha256'), row.get('bytes'))

    try:
        raw = history._replacement(parent, key, old_row_sha256, new, source=source(old), current=source(new),
                                   webp=asset(old), avif=asset(new), operation=target.get('checkpoint_operation'))
    except KeyError as error:
        raise ValueError('image shard or canonical target lacks exact source metadata') from error
    history._index(raw)
    return Splice(key, pin, canonical_sha256, raw)


# Exercise the actual reviewed worker baseline and finalizer, not copies of
# their timestamp/profile rules. The child has no credentials or bucket input.
_RECONCILE = '''
import json, sys
sys.path[:0] = sys.argv[1:]
from scripts import finalize_image_index, pilot_image_page_stream, reader_image_avif
key, number, count, canonical, shard, old = json.load(sys.stdin)
def row(index):
    return next(e for e in index['files'] if e['key'] == key)
target = row(canonical)
reader_image_avif.avif_route(target, reader_image_avif.BUCKET)
if old and (row(shard)['source_sha256'] != target['source_sha256']
            or not reader_image_avif.keeps_existing_profile(target, row(shard))):
    raise ValueError('the old shard is not covered by same-byte AVIF preservation')
worker = pilot_image_page_stream.shard_baseline(shard, canonical, number, count)
merged = finalize_image_index.merge(canonical, [(number, shard)], shard_count=count)
for label, result in [('ordinary worker baseline', worker), ('image finalizer merge', merged)]:
    if json.dumps(row(result), sort_keys=True) != json.dumps(target, sort_keys=True):
        raise ValueError(label + ' did not preserve the exact AVIF row')
'''


def prove_observed(plan: Splice, *, canonical: Snapshot, shard: Snapshot, producer_checkout: Path) -> dict:
    """Verify a readback with either the sealed old shard or the exact splice.

    Success proves the selected row survives the two real merges at this
    producer revision. Only 'spliced' proves the old row left the shard.
    Neither outcome authorizes publish, GC, or an automatic retry.
    """
    canonical_index, shard_index = _snapshot(canonical), _snapshot(shard)
    number = shard_for(plan.key, SHARDS)
    physical = _owned(shard_index, number, plan.key)
    target = history._selected(canonical_index, plan.key)[1]
    if (canonical.raw_sha256 != plan.canonical_sha256
            or plan.parent.path != f'pages/image/index-v2-{number:02d}.json'):
        raise ValueError('observed image roots do not match the reviewed plan')
    unchanged = (shard.raw_sha256, len(shard.raw), shard.etag) == (plan.parent.sha256, plan.parent.bytes, plan.parent.etag)
    if unchanged:
        reviewed = splice(key=plan.key, old_row_sha256=history.row_digest(physical), canonical_target=canonical.raw,
                          canonical_sha256=plan.canonical_sha256, shard=shard, pin=plan.parent)
        if reviewed != plan:
            raise ValueError('sealed shard splice differs from exact parent and target')
    elif (shard.raw != plan.target or shard.etag == plan.parent.etag
          or history._encode(physical) != history._encode(target)):
        raise ValueError('observed image shard is neither the exact parent nor the exact splice')

    checkout = producer_checkout.resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()

    if (git('rev-parse', '--show-toplevel') != str(checkout) or git('rev-parse', 'HEAD') != PRODUCER
            or git('status', '--porcelain', '--untracked-files=normal')):
        raise ValueError('merge proof needs the clean exact reviewed AVIF producer checkout')
    # Do not allow a fallback to a different scripts package if code is missing.
    files = ('scripts/finalize_image_index.py', 'scripts/pilot_image_page_stream.py', 'scripts/reader_image_avif.py')
    for file in files:
        if git('hash-object', '--', file) != git('rev-parse', f'{PRODUCER}:{file}'):
            raise ValueError('actual producer helper bytes differ from the reviewed revision')
    paths = [str(checkout), *(str(Path(p).resolve()) for p in sys.path if p and Path(p).resolve() != Path.cwd())]
    run = subprocess.run([sys.executable, '-IB', '-c', _RECONCILE, *paths], cwd=checkout,
                         env={k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL'}},
                         input=history._encode([plan.key, number, SHARDS, canonical_index, shard_index, unchanged]),
                         capture_output=True, timeout=30)
    if run.returncode:
        raise ValueError('reviewed image reconciliation proof failed: ' + run.stderr.decode(errors='replace').strip())
    return {'producer_revision': PRODUCER, 'shard_state': 'parent' if unchanged else 'spliced',
            'shard_path': plan.parent.path, 'shard_sha256': shard.raw_sha256, 'shard_etag': shard.etag,
            'canonical_sha256': canonical.raw_sha256, 'canonical_etag': canonical.etag,
            'selected_row_sha256': history.row_digest(target), 'deletion_authorized': False}
