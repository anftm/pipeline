"""Plan one BHA parsed rebuild against exact current and built-input identities.

All remote operations are reads. A build plan is not a build, deployment,
consumer receipt, write authorization or evidence that external writers are
fenced. Existing upstream OCR cache/patch branches are inputs, not new OCR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

try:
    from . import bha_fork_safe_sync as identity
    from . import build_fork_parsed as parsed
    from . import check_bha_revisions as revisions
    from . import search_snapshot
    from .bha_input_monitor import load_baseline
except ImportError:
    import bha_fork_safe_sync as identity
    import build_fork_parsed as parsed
    import check_bha_revisions as revisions
    import search_snapshot
    from bha_input_monitor import load_baseline


def _snapshot(token: str, archive: int) -> dict:
    repo, upstream_info, mirror_info = identity.identities(token, archive)
    mirror = parsed.branch_revisions(token, parsed.MIRROR_OWNER, repo)
    upstream = parsed.branch_revisions(token, parsed.UPSTREAM_OWNER, repo)
    for values in (mirror, upstream):
        for branch in (*parsed.INPUT_BRANCHES, parsed.PARSED_BRANCH):
            identity._sha(values[branch])
    helper = identity._sha(parsed.ref_revision(token, parsed.UPSTREAM_OWNER, 'ocr_helper', 'main'))
    key, commit, tree = revisions.archive_identity(token, archive)
    if key != str(archive) or commit != mirror['parsed']:
        raise ValueError('BHA parsed branch changed during revision inspection')
    identity._sha(tree)
    return {'archive': archive, 'upstream_repository_id': upstream_info['id'],
            'mirror_repository_id': mirror_info['id'], 'mirror_branches': mirror,
            'upstream_branches': upstream, 'helper_revision': helper, 'parsed_tree': tree}


def _body(archive: int, snapshot: dict, input_baseline: dict, tree_baseline: dict) -> dict:
    if (not isinstance(snapshot, dict) or set(snapshot) != {'archive', 'upstream_repository_id',
            'mirror_repository_id', 'mirror_branches', 'upstream_branches', 'helper_revision', 'parsed_tree'}
            or snapshot['archive'] != archive):
        raise ValueError('invalid BHA build inspection identity')
    for key in ('upstream_repository_id', 'mirror_repository_id'):
        if type(snapshot[key]) is not int or snapshot[key] <= 0:
            raise ValueError('invalid BHA repository identity')
    branches = {*parsed.INPUT_BRANCHES, parsed.PARSED_BRANCH}
    for owner in ('mirror_branches', 'upstream_branches'):
        values = snapshot[owner]
        if not isinstance(values, dict) or set(values) != branches:
            raise ValueError('invalid BHA source branch inventory')
        for value in values.values():
            identity._sha(value)
    identity._sha(snapshot['helper_revision']); identity._sha(snapshot['parsed_tree'])
    recorded = input_baseline.get(str(archive))
    if not isinstance(recorded, dict):
        raise ValueError('BHA input baseline lacks this archive')
    expected_input_keys = {*parsed.INPUT_BRANCHES, 'helper', 'patch_layout'}
    if set(recorded) != expected_input_keys or recorded.get('patch_layout') != str(parsed.PATCH_LAYOUT_VERSION):
        raise ValueError('BHA recorded parser input identity is incomplete')
    for key in (*parsed.INPUT_BRANCHES, 'helper'):
        identity._sha(recorded[key])
    if (not isinstance(tree_baseline, dict) or set(tree_baseline) != {str(i) for i in range(32)}):
        raise ValueError('BHA parsed tree baseline must contain all 32 archives')
    for value in tree_baseline.values():
        identity._sha(value)
    current_inputs = parsed.source_snapshot(snapshot['mirror_branches'], snapshot['helper_revision'])
    input_changed = current_inputs != recorded
    tree_changed = snapshot['parsed_tree'] != tree_baseline[str(archive)]
    return {'version': 1, 'kind': 'bha-parsed-build-readonly-plan', 'archive': archive,
            'snapshot': snapshot, 'recorded_inputs': recorded, 'current_inputs': current_inputs,
            'recorded_parsed_tree': tree_baseline[str(archive)], 'input_changed': input_changed,
            'parsed_tree_changed': tree_changed,
            'fork_requires_local_build': parsed.needs_local_build(snapshot['mirror_branches'], snapshot['upstream_branches']),
            'action': ('inspect-drift' if tree_changed else 'no-input-change' if not input_changed else
                       'build-candidate' if parsed.needs_local_build(snapshot['mirror_branches'], snapshot['upstream_branches'])
                       else 'inspect-upstream-parsed'),
            'snapshots_observed_twice': True, 'remote_writes': 0, 'build_requested': False,
            'consumer_verified': False, 'publication_ready': False, 'external_writers_verified': False,
            'scope': 'exact fork input heads and parsed tree; no candidate bytes or consumer delivery'}


def inspect(token: str, archive: int, *, input_path: Path, tree_path: Path, snapshot=_snapshot) -> dict:
    if not token or type(archive) is not int or not 0 <= archive <= 31:
        raise ValueError('one BHA archive and an existing GitHub read credential are required')
    inputs = load_baseline(input_path)
    if tree_path.is_symlink() or not tree_path.is_file() or tree_path.stat().st_size > 1024 * 1024:
        raise ValueError('BHA parsed tree baseline is missing or oversized')
    trees = json.loads(tree_path.read_bytes())
    before, after = snapshot(token, archive), snapshot(token, archive)
    if before != after:
        raise RuntimeError('BHA inputs or parsed branch changed during build planning')
    body = _body(archive, before, inputs, trees)
    return {**body, 'plan_sha256': hashlib.sha256(search_snapshot.canonical(body)).hexdigest()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=int)
    parser.add_argument('--inputs', type=Path, default=Path('state/bha-parsed-inputs.json'))
    parser.add_argument('--trees', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if (args.output.exists() or args.output.is_symlink()
            or args.output.resolve() in {args.inputs.resolve(), args.trees.resolve()}):
        raise ValueError('BHA build plan output must be a new path outside its baselines')
    token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
    result = inspect(token, args.archive, input_path=args.inputs, tree_path=args.trees)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
