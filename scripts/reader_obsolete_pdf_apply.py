"""Bounded obsolete-PDF reference retirement; no deletion or automatic resume.

reprepare() must invoke reader_obsolete_pdf.prepare with trusted inventory and
fresh observations. check_window() must enforce the caller's exclusive writer
window (raise or return False on loss); it must cover independent roots, not just
one CAS. Store conditional tokens must be real server tokens, and writes must
not retry internally. A transport exception is resolved by one exact readback.
No production adapter or command-line workflow is provided here.
"""
import base64
import copy
import json
import re

from . import ccrd_lifecycle, reader_obsolete_pdf as preparation

BUCKET, INDEX = preparation.BUCKET, preparation.INDEX
ASSETS = preparation.audit.ASSETS
INTENTS = 'reader-index/gc/obsolete-pdf-retirement/'
CCRD_ROOT = ('control/ocr-preserved-reader-'
             'fb1abf531f3a630d65e77ae0167bd049ddf9a6c46b8758ac4ed0672eb992819e.json')
require, proof = preparation.require, preparation.proof


def _comparison(plan):
    value = copy.deepcopy(plan)
    # Only these two receipt timestamps may change. Never strip arbitrary keys.
    for surface in ('hf', 'pages'):
        value['observations'][surface].pop('checked_at', None)
    return value


def _window(check_window):
    require(check_window() is not False, 'exclusive writer window unavailable')


def _ordered(mutations):
    """Validate the exact nine roots and byte splices, independently of ordering."""
    require(len(mutations) == 9, 'expected exactly nine mutations')
    roots = {(m['bucket'], m['path']): m for m in mutations}
    require(len(roots) == 9 and (BUCKET, INDEX) in roots, 'invalid mutation roots')
    for m in mutations:
        require(isinstance(m['before'], bytes) and isinstance(m['after'], bytes)
                and m['before'] != m['after'], 'invalid mutation bytes')
        require(m['before_identity'] == proof(m['before'])
                and m['after_identity'] == proof(m['after']), 'mutation proof mismatch')
    index = roots[BUCKET, INDEX]
    document = json.loads(index['before'])
    require(document.get('version') == 1 and document.get('kind') == 'pdf-derived-source-index',
            'invalid canonical PDF index')
    require(len({row['key'] for row in document['files']}) == len(document['files']),
            'duplicate canonical keys')
    manifests, after_index = [], index['before']
    for row in document['files']:
        key = (BUCKET, row.get('manifest_path'))
        if key not in roots:
            continue
        path = row.get('new_path', '')
        match = preparation.audit.NEW.fullmatch(path)
        require(match and match[1] == match[2][:2] and match[2] == row.get('source_sha256')
                and row.get('path') == path and row.get('bucket') == BUCKET
                and row.get('source_extension') == 'pdf'
                and row.get('profile') == row.get('generation') == 'gbk-font-repair-v2'
                and key[1] == path.rsplit('/', 1)[0] + '/derived-manifest.json',
                'invalid canonical object manifest path or identity')
        m = roots[key]
        require(json.loads(m['before']) == {**row, 'version': 1, 'kind': 'pdf-derived-source'},
                'object manifest differs from canonical index')
        previous = row['previous_pdf']
        require(m['after'] == preparation.remove_reference(m['before'], 'previous_pdf', previous),
                'unexpected manifest edit')
        after_index = preparation.remove_reference(after_index, 'previous_pdf', previous)
        manifests.append(m)
    require(len(manifests) == 7 and len({m['path'] for m in manifests}) == 7,
            'expected seven canonical object manifests')
    require(index['after'] == after_index, 'unexpected canonical index edit')
    other = [m for m in mutations if (m['bucket'], m['path']) != (BUCKET, INDEX)
             and (m['bucket'], m['path']) not in {(x['bucket'], x['path']) for x in manifests}]
    require(len(other) == 1, 'expected one CCRD control root')
    ccrd = other[0]
    require(ccrd['bucket'] == ccrd_lifecycle.BUCKET and ccrd['path'] == CCRD_ROOT,
            'CCRD control root outside narrow allowlist')
    refs = ccrd['reference_mutations']
    require(len(refs) == 1 and refs[0]['operation'] == 'remove' and refs[0]['field'] == 'resources',
            'unexpected CCRD edit')
    ref = refs[0]['before']
    require(ref.get('bucket') == BUCKET and ref.get('role') == 'provenance'
            and re.fullmatch(r'derived/[A-Za-z0-9._-]+/[a-f0-9]{32}/derived-manifest\.json', ref.get('path', '')),
            'unexpected CCRD provenance reference')
    require(ccrd['after'] == preparation.remove_reference(ccrd['before'], 'resources', ref, array=True),
            'unexpected CCRD bytes')
    return manifests + [ccrd, index]


def _packed(mutation):
    result = copy.deepcopy(mutation)
    for field in ('before', 'after'):
        result[field + '_base64'] = base64.b64encode(result.pop(field)).decode('ascii')
    return result


def _unpacked(mutation):
    result = copy.deepcopy(mutation)
    for field in ('before', 'after'):
        result[field] = base64.b64decode(result.pop(field + '_base64'), validate=True)
    return result


