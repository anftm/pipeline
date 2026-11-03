"""Exact-subset batch coordinator; storage and barriers are caller supplied.

This helper is not a standalone production entrypoint. The invoking workflow
must hold its locks, independently validate the live graph and restrict the
backend's mutation scope. A pending intent always survives a failed response.
"""
import json

try:
    from . import chm_subset_batch_model as batch, chm_subset_journal as model
    from . import chm_subset_journal_backend as chain, chm_subset_reconcile as reconcile
except ImportError:
    import chm_subset_batch_model as batch
    import chm_subset_journal as model
    import chm_subset_journal_backend as chain
    import chm_subset_reconcile as reconcile


def run(store, cursor, rows, old, plan, barrier, *, nonce, batch_size=100):
    if not callable(barrier) or not callable(nonce):
        raise ValueError('subset deletion barriers are required')
    selected, withheld = plan['selected'], plan['withheld']

    def observe(state):
        def read(bucket, path):
            try:
                return store.read_bytes(bucket, path)
            except FileNotFoundError:
                if store.assert_chm_bucket_available() is not True:
                    raise RuntimeError('CHM bucket health is unknown')
                return None
        return reconcile.reconcile(rows, old, selected, withheld, state, read, workers=4)

    head = cursor.head()
    while not head['state']['subset_complete']:
        barrier()
        observation = observe(head['state'])
        pending = head['state']['pending']
        if pending is None:
            verified = sum(len(item['paths']) for item in head['state']['receipts'])
            if verified == len(selected):
                event = batch.plan_completion(rows, old, selected, withheld, plan, head,
                                              observation, nonce=nonce())['envelope']
                cursor.append(model.digest(head), event)
                head = cursor.head()
                break
            event = batch.plan_intent(rows, old, selected, withheld, plan, head, observation,
                                      batch_size=batch_size, nonce=nonce())['envelope']
            cursor.append(model.digest(head), event)
            head = cursor.head()
            pending = head['state']['pending']
            if pending != event['state']['pending']:
                raise chain.Conflict('persisted subset intent differs')
            print(json.dumps({'stage': 'intent-persisted', 'objects': len(pending['paths']),
                              'already_verified': verified}), flush=True)
            barrier()
            observation = observe(head['state'])
        pending_set = set(pending['paths'])
        pending_rows = [item for item in observation['results'] if item['path'] in pending_set]
        if len(pending_rows) != len(pending_set):
            raise ValueError('pending target observations are incomplete')
        present = [item['path'] for item in pending_rows if item['status'] == 'pending-present']
        if present:
            # For recovery, send only the freshly verified still-present keys.
            # A transport exception leaves the intent pending and ends this run.
            store.delete(model.subset.ASSETS, present)
            print(json.dumps({'stage': 'delete-response-received', 'objects': len(present)}), flush=True)
            barrier()
            observation = observe(head['state'])
        event = batch.plan_receipt(rows, old, selected, withheld, plan, head,
                                   observation, nonce=nonce())['envelope']
        cursor.append(model.digest(head), event)
        head = cursor.head()
        print(json.dumps({'stage': 'receipt-verified',
                          'subset_deleted': sum(len(item['paths']) for item in head['state']['receipts'])}), flush=True)
    barrier()
    observe(head['state'])
    return head
