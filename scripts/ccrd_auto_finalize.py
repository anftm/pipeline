"""Resume one CCRD activation by an explicit, journal-identified action.

Each invocation performs at most one durable action. Unknown writes are read
and reconciled; they are not replayed. This does not provide external fencing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from . import ccrd_auto_activate as candidate
    from . import ccrd_auto_stage as stage
    from . import ccrd_pointer_transition as pointer
    from . import ccrd_publication_coordinator as coordinator
    from . import ccrd_rollback as rollback
    from . import ccrd_stage_objects as objects
    from . import ccrd_transaction as tx
    from . import ccrd_transaction_s3 as journal_s3
except ImportError:
    import ccrd_auto_activate as candidate
    import ccrd_auto_stage as stage
    import ccrd_pointer_transition as pointer
    import ccrd_publication_coordinator as coordinator
    import ccrd_rollback as rollback
    import ccrd_stage_objects as objects
    import ccrd_transaction as tx
    import ccrd_transaction_s3 as journal_s3


MODES = ('observe-space', 'start-pointer', 'apply-pointer', 'observe-pointer', 'finish',
         'rollback-start', 'rollback-apply', 'rollback-observe')


def step(mode: str, transaction: str, *, bundle: Path, token: str, api, journal, pointer_store,
         store_factory=objects.S3Store, capture=stage._source) -> dict:
    if mode not in MODES or not isinstance(transaction, str) or not tx.ID.fullmatch(transaction):
        raise ValueError('exact CCRD transaction and operation are required')
    parent, description, plan, _new_text = candidate._bundle(bundle)
    current = tx.load(journal)
    if current is None or current.record['transaction'] != transaction:
        raise ValueError('CCRD journal does not match requested transaction')
    tx.audit(journal, current)
    intent = coordinator._intent(journal, current)
    if (bytes.fromhex(intent['parent_pointer_hex']) != parent
            or bytes.fromhex(intent['candidate_manifest_hex']) != stage.lifecycle.canonical(plan['manifest'])
            or description.get('candidate_identity') != intent['activation_plan'].get('candidate_description_sha256')):
        raise ValueError('CCRD candidate bundle does not match the durable transaction')
    store = store_factory(generation=plan['generation'])
    source_capture = lambda: capture(token)[1]
    common = {'candidate_plan': plan, 'description': description, 'staging_store': store,
              'api': api, 'capture': source_capture}
    if mode == 'observe-space':
        current_pointer, etag = pointer_store.read_for_update()
        tx.strong_etag(etag)
        if current_pointer != parent:
            raise ValueError('CCRD pointer is not the exact parent before Space acceptance')
        result = coordinator.observe_space(journal, api=api)
        later, later_etag = pointer_store.read_for_update()
        if later != parent or tx.strong_etag(later_etag) != etag:
            raise ValueError('CCRD parent pointer changed during Space acceptance; halt and inspect')
    elif mode == 'start-pointer':
        value = coordinator.start_pointer(journal); result = {'state': value.record['state']}
    elif mode == 'apply-pointer':
        result = coordinator.apply_pointer_once(journal, pointer_store, **common)
    elif mode == 'observe-pointer':
        result = coordinator.observe_pointer(journal, pointer_store, **common)
    elif mode == 'finish':
        value = coordinator.finish(journal, pointer_store, **common); result = {'state': value.record['state']}
    elif mode == 'rollback-start':
        value = rollback.start(journal, pointer_store, api=api); result = {'state': value.record['state']}
    elif mode == 'rollback-apply':
        result = rollback.apply_once(journal, pointer_store, api=api)
    else:
        result = rollback.observe(journal, pointer_store, api=api)
    after = tx.load(journal)
    if after is None or after.record['transaction'] != transaction:
        raise ValueError('CCRD journal changed to another transaction during action')
    return {'kind': 'ccrd-finalization-action', 'mode': mode, 'transaction': transaction,
            'state': after.record['state'], 'sequence': after.record['sequence'],
            'result': result, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=MODES)
    parser.add_argument('--transaction', required=True)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--known-writers-coordinated', action='store_true')
    args = parser.parse_args(argv)
    if not args.known_writers_coordinated:
        raise ValueError('actual CCRD and Space writers must be coordinated')
    from huggingface_hub import HfApi
    result = step(args.mode, args.transaction, bundle=args.bundle, token=os.environ.get('GITHUB_TOKEN', ''),
                  api=HfApi(token=os.environ.get('HF_TOKEN')), journal=journal_s3.S3Store(), pointer_store=pointer.S3Pointer())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