def apply(store, plan, reprepare, check_window, checkpoint=None, owner=None):
    """Stage durable intent, then one CAS each: seven manifests, CCRD, index.

    Validation failures before staging raise without writes. After staging, any
    failure returns all nine per-root outcomes and an overall partial result.
    An exact after value is accepted only after this invocation attempted CAS;
    an already modified root at preflight is never treated as resumable.
    """
    reviewed = copy.deepcopy(plan)
    _window(check_window)
    fresh = copy.deepcopy(reprepare())
    require(_comparison(reviewed) == _comparison(fresh), 'plan differs from trusted fresh preparation')
    require(fresh['kind'] == 'reader-obsolete-pdf-preparation' and fresh['bucket'] == BUCKET
            and fresh['applied'] is False and fresh['deletion_authorized'] is False
            and fresh['blockers'] == [], 'preparation is blocked or invalid')
    require(len(set(fresh['delete_allowlist'])) == len(fresh['delete_allowlist']) == 30
            and fresh['delete_allowlist'] == fresh['target_delete_paths'], 'invalid retirement targets')
    mutations = _ordered(fresh['mutations'])
    tokens = []
    for m in mutations:
        raw, token = store.read_for_update(m['bucket'], m['path'])
        require(raw == m['before'] and token is not None, 'root changed before intent: ' + m['path'])
        tokens.append(token)
    _window(check_window)
    intent = {'version': 1, 'kind': 'reader-obsolete-pdf-retirement-intent',
              'generation': fresh['generation'], 'observations': fresh['observations'],
              'projection_identity': fresh['projection_identity'], 'root_proofs': fresh['root_proofs'],
              'target_delete_paths': fresh['target_delete_paths'], 'deletion_authorized': False,
              'mutations': [_packed(m) for m in mutations]}
    if owner is not None:
        intent['owner'] = copy.deepcopy(owner)
    intent_raw = preparation.p.encode(intent)
    intent_ref = {'bucket': ASSETS, 'path': INTENTS + proof(intent_raw)['sha256'] + '.json', **proof(intent_raw)}
    outcomes = [{'bucket': m['bucket'], 'path': m['path'], 'outcome': 'not_attempted', 'cas_attempts': 0}
                for m in mutations]
    result = {'kind': 'reader-obsolete-pdf-retirement-result', 'outcome': 'partial', 'applied': False,
              'generation': fresh['generation'], 'deletion_authorized': False,
              'intent_ref': intent_ref, 'intent_verified': False, 'roots': outcomes}
    if checkpoint:
        checkpoint(copy.deepcopy(result))
    try:
        store.immutable_put(ASSETS, intent_ref['path'], intent_raw)
        require(store.read_bytes(ASSETS, intent_ref['path']) == intent_raw, 'intent readback mismatch')
        result['intent_verified'] = True
    except Exception as error:
        result['error'] = 'intent: ' + type(error).__name__
        return result
    for m, token, outcome in zip(mutations, tokens, outcomes):
        try:
            observed, latest = store.read_for_update(m['bucket'], m['path'])
            if observed != m['before'] or latest != token:
                outcome['outcome'] = 'conflict'
                break
            _window(check_window)
        except Exception as error:
            outcome.update(outcome='unknown', error=type(error).__name__)
            break
        outcome['cas_attempts'] = 1
        outcome['outcome'] = 'unknown'
        if checkpoint:
            checkpoint(copy.deepcopy(result))
        try:
            store.put_if_match(m['bucket'], m['path'], m['after'], token)
        except Exception as error:
            outcome['cas_error'] = type(error).__name__
        try:
            actual, _ = store.read_for_update(m['bucket'], m['path'])
            outcome['outcome'] = ('applied' if actual == m['after'] else
                                  'unknown' if actual == m['before'] else 'conflict')
            outcome['recognized'] = actual == m['after'] and 'cas_error' in outcome
        except Exception as error:
            outcome.update(outcome='unknown', error=type(error).__name__)
        if checkpoint:
            checkpoint(copy.deepcopy(result))
        if outcome['outcome'] != 'applied':
            break
    result['applied'] = all(row['outcome'] == 'applied' for row in outcomes)
    result['outcome'] = 'applied' if result['applied'] else 'partial'
    if checkpoint:
        checkpoint(copy.deepcopy(result))
    return result


def inspect(store, intent_ref):
    """Read-only classification of a durable attempt; never resume or roll back."""
    require(intent_ref['bucket'] == ASSETS and re.fullmatch(
        re.escape(INTENTS) + r'[0-9a-f]{64}\.json', intent_ref['path']), 'invalid intent path')
    raw = store.read_bytes(ASSETS, intent_ref['path'])
    require(proof(raw) == {k: intent_ref[k] for k in ('bytes', 'sha256')}
            and intent_ref['path'] == INTENTS + proof(raw)['sha256'] + '.json', 'intent proof mismatch')
    intent = json.loads(raw)
    require(intent['version'] == 1 and intent['kind'] == 'reader-obsolete-pdf-retirement-intent'
            and intent['deletion_authorized'] is False, 'invalid intent')
    mutations = _ordered([_unpacked(m) for m in intent['mutations']])
    roots = []
    for m in mutations:
        row = {'bucket': m['bucket'], 'path': m['path']}
        try:
            actual = store.read_bytes(m['bucket'], m['path'])
            row['outcome'] = ('before' if actual == m['before'] else
                              'after' if actual == m['after'] else 'foreign')
        except Exception as error:
            row.update(outcome='unknown', error=type(error).__name__)
        roots.append(row)
    return {'kind': 'reader-obsolete-pdf-retirement-inspection', 'roots': roots,
            'generation': intent['generation'], 'deletion_authorized': False,
            'resume_authorized': False}
