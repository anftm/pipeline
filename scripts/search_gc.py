"""Bounded, two-phase retirement of search generations in search-data-v1.

All stage/promotions/adopt/GC writers must share this repository's daily-sync
concurrency group (cancel-in-progress: false), with other known writers stopped
or independently coordinated for the entire invocation. The explicit declaration
is an operator assertion, NOT a global lock or proof that unknown writers are
fenced. Unknown writers prohibit apply. History records/receipts remain intact;
terminal journal history does not retain historical payloads forever.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

try:
    from . import search_daily_journal as journal, search_snapshot, search_transition as tx
    from .search_artifact_store import make_manifest, object_key
    from .search_publication_pointer import BUCKET, decode_pointer
    from .reader_page_cas import strong_etag
except ImportError:
    import search_daily_journal as journal
    import search_snapshot
    import search_transition as tx
    from search_artifact_store import make_manifest, object_key
    from search_publication_pointer import BUCKET, decode_pointer
    from reader_page_cas import strong_etag

DAY = 86400
RETENTION = 7 * DAY
MARK_DELAY = DAY
MAX_MANIFESTS = 2
MAX_OBJECTS = 100
MAX_BYTES = 128 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
MANIFEST_RE = re.compile(r'generations/([0-9a-f]{64})/manifest\.json\Z')
OBJECT_RE = re.compile(r'objects/sha256/([0-9a-f]{2})/([0-9a-f]{64})\Z')
GC_RE = re.compile(r'gc/(plans|attempts|results)/([0-9]{1,32})\.json\Z')
canonical = search_snapshot.canonical


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid timestamp')
    return value


def run_identity(value):
    # GitHub reruns retain GITHUB_RUN_ID; run_attempt must never be appended.
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,31}', value):
        raise ValueError('run id must be the stable numeric workflow run id, without run_attempt')
    return value


def payload_key(key):
    if not isinstance(key, str):
        raise ValueError('invalid payload key')
    if MANIFEST_RE.fullmatch(key):
        return
    match = OBJECT_RE.fullmatch(key)
    if not match or object_key(match[2]) != key:
        raise ValueError('key outside search payload namespace')


def metadata(value):
    if not isinstance(value, dict) or set(value) != {'key', 'etag', 'size', 'mtime'}:
        raise ValueError('invalid inventory metadata')
    if not isinstance(value['key'], str) or not value['key']:
        raise ValueError('invalid inventory key')
    strong_etag(value['etag'])
    if type(value['size']) is not int or value['size'] < 0:
        raise ValueError('invalid inventory size')
    timestamp(value['mtime'])
    return value


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON:
        raise ValueError('missing or oversized JSON')
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical(value) != raw:
        raise ValueError('expected canonical JSON object')
    return value


def manifest_refs(raw, key):
    """Rebuild the staging manifest to check identity, paths, sizes and refs."""
    payload_key(key)
    match = MANIFEST_RE.fullmatch(key)
    value = decode(raw)
    if (not match or value.get('generation') != match[1] or value.get('version') != 1
            or value.get('kind') != 'search-snapshot-local'
            or type(value.get('record_count')) is not int or value['record_count'] < 1):
        raise ValueError('invalid generation manifest identity')
    checks = value.get('additional_checks')
    if checks is not None and (not isinstance(checks, dict) or
            any(not isinstance(k, str) or not isinstance(v, str) for k, v in checks.items())):
        raise ValueError('invalid manifest additional checks')
    report = {k: v for k, v in value.items()
              if k not in {'objects', 'object_layout', 'additional_checks'}}
    report['remote_writes'] = 0
    report['publication_ready'] = False
    expected = make_manifest(report)
    if {k: v for k, v in value.items() if k != 'additional_checks'} != expected:
        raise ValueError('manifest identity or references mismatch')
    refs = {}
    for members in value['objects'].values():
        for item in members.values():
            if refs.setdefault(item['object'], item['bytes']) != item['bytes']:
                raise ValueError('conflicting object sizes')
    return refs


class JournalView:
    """Read-only bridge; reuse the unchanged records/receipts history audit."""
    def __init__(self, store):
        self.store = store

    def read_head(self):
        key = tx.HEAD_KEY
        meta = self.store.head(key)
        return (None, None) if meta is None else (self.store.read(key, meta), meta['etag'])

    def read_pointer(self):
        return self.store.read('published/current.json')

    def read_record(self, key):
        tx.S3TransitionStore._check_record(key)
        return self.store.read(key)

    def read_receipt(self, key):
        tx.S3TransitionStore._check_receipt(key)
        return self.store.read(key)


def inventory(store):
    items = {}
    for item in store.inventory():
        metadata(item)
        key = item['key']
        if key in items:
            raise ValueError('duplicate inventory key')
        if key.startswith(('generations/', 'objects/')):
            payload_key(key)
        if key.startswith('gc/') and not GC_RE.fullmatch(key):
            raise ValueError('unknown GC metadata key')
        items[key] = item
    return items


def graph(store, now):
    """Full manifest inventory. Any missing referenced object fails closed."""
    timestamp(now)
    view = JournalView(store)
    observation = journal.inspect(view)
    pointer = decode_pointer(view.read_pointer())
    items = inventory(store)
    manifests, refs = {}, {}
    for key, item in items.items():
        if item['mtime'] > now and key.startswith(('generations/', 'objects/')):
            raise ValueError('payload inventory timestamp is in the future')
        if MANIFEST_RE.fullmatch(key):
            raw = store.read(key, item)
            manifests[key] = {'meta': item, 'manifest': decode(raw)}
            refs[key] = manifest_refs(raw, key)
    for members in refs.values():
        for key, size in members.items():
            if key not in items or items[key]['size'] != size or store.head(key) != items[key]:
                raise ValueError('missing or changed referenced object: ' + key)
    protected = {f'generations/{g}/manifest.json' for g in
                 (pointer['desired'], pointer['previous']) if g is not None}
    retained = {}
    for role in ('desired', 'previous'):
        if pointer[role] is not None:
            retained.setdefault(f"generations/{pointer[role]}/manifest.json", []).append('current-' + role)
    if not protected <= manifests.keys():
        raise ValueError('current/previous manifest is missing')
    newest = sorted((entry['meta']['mtime'] for entry in manifests.values()), reverse=True)
    cutoff = newest[min(1, len(newest) - 1)]
    # Protect ties too: an arbitrary key order must not evict a newest generation.
    for key, entry in manifests.items():
        if entry['meta']['mtime'] >= cutoff:
            retained.setdefault(key, []).append('newest-at-least-two-including-ties')
        if now - entry['meta']['mtime'] < RETENTION:
            retained.setdefault(key, []).append('age-less-than-seven-days')
    protected.update(retained)
    journal.verify_unchanged(observation, journal.inspect(view))
    if decode_pointer(view.read_pointer()) != pointer:
        raise ValueError('pointer changed during inventory')
    return {'items': items, 'manifests': manifests, 'refs': refs,
            'protected': protected, 'retained_generations': retained, 'journal': observation}


def budget(manifests, objects):
    total_bytes = (sum(entry['meta']['size'] for entry in manifests.values())
                   + sum(item['size'] for item in objects.values()))
    if (len(manifests) > MAX_MANIFESTS or len(objects) > MAX_OBJECTS or total_bytes > MAX_BYTES):
        raise ValueError('GC candidate budget exceeded')


def plan(store, *, run_id, now, run_attempt='1'):
    run_identity(run_id)
    run_identity(run_attempt)
    current = graph(store, now)
    selected, objects = {}, {}
    eligible = sorted(set(current['manifests']) - current['protected'],
                      key=lambda k: (current['items'][k]['mtime'], k))
    for key in eligible:
        trial = {**selected, key: current['manifests'][key]}
        remaining_refs = {ref for k, refs in current['refs'].items() if k not in trial for ref in refs}
        candidate_refs = {ref for k in trial for ref in current['refs'][k]} - remaining_refs
        candidate_objects = {}
        # Retire even a large generation. Keep ALL manifest bytes/refs as audit
        # evidence, but delete only this bounded object batch. Remaining objects
        # become old orphans eligible for later independent mark/sweep batches.
        for ref in sorted(candidate_refs):
            batch = {**candidate_objects, ref: current['items'][ref]}
            try:
                budget(trial, batch)
            except ValueError:
                continue
            candidate_objects = batch
        selected, objects = trial, candidate_objects
        if len(selected) == MAX_MANIFESTS:
            break
    all_refs = {ref for refs in current['refs'].values() for ref in refs}
    orphans = {}
    for key, item in sorted(current['items'].items(), key=lambda pair: (pair[1]['mtime'], pair[0])):
        if OBJECT_RE.fullmatch(key) and key not in all_refs and now - item['mtime'] >= RETENTION:
            trial = {**objects, key: item}
            try:
                budget(selected, trial)
            except ValueError:
                continue
            if store.head(key) != item:
                raise ValueError('orphan metadata changed during planning')
            objects, orphans[key] = trial, item
    return {'version': 1, 'kind': 'search-gc-plan', 'bucket': BUCKET,
            'run_id': run_id, 'run_attempt': run_attempt, 'created_at': now, 'journal': current['journal'],
            'manifest_inventory': {k: v['meta'] for k, v in current['manifests'].items()},
            'manifests': selected, 'objects': objects, 'orphan_objects': orphans,
            'retained_generations': current['retained_generations'],
            'deferred_generations': sorted(set(current['manifests']) - current['protected'] - set(selected))}


def validate_plan(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'kind', 'bucket', 'run_id',
            'run_attempt', 'created_at', 'journal', 'manifest_inventory', 'manifests', 'objects', 'orphan_objects',
            'retained_generations', 'deferred_generations'}
            or value['version'] != 1 or value['kind'] != 'search-gc-plan' or value['bucket'] != BUCKET):
        raise ValueError('invalid GC plan')
    run_identity(value['run_id'])
    run_identity(value['run_attempt'])
    timestamp(value['created_at'])
    for field in ('manifest_inventory', 'manifests', 'objects', 'orphan_objects', 'journal', 'retained_generations'):
        if not isinstance(value[field], dict):
            raise ValueError('invalid plan mapping')
    refs = {}
    for key, item in value['manifest_inventory'].items():
        if not MANIFEST_RE.fullmatch(key) or metadata(item)['key'] != key:
            raise ValueError('invalid planned manifest inventory')
    for key, entry in value['manifests'].items():
        if not isinstance(entry, dict) or set(entry) != {'meta', 'manifest'}:
            raise ValueError('invalid planned manifest evidence')
        item = metadata(entry['meta'])
        raw = canonical(entry['manifest'])
        if (item['key'] != key or value['manifest_inventory'].get(key) != item
                or item['size'] != len(raw) or value['created_at'] - item['mtime'] < RETENTION):
            raise ValueError('invalid planned manifest metadata or age')
        for ref, size in manifest_refs(raw, key).items():
            if refs.setdefault(ref, size) != size:
                raise ValueError('conflicting planned object size')
    for key, item in value['orphan_objects'].items():
        if (not OBJECT_RE.fullmatch(key) or metadata(item)['key'] != key
                or value['objects'].get(key) != item or key in refs
                or value['created_at'] - item['mtime'] < RETENTION):
            raise ValueError('invalid old orphan evidence')
    for key, item in value['objects'].items():
        payload_key(key)
        if (metadata(item)['key'] != key or not OBJECT_RE.fullmatch(key)
                or (key not in value['orphan_objects'] and refs.get(key) != item['size'])):
            raise ValueError('planned object lacks manifest or orphan evidence')
    budget(value['manifests'], value['objects'])
    return value


def verified_results(store, items):
    """Bind historical results to their immutable attempt and exact marked plan.

    These are audit evidence, not tombstones: only a matching manifest identity
    with a delete request and an absent readback can explain a later missing key.
    An `absent` event alone never proves who retired a manifest.
    """
    for key, item in items.items():
        if not key.startswith('gc/results/'):
            continue
        result = decode(store.read(key, item))
        fields = {'version', 'kind', 'run_id', 'run_attempt', 'plan_key', 'plan_sha256',
                  'created_at', 'status', 'events', 'cancelled_manifests',
                  'known_writers_declared_coordinated', 'global_lock'}
        if result.get('status') == 'stopped':
            fields.add('error_type')
        if (set(result) != fields or result['version'] != 1 or result['kind'] != 'search-gc-result'
                or result['status'] not in {'complete', 'stopped'}
                or result['known_writers_declared_coordinated'] is not True or result['global_lock'] is not False):
            raise ValueError('malformed GC result evidence')
        run_identity(result['run_id'])
        run_identity(result['run_attempt'])
        timestamp(result['created_at'])
        if key != f"gc/results/{result['run_id']}.json":
            raise ValueError('GC result run identity mismatch')
        plan_key = result['plan_key']
        match = GC_RE.fullmatch(plan_key) if isinstance(plan_key, str) else None
        if not match or match[1] != 'plans' or plan_key not in items:
            raise ValueError('GC result plan evidence is missing')
        raw = store.read(plan_key, items[plan_key])
        value = validate_plan(decode(raw))
        if (value['run_id'] != match[2] or value['run_id'] == result['run_id']
                or hashlib.sha256(raw).hexdigest() != result['plan_sha256']
                or result['created_at'] - max(value['created_at'], items[plan_key]['mtime']) < MARK_DELAY):
            raise ValueError('GC result plan identity or age mismatch')
        attempt_key = f"gc/attempts/{result['run_id']}.json"
        if attempt_key not in items:
            raise ValueError('GC result attempt evidence is missing')
        attempt = {k: v for k, v in result.items() if k != 'error_type'}
        attempt.update(status='started', events=[])
        if decode(store.read(attempt_key, items[attempt_key])) != attempt:
            raise ValueError('GC result differs from immutable attempt')
        cancelled = result['cancelled_manifests']
        if (not isinstance(cancelled, list) or any(not isinstance(k, str) for k in cancelled)
                or len(set(cancelled)) != len(cancelled) or not set(cancelled) <= value['manifests'].keys()
                or not isinstance(result['events'], list)):
            raise ValueError('invalid GC result events or cancellations')
        seen = set()
        for event in result['events']:
            if not isinstance(event, dict) or not isinstance(event.get('key'), str):
                raise ValueError('invalid GC result event')
            target, status = event['key'], event.get('status')
            expected_fields = {'key', 'status'}
            if status in {'deleted', 'present', 'unknown'}:
                expected_fields.add('readback')
            if (set(event) != expected_fields or target in seen or target in cancelled
                    or target not in (value['manifests'].keys() | value['objects'].keys())
                    or status not in {'deleted', 'present', 'unknown', 'absent',
                                      'retained-reference', 'retained-changed'}):
                raise ValueError('invalid GC result event binding')
            seen.add(target)
            if status == 'deleted' and event['readback'] is not None:
                raise ValueError('GC deleted event lacks absent readback')
            if status in {'unknown', 'present'} and result['status'] != 'stopped':
                raise ValueError('GC unknown result cannot be complete')
        yield result, value


def explained_retirements(store, items, missing):
    explained = set()
    for result, value in verified_results(store, items):
        for event in result['events']:
            key = event['key']
            entry = value['manifests'].get(key)
            if (key in missing and entry is not None and entry['meta'] == missing[key]
                    and event['status'] in {'deleted', 'unknown'} and event['readback'] is None):
                explained.add(key)
    return explained


def coordinated(declared):
    if declared is not True:
        raise ValueError('apply requires --known-writers-coordinated: all stage/promotions/adopt/GC '
                         'writers in same-repository daily-sync; unknown writers prohibit apply')


def create_once(store, key, value):
    raw = canonical(value)
    if len(raw) > MAX_JSON:
        raise ValueError('GC metadata exceeds read cap')
    try:
        store.create(key, raw)
    except Exception:
        # Inspect once even if the write succeeded; stop, never replay it.
        store.read(key)
        raise
    if store.read(key) != raw:
        raise ValueError('GC metadata readback mismatch')


def unused_run(store, run_id):
    run_identity(run_id)
    for prefix in ('plans', 'attempts', 'results'):
        if store.head(f'gc/{prefix}/{run_id}.json') is not None:
            raise ValueError('same run/rerun cannot apply GC again')


def mark(store, *, run_id, now, run_attempt='1', known_writers_coordinated=False):
    coordinated(known_writers_coordinated)
    unused_run(store, run_id)
    value = validate_plan(plan(store, run_id=run_id, now=now, run_attempt=run_attempt))
    if not value['manifests'] and not value['objects']:
        return {'status': 'skip', 'reason': 'no eligible retired generations or old orphans within budget', 'plan': value}
    key = f'gc/plans/{run_id}.json'
    create_once(store, key, value)
    return {'status': 'marked', 'plan_key': key, 'plan': value}


def delete_once(store, item, events, observation):
    key = item['key']
    observed = store.head(key)
    if observed is None:
        events.append({'key': key, 'status': 'absent'})
        return
    if observed != item:
        raise ValueError('delete identity changed: ' + key)
    journal.verify_unchanged(observation, journal.inspect(JournalView(store)))
    try:
        store.delete(item)
    except Exception:
        # One conditional request only. Unknown result always stops this run,
        # even when readback proves absence. A new run rebuilds the full graph.
        after = store.head(key)
        events.append({'key': key, 'status': 'unknown', 'readback': after})
        raise
    after = store.head(key)
    events.append({'key': key, 'status': 'deleted' if after is None else 'present', 'readback': after})
    if after is not None:
        raise ValueError('conditional delete readback still present: ' + key)


def sweep(store, *, plan_key=None, run_id, now, run_attempt='1', known_writers_coordinated=False):
    coordinated(known_writers_coordinated)
    unused_run(store, run_id)
    run_identity(run_attempt)
    if plan_key is None:
        plan_key = oldest_plan(store, run_id=run_id, now=now)
        if plan_key is None:
            graph(store, now)
            return {'status': 'skip', 'reason': 'no eligible marked plan'}
    match = GC_RE.fullmatch(plan_key)
    if not match or match[1] != 'plans':
        raise ValueError('sweep requires an immutable GC plan key')
    plan_meta = store.head(plan_key)
    value = validate_plan(decode(store.read(plan_key, plan_meta)))
    if value['run_id'] != match[2] or value['run_id'] == run_id:
        raise ValueError('same run/rerun cannot sweep its mark')
    timestamp(now)
    if plan_meta is None or now - max(value['created_at'], plan_meta['mtime']) < MARK_DELAY:
        raise ValueError('mark must be at least 24h old')
    current = graph(store, now)
    unexpected = set(value['manifest_inventory']) - set(current['manifests']) - set(value['manifests'])
    if unexpected:
        missing = {key: value['manifest_inventory'][key] for key in unexpected}
        unexpected -= explained_retirements(store, current['items'], missing)
        if unexpected:
            raise ValueError('unexpected missing manifest outside planned retirement')
    approved = {}
    for key, entry in value['manifests'].items():
        if key not in current['manifests']:
            # Saved manifest bytes/refs permit recovery, not permanent retention.
            continue
        if (key not in current['protected'] and current['manifests'][key] == entry
                and now - entry['meta']['mtime'] >= RETENTION):
            approved[key] = entry['meta']
    result = {'version': 1, 'kind': 'search-gc-result', 'run_id': run_id, 'plan_key': plan_key,
              'plan_sha256': hashlib.sha256(canonical(value)).hexdigest(), 'created_at': now,
              'run_attempt': run_attempt,
              'status': 'started', 'events': [], 'cancelled_manifests': sorted(
                  set(value['manifests']) & set(current['manifests']) - set(approved)),
              'known_writers_declared_coordinated': True, 'global_lock': False}
    create_once(store, f'gc/attempts/{run_id}.json', result)
    try:
        for item in approved.values():
            delete_once(store, item, result['events'], current['journal'])
        # Re-list ALL manifests after retirement, including future/new references.
        after = graph(store, now)
        journal.verify_unchanged(current['journal'], after['journal'])
        unexpected = set(current['manifests']) - set(after['manifests']) - set(approved)
        if unexpected:
            raise ValueError('unexpected missing manifest during sweep')
        remaining_refs = {ref for refs in after['refs'].values() for ref in refs}
        for key, item in value['objects'].items():
            if key in remaining_refs:
                result['events'].append({'key': key, 'status': 'retained-reference'})
                continue
            observed = store.head(key)
            if observed is not None and observed != item:
                result['events'].append({'key': key, 'status': 'retained-changed'})
                continue
            delete_once(store, item, result['events'], current['journal'])
        journal.verify_unchanged(current['journal'], journal.inspect(JournalView(store)))
        result['status'] = 'complete'
    except Exception as error:
        result['status'] = 'stopped'
        result['error_type'] = type(error).__name__
        create_once(store, f'gc/results/{run_id}.json', result)
        raise
    create_once(store, f'gc/results/{run_id}.json', result)
    return result


def oldest_plan(store, *, run_id, now):
    items = inventory(store)
    finished = set()
    for result, _value in verified_results(store, items):
        if result['status'] == 'complete':
            finished.add(result['plan_key'])
    for key in sorted(items, key=lambda k: (items[k]['mtime'], k)):
        if key.startswith('gc/plans/'):
            value = validate_plan(decode(store.read(key, items[key])))
            if (key not in finished and value['run_id'] != run_id
                    and now - max(value['created_at'], items[key]['mtime']) >= MARK_DELAY):
                return key
    return None


def auto(store, *, run_id, now, run_attempt='1', known_writers_coordinated=False):
    """At most one old batch OR one new mark per invocation."""
    coordinated(known_writers_coordinated)
    unused_run(store, run_id)
    key = oldest_plan(store, run_id=run_id, now=now)
    kwargs = dict(run_id=run_id, now=now, run_attempt=run_attempt, known_writers_coordinated=True)
    if key is not None:
        return sweep(store, plan_key=key, **kwargs)
    return mark(store, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('preview', 'mark', 'sweep', 'auto'), default='preview')
    parser.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID'),
                        help='stable numeric GITHUB_RUN_ID; never include run_attempt')
    parser.add_argument('--run-attempt', default=os.environ.get('GITHUB_RUN_ATTEMPT', '1'),
                        help='audit only; reruns share run-id and cannot sweep their own mark')
    parser.add_argument('--output', type=Path, help='write local JSON report (must be a new file)')
    parser.add_argument('--plan-key', help='sweep this plan; otherwise oldest eligible plan')
    parser.add_argument('--known-writers-coordinated', action='store_true', help=(
        'declare all stage/promotions/adopt/GC writers share same-repository daily-sync '
        '(cancel-in-progress false); other known writers stopped/coordinated; unknown writers prohibit apply; '
        'this is not a global lock'))
    args = parser.parse_args(argv)
    if args.mode != 'preview':
        coordinated(args.known_writers_coordinated)
        run_identity(args.run_id)
    run_identity(args.run_attempt)
    if args.mode != 'sweep' and args.plan_key is not None:
        raise ValueError('--plan-key is allowed only for sweep')
    if args.output and (args.output.exists() or args.output.is_symlink()):
        raise ValueError('--output must be a new file')
    try:
        from .search_gc_s3 import S3SearchGCStore
    except ImportError:
        from search_gc_s3 import S3SearchGCStore
    store = S3SearchGCStore()
    kwargs = {'run_id': args.run_id or '1', 'run_attempt': args.run_attempt, 'now': time.time()}
    if args.mode == 'preview':
        result = {'status': 'preview', 'remote_writes': 0, 'plan': plan(store, **kwargs)}
    else:
        kwargs['known_writers_coordinated'] = args.known_writers_coordinated
        if args.mode == 'sweep':
            kwargs['plan_key'] = args.plan_key
        result = {'mark': mark, 'sweep': sweep, 'auto': auto}[args.mode](store, **kwargs)
    result.update({'run_id': kwargs['run_id'], 'run_attempt': args.run_attempt, 'mode': args.mode})
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf8') as output:
            output.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + '\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
