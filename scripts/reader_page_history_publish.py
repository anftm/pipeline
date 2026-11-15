"""Bounded reviewed PDF duplicate-history replacement, without object deletion.

The caller owns the serialized writer window, including independent writers.
This is not a full reference graph or GC authority. Equivalent replacements
preserve rollback aliases; this explicit duplicate-cleanup operation does not
pretend that old promotions acquired ACKs or passed ordinary retention.

Store contract (no production adapter is installed here): read_bytes/list_files,
assert_serialized_writer, read_for_update(ASSETS, RETIREMENT) -> (bytes, token),
immutable_put, and put_if_match(ASSETS, RETIREMENT, bytes, token). An absent root
must return (None, None); token=None MUST mean atomic create-if-absent. Both
writes must disable transport retries. Immutable writes must create-if-absent,
accept identical existing bytes, and reject conflicts. The future adapter must
allow only this retirement root and the immutable paths validated below.

Before deployment, ordinary reader_v3_retirement must stop wiping resources:
its resources=[pointer['catalog']] assignment would discard successor pins.
"""
import base64
import copy
import json
import re

try:
    from . import reader_page_history_replacement as replacement
    from . import reader_page_history_inventory as inventory
    from . import publish_reader_v3 as publication, reader_page_audit as audit
    from . import reader_page_redirect_retirement as finalization, reader_route_control as control
    from . import reader_page_redirects as redirects, reader_page_migrate as migration
except ImportError:
    import reader_page_history_replacement as replacement
    import reader_page_history_inventory as inventory
    import publish_reader_v3 as publication
    import reader_page_audit as audit
    import reader_page_redirect_retirement as finalization
    import reader_route_control as control
    import reader_page_redirects as redirects
    import reader_page_migrate as migration

RETIREMENT = inventory.RETIREMENT
INTENTS = 'reader-index/v3/history-replacements/derived-pdf-intents/'
ACKED = {'hf': True, 'pages': True}


def _optional(store, path):
    try:
        return store.read_bytes(audit.ASSETS, path)
    except FileNotFoundError:
        return None


def _receipt(store, surface, receipt):
    if (not isinstance(receipt, dict) or receipt.get('projection_verified') is not True
            or not receipt.get('checked_url') or not receipt.get('checked_at')):
        raise ValueError('current consumer requires verified projection evidence')
    publication.acknowledge(store, surface, receipt, apply=False)


def _observe(store, generation, observe_consumers):
    """Callback(store, generation) performs fresh reads, returning hf/pages receipts.

    Each receipt follows publication.observe_consumer, including checked_at,
    checked_url and projection_verified. No cached boolean acceptance is valid.
    The callback must not publish ACKs or mutate storage.
    """
    receipts = copy.deepcopy(observe_consumers(store, generation))
    _validate_observations(store, generation, receipts)
    return receipts


def _validate_observations(store, generation, receipts):
    if not isinstance(receipts, dict) or set(receipts) != set(ACKED):
        raise ValueError('observations of both current consumers are required')
    for surface, receipt in receipts.items():
        _receipt(store, surface, receipt)
        if receipt.get('generation') != generation:
            raise ValueError('consumer observation differs from verified generation')


