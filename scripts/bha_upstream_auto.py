"""Bounded BHA upstream normalization and publication, one archive per invocation.

Inspect is read-only. Auto uses BHA-owned CAS receipts before branch writes;
unknown writes are observed, never replayed. Only main/config fast-forwards are
eligible. Existing local corrections are retained; changed OCR inputs and the
planner's inspect-upstream-parsed path require separate review. Baseline/main
publication is deliberately deferred to bha_baseline_publish at a Reader-safe
boundary. A retained handoff blocks new work until its exact pair is adopted.
The handoff contains the full candidate pair and publication.json; validate its
BHA content address before feeding publication.json and its SHA-256 to the
existing baseline publisher. The manual baseline workflow's older provenance
allowlist does not accept this workflow's artifacts automatically.

Deploy with the maintain-archive-forks workflow concurrency group. CAS protects
cooperating callers, not external writers. No workflow artifact is a lock.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from . import bha_build_plan as planning, bha_fork_safe_sync as sync
from . import bha_input_monitor as monitor, bha_parsed_publish as publication
from . import bha_parsed_baseline as baseline, bha_reindex_journal as journal
from . import bha_reindex_run as delivery, build_fork_parsed as builder
from . import check_bha_reindex_access as access, search_snapshot
from .reader_page_cas import strong_etag

ROOT = 'bha-upstream-auto/'
HEAD = ROOT + 'head.json'
MAX_JSON = 4 * 1024 * 1024
REGULAR = ('main', 'config')
STAGES = {'syncing', 'sync-intent', 'building', 'publish-intent', 'published',
          'baseline-deferred', 'equal-tree-deferred', 'complete'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON:
        raise ValueError('bounded receipt required')
    value = json.loads(raw)
    if not isinstance(value, dict) or search_snapshot.canonical(value) != raw:
        raise ValueError('canonical receipt required')
    return value


class S3Store:
    """Single-attempt writes in a separate namespace of the existing BHA bucket."""
    def __init__(self):
        self.backend = journal.S3Store()  # config disables SDK write retries

    @staticmethod
    def key(key):
        if key != HEAD and not re.fullmatch(ROOT + r'objects/[0-9a-f]{64}\.json', key):
            raise ValueError('upstream receipt key outside allowlist')

    def get(self, key):
        self.key(key)
        client, bucket = self.backend._client()
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None, None
            raise
        body = response['Body']
        try:
            size = response.get('ContentLength')
            if type(size) is not int or not 0 < size <= MAX_JSON:
                raise ValueError('upstream receipt size invalid')
            raw = body.read(size + 1)
        finally:
            body.close()
        if len(raw) != size:
            raise ValueError('upstream receipt truncated')
        return raw, strong_etag(response.get('ETag'))

    def put(self, key, raw, etag=None):
        self.key(key)
        decode(raw)
        client, bucket = self.backend._client()
        condition = {'IfNoneMatch': '*'} if etag is None else {'IfMatch': strong_etag(etag)}
        client.put_object(Bucket=bucket, Key=key, Body=raw, ContentType='application/json', **condition)


class Receipts:
    """Content-addressed evidence and a CAS pointer. Readback never retries PUT."""
    def __init__(self, store):
        self.store = store
        raw, self.etag = store.get(HEAD)
        self.state = None
        if raw is None:
            if self.etag is not None:
                raise ValueError('absent head has ETag')
        else:
            strong_etag(self.etag)
            self.state = self.read(decode(raw))
            if (self.state.get('kind') != 'bha-upstream-auto-state' or self.state.get('version') != 1
                    or self.state.get('stage') not in STAGES
                    or type(self.state.get('archive')) is not int or not 0 <= self.state['archive'] < 32):
                raise ValueError('invalid upstream state')

    def read(self, ref):
        if (not isinstance(ref, dict) or set(ref) != {'bucket', 'key', 'sha256', 'bytes'}
                or ref['bucket'] != journal.BUCKET
                or not re.fullmatch(r'[0-9a-f]{64}', str(ref['sha256']))
                or ref['key'] != ROOT + 'objects/' + ref['sha256'] + '.json'):
            raise ValueError('receipt provenance differs')
        raw, _ = self.store.get(ref['key'])
        if raw is None or len(raw) != ref['bytes'] or digest(raw) != ref['sha256']:
            raise ValueError('exact receipt bytes unavailable')
        return decode(raw)

    def retain(self, value):
        raw = search_snapshot.canonical(value)
        decode(raw)
        ref = {'bucket': journal.BUCKET, 'key': ROOT + 'objects/' + digest(raw) + '.json',
               'sha256': digest(raw), 'bytes': len(raw)}
        prior, _ = self.store.get(ref['key'])
        if prior is None:
            try:
                self.store.put(ref['key'], raw)
            except Exception:
                # One read resolves an acknowledged or lost response, never a replay.
                if self.store.get(ref['key'])[0] != raw:
                    raise RuntimeError('receipt write unknown; stop') from None
        if self.read(ref) != value:
            raise ValueError('receipt readback differs')
        return ref

    def save(self, state):
        # A unique transition token distinguishes our lost response from a
        # competing caller that attempted the same semantic transition.
        state['transition'] = uuid.uuid4().hex
        ref = self.retain(state)
        raw = search_snapshot.canonical(ref)
        try:
            self.store.put(HEAD, raw, self.etag)
        except Exception:
            actual, etag = self.store.get(HEAD)
            if actual != raw:
                raise RuntimeError('upstream CAS outcome unknown or conflicting; stop') from None
        else:
            actual, etag = self.store.get(HEAD)
        if actual != raw:
            raise RuntimeError('upstream head readback differs')
        self.etag = strong_etag(etag)
        self.state = copy.deepcopy(state)
        return ref


def choose(report, inspect_branch):
    """Find the first eligible archive, preserving ahead corrections explicitly.

    Inspect every regular mismatch before selecting work. A diverged fork is
    blocked even if the built mirror inputs have not changed. OCR branch heads
    may differ from upstream (existing corrections) but are never synchronized.
    """
    choices, blocked, preserved = [], [], []
    for row in report['archives']:
        archive = row['archive']
        plans = []
        reasons = []
        for branch in REGULAR:
            if row['mirror_inputs'][branch] == row['upstream_inputs'][branch]:
                continue
            plan = inspect_branch(archive, branch)
            if (plan['archive'] != archive or plan['branch'] != branch
                    or plan['mirror'] != row['mirror_inputs'][branch]
                    or plan['upstream'] != row['upstream_inputs'][branch]
                    or plan['mirror_repository_id'] != row['mirror_repository_id']
                    or plan['upstream_repository_id'] != row['upstream_repository_id']):
                raise ValueError('branch plan differs from observed inputs')
            if plan['mirror_relation'] == 'behind' and plan['can_fast_forward'] is True:
                plans.append(plan)
            elif plan['mirror_relation'] == 'ahead':
                preserved.append({'archive': archive, 'branch': branch})
            else:
                reasons.append('diverged-or-unknown-' + branch)
        if reasons:
            blocked.append({'archive': archive, 'reasons': reasons})
        elif plans or row['input_changed']:
            choices.append((archive, plans))
    return choices, blocked, preserved


def eligible(plan, branches):
    if plan['parsed_tree_changed']:
        return 'parsed-tree-drift'
    # Only regular source edits may trigger this slice; existing cache and patch
    # contents can be read by build_parsed, never regenerated or synchronized.
    for key in ('ocr_cache', 'ocr_patch', 'helper', 'patch_layout'):
        if plan['current_inputs'][key] != plan['recorded_inputs'][key]:
            return 'non-regular-input-change'
    snapshot = plan['snapshot']
    expected = copy.deepcopy(snapshot['mirror_branches'])
    for branch in branches:
        name = branch['branch']
        if (branch['archive'] != plan['archive'] or name not in REGULAR
                or snapshot['mirror_branches'][name] != branch['mirror']
                or snapshot['upstream_branches'][name] != branch['upstream']
                or any(snapshot[k] != branch[k] for k in ('mirror_repository_id', 'upstream_repository_id'))):
            return 'branch-plan-drift'
        expected[name] = branch['upstream']
    if not builder.needs_local_build(expected, plan['snapshot']['upstream_branches']):
        return 'upstream-parsed-review-required'
    if not branches and plan['action'] != 'build-candidate':
        return 'build-plan-review-required'
    return None


def published_receipt(value):
    plan, candidate = value['plan'], value['candidate']
    return {'kind': 'bha-parsed-publication-result', 'status': 'published', 'archive': plan['archive'],
            'commit': candidate['commit'], 'tree': candidate['tree'], 'plan_sha256': plan['plan_sha256'],
            'built_inputs': plan['current_inputs'], 'previous_inputs': plan['recorded_inputs'],
            'previous_tree': plan['recorded_parsed_tree'], 'source_snapshot': plan['snapshot'],
            'consumer_verified': False, 'baseline_updated': False}


def published_snapshot(proof):
    expected = copy.deepcopy(proof['source_snapshot'])
    expected['mirror_branches']['parsed'] = proof['commit']
    expected['parsed_tree'] = proof['tree']
    return expected


def run(*, action, receipts, ops, provenance):
    """Injectable orchestration. All write boundaries have durable intent first."""
    if action not in {'inspect', 'auto'}:
        raise ValueError('invalid action')
    state = copy.deepcopy(receipts.state)
    base = {'kind': 'bha-upstream-auto-result', 'version': 1, 'action': action,
            'baseline_published': False, 'external_writers_verified': False,
            'scope': 'regular main/config inputs; no OCR production or upstream parsed replacement',
            'consumer_verified': False}

    def result(status, **extra):
        return {**base, 'status': status, **extra}

    def save(stage, **extra):
        state.update(stage=stage, **extra)
        receipts.save(state)

    if state and state['stage'] != 'complete':
        if action == 'inspect':
            return result('pending-receipt', archive=state['archive'], stage=state['stage'], remote_writes=0)
        if state['stage'] in {'baseline-deferred', 'equal-tree-deferred'}:
            proof = receipts.read(state['publication']) if 'publication' in state else None
            handoff = receipts.read(state['handoff'])
            if ops.adopted(handoff):
                save('complete')
                return result('baseline-adopted', archive=state['archive'], baseline_published=False)
            # Re-export exact retained bytes, no rebuild, reindex or new baseline.
            ops.export('handoff.json', handoff)
            if proof:
                ops.export('publication.json', proof)
            return result(state['stage'], archive=state['archive'], handoff=state['handoff'],
                          remote_writes=0, reused=True)
        if state['stage'] == 'building':
            return result('blocked', archive=state['archive'], reason='interrupted-build-review-required', remote_writes=0)
    else:
        report = ops.monitor()
        choices, blocked, preserved = choose(report, ops.branch)
        if not choices:
            return result('blocked' if blocked else 'no-change', blocked=blocked,
                          preserved_corrections=preserved, remote_writes=0, build_requested=False)
        archive, branches = choices[0]
        plan = ops.plan(archive)
        reason = eligible(plan, branches)
        if reason:
            return result('blocked', archive=archive, reason=reason, remote_writes=0, blocked=blocked)
        if action == 'inspect':
            return result('eligible', archive=archive, plan=plan, fast_forwards=branches,
                          remote_writes=0, blocked=blocked)
        state = {'version': 1, 'kind': 'bha-upstream-auto-state', 'archive': archive,
                 'provenance': provenance, 'initial_plan': plan, 'branches': branches,
                 'next_branch': 0, 'stage': 'syncing'}
        receipts.save(state)

    archive = state['archive']
    if state['stage'] == 'sync-intent':
        old = state['branches'][state['next_branch']]
        observed = ops.branch(archive, old['branch'])
        if (observed['mirror'] != old['upstream'] or observed['upstream'] != old['upstream']
                or any(observed[k] != old[k] for k in ('mirror_repository_id', 'upstream_repository_id'))):
            return result('blocked', archive=archive, reason='sync-outcome-unknown-no-replay', remote_writes=0)
        save('syncing', next_branch=state['next_branch'] + 1)
    if state['stage'] == 'syncing':
        while state['next_branch'] < len(state['branches']):
            branch = state['branches'][state['next_branch']]
            save('sync-intent')
            ops.sync(branch)
            # Even a helper success must be read back against the exact target.
            observed = ops.branch(archive, branch['branch'])
            if (observed['mirror'] != branch['upstream'] or observed['upstream'] != branch['upstream']
                    or any(observed[k] != branch[k] for k in ('mirror_repository_id', 'upstream_repository_id'))):
                raise ValueError('fast-forward readback differs')
            save('syncing', next_branch=state['next_branch'] + 1)
        plan = ops.plan(archive)
        expected = copy.deepcopy(state['initial_plan']['snapshot'])
        for branch in state['branches']:
            expected['mirror_branches'][branch['branch']] = branch['upstream']
        if (plan['snapshot'] != expected or plan['action'] != 'build-candidate'
                or plan['recorded_inputs'] != state['initial_plan']['recorded_inputs']
                or plan['recorded_parsed_tree'] != state['initial_plan']['recorded_parsed_tree']):
            return result('blocked', archive=archive, reason='normalized-plan-drift', remote_writes=None)
        save('building', plan=receipts.retain(plan))
        value = ops.build(plan)
        candidate_ref = receipts.retain(value)
        if value['status'] == 'no-change':
            # No publication means no exact index operation for baseline.prepare.
            # Keep the build receipt rather than repeating this equal-tree build.
            handoff = {'kind': 'bha-upstream-equal-tree-handoff', 'archive': archive,
                       'built_inputs': plan['current_inputs'], 'tree': plan['snapshot']['parsed_tree'],
                       'candidate': candidate_ref, 'baseline_published': False}
            save('equal-tree-deferred', handoff=receipts.retain(handoff))
            ops.export('handoff.json', handoff)
            return result('equal-tree-deferred', archive=archive, handoff=state['handoff'])
        if value['status'] != 'candidate-ready' or value.get('plan') != plan:
            raise ValueError('candidate differs from exact normalized plan')
        # The bundle is verified by the existing publisher before this callback.
        # Persist full receipt + artifact digest before entering any push code.
        save('publish-intent', candidate=candidate_ref)
        proof = ops.publish(value, lambda checkpoint: receipts.retain(checkpoint))
        if proof != published_receipt(value):
            raise ValueError('publication receipt differs from candidate')
        save('published', publication=receipts.retain(proof))
    elif state['stage'] == 'publish-intent':
        value = receipts.read(state['candidate'])
        proof = published_receipt(value)
        # Lost publication response: recover only exact commit/tree AND all input
        # and repository identities. A preimage still present is never replayed.
        if ops.snapshot(archive) != published_snapshot(proof):
            return result('blocked', archive=archive, reason='publication-outcome-unknown-no-replay', remote_writes=0)
        save('published', publication=receipts.retain(proof))
    if state['stage'] == 'published':
        proof = receipts.read(state['publication'])
        if ops.snapshot(archive) != published_snapshot(proof):
            return result('blocked', archive=archive, reason='published-source-drift', remote_writes=0)
        outcome = ops.deliver(proof)
        if not outcome.get('ok') or outcome['status'] not in {'verified', 'no-change'}:
            return result('delivery-pending', archive=archive, delivery=outcome)
        handoff = ops.baseline(proof)  # exact audited operation, samples, snapshot
        save('baseline-deferred', handoff=receipts.retain(handoff), delivery=receipts.retain(outcome))
        ops.export('publication.json', proof)
        ops.export('handoff.json', handoff)
        return result('baseline-deferred', archive=archive, handoff=state['handoff'],
                      consumer_verified=True, reason='main-commit-requires-reader-safe-handoff')
    raise ValueError('unsupported orchestration state')


class Operations:
    def __init__(self, token, output, inputs, trees, samples):
        self.token, self.output = token, output
        self.inputs, self.trees, self.samples = inputs, trees, samples

    def export(self, name, value):
        path = self.output / name
        raw = search_snapshot.canonical(value)
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError('local artifact already differs')
            return
        path.write_bytes(raw)

    def monitor(self):
        return monitor.inspect(self.token, self.inputs)

    def branch(self, archive, branch):
        return sync.inspect(self.token, archive=archive, branch=branch)

    def sync(self, plan):
        return sync.apply(self.token, plan, confirmed=True)

    def plan(self, archive):
        return planning.inspect(self.token, archive, input_path=self.inputs, tree_path=self.trees)

    def snapshot(self, archive):
        return planning._snapshot(self.token, archive)

    def build(self, plan):
        self.export('build-plan.json', plan)
        # Suppress helper subprocess output: inherited third-party diagnostics
        # are not safe audit messages. The exact candidate JSON remains visible.
        command = [sys.executable, '-m', 'scripts.bha_parsed_candidate', '--plan',
                   str(self.output / 'build-plan.json'), '--plan-sha256',
                   digest((self.output / 'build-plan.json').read_bytes()), '--output',
                   str(self.output / 'candidate.json'), '--bundle', str(self.output / 'parsed.bundle'),
                   '--inputs', str(self.inputs), '--trees', str(self.trees)]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return json.loads((self.output / 'candidate.json').read_bytes())

    def publish(self, value, checkpoint):
        path = self.output / 'candidate.json'
        bundle = self.output / 'parsed.bundle'
        reviewed = publication.read_candidate(path, digest(path.read_bytes()), bundle)
        if reviewed != value:
            raise ValueError('candidate artifact changed before publication')
        def restore(root, candidate):
            from . import bha_parsed_content
            repo = publication.restore_bundle(root, candidate, bundle, self.token)
            actual = bha_parsed_content.inspect(repo, candidate['before_tree'], candidate['tree'],
                                                candidate['archive'], builder.clean_environment())
            if actual != candidate.get('content_review'):
                raise ValueError('bundle content review differs')
        # Git output is suppressed by the workflow; only runner diagnostics are
        # exported. The publisher still performs its exact lease and rechecks.
        with tempfile.TemporaryDirectory(prefix='bha-upstream-publish-') as directory:
            return publication.publish_reviewed(value, inspect_live=lambda: self.plan(value['plan']['archive']),
                restore=restore, publish=lambda root, candidate, record: builder.publish_candidate(
                    self.token, root, candidate, checkpoint=record), workspace=Path(directory), checkpoint=checkpoint)

    def deliver(self, proof):
        from . import bha_empty_coverage as empty
        token = os.environ.get('BHA_REINDEX_TOKEN', '')
        selected = json.loads(self.samples.read_bytes())['samples']
        store = journal.S3Store()
        current = journal.load(store)
        if current is not None:
            journal.audit(store, current)
            if current.record['state'] not in {'verified', 'abandoned'}:
                # Do not reconcile some unrelated pending operation on behalf of
                # this publication, nor start a multi-archive update here.
                record = current.record
                key = str(proof['archive'])
                if (record['changed_archives'] != [proof['archive']]
                        or record['archive_commits'][key] != proof['commit']
                        or record['archive_revisions'][key] != proof['tree']):
                    raise ValueError('unrelated index operation requires separate recovery')
        def collect():
            plan = delivery.inspect_plan_with_samples(self.token, token, self.trees, selected_samples=selected)
            if plan['changed_archives'] not in ([], [proof['archive']]):
                raise ValueError('index update exceeds one archive')
            key = str(proof['archive'])
            if plan['archive_commits'][key] != proof['commit'] or plan['archive_revisions'][key] != proof['tree']:
                raise ValueError('index target differs from publication')
            missing = plan['post_index_sample_missing_archives']
            if missing:
                plan['empty_source_proofs'] = [empty.source(n, plan['archive_commits'][str(n)],
                    plan['archive_revisions'][str(n)], self.token) for n in missing]
                plan['post_index_sample_missing_archives'] = []
                plan['plan_sha256'] = digest(search_snapshot.canonical({k: v for k, v in plan.items() if k != 'plan_sha256'}))
            return plan
        def empty_proofs(record):
            missing = sorted(set(record['changed_archives']) - {row['archive'] for row in selected})
            return empty.observe(record, missing, self.token, token) if missing else []
        return delivery.run(action='auto', store=store, collect_plan=collect, load_reviewed=lambda: None,
                            selected=selected, token=token, timeout_seconds=3600, empty_proofs=empty_proofs)

    def baseline(self, proof):
        return baseline.prepare(proof, json.loads(self.inputs.read_bytes()), json.loads(self.trees.read_bytes()),
            journal.S3Store(), inspect_source=lambda: self.snapshot(proof['archive']),
            observe_consumer=lambda: access.read_status(os.environ.get('BHA_REINDEX_TOKEN', '')))

    def adopted(self, handoff):
        inputs = json.loads(self.inputs.read_bytes())['archives']
        trees = json.loads(self.trees.read_bytes())
        key = str(handoff['archive'])
        if handoff['kind'] == 'bha-parsed-baseline-candidate':
            return inputs[key] == handoff['inputs']['archives'][key] and trees[key] == handoff['trees'][key]
        if handoff['kind'] != 'bha-upstream-equal-tree-handoff':
            raise ValueError('unknown baseline handoff kind')
        return inputs[key] == handoff['built_inputs'] and trees[key] == handoff['tree']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('inspect', 'auto'), default='inspect')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=Path('state/bha-parsed-inputs.json'))
    parser.add_argument('--trees', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--samples', type=Path, default=Path('config/bha-delivery-samples.json'))
    args = parser.parse_args(argv)
    if args.output_dir.exists() or args.output_dir.is_symlink():
        parser.error('output directory must be new')
    args.output_dir.mkdir(parents=True)
    try:
        if args.action == 'auto' and (os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
                or os.environ.get('GITHUB_REF') != 'refs/heads/main'
                or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'):
            raise ValueError('auto requires first central main workflow attempt')
        token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
        if not token:
            raise ValueError('source credential unavailable')
        ops = Operations(token, args.output_dir.resolve(), args.inputs, args.trees, args.samples)
        receipts = Receipts(S3Store())
        result = run(action=args.action, receipts=receipts, ops=ops,
                     provenance={'repository': os.environ.get('GITHUB_REPOSITORY'),
                                 'sha': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID')})
    except Exception as error:
        result = {'kind': 'bha-upstream-auto-result', 'status': 'error', 'error_type': type(error).__name__,
                  'recovery': 'inspect durable upstream receipt and index journal; do not replay unknown writes',
                  'baseline_published': False}
    (args.output_dir / 'result.json').write_bytes(search_snapshot.canonical(result))
    print(json.dumps({'status': result['status'], 'baseline_published': False}))
    return 1 if result['status'] in {'error', 'blocked', 'delivery-pending'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
