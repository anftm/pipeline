#!/usr/bin/env python3
"""Deactivate CCRD OCR search without deleting data or changing Reader routes.

This deliberately does not retire the current release root, reset stable IDs,
run GC, or rewrite the newspaper index. It writes only a bounded consumer
acknowledgment after the Space confirms withdrawal. The existing serialized
CCRD lock and an exact Space parent revision prevent lost updates.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

try:
    from . import ccrd_lifecycle as lifecycle
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import ccrd_lifecycle as lifecycle
    from reader_bucket_store import HubBucketStore


REPOSITORY = 'anftm/pipeline'
WORKFLOW = REPOSITORY + '/.github/workflows/ccrd-ocr-withdraw-consumer.yml@refs/heads/main'
SPACE = 'vomebook/Search'
BASE_FILE = 'data/index-release.json'
OCR_FILE = 'data/ocr-release.json'
CURRENT = 'control/ocr-current.json'
MAINTENANCE_BUCKET = 'vomebook/reader-assets-v2'
MAINTENANCE = 'reader-index/gc/maintenance/current.json'
SOURCES = frozenset({'CCRD', 'CW', 'RMRB', 'PLA'})
ACTIVE_STATUSES = ('in_progress', 'queued', 'pending', 'waiting', 'requested')


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def assert_writer():
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW
            or not os.environ.get('GITHUB_RUN_ID')):
        raise RuntimeError('OCR deactivation requires the serialized central workflow')


def expected_values(revision, generation, release_sha, base_sha):
    if not re.fullmatch(r'[0-9a-f]{40}', str(revision)):
        raise ValueError('expected Space revision must be an exact 40-hex commit')
    if any(not re.fullmatch(r'[0-9a-f]{64}', str(value))
           for value in (generation, release_sha, base_sha)):
        raise ValueError('generation, release and base identities must be exact SHA-256 values')
    return {'revision': revision, 'generation': generation,
            'release_sha256': release_sha, 'base_sha256': base_sha}


def check_base(raw, base_sha):
    if digest(raw) != base_sha:
        raise ValueError('base index activation changed')
    value = json.loads(raw)
    path = value.get('path', '')
    if (value.get('version') != 1 or value.get('kind') != 'ccrd-index-activation'
            or value.get('bucket') != lifecycle.BUCKET
            or not re.fullmatch(r'generations/(ccrd-four-[0-9a-f]{64})/manifest\.json', path)):
        raise ValueError('unexpected base index activation')
    return path.split('/')[1]


def check_ocr_activation(raw, expected):
    activation = json.loads(raw)
    target = {'version': 1, 'kind': 'ccrd-ocr-activation', 'bucket': lifecycle.BUCKET,
              'path': f'releases/ocr-{expected["generation"]}/manifest.json',
              'sha256': expected['release_sha256']}
    if activation != target:
        raise ValueError('Space OCR activation differs from the exact current OCR pointer')
    return activation


def check_live(status, generation, base_generation):
    bucket = status.get('bucket') or {}
    if (status.get('ready') is not True or status.get('ocr_release') != generation
            or not SOURCES <= set(status.get('loaded_sources') or [])
            or bucket.get('state') != 'ready' or bucket.get('generation') != base_generation):
        raise ValueError('consumer does not expose the expected OCR/base state')


def maintenance_state(store):
    """Do not change a retained CCRD root during unfinished Reader GC."""
    try:
        raw = store.read_bytes(MAINTENANCE_BUCKET, MAINTENANCE)
    except FileNotFoundError:
        return None
    journal = json.loads(raw)
    if (journal.get('version') != 1 or journal.get('kind') != 'reader-maintenance-journal'
            or journal.get('restored') is not True):
        raise ValueError('Reader GC maintenance is unfinished or unknown')
    return digest(raw)


def active_maintenance_runs():
    """A protected queued publisher still counts as unfinished maintenance."""
    active = []
    endpoint = f'repos/{REPOSITORY}/actions/workflows/reader-maintenance.yml/runs'
    for status in ACTIVE_STATUSES:
        command = ['gh', 'api', '--paginate', '--slurp', f'{endpoint}?status={status}&per_page=100']
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=True)
        pages = json.loads(result.stdout)
        if not isinstance(pages, list):
            raise ValueError('invalid maintenance-run inventory')
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get('workflow_runs'), list):
                raise ValueError('invalid maintenance-run inventory')
            active.extend(str(row['id']) for row in page['workflow_runs'])
    return sorted(set(active))


def snapshot(store, space, status, expected):
    """Read and validate all inputs; no writes or remote object retirement."""
    maintenance_sha = maintenance_state(store)
    head = space.head()
    if head != expected['revision']:
        raise ValueError('Space revision changed')
    if space.has_ocr_override():
        raise ValueError('Space has an independent OCR activation override')
    base = space.read(BASE_FILE, head)
    base_generation = check_base(base, expected['base_sha256'])
    raw = store.read_bytes(lifecycle.BUCKET, CURRENT)
    pointer = json.loads(raw)
    lifecycle.references(pointer, CURRENT)
    release = pointer.get('release', {})
    if (pointer.get('generation') != expected['generation']
            or release.get('path') != f'releases/ocr-{expected["generation"]}/manifest.json'
            or release.get('sha256') != expected['release_sha256']):
        raise ValueError('current OCR pointer changed')
    check_ocr_activation(space.read(OCR_FILE, head), expected)
    check_live(status, expected['generation'], base_generation)
    return {'space_revision': head, 'base_generation': base_generation,
            'base_activation_sha256': digest(base), 'ocr_generation': expected['generation'],
            'ocr_release_sha256': expected['release_sha256'], 'pointer_sha256': digest(raw),
            'maintenance_journal_sha256': maintenance_sha}


def ensure_single_file_commit(space, revision, expected):
    """Reject unrelated Space revisions when resuming a partially applied run."""
    commits = space.recent_commits(revision)
    if len(commits) < 2 or commits[:2] != [revision, expected['revision']]:
        raise ValueError('Space revision is not the exact single-file withdrawal commit')
    diff = space.commit_diff(revision)
    files = re.findall(r'^diff --git a/(.+?) b/(.+?)$', diff, flags=re.MULTILINE)
    if files != [(OCR_FILE, OCR_FILE)] or 'deleted file mode ' not in diff:
        raise ValueError('Space revision is not the exact single-file withdrawal commit')


def write_withdrawal_receipt(store, report, revision):
    """Pin the disconnected consumer while keeping every existing data root."""
    if not re.fullmatch(r'[0-9a-f]{40}', str(revision)):
        raise ValueError('withdrawn consumer revision is not immutable')
    value = {'version': 1, 'kind': 'ccrd-ocr-consumer', 'status': 'consumer-withdrawn',
             'resources': [], 'consumer_revision': revision,
             'previous_generation': report['before']['ocr_generation'],
             'pointer_sha256': report['before']['pointer_sha256'],
             'base_activation_sha256': report['before']['base_activation_sha256'],
             'base_generation': report['before']['base_generation'],
             'workflow_run_id': os.environ['GITHUB_RUN_ID']}
    path = 'control/ocr-consumer.json'
    try:
        store.put_json(lifecycle.BUCKET, path, value)
    except Exception:
        # The write response can be lost after commit. Accept only an exact
        # readback and otherwise leave the workflow as a resumable failure.
        actual = store.read_bytes(lifecycle.BUCKET, path)
        if json.loads(actual) != value:
            raise
    else:
        actual = store.read_bytes(lifecycle.BUCKET, path)
    if json.loads(actual) != value:
        raise ValueError('withdrawn consumer receipt readback mismatch')
    return digest(actual)


def deactivate(store, space, live, expected, *, apply=False, save=None, timeout=2400,
               maintenance_runs=None, resume_revision=None):
    if resume_revision and not apply:
        raise ValueError('resumption requires apply')
    report = {'version': 1, 'kind': 'ccrd-ocr-consumer-withdrawal',
              'started_at': datetime.now(timezone.utc).isoformat(), 'phase': 'planned',
              'remote_objects_deleted': 0, 'control_roots_changed': False,
              'stable_ids_changed': False, 'reader_routes_changed': False,
              'base_activation_changed': False, 'applied': False,
              'reader_gc_projection_blocked_until_safe_reconciliation': True}
    if resume_revision:
        if not re.fullmatch(r'[0-9a-f]{40}', str(resume_revision)):
            raise ValueError('resume revision must be a 40-hex Space commit')
        assert_writer()
        maintenance_runs = maintenance_runs or active_maintenance_runs
        if maintenance_runs():
            raise ValueError('Reader GC maintenance workflow has an unfinished run')
        if space.head() != resume_revision or space.has_ocr_override():
            raise ValueError('Space head or OCR override does not match the withdrawal commit')
        ensure_single_file_commit(space, resume_revision, expected)
        check_ocr_activation(space.read(OCR_FILE, expected['revision']), expected)
        base_generation = check_base(space.read(BASE_FILE, resume_revision), expected['base_sha256'])
        check_base(space.read(BASE_FILE, expected['revision']), expected['base_sha256'])
        raw = store.read_bytes(lifecycle.BUCKET, CURRENT)
        pointer = json.loads(raw)
        lifecycle.references(pointer, CURRENT)
        if (pointer.get('generation') != expected['generation']
                or pointer.get('release', {}).get('sha256') != expected['release_sha256']):
            raise ValueError('OCR pointer changed before resumption')
        before = {'space_revision': expected['revision'], 'base_generation': base_generation,
                  'base_activation_sha256': expected['base_sha256'],
                  'ocr_generation': expected['generation'],
                  'ocr_release_sha256': expected['release_sha256'], 'pointer_sha256': digest(raw),
                  'maintenance_journal_sha256': maintenance_state(store)}
        revision = resume_revision
        report.update(before=before, phase='waiting-for-consumer', applied=True,
                      space_revision=revision, resumed=True)
        if save:
            save(report)
    else:
        before = snapshot(store, space, live(), expected)
        report['before'] = before
    if not apply:
        return report
    if not resume_revision:
        assert_writer()
        maintenance_runs = maintenance_runs or active_maintenance_runs
        if maintenance_runs():
            raise ValueError('Reader GC maintenance workflow has an unfinished run')
        if save:
            save(report)
        # Re-evaluate exact writer inputs immediately before the parent-locked
        # commit. Failed or changed inputs never mutate Space.
        again = snapshot(store, space, live(), expected)
        if again != before:
            raise ValueError('OCR/base snapshot changed before deactivation')
        if maintenance_runs():
            raise ValueError('Reader GC maintenance workflow acquired an unfinished run')
        try:
            revision = space.remove_ocr_activation(parent=expected['revision'])
        except Exception:
            # If the API response was lost after committing, bind recovery to
            # the exact parent and single-file diff; never retry the mutation.
            observed = space.head()
            if observed != expected['revision']:
                ensure_single_file_commit(space, observed, expected)
                report.update(phase='commit-outcome-unknown', applied=False,
                              candidate_space_revision=observed)
                if save:
                    save(report)
            raise
        report.update(phase='waiting-for-consumer', applied=True, space_revision=revision)
        if save:
            save(report)
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            if space.head() != revision:
                raise ValueError('Space revision advanced after OCR deactivation')
            if space.has_ocr_override():
                raise ValueError('OCR activation override appeared after deactivation')
            base_generation = check_base(space.read(BASE_FILE, revision), expected['base_sha256'])
            if space.exists(OCR_FILE, revision):
                raise ValueError('OCR activation still exists after deactivation')
            check_live(live(), None, base_generation)
            # No current pointer or stable-ID data are touched by this step.
            try:
                pointer = store.read_bytes(lifecycle.BUCKET, CURRENT)
            except FileNotFoundError as error:
                raise ValueError('OCR pointer changed during deactivation') from error
            if digest(pointer) != before['pointer_sha256']:
                raise ValueError('OCR pointer changed during deactivation')
            receipt_sha = write_withdrawal_receipt(store, report, revision)
            report.update(phase='consumer-withdrawn', complete=True,
                          consumer_receipt_sha256=receipt_sha, control_roots_changed=True,
                          reader_gc_projection_blocked_until_safe_reconciliation=False,
                          finished_at=datetime.now(timezone.utc).isoformat())
            if save:
                save(report)
            return report
        except ValueError as error:
            last = str(error)
            # A moved Space head or override is not a transient readiness issue.
            if 'revision advanced' in last or 'override appeared' in last or 'pointer changed' in last:
                raise
        except Exception as error:
            import httpx
            if not isinstance(error, httpx.HTTPError):
                raise
            last = type(error).__name__
        if time.monotonic() >= deadline:
            raise TimeoutError('consumer deactivation not yet confirmed: ' + str(last))
        time.sleep(20)


class Space:
    def __init__(self):
        from huggingface_hub import HfApi
        self.api = HfApi(token=os.environ.get('HF_TOKEN'))

    def head(self):
        return self.api.repo_info(SPACE, repo_type='space').sha

    def has_ocr_override(self):
        values = self.api.get_space_variables(SPACE)
        return any(getattr(values.get(name), 'value', '') for name in
                   ('CCRD_OCR_RELEASE_PATH', 'CCRD_OCR_RELEASE_SHA256'))

    def read(self, path, revision):
        from huggingface_hub import hf_hub_download
        return Path(hf_hub_download(SPACE, path, repo_type='space', revision=revision,
                                    token=os.environ.get('HF_TOKEN'))).read_bytes()

    def exists(self, path, revision):
        from huggingface_hub.errors import EntryNotFoundError
        try:
            self.read(path, revision)
            return True
        except EntryNotFoundError:
            return False

    def remove_ocr_activation(self, *, parent):
        from huggingface_hub import CommitOperationDelete
        result = self.api.create_commit(SPACE, repo_type='space', parent_commit=parent,
            commit_message='Withdraw OCR search activation; preserve base index',
            operations=[CommitOperationDelete(path_in_repo=OCR_FILE)])
        return result.oid

    def recent_commits(self, revision):
        # Read metadata only: no credentials are printed or stored.
        import httpx
        response = httpx.get(f'https://huggingface.co/api/spaces/{SPACE}/commits/{revision}',
                             headers={'Authorization': f'Bearer {os.environ["HF_TOKEN"]}'},
                             timeout=60, follow_redirects=True)
        response.raise_for_status()
        return [row['id'] for row in response.json()]

    def commit_diff(self, revision):
        import httpx
        response = httpx.get(f'https://huggingface.co/spaces/{SPACE}/commit/{revision}.diff',
                             headers={'Authorization': f'Bearer {os.environ["HF_TOKEN"]}'},
                             timeout=60, follow_redirects=True)
        response.raise_for_status()
        return response.text


def live_status():
    import httpx
    response = httpx.get('https://vomebook-search.hf.space/api/index-status',
                         headers={'Cache-Control': 'no-cache'}, follow_redirects=True, timeout=60)
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--space-revision', required=True)
    parser.add_argument('--ocr-generation', required=True)
    parser.add_argument('--ocr-release-sha256', required=True)
    parser.add_argument('--base-activation-sha256', required=True)
    parser.add_argument('--resume-revision', default='')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    expected = expected_values(args.space_revision, args.ocr_generation,
                               args.ocr_release_sha256, args.base_activation_sha256)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save(value):
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                               encoding='utf8')

    try:
        report = deactivate(HubBucketStore(), Space(), live_status, expected, apply=args.apply, save=save,
                            resume_revision=args.resume_revision or None)
    except Exception as error:
        existing = json.loads(args.output.read_bytes()) if args.output.is_file() else {}
        existing.update(complete=False, error_type=type(error).__name__, error=str(error))
        save(existing)
        raise
    save(report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