def _completed_finalization(store, catalog, roots):
    """Check finalizer postconditions without creating a new finalization plan.

    Mirror its current legacy binding checks and reuse its suppression-aware
    base preparation. Snapshot metadata is checked as in finalizer._verify;
    return that extra evidence because history inventory does not read it.
    """
    pinned = publication.pinned_base(store, catalog, None)
    finalization._no_selected_bundle(pinned, 'current base', roots)
    canonical = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    base = audit.decode_sidecar(canonical)
    finalization._no_selected_bundle(base, 'canonical sidecar', roots)
    canonical_packed, _ = control.prepare_base(catalog, canonical)
    if audit.decode_sidecar(canonical_packed) != pinned:
        raise ValueError('canonical sidecar differs from full pinned base')
    legacy = json.loads(store.read_bytes(audit.ASSETS, migration.CATALOG))
    if 'redirect_sidecar_pending' in legacy:
        raise ValueError('canonical sidecar reconciliation is pending')
    current = legacy.get('current')
    current_id = current.get('generation') if isinstance(current, dict) else None
    generations = legacy.get('generations')
    entry = generations.get(current_id) if isinstance(generations, dict) and isinstance(current_id, str) else None
    if (legacy.get('version') != 1 or legacy.get('kind') != 'reader-catalog'
            or not isinstance(entry, dict) or entry.get('generation') != current_id
            or entry.get('status') != 'current'
            or entry.get('sidecar_sha256') != finalization._sha(audit.encode(base))):
        raise ValueError('current legacy generation does not bind canonical sidecar')
    refs = entry.get('references')
    if not isinstance(refs, list) or any(not isinstance(ref, dict)
            or not isinstance(ref.get('bucket'), str) or not isinstance(ref.get('path'), str) for ref in refs):
        raise ValueError('current legacy references are invalid')
    if not {(ref['bucket'], ref['path']) for ref in publication.reader_lifecycle.sidecar_references(base)} <= {
            (ref['bucket'], ref['path']) for ref in refs}:
        raise ValueError('current legacy references do not cover canonical sidecar')
    finalization._no_selected_bundle(entry, 'current legacy generation', roots)
    legacy_ref = entry.get('snapshot')
    if legacy_ref is None:
        return []
    if (not isinstance(legacy_ref, dict) or legacy_ref.get('bucket') != audit.ASSETS
            or legacy_ref.get('path') != f'reader-index/generations/{current_id}/reader_assets.json.gz'
            or not audit.SHA.fullmatch(str(legacy_ref.get('sha256', '')))
            or type(legacy_ref.get('bytes')) is not int or legacy_ref['bytes'] < 1):
        raise ValueError('invalid current legacy snapshot identity')
    legacy_packed = store.read_bytes(legacy_ref['bucket'], legacy_ref['path'])
    if (len(legacy_packed) != legacy_ref['bytes']
            or finalization._sha(legacy_packed) != legacy_ref['sha256']):
        raise ValueError('current legacy snapshot checksum mismatch')
    if audit.decode_sidecar(legacy_packed) != base:
        raise ValueError('current legacy snapshot differs from canonical sidecar')
    return [copy.deepcopy(legacy_ref)]


