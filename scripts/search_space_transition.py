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


def _verify_candidate_sources(manifest, expected_digest):
    import hashlib
    revisions = manifest.get('source_revisions') if isinstance(manifest, dict) else None
    if (not isinstance(revisions, dict) or set(revisions) != set(fetch.REPOS)
            or any(not isinstance(sha, str) or not SHA.fullmatch(sha) for sha in revisions.values())
            or hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest() != expected_digest):
        raise ValueError('Space candidate source identity differs from the durable intent')
    token = os.environ.get('HF_TOKEN', '')
    if {repo: fetch.get_repo_sha(repo, token) for repo in fetch.REPOS} != revisions:
        raise ValueError('Space candidate sources changed before update')


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


def verify_reviewed_successor(*, expected_revision, successor, test_path) -> dict:
    """Verify Git ancestry and the exact reviewed file; never checkout or run it.

    Hub commit lists omit parents and its tree API omits modes. Read actual Git
    objects instead; blob IDs also bind LFS pointers. Operator review remains
    necessary: a tests/ path alone does not prove that code is never imported.
    """
    if (not isinstance(successor, str) or not SHA.fullmatch(successor)
            or not SHA.fullmatch(expected_revision) or successor == expected_revision):
        raise ValueError('reviewed Space successor requires two distinct exact SHAs')
    if (not isinstance(test_path, str) or not test_path.startswith('tests/')
            or not test_path.endswith('.py') or str(PurePosixPath(test_path)) != test_path
            or any(part in {'', '.', '..'} for part in test_path.split('/'))
            or any(ord(char) < 32 for char in test_path) or any(char in test_path for char in '\\*?[]')):
        raise ValueError('reviewed Space test path must be one exact tests Python file')
    with tempfile.TemporaryDirectory(prefix='search-space-successor-') as directory:
        def git(*args):
            return subprocess.run(['git', '-C', directory, '-c', 'credential.helper=',
                                   '-c', 'core.hooksPath=/dev/null', *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                                  env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}).stdout
        git('init', '--bare', '--quiet')
        git('fetch', '--quiet', '--no-tags', '--depth=2', SPACE_GIT_URL, successor)
        commit = git('cat-file', 'commit', successor)
        parents = [line[7:].decode('ascii') for line in commit.split(b'\n\n', 1)[0].splitlines()
                   if line.startswith(b'parent ')]
        if parents != [expected_revision]:
            raise ValueError('reviewed Space successor is not a direct single-parent successor')

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
        path = test_path.encode('utf8')
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if changed != {path} or path not in before or path not in after:
            raise ValueError('Space successor changes paths outside the exact reviewed file')
        old_mode, old_type, old_blob = before[path].split()
        new_mode, new_type, new_blob = after[path].split()
        if (old_mode != new_mode or old_mode not in {b'100644', b'100755'}
                or old_type != b'blob' or new_type != b'blob' or old_blob == new_blob):
            raise ValueError('reviewed Space file must remain a regular file with unchanged mode')
        return {'expected_revision': expected_revision, 'successor': successor, 'reviewed_test_path': test_path,
                'single_parent': expected_revision, 'before_tree_sha256': hashlib.sha256(before_raw).hexdigest(),
                'after_tree_sha256': hashlib.sha256(after_raw).hexdigest(),
                'before_blob': old_blob.decode('ascii'), 'after_blob': new_blob.decode('ascii'),
                'mode': old_mode.decode('ascii')}


def reconcile(store, *, api, hub, get, operation_id: str,
              reviewed_successor=None, reviewed_test_path=None) -> dict:
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
    if reviewed_successor is not None or reviewed_test_path is not None:
        if pending['operation'] != 'apply' or not pending.get('write_claim'):
            raise ValueError('reviewed successor requires a consumed Space apply claim')
        witness = verify_reviewed_successor(expected_revision=expected_revision,
                                            successor=reviewed_successor, test_path=reviewed_test_path)
        expected_revision = reviewed_successor
        deploy.runtime(api, expected_sha=expected_revision)
    configured, updated = deploy.variable(api)
    source = pending['config_before']['generation']
    target = current.record['parent_pointer']['desired'] if pending['operation'] == 'rollback' else current.record['candidate']
    if configured == source:
        status = 'candidate-configured' if pending['operation'] == 'rollback' else 'parent-configured'
        return {'kind': 'search-space-observation', 'operation': operation_id, 'status': status,
                'journal_advanced': False, 'external_writers_verified': False}
    if configured != target:
        raise ValueError('Space configuration is neither the source nor the requested target')
    if deploy._timestamp(updated) < deploy._timestamp(pending['config_before']['updated_at']):
        raise ValueError('Space configuration timestamp moved backwards')
    state = api.get_space_runtime(deploy.SPACE)
    if state.stage == 'RUNNING_BUILDING':
        return {'kind': 'search-space-observation', 'operation': operation_id, 'status': 'building',
                'journal_advanced': False, 'external_writers_verified': False}
    if state.stage != 'RUNNING' or not isinstance(state.raw, dict) or state.raw.get('sha') != expected_revision:
        raise ValueError('Space runtime is unavailable or running an unexpected revision')
    deploy.runtime(api, expected_sha=expected_revision)
    manifest = adoption.validate_generation(hub, target)
    actual = receipts.observe('space', target, manifest, get=get)
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed during Space reconciliation')
    configured_after, updated_after = deploy.variable(api)
    if (configured_after, updated_after) != (configured, updated):
        raise ValueError('Space configuration changed during reconciliation')
    deploy.runtime(api, expected_sha=expected_revision)
    if witness is not None:
        evidence = json.loads(actual)
        evidence['details']['reviewed_space_successor'] = witness
        actual = search_snapshot.canonical(evidence)
    result = tx.observe_site(store, current, site='space', generation=target, receipt=actual)
    status = 'parent-loaded' if pending['operation'] == 'rollback' else 'candidate-loaded'
    return {'kind': 'search-space-observation', 'operation': operation_id, 'status': status,
            'generation': target, 'journal_advanced': True, 'sequence': result.record['sequence'],
            'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('prepare', 'rollback-prepare', 'apply', 'reconcile'))
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
        result = reconcile(store, api=api, hub=HubBucketStore(), get=bounded_get, operation_id=args.operation)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
