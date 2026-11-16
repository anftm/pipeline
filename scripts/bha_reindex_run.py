"""Incremental BHA auto/plan/apply/reconcile runner.

Auto reconciles pending work first, or retains a fresh changed plan in BHA
storage before preparation. For manual apply, --plan-sha256 binds the exact
downloaded file bytes. A claimed request is never posted again. Recovery uses
the existing journal target, even when source heads have since moved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time

try:
    from . import bha_consumer_sample as consumer
    from . import bha_reindex_delivery as delivery
    from . import bha_reindex_journal as journal
    from . import bha_reindex_plan as planning
    from . import bha_snapshot_inspection as snapshot
    from . import search_snapshot
except ImportError:
    import bha_consumer_sample as consumer
    import bha_reindex_delivery as delivery
    import bha_reindex_journal as journal
    import bha_reindex_plan as planning
    import bha_snapshot_inspection as snapshot
    import search_snapshot


class RunError(ValueError):
    """A runner-owned diagnostic safe to include in an audit artifact."""


def read_json(path: Path) -> tuple[bytes, dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > planning.MAX_STATE:
        raise RunError('input must be a bounded regular JSON file')
    raw = path.read_bytes()
    if not 0 < len(raw) <= planning.MAX_STATE:
        raise RunError('input JSON size is invalid')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RunError('input JSON must be an object')
    return raw, value


def validate_samples(selected):
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 32
            or any(not isinstance(row, dict) or set(row) != {'archive', 'path'} for row in selected)):
        raise RunError('one to thirty-two explicit samples are required')
    for row in selected:
        consumer.sample_path(row['archive'], 'a' * 40, row['path'])
    if len({(row['archive'], row['path']) for row in selected}) != len(selected):
        raise RunError('duplicate sample selection')


def validate_plan(value):
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('kind') != 'bha-reindex-readonly-plan'
            or value.get('remote_writes') != 0 or value.get('publication_ready') is not False):
        raise RunError('invalid read-only reindex plan')
    body = {key: item for key, item in value.items() if key != 'plan_sha256'}
    if hashlib.sha256(search_snapshot.canonical(body)).hexdigest() != value.get('plan_sha256'):
        raise RunError('plan content digest differs')
    for key in ('archive_commits', 'archive_revisions', 'consumer_revisions'):
        planning._trees(value.get(key), key)
    changed = [int(key) for key in sorted(planning.KEYS, key=int)
               if value['archive_revisions'][key] != value['consumer_revisions'][key]]
    if (value.get('changed_archives') != changed
            or any(type(item) is not int for item in value['changed_archives'])):
        raise RunError('plan changed archives differ from its revisions')


def inspect_plan_with_samples(github_token, token, baseline, *, selected_samples):
    """Combine at most two bounded inspections of the same complete target.

    Each inspection checks all 32 heads, status and aggregate health before
    and after its samples. Comparing every non-sample field also binds both
    chunks to the same captured identities and health semantics. This remains
    observational evidence, not a fence against external writers or ABA.
    """
    validate_samples(selected_samples)
    first = planning.inspect(github_token, token, baseline, selected_samples=selected_samples[:16])
    if len(selected_samples) <= 16:
        return first
    second = planning.inspect(github_token, token, baseline, selected_samples=selected_samples[16:])
    validate_plan(first)
    validate_plan(second)
    per_chunk = {'samples', 'sample_count', 'deferred_samples',
                 'post_index_sample_missing_archives', 'plan_sha256'}
    fixed = lambda value: {key: item for key, item in value.items() if key not in per_chunk}
    if fixed(first) != fixed(second):
        raise RunError('BHA source, consumer or health changed between sample chunks')
    combined = {**first, 'samples': first['samples'] + second['samples'],
                'deferred_samples': first['deferred_samples'] + second['deferred_samples']}
    combined['sample_count'] = len(combined['samples'])
    combined['post_index_sample_missing_archives'] = sorted(
        set(combined['changed_archives']) - {row['archive'] for row in selected_samples})
    body = {key: value for key, value in combined.items() if key != 'plan_sha256'}
    combined['plan_sha256'] = hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    return combined


def reviewed_plan(path: Path | None, digest: str | None) -> dict:
    if path is None or not isinstance(digest, str) or not journal.SHA.fullmatch(digest):
        raise RunError('apply requires --plan and --plan-sha256 of the reviewed file bytes')
    raw, value = read_json(path)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RunError('reviewed plan file SHA-256 differs')
    validate_plan(value)
    return value


def require_coverage(changed, selected, plan=None):
    missing = sorted(set(changed) - {row['archive'] for row in selected})
    declared = plan.get('post_index_sample_missing_archives') if plan is not None else []
    if missing or declared != []:
        raise RunError('plan post_index_sample_missing_archives must be empty; all changed archives need samples')


def observe_target(record, selected):
    """Keep the existing aggregate health semantics and sample exact commits."""
    require_coverage(record['changed_archives'], selected)
    before = consumer.health(consumer.get(consumer.BASE + '/api/health', consumer.MAX_HEALTH))
    rows = []
    for selected_row in selected:
        archive = selected_row['archive']
        key = str(archive)
        row = consumer.observe_one(archive, record['archive_commits'][key], selected_row['path'])
        rows.append({**row, 'tree': record['archive_revisions'][key]})
    after = consumer.health(consumer.get(consumer.BASE + '/api/health', consumer.MAX_HEALTH))
    if before != after:
        raise RunError('consumer health changed during target sampling')
    return {'version': 1, 'kind': 'bha-consumer-sample', 'samples': rows,
            'consumer_health_observed_twice': True, 'remote_writes': 0}


def run(*, action, store, collect_plan, load_reviewed, selected, token,
        operation_id=None, reason=None, timeout_seconds=9000, poll_seconds=30,
        emit=lambda value: None, clock=time.monotonic, sleep=time.sleep,
        retain_plan=journal.retain_plan):
    """Run one operation; injectable clock and boundaries permit offline tests."""
    if action not in {'auto', 'plan', 'apply', 'reconcile', 'abandon'}:
        raise RunError('invalid action')
    if action == 'auto' and (operation_id is not None or reason is not None):
        raise RunError('auto rejects --operation-id and --reason')
    if (not math.isfinite(timeout_seconds) or timeout_seconds <= 0
            or not math.isfinite(poll_seconds) or not 0 < poll_seconds <= 30):
        raise RunError('timeout must be positive and polling must be between zero and 30 seconds')
    if operation_id is not None and not journal.ID.fullmatch(operation_id):
        raise RunError('operation ID must be 32 lowercase hexadecimal characters')
    if action != 'abandon':
        validate_samples(selected)
    if reason is not None and action != 'abandon':
        raise RunError('--reason is only valid for abandon')
    report = {'version': 1, 'kind': 'bha-reindex-run', 'action': action, 'mode': 'incremental',
              'operation_id': None, 'reindex_post_attempts': 0, 'external_writers_verified': False}

    def publish(**fields):
        report.update(fields)
        emit(dict(report))
        return dict(report)

    # Inspect durable work before touching a reviewed plan or read-only planner.
    current = journal.load(store)
    if current is not None:
        journal.audit(store, current)
        record = current.record
        publish(operation_id=record['operation'], journal_state=record['state'], status='journal-observed')
        if record['mode'] != 'incremental':
            raise RunError('existing operation is not incremental; separate recovery required')
        if operation_id is not None and operation_id != record['operation']:
            raise RunError('operation ID differs from the current journal')
        if record['state'] == 'failed':
            raise RunError('failed journal blocks further work; explicit investigation required')
    elif operation_id is not None:
        raise RunError('operation ID has no existing journal to resume')

    if action == 'abandon':
        if operation_id is None:
            raise RunError('abandon requires explicit --operation-id')
        if current is None or current.record['state'] != 'prepared':
            raise RunError('only the exact prepared operation can be abandoned')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise RunError('abandon requires a nonempty --reason of at most 512 characters')
        if reason not in journal.ABANDON_REASONS:
            raise RunError('abandon reason must be stale-plan, superseded-plan or operator-cancelled')
        abandoned = journal.abandon_prepared(store, current, reason=reason)
        audited_records = journal.audit(store, abandoned)
        return publish(status='abandoned', ok=True, journal_state=abandoned.record['state'],
                       sequence=abandoned.record['sequence'], reason=reason,
                       journal_audited=True, audited_records=audited_records)

    def poll(record):
        require_coverage(record['changed_archives'], selected)
        deadline = clock() + timeout_seconds
        while True:
            if clock() >= deadline:
                return publish(status='timeout', ok=False, journal_preserved=True)
            observed = delivery.reconcile(
                store, operation=record['operation'], token=token,
                verify_samples=lambda: observe_target(record, selected),
                verify_snapshot=snapshot.inspect)
            state = observed['status']
            journal_state = ('accepted' if state in {'accepted', 'running', 'snapshot-pending', 'snapshot-unverified'}
                             else state if state in {'verified', 'failed'} else report['journal_state'])
            publish(status=state, observation=observed, journal_state=journal_state)
            if observed['status'] in {'verified', 'failed'}:
                return publish(ok=observed['status'] == 'verified', journal_state=observed['status'])
            remaining = deadline - clock()
            if remaining <= 0:
                return publish(status='timeout', ok=False, journal_preserved=True)
            sleep(min(poll_seconds, remaining))

    if action == 'reconcile':
        if operation_id is None:
            raise RunError('reconcile requires explicit --operation-id')
        if current.record['state'] == 'verified':
            return publish(status='verified', ok=True, remote_writes=0)
        if current.record['state'] not in {'claimed', 'accepted'}:
            raise RunError('prepared operation requires apply with explicit --operation-id and reviewed plan')
        return poll(current.record)

    if current is not None and current.record['state'] in {'claimed', 'accepted'}:
        if action == 'plan':
            return publish(status='pending-reconciliation', ok=False, remote_writes=0)
        return poll(current.record)
    if current is not None and current.record['state'] == 'prepared':
        if action != 'apply' or operation_id is None:
            raise RunError('prepared operation requires explicit --operation-id to resume apply')
    elif operation_id is not None:
        raise RunError('operation ID only resumes existing prepared or pending work')

    reviewed = load_reviewed() if action == 'apply' else None
    if reviewed is not None:
        validate_plan(reviewed)
        require_coverage(reviewed['changed_archives'], selected, reviewed)
        if current is not None and current.record['state'] == 'prepared':
            if reviewed['plan_sha256'] != current.record['plan_sha256']:
                raise RunError('reviewed plan differs from the prepared operation')

    retained_digest = None

    def fresh_plan():
        value = collect_plan()
        validate_plan(value)
        if action in {'apply', 'auto'}:
            require_coverage(value['changed_archives'], selected, value)
            if reviewed is not None and value['plan_sha256'] != reviewed['plan_sha256']:
                raise RunError('reviewed plan is stale; collect and review a new plan')
            if (retained_digest is not None
                    and hashlib.sha256(search_snapshot.canonical(value)).hexdigest() != retained_digest):
                raise RunError('retained plan is stale; fresh plan file digest differs')
        return value

    plan = fresh_plan()
    publish(plan=plan, status='planned')
    if action == 'plan':
        return publish(status='planned', ok=True, remote_writes=0)
    if not plan['changed_archives']:
        return publish(status='no-change', ok=True, remote_writes=0)
    if action == 'auto':
        retained_digest = hashlib.sha256(search_snapshot.canonical(plan)).hexdigest()
        artifact = retain_plan(store, plan)
        if artifact != {'bucket': journal.BUCKET, 'key': journal._plan_key(retained_digest),
                        'sha256': retained_digest}:
            raise RunError('durable plan retention did not confirm exact provenance')
        publish(status='plan-retained', plan_artifact=artifact, plan_file_sha256=retained_digest)
    if current is None or current.record['state'] in {'verified', 'abandoned'}:
        current = delivery.prepare(store, plan=plan, mode='incremental', inspect_live=fresh_plan)
    publish(operation_id=current.record['operation'], journal_state='prepared', status='prepared')

    def post_once(credential, *, mode, payload):
        # apply_once has durably claimed before this callback. Persist the ID
        # and attempt marker before any network send, including lost responses.
        publish(status='posting', journal_state='claimed', reindex_post_attempts=1)
        return delivery._post(credential, mode=mode, payload=payload)

    delivery.apply_once(store, operation=current.record['operation'], token=token,
                        inspect_live=fresh_plan, post=post_once)
    publish(status='accepted', journal_state='accepted')
    return poll(current.record)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', required=True, choices=('auto', 'plan', 'apply', 'reconcile', 'abandon'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--plan', type=Path, help='local output for plan/auto; downloaded reviewed input for apply')
    parser.add_argument('--plan-sha256', help='SHA-256 of the exact reviewed plan file bytes')
    parser.add_argument('--operation-id', help='existing operation for reconcile, abandon or explicit prepared resume')
    parser.add_argument('--reason', help='required for abandon: stale-plan, superseded-plan or operator-cancelled')
    parser.add_argument('--samples', type=Path, default=Path('config/bha-consumer-samples.json'))
    parser.add_argument('--baseline', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--poll-seconds', type=float, default=30)
    parser.add_argument('--timeout-seconds', type=float, default=150 * 60)
    args = parser.parse_args(argv)
    if args.action == 'auto' and any(value is not None for value in
                                   (args.plan_sha256, args.operation_id, args.reason)):
        parser.error('auto rejects --plan-sha256, --operation-id and --reason')
    inputs = {args.samples.resolve(), args.baseline.resolve()}
    if args.plan is not None:
        inputs.add(args.plan.resolve())
    if args.output.resolve() in inputs or args.output.exists() or args.output.is_symlink():
        parser.error('--output must be a new path separate from inputs')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep a durable local checkpoint at the result path throughout the run.
    with args.output.open('x', encoding='utf-8') as stream:
        latest = {'version': 1, 'kind': 'bha-reindex-run', 'action': args.action,
                  'status': 'starting', 'operation_id': None, 'reindex_post_attempts': 0}

        def emit(value):
            latest.clear()
            latest.update(value)
            stream.seek(0)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)

        try:
            emit(dict(latest))
            if args.action == 'plan' or args.action == 'auto' and args.plan is not None:
                if (args.plan is None or args.plan.exists() or args.plan.is_symlink()
                        or args.plan.resolve() in {args.samples.resolve(), args.baseline.resolve()}):
                    raise RunError('plan output requires --plan pointing to a new path outside its inputs')
            selected = None
            if args.action != 'abandon':
                _, spec = read_json(args.samples)
                if set(spec) != {'version', 'samples'} or type(spec['version']) is not int or spec['version'] != 1:
                    raise RunError('invalid sample config')
                selected = spec['samples']
            token = os.environ.get('BHA_REINDEX_TOKEN', '')
            github_token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')

            def collect():
                return inspect_plan_with_samples(github_token, token, args.baseline, selected_samples=selected)

            def retain(store, value):
                artifact = journal.retain_plan(store, value)
                if args.plan is not None:
                    args.plan.parent.mkdir(parents=True, exist_ok=True)
                    with args.plan.open('xb') as plan_file:
                        plan_file.write(search_snapshot.canonical(value))
                        plan_file.flush()
                        os.fsync(plan_file.fileno())
                    if hashlib.sha256(args.plan.read_bytes()).hexdigest() != artifact['sha256']:
                        raise RunError('local retained plan readback differs')
                return artifact

            result = run(action=args.action, store=journal.S3Store(), collect_plan=collect,
                         load_reviewed=lambda: reviewed_plan(args.plan, args.plan_sha256),
                         selected=selected, token=token, operation_id=args.operation_id, reason=args.reason,
                         timeout_seconds=args.timeout_seconds, poll_seconds=args.poll_seconds, emit=emit,
                         retain_plan=retain)
            if args.action == 'auto' and args.plan is not None and 'plan' in result:
                if not args.plan.exists():
                    args.plan.parent.mkdir(parents=True, exist_ok=True)
                    with args.plan.open('xb') as plan_file:
                        plan_file.write(search_snapshot.canonical(result['plan']))
                emit({**result, 'plan_file_sha256': hashlib.sha256(args.plan.read_bytes()).hexdigest(),
                      'plan_path': str(args.plan)})
            if args.action == 'plan' and result['ok']:
                args.plan.parent.mkdir(parents=True, exist_ok=True)
                with args.plan.open('x', encoding='utf-8') as plan_file:
                    json.dump(result['plan'], plan_file, ensure_ascii=False, sort_keys=True)
                    plan_file.write('\n')
                emit({**result, 'plan_file_sha256': hashlib.sha256(args.plan.read_bytes()).hexdigest()})
            return 0 if result['ok'] else 1
        except Exception as error:
            # Upstream HTTP/S3 exceptions can contain credentials or request
            # details. Only diagnostics authored by this runner are exported.
            message = str(error) if isinstance(error, RunError) else 'reindex run failed; inspect operation and journal before recovery'
            emit({**latest, 'status': 'error', 'ok': False, 'error': message,
                  'error_type': type(error).__name__, 'journal_preserved': True})
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
