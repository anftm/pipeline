"""Drive one exact CCRD candidate after independent writer coordination.

This command is not a global lock. It starts a Space mutation at most once;
claimed or uncertain effects are only observed, never replayed. A timeout or
conflict leaves the journal for explicit recovery. No synthetic inputs are
accepted by the actual activation/staging validation chain.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

try:
    from . import ccrd_auto_activate as activation
    from . import ccrd_auto_finalize as finalization
    from . import ccrd_pointer_transition as pointer
    from . import ccrd_transaction as tx
    from . import ccrd_transaction_s3 as journal_s3
except ImportError:
    import ccrd_auto_activate as activation
    import ccrd_auto_finalize as finalization
    import ccrd_pointer_transition as pointer
    import ccrd_transaction as tx
    import ccrd_transaction_s3 as journal_s3


STATES = {'prepared', 'space-intent', 'space-claimed', 'space-verified', 'pointer-intent',
          'pointer-claimed', 'pointer-verified', 'complete'}


def _current(journal, transaction: str, bundle: Path) -> tx.Current:
    parent, description, plan, _ = activation._bundle(bundle)
    current = tx.load(journal)
    if current is None or current.record['transaction'] != transaction:
        raise ValueError('CCRD release journal does not match the requested transaction')
    tx.audit(journal, current)
    from_intent = finalization.coordinator._intent(journal, current)
    if (bytes.fromhex(from_intent['parent_pointer_hex']) != parent
            or bytes.fromhex(from_intent['candidate_manifest_hex']) != finalization.stage.lifecycle.canonical(plan['manifest'])
            or from_intent['activation_plan'].get('candidate_description_sha256') != description.get('candidate_identity')):
        raise ValueError('CCRD release candidate differs from durable journal')
    return current


def _run(mode: str, transaction: str, *, bundle: Path, token: str, api, journal, pointer_store,
         timeout: int, poll: int, sleep, clock, step) -> dict:
    if (mode not in {'fresh', 'resume'} or not isinstance(transaction, str) or not tx.ID.fullmatch(transaction)
            or type(timeout) is not int or not 1 <= timeout <= 4 * 3600
            or type(poll) is not int or not 1 <= poll <= 300):
        raise ValueError('invalid CCRD release driver request')
    deadline = clock() + timeout
    initial = _current(journal, transaction, bundle)
    if mode == 'fresh' and initial.record['state'] != 'space-claimed':
        raise ValueError('fresh CCRD release must follow the exact claimed Space activation')
    # A freshly created pointer intent can be dispatched once by this process.
    # An intent inherited from a terminated process is never replayed.
    pointer_intent_owned = False
    last_verified_head = None
    while True:
        current = _current(journal, transaction, bundle)
        state = current.record['state']
        if state not in STATES:
            raise ValueError('CCRD release is not in a forward state; inspect rollback or conflict')
        if state == 'complete':
            if last_verified_head != current.head_raw:
                return {'kind': 'ccrd-release-drive', 'status': 'already-complete-unchecked',
                        'transaction': transaction, 'generation': current.record['candidate_generation'],
                        'external_writers_verified': False}
            return {'kind': 'ccrd-release-drive', 'status': 'complete', 'transaction': transaction,
                    'generation': current.record['candidate_generation'], 'external_writers_verified': False}
        if clock() >= deadline:
            return {'kind': 'ccrd-release-drive', 'status': 'waiting', 'state': state, 'transaction': transaction,
                    'generation': current.record['candidate_generation'], 'external_writers_verified': False}
        common = {'bundle': bundle, 'token': token, 'api': api, 'journal': journal, 'pointer_store': pointer_store}
        if state in {'prepared', 'space-intent'}:
            # A prior process may have died before or after its Space request.
            # Never create a commit from a resumed driver.
            return {'kind': 'ccrd-release-drive', 'status': 'manual-recovery', 'state': state,
                    'transaction': transaction, 'external_writers_verified': False}
        if state == 'space-claimed':
            result = step('observe-space', transaction, **common)
            if not result['result'].get('journal_advanced'):
                sleep(poll)
        elif state == 'space-verified':
            step('start-pointer', transaction, **common)
            pointer_intent_owned = True
        elif state == 'pointer-intent':
            if not pointer_intent_owned:
                return {'kind': 'ccrd-release-drive', 'status': 'manual-recovery', 'state': state,
                        'transaction': transaction, 'external_writers_verified': False}
            pointer_intent_owned = False
            step('apply-pointer', transaction, **common)
        elif state == 'pointer-claimed':
            result = step('observe-pointer', transaction, **common)
            if not result['result'].get('journal_advanced'):
                sleep(poll)
        elif state == 'pointer-verified':
            result = step('finish', transaction, **common)
            after = _current(journal, transaction, bundle)
            if (after.record['state'] != 'complete' or result.get('state') != 'complete'
                    or result.get('sequence') != after.record['sequence']):
                raise ValueError('CCRD verified completion does not match its journal')
            last_verified_head = after.head_raw


def start(bundle: Path, *, metadata_root: Path, token: str, api, journal, pointer_store,
          timeout: int = 3600, poll: int = 30, sleep=time.sleep, clock=time.monotonic,
          activate=activation.start, step=finalization.step) -> dict:
    result = activate(bundle, metadata_root=metadata_root, token=token, api=api, journal=journal, pointer_store=pointer_store)
    transaction = result.get('transaction') if isinstance(result, dict) else None
    if (not isinstance(transaction, str) or not tx.ID.fullmatch(transaction)
            or result.get('kind') != 'ccrd-activation-request' or result.get('pointer_written') is not False):
        raise ValueError('CCRD Space activation did not return a durable transaction')
    return _run('fresh', transaction, bundle=bundle, token=token, api=api, journal=journal,
                pointer_store=pointer_store, timeout=timeout, poll=poll, sleep=sleep, clock=clock, step=step)


def resume(bundle: Path, transaction: str, *, token: str, api, journal, pointer_store,
           timeout: int = 3600, poll: int = 30, sleep=time.sleep, clock=time.monotonic,
           step=finalization.step) -> dict:
    return _run('resume', transaction, bundle=bundle, token=token, api=api, journal=journal,
                pointer_store=pointer_store, timeout=timeout, poll=poll, sleep=sleep, clock=clock, step=step)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--metadata-root', type=Path)
    parser.add_argument('--resume-transaction', default='')
    parser.add_argument('--known-writers-coordinated', action='store_true')
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args(argv)
    if not args.known_writers_coordinated:
        raise ValueError('actual CCRD and Space writers must be coordinated')
    from huggingface_hub import HfApi
    common = {'token': os.environ.get('GITHUB_TOKEN', ''), 'api': HfApi(token=os.environ.get('HF_TOKEN')),
              'journal': journal_s3.S3Store(), 'pointer_store': pointer.S3Pointer(), 'timeout': args.timeout}
    if args.resume_transaction:
        if args.metadata_root is not None: raise ValueError('resume does not create or retry a Space commit')
        result = resume(args.bundle, args.resume_transaction, **common)
    else:
        if args.metadata_root is None: raise ValueError('fresh release requires a new metadata work directory')
        result = start(args.bundle, metadata_root=args.metadata_root, **common)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result['status'] == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
