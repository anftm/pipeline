"""Inspect or fast-forward one known BHA fork branch without branch deletion.

An apply is exact-parent, fast-forward-only and single-attempt. This does not
fence other writers or prove parsed consumer delivery. A changed branch, missing
proof, unknown outcome, or non-fast-forward relation stops the operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

try:
    from . import build_fork_parsed as parsed
    from . import fork_repositories as fork
except ImportError:
    import build_fork_parsed as parsed
    import fork_repositories as fork


BRANCHES = frozenset({'main', 'config', 'ocr_cache', 'ocr_patch', 'ocr_config',
                      'parsed_article', 'tags', 'origin', 'selected'})
SHA = re.compile(r'[0-9a-f]{40}\Z')


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('expected a full Git revision')
    return value


def identities(token, archive):
    if not token or type(archive) is not int or not 0 <= archive <= 31:
        raise ValueError('one archive and an existing GitHub credential are required')
    repo = f'banned-historical-archives{archive}'
    result = []
    for owner in (fork.UPSTREAM_OWNER, fork.MIRROR_OWNER):
        status, value = fork.api_request(token, 'GET', fork.repo_path(owner, repo))
        if (status != 200 or not isinstance(value, dict) or value.get('full_name') != f'{owner}/{repo}'
                or type(value.get('id')) is not int or value['id'] <= 0):
            raise ValueError('BHA repository identity is unavailable or invalid')
        result.append(value)
    upstream, mirror = result
    if (mirror.get('fork') is not True or not isinstance(mirror.get('parent'), dict)
            or mirror['parent'].get('id') != upstream['id']
            or mirror['parent'].get('full_name') != upstream['full_name']):
        raise ValueError('BHA mirror is not the expected upstream fork')
    return repo, upstream, mirror


def inspect(token, *, archive, branch):
    if not isinstance(branch, str) or branch not in BRANCHES:
        raise ValueError('branch is not an approved BHA input branch')
    repo, upstream, mirror = identities(token, archive)
    source = fork.branch_sha(token, fork.UPSTREAM_OWNER, repo, branch)
    target = fork.branch_sha(token, fork.MIRROR_OWNER, repo, branch)
    _sha(source)
    _sha(target)
    relation = 'identical'
    if source != target:
        path = f'{fork.repo_path(fork.MIRROR_OWNER, repo)}/compare/{source}...{target}'
        code, compared = fork.api_request(token, 'GET', path)
        if code != 200 or not isinstance(compared, dict) or compared.get('status') not in {'ahead', 'behind', 'diverged'}:
            raise ValueError('BHA branch relationship is unknown')
        relation = compared['status']
    if (fork.branch_sha(token, fork.UPSTREAM_OWNER, repo, branch) != source
            or fork.branch_sha(token, fork.MIRROR_OWNER, repo, branch) != target):
        raise RuntimeError('BHA branch changed during inspection')
    second_repo, second_upstream, second_mirror = identities(token, archive)
    if (second_repo != repo or second_upstream['id'] != upstream['id'] or second_mirror['id'] != mirror['id']):
        raise RuntimeError('BHA repository identity changed during inspection')
    return {'version': 1, 'kind': 'bha-fork-branch-plan', 'archive': archive, 'branch': branch,
            'upstream_repository': upstream['full_name'], 'upstream_repository_id': upstream['id'],
            'mirror_repository': mirror['full_name'], 'mirror_repository_id': mirror['id'],
            'upstream': source, 'mirror': target, 'mirror_relation': relation,
            'can_fast_forward': relation == 'behind', 'remote_writes': 0, 'external_writers_verified': False}


def _run(command, *, cwd, env):
    return subprocess.run(command, cwd=cwd, env=env, check=True, text=True, capture_output=True, timeout=180)


def apply(token, plan, *, confirmed):
    if confirmed is not True or not isinstance(plan, dict):
        raise ValueError('exact plan confirmation is required')
    archive, branch = plan.get('archive'), plan.get('branch')
    current = inspect(token, archive=archive, branch=branch)
    if current != plan or current['can_fast_forward'] is not True:
        raise ValueError('branch plan changed or is not an exact fast-forward')
    _sha(plan['upstream'])
    _sha(plan['mirror'])
    repo = f'banned-historical-archives{archive}'
    upstream_url = f'https://github.com/{fork.UPSTREAM_OWNER}/{repo}.git'
    mirror_url = f'https://github.com/{fork.MIRROR_OWNER}/{repo}.git'
    with tempfile.TemporaryDirectory(prefix='bha-fork-ff-') as directory:
        root = Path(directory)
        env = parsed.git_environment(token)
        _run(['git', 'init', '--bare', str(root / 'repo.git')], cwd=root, env=env)
        git = root / 'repo.git'
        _run(['git', 'fetch', '--no-tags', upstream_url, f'refs/heads/{branch}:refs/verification/upstream'], cwd=git, env=env)
        _run(['git', 'fetch', '--no-tags', mirror_url, f'refs/heads/{branch}:refs/verification/mirror'], cwd=git, env=env)
        actual_source = _run(['git', 'rev-parse', 'refs/verification/upstream'], cwd=git, env=env).stdout.strip()
        actual_mirror = _run(['git', 'rev-parse', 'refs/verification/mirror'], cwd=git, env=env).stdout.strip()
        if actual_source != plan['upstream'] or actual_mirror != plan['mirror']:
            raise ValueError('Git refs changed before fast-forward')
        _run(['git', 'merge-base', '--is-ancestor', actual_mirror, actual_source], cwd=git, env=env)
        if inspect(token, archive=archive, branch=branch) != plan:
            raise ValueError('BHA branch identity changed before push')
        try:
            _run(['git', 'push', f'--force-with-lease=refs/heads/{branch}:{actual_mirror}', mirror_url,
                  f'{actual_source}:refs/heads/{branch}'], cwd=git, env=env)
        except (subprocess.SubprocessError, OSError):
            # A lost response is unknown; inspect once and do not replay.
            after = inspect(token, archive=archive, branch=branch)
            if (after['upstream'] == actual_source and after['mirror'] == actual_source
                    and after['upstream_repository_id'] == plan['upstream_repository_id']
                    and after['mirror_repository_id'] == plan['mirror_repository_id']):
                return {'status': 'written-response-unknown', 'plan': plan, 'actual': after,
                        'remote_writes': 1, 'external_writers_verified': False}
            raise
    after = inspect(token, archive=archive, branch=branch)
    if (after['mirror'] != plan['upstream'] or after['upstream'] != plan['upstream']
            or after['mirror_repository_id'] != plan['mirror_repository_id']
            or after['upstream_repository_id'] != plan['upstream_repository_id']):
        raise RuntimeError('BHA branch readback differs; inspect before recovery')
    return {'status': 'written', 'plan': plan, 'actual': after,
            'remote_writes': 1, 'external_writers_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=int)
    parser.add_argument('--branch', required=True, choices=sorted(BRANCHES))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--apply-plan', type=Path)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--confirm-exact-fast-forward', action='store_true')
    args = parser.parse_args(argv)
    if args.apply_plan and args.output.resolve() == args.apply_plan.resolve():
        raise ValueError('report cannot overwrite the approved plan')
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('report output must not already exist')
    token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
    if args.apply_plan:
        if (not isinstance(args.plan_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', args.plan_sha256)
                or args.apply_plan.is_symlink() or not args.apply_plan.is_file()
                or args.apply_plan.stat().st_size > 64 * 1024):
            raise ValueError('a bounded, exact plan and its SHA-256 are required')
        raw = args.apply_plan.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.plan_sha256:
            raise ValueError('approved plan SHA-256 differs')
        plan = json.loads(raw)
        if not isinstance(plan, dict) or plan.get('archive') != args.archive or plan.get('branch') != args.branch:
            raise ValueError('plan does not match requested archive and branch')
        report = apply(token, plan, confirmed=args.confirm_exact_fast_forward)
    else:
        if args.confirm_exact_fast_forward or args.plan_sha256:
            raise ValueError('confirmation requires an exact previously reviewed plan')
        report = inspect(token, archive=args.archive, branch=args.branch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        output.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