def _core(store, mapping, generation, api):
    if not isinstance(generation, str) or not audit.SHA.fullmatch(generation):
        raise ValueError('explicit current_ack_verified_generation is required')
    before = _optional(store, RETIREMENT)
    plan = replacement.prepare(store, mapping, api=api)
    report = plan.get('history_inventory')
    if (plan.get('discovery_mode') != 'inventory'
            or plan.get('history_inventory_complete') is not True
            or not report or report.get('scope_complete') is not True
            or report.get('blockers') != [] or plan.get('blockers', []) != []):
        raise ValueError('scope complete inventory and plan with zero blockers required')
    if any(row['selected_root_references'] for row in report['legacy_generations']):
        raise ValueError('selected legacy references must be zero')
    pointer, catalog = publication.current(store)
    if not pointer or pointer['generation'] != generation or pointer != plan['old_pointer']:
        raise ValueError('current pointer differs from verified generation')
    roots = plan['selected_old_bundles']
    # Completed current finalization includes the pinned base and canonical
    # roots, not merely an empty redirect table.
    if set(redirects.validated(catalog)) & set(plan['mapping']):
        raise ValueError('current finalization is not complete: selected redirects')
    finalization._no_selected_bundle(catalog, 'current catalog', roots)
    finalization_evidence = _completed_finalization(store, catalog, roots)
    finalization._no_selected_bundle(json.loads(store.read_bytes(audit.BUCKET, audit.INDEX)),
                                    'canonical PDF index', roots)
    promotion_path = replacement.PROMOTIONS + generation + '.json'
    promotion_raw = store.read_bytes(audit.ASSETS, promotion_path)
    promotion = json.loads(promotion_raw)
    if (promotion.get('version') != 1 or promotion.get('kind') != 'reader-reading-promotion'
            or promotion.get('generation') != generation
            or promotion.get('resources') != [pointer['catalog']]
            or promotion.get('acks') != ACKED):
        raise ValueError('actual current dual ACK promotion is required')
    for surface in ACKED:
        _receipt(store, surface, promotion.get('receipts', {}).get(surface))
    if not plan['replacements']:
        raise ValueError('no selected historical replacements')
    state = json.loads(before) if before is not None else {
        'version': 1, 'kind': 'reader-v3-retirement', 'resources': [], 'retired_paths': []}
    retired = set(state['retired_paths'])
    if any(not isinstance(path, str) for path in retired):
        raise ValueError('invalid retired paths')
    successors = {}
    for item in plan['replacements']:
        old = item['old_catalog']
        standard = f"reader-index/v3/generations/{item['old_generation']}/catalog.json"
        if old['path'] != standard:
            raise ValueError('only standard old catalog paths may be retired')
        successor = (item['catalog_ref'] if item['candidate_required'] else
                     plan['rollback_replacements'][item['old_generation']])
        successors[old['path']] = successor
        retired.add(standard)
        promotion_ref = item['old_promotion']
        if promotion_ref is not None:
            if promotion_ref['path'] != replacement.PROMOTIONS + item['old_generation'] + '.json':
                raise ValueError('only standard old promotion paths may be retired')
            retired.add(promotion_ref['path'])
    resources = [copy.deepcopy(successors.get(ref['path'], ref)) for ref in state['resources']]
    for ref in [pointer['catalog'], *successors.values(), *plan['rollback_replacements'].values()]:
        if ref not in resources:
            resources.append(copy.deepcopy(ref))
    if any(ref['path'] in retired for ref in resources):
        raise ValueError('successor or retained resource is retired')
    after = copy.deepcopy(state)
    after.update(resources=resources, retired_paths=sorted(retired),
                 rollback_replacements=copy.deepcopy(plan['rollback_replacements']),
                 replacement_generation=generation, replacement_acks=copy.deepcopy(ACKED))
    if _optional(store, RETIREMENT) != before:
        raise ValueError('retirement root changed during planning')
    return {'version': 1, 'kind': 'reader-derived-pdf-history-publish-plan',
            'current_ack_verified_generation': generation, 'replacement_plan': plan,
            'before': before, 'after': publication.encode(after),
            'current_pointer_raw': store.read_bytes(audit.ASSETS, publication.POINTER),
            'current_promotion_raw': promotion_raw, 'blockers': [],
            'finalization_evidence_resources': finalization_evidence,
            'apply_allowed': True, 'graph_complete': False, 'fully_unreferenced': False,
            'deletion_authorized': False}


def prepare(store, mapping, *, current_ack_verified_generation, observe_consumers, api=None):
    """Read-only review plan; requires explicit current generation and live reads."""
    api = api if api is not None else getattr(store, 'api', None)
    plan = _core(store, mapping, current_ack_verified_generation, api)
    receipts = _observe(store, current_ack_verified_generation, observe_consumers)
    if _core(store, mapping, current_ack_verified_generation, api) != plan:
        raise ValueError('history publish evidence changed during consumer observation')
    return {**plan, 'consumer_observations': receipts}


