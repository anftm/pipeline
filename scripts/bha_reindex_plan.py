"""Plan BHA incremental and full-index work from stable exact revisions.

Only GET requests are used. This does not call reindex, write a baseline,
authorize a build or prove all indexed bytes. A plan is a historical snapshot;
callers must independently recheck it immediately before any mutation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from . import bha_consumer_sample as samples
    from . import check_bha_reindex_access as access
    from . import check_bha_revisions as revisions
    from . import search_snapshot
except ImportError:
    import bha_consumer_sample as samples
    import check_bha_reindex_access as access
    import check_bha_revisions as revisions
    import search_snapshot


KEYS = {str(number) for number in range(32)}
MAX_STATE = 256 * 1024


def _trees(value, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError('BHA ' + label + ' must contain all 32 archives')
    if any(not isinstance(sha, str) or not samples.SHA.fullmatch(sha) for sha in value.values()):
        raise ValueError('BHA ' + label + ' contains an invalid Git identity')
    return dict(value)


def _job(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('BHA reindex job status is invalid')
    state, job_id = value.get('state'), value.get('job_id')
    if state not in {'succeeded', 'failed'} or not isinstance(job_id, str) or not job_id:
        raise ValueError('BHA reindex job is active or has unknown state')
    return {'job_id': job_id, 'state': state, 'mode': value.get('mode')}


def inspect(github_token: str, reindex_token: str, state_path: Path, *, lookup=revisions.archive_identity,
            read_status=access.read_status, fetch=samples.get, selected_samples=None) -> dict:
    if not github_token or not reindex_token:
        raise ValueError('existing GitHub and BHA status credentials are required')
    if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > MAX_STATE:
        raise ValueError('BHA parsed baseline is missing or oversized')
    baseline = _trees(json.loads(state_path.read_bytes()), 'baseline')
    def consumer():
        status = read_status(reindex_token)
        if (not isinstance(status, dict) or status.get('index_status') != 'ready'
                or status.get('switch_pending') is not False):
            raise ValueError('BHA consumer is not stably ready')
        return {'trees': _trees(status.get('archive_revisions'), 'consumer trees'),
                'fingerprint': status.get('build_fingerprint'), 'job': _job(status.get('job'))}
    before = consumer()
    health_before = samples.health(fetch(samples.BASE + '/api/health', samples.MAX_HEALTH))
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda number: lookup(github_token, number), range(32)))
    if (len(identities) != 32 or any(not isinstance(row, tuple) or len(row) != 3 or row[0] != str(index)
            or not isinstance(row[1], str) or not samples.SHA.fullmatch(row[1])
            or not isinstance(row[2], str) or not samples.SHA.fullmatch(row[2])
            for index, row in enumerate(identities))):
        raise ValueError('BHA parsed identities are incomplete or invalid')
    commits = {key: commit for key, commit, _ in identities}
    trees = {key: tree for key, _, tree in identities}
    changed = [int(key) for key in sorted(KEYS, key=int) if trees[key] != before['trees'][key]]
    checked_samples, deferred_samples = [], []
    if selected_samples is not None:
        # Source and serving index intentionally differ for changed archives.
        # Comparing their current article bytes here would block the very
        # reindex needed to reconcile them. Retain those samples for the
        # post-index acceptance step instead of weakening the comparison.
        if (not isinstance(selected_samples, list) or not 1 <= len(selected_samples) <= 16
                or any(not isinstance(row, dict) or set(row) != {'archive', 'path'}
                       or type(row['archive']) is not int or not isinstance(row['path'], str)
                       for row in selected_samples)):
            raise ValueError('invalid BHA sample selection')
        for row in selected_samples:
            samples.sample_path(row['archive'], 'a' * 40, row['path'])
        if len({(row['archive'], row['path']) for row in selected_samples}) != len(selected_samples):
            raise ValueError('duplicate BHA sample selection')
        eligible = [row for row in selected_samples if trees[str(row['archive'])] == before['trees'][str(row['archive'])]]
        deferred_samples = [row for row in selected_samples if trees[str(row['archive'])] != before['trees'][str(row['archive'])]]
        if eligible:
            sample_result = samples.observe(github_token, eligible, fetch=fetch, lookup=lookup)
            rows = sample_result.get('samples') if isinstance(sample_result, dict) else None
            if (not isinstance(rows, list) or len(rows) != len(eligible)
                    or {(row.get('archive'), row.get('path')) for row in rows if isinstance(row, dict)} !=
                    {(row['archive'], row['path']) for row in eligible}):
                raise ValueError('BHA live sample results are incomplete')
            for row in rows:
                key = str(row['archive'])
                if ((row['commit'], row['tree']) != (commits[key], trees[key])
                        or row.get('preview_article_equal') is not True or row.get('exact_title_search_found') is not True):
                    raise ValueError('BHA sample differs from captured parsed identity')
            checked_samples = rows
    # Re-read all heads, not just samples: two stable observations do not fence
    # a later change, but avoid planning against a moving snapshot.
    with ThreadPoolExecutor(max_workers=4) as pool:
        after_identities = list(pool.map(lambda number: lookup(github_token, number), range(32)))
    if after_identities != identities:
        raise ValueError('BHA parsed identities changed during inspection')
    after = consumer()
    health_after = samples.health(fetch(samples.BASE + '/api/health', samples.MAX_HEALTH))
    if after != before or health_after != health_before:
        raise ValueError('BHA consumer changed during inspection')
    baseline_drift = [int(key) for key in sorted(KEYS, key=int) if trees[key] != baseline[key]]
    body = {'version': 1, 'kind': 'bha-reindex-readonly-plan', 'archive_commits': commits,
            'archive_revisions': trees, 'consumer_revisions': before['trees'], 'changed_archives': changed,
            'baseline_drift': baseline_drift, 'consumer_fingerprint': before['fingerprint'],
            'last_job': before['job'], 'document_count': health_before['document_count'],
            'sample_count': len(checked_samples), 'samples': checked_samples, 'deferred_samples': deferred_samples,
            'incremental_action': 'review-changes' if changed else 'no-change',
            'post_index_sample_missing_archives': sorted(set(changed) - {
                row['archive'] for row in (selected_samples or [])}),
            'full_rebuild': 'not-requested', 'remote_writes': 0, 'reindex_requested': False,
            'publication_ready': False, 'external_writers_verified': False,
            'scope': 'exact Git commits and trees, reported consumer revisions and eligible live samples; changed archives deferred until reindex'}
    return {**body, 'plan_sha256': hashlib.sha256(search_snapshot.canonical(body)).hexdigest()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--samples', type=Path, default=Path('config/bha-consumer-samples.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink() or args.output.resolve() in {args.baseline.resolve(), args.samples.resolve()}:
        raise ValueError('BHA index plan output must be a new path outside its inputs')
    if args.samples.is_symlink() or not args.samples.is_file() or args.samples.stat().st_size > MAX_STATE:
        raise ValueError('BHA sample selection is missing or oversized')
    spec = json.loads(args.samples.read_bytes())
    if not isinstance(spec, dict) or set(spec) != {'version', 'samples'} or spec['version'] != 1:
        raise ValueError('invalid BHA sample selection')
    result = inspect(os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', ''),
                     os.environ.get('BHA_REINDEX_TOKEN', ''), args.baseline, selected_samples=spec['samples'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True); output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
