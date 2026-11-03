"""Plan immutable transition records without touching remote storage.

The planned keys deliberately live under the existing GC audit namespace, not a
Reader live-root prefix. This model returns bytes only and supplies no storage
client, commit marker, authorization, token revocation or GC activation.
"""
from __future__ import annotations

import hashlib
import json
import re

try:
    from . import reader_producer_transition_state as state
    from . import reader_producer_writer_proofs as writers
except ImportError:
    import reader_producer_transition_state as state
    import reader_producer_writer_proofs as writers


PREFIX = 'reader-index/gc/producer-scope-transition/v2/'
HEX = re.compile(r'[0-9a-f]{64}')


def plan(candidate, receipt_raws, writer_inventory_raw, *, now=None):
    """Return content-addressed write intents, never publish them.

    `writer_inventory_raw` is retained solely as an opaque, nonsecret audit
    identity. Its existence does not mean any credential is revoked.
    """
    if not isinstance(writer_inventory_raw, bytes) or not writer_inventory_raw or len(writer_inventory_raw) > 128 * 1024:
        raise ValueError('invalid source-writer inventory bytes')
    try:
        inventory = json.loads(writer_inventory_raw, object_pairs_hook=state.scope.unique_object)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid source-writer inventory') from error
    if (not isinstance(inventory, dict) or inventory.get('kind') != 'retired-worker-source-writer-inventory'
            or inventory.get('version') != 1 or inventory.get('gc_authorized') is not False
            or inventory.get('external_writer_isolation_verified') is not False
            or set(inventory) != {'version', 'kind', 'repositories', 'external_writer_isolation_verified', 'gc_authorized'}
            or not isinstance(inventory.get('repositories'), list)):
        raise ValueError('source-writer inventory claims unsafe authorization')
    rows = {}
    for entry in inventory['repositories']:
        if (not isinstance(entry, dict) or set(entry) != {'repository', 'repository_id', 'secrets'}
                or not isinstance(entry.get('repository'), str) or entry['repository'] in rows
                or not isinstance(entry.get('secrets'), list)
                or any(not isinstance(item, dict) or set(item) != {'name', 'source_status'}
                       or item.get('source_status') != 'unknown' for item in entry['secrets'])):
            raise ValueError('source-writer inventory rows differ')
        rows[entry['repository']] = {'repository_id': entry['repository_id'],
                                     'secret_names': [item['name'] for item in entry['secrets']]}
    if writers.build_inventory(rows) != inventory:
        raise ValueError('source-writer inventory identity differs')
    check = state.verify_candidate_receipts(candidate, receipt_raws, now=now)
    candidate_raw = state.encode(candidate)
    candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
    writer_sha = hashlib.sha256(writer_inventory_raw).hexdigest()
    objects = {}
    for repo, raw in receipt_raws.items():
        sha = hashlib.sha256(raw).hexdigest()
        objects[PREFIX + 'receipts/' + sha + '.json'] = raw
    objects[PREFIX + 'writer-inventory/' + writer_sha + '.json'] = writer_inventory_raw
    objects[PREFIX + 'candidates/' + candidate_sha + '.json'] = candidate_raw
    intent = {'version': 1, 'kind': 'reader-producer-migration-audit-intent',
              'candidate_sha256': candidate_sha, 'writer_inventory_sha256': writer_sha,
              'receipt_sha256s': sorted(hashlib.sha256(raw).hexdigest() for raw in receipt_raws.values()),
              'phase': 'prepared', 'remote_mutations': 0, 'writer_isolation_verified': False,
              'gc_authorized': False}
    intent_raw = state.encode(intent)
    intent_sha = hashlib.sha256(intent_raw).hexdigest()
    objects[PREFIX + 'intents/' + intent_sha + '.json'] = intent_raw
    if len(objects) != 12 or any(not HEX.fullmatch(path.rsplit('/', 1)[-1].removesuffix('.json'))
                                 for path in objects):
        raise ValueError('content-addressed intent scope differs')
    return {'kind': 'reader-producer-migration-offline-plan', 'candidate_sha256': candidate_sha,
            'intent_sha256': intent_sha, 'object_count': len(objects), 'receipt_count': check['receipt_count'],
            'objects': objects, 'remote_mutations': 0, 'gc_authorized': False}