def _jsonable(value):
    if isinstance(value, bytes):
        return {'base64': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {key: _jsonable(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_jsonable(child) for child in value]
    return value


def _immutable(store, ref, raw):
    """Exact immutable allowlist for the future adapter, with readback."""
    path = ref['path']
    if ref != publication.metadata(audit.ASSETS, path, raw):
        raise ValueError('immutable metadata mismatch')
    snapshot = re.fullmatch(r'reader-index/snapshots/([0-9a-f]{64})/reader_assets\.json\.gz', path)
    intent = re.fullmatch(re.escape(INTENTS) + r'([0-9a-f]{64})\.json', path)
    catalog = re.fullmatch(re.escape(replacement.PREFIX) +
                          r'([0-9a-f]{64})/([0-9a-f]{64})/catalog\.json', path)
    if snapshot or intent:
        if (snapshot or intent)[1] != finalization._sha(raw):
            raise ValueError('immutable content address mismatch')
    elif catalog:
        value = publication.validate_catalog(json.loads(raw))
        if value['generation'] != catalog[2]:
            raise ValueError('immutable catalog generation mismatch')
    else:
        raise ValueError('immutable publication outside allowlist')
    store.immutable_put(audit.ASSETS, path, raw)
    if store.read_bytes(audit.ASSETS, path) != raw:
        raise ValueError('immutable publication readback mismatch')


def apply(store, plan, *, current_ack_verified_generation, observe_consumers, api=None):
    """Stage candidates and an exact before/after intent, then attempt ONE CAS.

    Readback resolves a lost CAS response only when the exact after bytes are
    present. Before bytes or failed readback leave outcome unknown; foreign
    bytes report conflict (possibly superseded). Never retry or roll back here.
    The intent retains the complete reviewed plan and fresh consumer evidence.
    """
    # Snapshot the entire reviewed input before any store or consumer callback.
    # Verification, candidate staging and the intent all use this same copy.
    reviewed = copy.deepcopy(plan)
    publication.assert_writer(store)
    api = api if api is not None else getattr(store, 'api', None)
    expected = {key: value for key, value in reviewed.items() if key != 'consumer_observations'}
    if current_ack_verified_generation != expected['current_ack_verified_generation']:
        raise ValueError('apply verified generation differs from reviewed plan')
    mapping = expected['replacement_plan']['mapping']

    def verify():
        if _core(store, mapping, current_ack_verified_generation, api) != expected:
            raise ValueError('history publish plan differs from fresh evidence')

    verify()
    _validate_observations(store, current_ack_verified_generation, reviewed.get('consumer_observations'))
    before, token = store.read_for_update(audit.ASSETS, RETIREMENT)
    if before != expected['before'] or (before is None) != (token is None):
        raise ValueError('retirement conditional read differs from reviewed before bytes')
    for item in expected['replacement_plan']['replacements']:
        if item['candidate_required']:
            _immutable(store, item['snapshot_ref'], item['packed'])
            _immutable(store, item['catalog_ref'], item['catalog_raw'])
    receipts = _observe(store, current_ack_verified_generation, observe_consumers)
    verify()
    intent = {'version': 1, 'kind': 'reader-derived-pdf-history-publish-intent',
              'reason': 'explicit-user-requested-equivalent-duplicate-cleanup',
              'reviewed_plan': _jsonable(reviewed), 'fresh_consumer_observations': receipts,
              'before': _jsonable(before), 'after': _jsonable(expected['after']),
              'before_sha256': finalization._sha(before) if before is not None else None,
              'after_sha256': finalization._sha(expected['after']),
              'conditional_token': token, 'deletion_authorized': False}
    intent_raw = publication.encode(intent)
    intent_ref = publication.metadata(audit.ASSETS, INTENTS + finalization._sha(intent_raw) + '.json', intent_raw)
    _immutable(store, intent_ref, intent_raw)
    # Includes inventory listings, absent roots, every evidence resource and
    # fresh exact-copy audit. The serialized window is still essential: a
    # single retirement CAS cannot lock all these independent roots.
    verify()
    observed, latest_token = store.read_for_update(audit.ASSETS, RETIREMENT)
    if observed != before or latest_token != token:
        raise ValueError('retirement root changed before CAS')
    cas_error = None
    try:
        store.put_if_match(audit.ASSETS, RETIREMENT, expected['after'], token)
    except Exception as error:
        cas_error = type(error).__name__
    try:
        actual, _ = store.read_for_update(audit.ASSETS, RETIREMENT)
        outcome = ('applied' if actual == expected['after'] else
                   'unknown' if actual == before else 'conflict')
    except Exception:
        outcome = 'unknown'
    return {'version': 1, 'kind': 'reader-derived-pdf-history-publish-result',
            'outcome': outcome, 'applied': outcome == 'applied', 'cas_attempts': 1,
            'cas_error': cas_error, 'intent_ref': intent_ref,
            'current_ack_verified_generation': current_ack_verified_generation,
            'scope_complete': True, 'graph_complete': False, 'fully_unreferenced': False,
            'consumer_acks_created': False, 'canonical_indexes_changed': False,
            'objects_deleted': False, 'deletion_authorized': False}
