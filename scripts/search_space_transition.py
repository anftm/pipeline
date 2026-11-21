"""Persist, apply once, and independently reconcile one Space search-data switch.

The operation ID is journaled before the Space variable changes.  A timeout or
unknown response is resolved only by observing exact revision, variable and
loaded generation; this module does not automatically replay a failed write.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
import hashlib
from pathlib import PurePosixPath
from datetime import UTC, datetime

try:
    from . import search_adopt as adoption
    from . import search_consumer_receipts as receipts
    from . import search_space_deploy as deploy
    from . import search_transition as tx
    from . import fetch_and_parse as fetch, search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import search_adopt as adoption
    import search_consumer_receipts as receipts
    import search_space_deploy as deploy
    import search_transition as tx
    import fetch_and_parse as fetch
    import search_snapshot
    from reader_bucket_store import HubBucketStore


SHA = re.compile(r'[0-9a-f]{40}\Z')
WAIT_STAGES = frozenset({'RUNNING_BUILDING', 'BUILDING', 'APP_STARTING', 'RUNNING_APP_STARTING'})


def _runtime_sha(state):
    raw = getattr(state, 'raw', None)
    return raw.get('sha') if isinstance(raw, dict) else None


def runtime_stage(api, *, expected_sha: str) -> str:
    """Use one runtime sample for both stage and SHA; RUNNING requires an exact SHA."""
    if api.space_info(deploy.SPACE, timeout=20).sha != expected_sha:
        raise ValueError('Space repository differs from bound revision')
    state = api.get_space_runtime(deploy.SPACE)
    sha = _runtime_sha(state)
    if (sha is not None or state.stage == 'RUNNING') and sha != expected_sha:
        raise ValueError('Space runtime differs from bound revision')
    return state.stage


def prepare(store, *, api, expected_revision: str, operation_id: str, created_at: str,
            rollback: bool = False, hub=None, get=None) -> tx.Current:
    current = tx.load(store)
    allowed = {'applying', 'verified', 'rolling-back'} if rollback else {'prepared', 'applying'}
    if current is None or current.record['state'] not in allowed or current.record['pending'] is not None:
        raise ValueError('search transaction is not ready for a Space operation')
    tx.audit_history(store, current)
    expected = current.record['candidate'] if rollback else current.record['parent_pointer']['desired']
    if current.record['observed']['space']['generation'] != expected:
        raise ValueError('Space does not have the expected source observation')
    deploy.runtime(api, expected_sha=expected_revision)
    configured, updated = deploy.variable(api)
    if configured != expected:
        raise ValueError('Space configured generation differs from expected source')
    if rollback:
        if hub is None or get is None:
            raise ValueError('Space rollback requires fresh generation and consumer checks')
        manifest = adoption.validate_generation(hub, expected)
        receipts.observe('space', expected, manifest, get=get)
        adoption.validate_generation(hub, current.record['parent_pointer']['desired'])
        if deploy.variable(api) != (configured, updated):
            raise ValueError('Space configuration changed during rollback preparation')
        deploy.runtime(api, expected_sha=expected_revision)
    return tx.start_site(store, current, site='space', operation_id=operation_id, expected_revision=expected_revision,
                         created_at=created_at, config_before={'generation': configured, 'updated_at': updated},
                         rollback=rollback)


class CandidateSourcesChanged(ValueError):
    """A valid durable source map differs from a complete, valid live map."""


def _source_sha(sha: object) -> bool:
    return isinstance(sha, str) and SHA.fullmatch(sha) is not None and sha != '0' * 40


def _verify_candidate_sources(manifest, expected_digest, *, revision=None):
    revisions = manifest.get('source_revisions') if isinstance(manifest, dict) else None
    if (not isinstance(revisions, dict) or set(revisions) != set(fetch.REPOS)
            or any(not _source_sha(sha) for sha in revisions.values())
            or hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest() != expected_digest):
        raise ValueError('Space candidate source identity differs from the durable intent')
    token = os.environ.get('HF_TOKEN', '')
    revision = fetch.get_repo_sha if revision is None else revision
    live = {repo: revision(repo, token) for repo in fetch.REPOS}
    if any(not _source_sha(sha) for sha in live.values()):
        raise ValueError('live search source revision is unavailable or malformed')
    if live != revisions:
        raise CandidateSourcesChanged('search candidate sources changed before update')


def apply_once(store, *, api, operation_id: str, hub=None) -> dict:
    current = tx.load(store)
    pending = current.record['pending'] if current is not None else None
    if (current is None or not isinstance(pending, dict) or pending.get('site') != 'space'
            or pending.get('operation') not in {'apply', 'rollback'} or pending.get('operation_id') != operation_id):
        raise ValueError('no exact pending Space operation')
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed before Space update')
    # A durable intent alone does not prove that the candidate objects or
    # source dataset revisions stayed current until the mutation. Re-read the
    # exact intended generation immediately before consuming the write claim.
    if hub is None:
        raise ValueError('Space update requires a fresh target generation check')
    target = current.record['parent_pointer']['desired'] if pending['operation'] == 'rollback' else current.record['candidate']
    manifest = adoption.validate_generation(hub, target)
    if pending['operation'] == 'apply':
        _verify_candidate_sources(manifest, current.record['source_revisions_sha256'])
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed during Space preflight')
    # Consume a single durable write opportunity, even if the process crashes
    # before the API call. Unknown outcomes are reconciled, never replayed.
    current = tx.claim_write(store, current, site='space', operation=pending['operation'], operation_id=operation_id)
    tx.check_write_claim(store, current)
    before = pending['config_before']
    deploy.set_generation(api, expected=before['generation'], expected_updated_at=before['updated_at'],
                          target=target, expected_sha=pending['expected_revision'])
    return {'kind': 'search-space-update-request', 'operation': operation_id, 'variable_written': True,
            'loaded_verified': False, 'journal_advanced': False, 'external_writers_verified': False}


def search_parent_bytes(current: tx.Current) -> bytes:
    try:
        from . import search_snapshot
    except ImportError:
        import search_snapshot
    return search_snapshot.canonical(current.record['parent_pointer'])


SPACE_GIT_URL = 'https://huggingface.co/spaces/VoiceOfML/Search'


ROLLBACK_HEAD = 'c86bef334ea62c095fd5522e0e24c834802f4f7c9c36cbb1e7cbef0f19d2fd56'
ROLLBACK_OPERATION = '5232f5e170717b55d9004d2395a2f620'
ROLLBACK_SPACE_BEFORE = '02a27d44291de288f5fa326dd0b02303bcd10b83'
ROLLBACK_SPACE_AFTER = '925b307e8f4a5c852a1f15e5f4b38ab2eca84464'
ROLLBACK_TEST_PATHS = ('tests/reader/browser/test_reader_speech.py',
                       'tests/reader/core/test_reader_architecture.js')


def verify_reviewed_successor(*, expected_revision, successor, test_path=None, reviewed_paths=None,
                              rollback_tests=False) -> dict:
    """Verify a bounded single-parent chain and the exact manually reviewed diff.

    Read Git objects, including modes and LFS pointer IDs, without checkout.
    These path restrictions do not replace manual review of each invocation.
    """
    if (not isinstance(successor, str) or not SHA.fullmatch(successor)
            or not SHA.fullmatch(expected_revision) or successor == expected_revision):
        raise ValueError('reviewed Space successor requires two distinct exact SHAs')
    if reviewed_paths is not None and not isinstance(reviewed_paths, (list, tuple)):
        raise ValueError('reviewed Space paths must be an explicit list')
    paths = list(reviewed_paths or [])
    if test_path is not None:
        if not isinstance(test_path, str) or not test_path.startswith('tests/') or not test_path.endswith('.py'):
            raise ValueError('legacy reviewed test path must be a tests Python file')
        paths.append(test_path)
    if not paths or any(not isinstance(path, str) for path in paths) or len(paths) != len(set(paths)):
        raise ValueError('reviewed Space paths must be nonempty and unique')
    if rollback_tests and (expected_revision, successor, tuple(sorted(paths))) != (
            ROLLBACK_SPACE_BEFORE, ROLLBACK_SPACE_AFTER, ROLLBACK_TEST_PATHS):
        raise ValueError('unreviewed Space rollback test successor')
    for path in paths:
        if (not isinstance(path, str)
                or not ((path.startswith('tests/') and path.endswith('.py'))
                        or (path.startswith('static/reader/') and path.endswith('.mjs'))
                        or (rollback_tests and path in ROLLBACK_TEST_PATHS))
                or str(PurePosixPath(path)) != path
                or any(part in {'', '.', '..'} for part in path.split('/'))
                or any(ord(char) < 32 for char in path) or any(char in path for char in '\\*?[]')):
            raise ValueError('reviewed Space path must be an exact tests Python or static/reader mjs file')
    with tempfile.TemporaryDirectory(prefix='search-space-successor-') as directory:
        def git(*args):
            return subprocess.run(['git', '-C', directory, '-c', 'credential.helper=',
                                   '-c', 'core.hooksPath=/dev/null', *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                                  env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}).stdout
        git('init', '--bare', '--quiet')
        git('fetch', '--quiet', '--no-tags', '--depth=32', SPACE_GIT_URL, successor)
        chain = [successor]
        for _ in range(31):
            commit = git('cat-file', 'commit', chain[-1])
            parents = [line[7:].decode('ascii') for line in commit.split(b'\n\n', 1)[0].splitlines()
                       if line.startswith(b'parent ')]
            if len(parents) != 1:
                raise ValueError('reviewed Space successor requires a single-parent chain')
            chain.append(parents[0])
            if parents[0] == expected_revision:
                break
        else:
            raise ValueError('expected Space ancestor not reached within 31 successors')

        def tree(sha):
            raw = git('ls-tree', '-r', '-z', '--full-tree', sha)
            entries = {}
            for entry in raw.split(b'\0'):
                if not entry:
                    continue
                metadata, path = entry.split(b'\t', 1)
                if path in entries:
                    raise ValueError('duplicate Space Git tree path')
                entries[path] = metadata
            return raw, entries
        before_raw, before = tree(expected_revision)
        after_raw, after = tree(successor)
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if changed != {path.encode('utf8') for path in paths}:
            raise ValueError('Space successor changes paths outside the exact reviewed allowlist')
        files = []
        for path in sorted(paths):
            key = path.encode('utf8')
            if key not in after:
                raise ValueError('reviewed Space file deletion is not allowed')
            new_mode, new_type, new_blob = after[key].split()
            old_mode, old_type, old_blob = before[key].split() if key in before else (None, None, None)
            if (rollback_tests and (old_mode, new_mode) != (b'100644', b'100644')):
                raise ValueError('rollback successor permits only existing regular test files')
            if (new_type != b'blob' or new_mode not in {b'100644', b'100755'}
                    or (key in before and (old_mode != new_mode or old_type != b'blob'))
                    or (key not in before and (not path.startswith('tests/') or new_mode != b'100644'))):
                raise ValueError('reviewed Space files require unchanged mode; only regular 100644 tests may be added')
            files.append({'path': path, 'before_blob': old_blob.decode('ascii') if old_blob else None,
                          'after_blob': new_blob.decode('ascii'),
                          'before_mode': old_mode.decode('ascii') if old_mode else None,
                          'after_mode': new_mode.decode('ascii')})
        return {'expected_revision': expected_revision, 'successor': successor, 'reviewed_test_path': test_path,
                'chain': list(reversed(chain)), 'files': files,
                'before_tree_sha256': hashlib.sha256(before_raw).hexdigest(),
                'after_tree_sha256': hashlib.sha256(after_raw).hexdigest()}


def reconcile(store, *, api, hub, get, operation_id: str,
              reviewed_successor=None, reviewed_test_path=None, reviewed_paths=None, poll_runtime=False,
              rollback_test_successor=False) -> dict:
    """Observe one operation; the daily poller may defer on an exact-revision restart."""
    current = tx.load(store)
    pending = current.record['pending'] if current is not None else None
    if (current is None or not isinstance(pending, dict) or pending.get('site') != 'space'
            or pending.get('operation') not in {'apply', 'rollback'} or pending.get('operation_id') != operation_id):
        raise ValueError('no exact pending Space operation')
    tx.audit_history(store, current)
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed before Space reconciliation')
    expected_revision = pending['expected_revision']
    witness = None
    if rollback_test_successor:
        if (reviewed_successor is not None or reviewed_test_path is not None or reviewed_paths is not None
                or (current.head['record'], current.record['state'], pending['operation'], operation_id,
                    expected_revision) != (ROLLBACK_HEAD, 'rolling-back', 'rollback', ROLLBACK_OPERATION,
                                           ROLLBACK_SPACE_BEFORE) or not pending.get('write_claim')):
            raise ValueError('no exact claimed Space rollback for the reviewed successor')
        witness = verify_reviewed_successor(expected_revision=expected_revision, successor=ROLLBACK_SPACE_AFTER,
                                           reviewed_paths=ROLLBACK_TEST_PATHS, rollback_tests=True)
        expected_revision = ROLLBACK_SPACE_AFTER
        deploy.runtime(api, expected_sha=expected_revision)
    elif reviewed_successor is not None or reviewed_test_path is not None or reviewed_paths is not None:
        if pending['operation'] != 'apply' or not pending.get('write_claim'):
            raise ValueError('reviewed successor requires a consumed Space apply claim')
        witness = verify_reviewed_successor(expected_revision=expected_revision,
                                            successor=reviewed_successor, test_path=reviewed_test_path,
                                            reviewed_paths=reviewed_paths)
        expected_revision = reviewed_successor
        if not poll_runtime:
            deploy.runtime(api, expected_sha=expected_revision)

    def building():
        return {'kind': 'search-space-observation', 'operation': operation_id, 'status': 'building',
                'journal_advanced': False, 'external_writers_verified': False}

    def running():
        if not poll_runtime:
            deploy.runtime(api, expected_sha=expected_revision)
            return True
        stage = runtime_stage(api, expected_sha=expected_revision)
        if stage in WAIT_STAGES:
            return False
        if stage != 'RUNNING':
            raise ValueError('Space runtime is unavailable: ' + str(stage))
        return True

    configured, updated = deploy.variable(api)
    source = pending['config_before']['generation']
    target = current.record['parent_pointer']['desired'] if pending['operation'] == 'rollback' else current.record['candidate']
    if configured not in {source, target}:
        raise ValueError('Space configuration is neither the source nor the requested target')
    if ((poll_runtime or configured == target or pending['operation'] == 'rollback')
            and deploy._timestamp(updated) < deploy._timestamp(pending['config_before']['updated_at'])):
        raise ValueError('Space configuration timestamp moved backwards')
    if poll_runtime and not running():
        return building()
    if configured == source:
        if rollback_test_successor:
            raise ValueError('Space rollback target is not configured')
        status = 'candidate-configured' if pending['operation'] == 'rollback' else 'parent-configured'
        return {'kind': 'search-space-observation', 'operation': operation_id, 'status': status,
                'journal_advanced': False, 'external_writers_verified': False}
    if not poll_runtime:
        state = api.get_space_runtime(deploy.SPACE)
        sha = _runtime_sha(state)
        if state.stage == 'RUNNING_BUILDING' and sha in (None, expected_revision):
            return building()
        if state.stage != 'RUNNING' or sha != expected_revision:
            raise ValueError('Space runtime is unavailable or running an unexpected revision')
        deploy.runtime(api, expected_sha=expected_revision)
    manifest = adoption.validate_generation(hub, target)
    actual = receipts.observe('space', target, manifest, get=get)
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed during Space reconciliation')
    configured_after, updated_after = deploy.variable(api)
    if (configured_after, updated_after) != (configured, updated):
        raise ValueError('Space configuration changed during reconciliation')
    if not running():
        # A rebuild during readback invalidates this receipt. Poll from scratch.
        return building()
    if witness is not None:
        evidence = json.loads(actual)
        evidence['details']['reviewed_space_successor'] = witness
        actual = search_snapshot.canonical(evidence)
    if rollback_test_successor:
        active = tx.load(store)
        if active is None or (active.head_raw, active.etag) != (current.head_raw, current.etag):
            raise ValueError('Space rollback journal changed during successor observation')
    result = tx.observe_site(store, current, site='space', generation=target, receipt=actual)
    status = 'parent-loaded' if pending['operation'] == 'rollback' else 'candidate-loaded'
    return {'kind': 'search-space-observation', 'operation': operation_id, 'status': status,
            'generation': target, 'journal_advanced': True, 'sequence': result.record['sequence'],
            'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('prepare', 'rollback-prepare', 'apply', 'reconcile',
                                                         'reconcile-rollback-test-successor'))
    parser.add_argument('--expected-revision', default='')
    parser.add_argument('--operation', default='')
    args = parser.parse_args(argv)
    from huggingface_hub import HfApi
    api = HfApi(token=os.environ.get('HF_TOKEN')); store = tx.S3TransitionStore()
    if args.mode in {'prepare', 'rollback-prepare'}:
        if not SHA.fullmatch(args.expected_revision) or args.operation:
            raise ValueError('prepare requires one exact Space revision and no caller operation ID')
        op = secrets.token_hex(16); now = datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
        print(json.dumps({'kind': 'search-space-intent-start', 'operation': op, 'revision': args.expected_revision},
                         sort_keys=True), flush=True)
        if args.mode == 'rollback-prepare':
            try:
                from .search_daily import bounded_get
            except ImportError:
                from search_daily import bounded_get
            current = prepare(store, api=api, expected_revision=args.expected_revision, operation_id=op,
                              created_at=now, rollback=True, hub=HubBucketStore(), get=bounded_get)
        else:
            current = prepare(store, api=api, expected_revision=args.expected_revision, operation_id=op,
                              created_at=now)
        result = {'kind': 'search-space-intent', 'operation': op, 'sequence': current.record['sequence'],
                  'candidate': current.record['candidate'], 'site_changed': False, 'pointer_written': False}
    elif args.mode == 'apply':
        result = apply_once(store, api=api, operation_id=args.operation, hub=HubBucketStore())
    else:
        try:
            from .search_daily import bounded_get
        except ImportError:
            from search_daily import bounded_get
        result = reconcile(store, api=api, hub=HubBucketStore(), get=bounded_get, operation_id=args.operation,
                           rollback_test_successor=args.mode == 'reconcile-rollback-test-successor')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
