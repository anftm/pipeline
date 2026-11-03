#!/usr/bin/env python3
"""Bridge completed exact subset receipts to the original maintenance journal.

No EPUB deletion or chapter upload occurs here. The original 600 receipts and
their fixed progress remain byte-for-byte unchanged. A separate content-bound
handoff records 600+352+20 actual absences and only then permits restoration.
Ordinary GC remains separately guarded; external clients are not fenced.
"""
import hashlib
import json
import os
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

try:
    from . import chm_epub_journal as original, chm_final20_execute as final
    from . import chm_remaining352_after_final20 as remaining, chm_subset_journal as model
    from . import chm_subset_journal_backend as chain, chm_subset_locked_preflight as preflight
    from . import chm_subset_s3_journal as subset, reader_gc_maintenance as maintenance
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_epub_journal as original
    import chm_final20_execute as final
    import chm_remaining352_after_final20 as remaining
    import chm_subset_journal as model
    import chm_subset_journal_backend as chain
    import chm_subset_locked_preflight as preflight
    import chm_subset_s3_journal as subset
    import reader_gc_maintenance as maintenance
    from reader_gc_fence import GitHubProducerFence

ASSETS = preflight.chm.ASSETS
PREFIX = 'reader-index/gc/chm-terminal/0507a077f1ee4c99b8c64c0de7b58035/'
WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-terminal-release.yml@refs/heads/main'
OUTPUT = Path('output/chm-terminal-release/result.json')
LOCAL_MAINTENANCE = Path('output/chm-terminal-release/maintenance.json')
OLD_OWNER = '41257406256'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class JournalReader:
    def __init__(self, reader):
        self.reader = reader
    def names(self):
        paths = self.reader.list_files(ASSETS, (subset.PREFIX,))
        names = {path[len(subset.PREFIX):] for path in paths if path.startswith(subset.PREFIX)}
        if len(paths) != len(names) or any(not subset.NAME.fullmatch(name) for name in names):
            raise ValueError('subset journal namespace differs')
        return names
    def read(self, name):
        if not subset.NAME.fullmatch(name):
            raise ValueError('invalid subset journal key')
        try:
            return self.reader.read_bytes(ASSETS, subset.PREFIX + name)
        except FileNotFoundError:
            return None
    def create(self, *_):
        raise AssertionError('terminal subset journal is read-only')


def terminal_evidence(reader, rows, plan, progress):
    final_head = remaining.final_head(reader, plan)
    backend = JournalReader(reader)
    first = backend.read('event-00000000.json')
    if first is None or model.digest(json.loads(first)) != subset.INITIAL_SHA:
        raise ValueError('subset initial event differs')
    head = chain.Cursor(backend, plan).head()
    if head['state']['subset_complete'] is not True or head['state']['pending'] is not None:
        raise ValueError('352 transaction is not complete')
    observed = remaining.observe(reader, rows, progress['batches'], plan, head['state'])
    if (len(observed) != 972 or any(status != 'absent' for status in observed.values())
            or sum(len(item['paths']) for item in head['state']['receipts']) != 352):
        raise ValueError('all exact CHM targets are not verified absent')
    return {'kind': 'chm-exact-terminal-handoff', 'version': 1, 'inventory_sha256': preflight.chm.preview.INVENTORY_SHA256,
            'original_plan_sha256': preflight.PLAN_SHA, 'original_progress_sha256': preflight.PROGRESS_SHA,
            'original_maintenance_sha256': preflight.MAINTENANCE_SHA,
            'original_receipts_sha256': locked_receipts(), 'original_confirmed': 600,
            'subset_plan_sha256': subset.PLAN_SHA, 'subset_head_sha256': model.digest(head),
            'subset_confirmed': 352, 'final20_head_sha256': model.digest(final_head), 'final20_confirmed': 20,
            'verified_absent_objects': 972, 'verified_absent_bytes': sum(row['bytes'] for row in rows),
            'original_progress_unchanged': True, 'ordinary_gc_authorized': False}


def locked_receipts():
    return final.locked.IDENTITY['old_receipts_sha256']


class Writer:
    def __init__(self):
        access, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('terminal CHM writer credentials unavailable')
        self.client = boto3.client('s3', endpoint_url='https://s3.hf.co/vomebook',
                                   aws_access_key_id=access, aws_secret_access_key=secret,
                                   config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                                 retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=30,
                                                 request_checksum_calculation='when_required',
                                                 response_checksum_validation='when_required'))
    def put(self, path, raw, *, etag=None):
        import re
        allowed = (path == PREFIX + 'original-maintenance.json'
                   or bool(re.fullmatch(re.escape(PREFIX) + r'[0-9a-f]{64}\.json', path))
                   or path == maintenance.DURABLE_JOURNAL)
        if not allowed or not isinstance(raw, bytes) or not raw:
            raise ValueError('terminal CHM write outside audit or maintenance key')
        if path == maintenance.DURABLE_JOURNAL and not etag or path != maintenance.DURABLE_JOURNAL and etag:
            raise ValueError('terminal CHM write condition differs')
        conditions = {'IfMatch': etag} if etag else {'IfNoneMatch': '*'}
        try:
            response = self.client.put_object(Bucket='reader-assets-v2', Key=path, Body=raw,
                                              ContentType='application/json', **conditions)
        except Exception as error:
            raise RuntimeError('terminal CHM write outcome unknown; reconcile before retry') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise RuntimeError('terminal CHM write response ambiguous')


def save_exact(reader, writer, path, raw):
    try:
        old = reader.read_bytes(ASSETS, path)
    except FileNotFoundError:
        writer.put(path, raw)
        old = reader.read_bytes(ASSETS, path)
    if old != raw:
        raise ValueError('terminal CHM audit object differs: ' + path)


def prepare(reader, writer, fence, rows, plan, progress, run_id, owner):
    if (not str(run_id).isdigit() or owner.get('id') != int(OLD_OWNER) or owner.get('status') != 'completed'
            or owner.get('path', '').split('@', 1)[0] != '.github/workflows/chm-epub-retirement.yml'):
        raise ValueError('old CHM callback owner is not terminal')
    progress_raw, maintenance_raw, retention_raw, current = preflight._identity(reader)
    window = json.loads(maintenance_raw)
    if (window.get('callback_owner_run_id') != OLD_OWNER or window.get('callback_mutation_started') is not True
            or window.get('callback_handoff_stage') != 'references-retired'
            or window.get('callback_committed') is True or current != progress
            or not isinstance(window.get('repositories'), dict)
            or any(record.get('cancelled') for record in window['repositories'].values())):
        raise ValueError('original CHM maintenance identity differs')
    fence.check()
    evidence = terminal_evidence(reader, rows, plan, progress)
    # The exact original audit and retention are saved separately, never changed.
    evidence_raw = model.encode(evidence)
    evidence_path = PREFIX + sha(evidence_raw) + '.json'
    save_exact(reader, writer, PREFIX + 'original-maintenance.json', maintenance_raw)
    save_exact(reader, writer, evidence_path, evidence_raw)
    preflight._identity(reader)
    fence.check()
    if terminal_evidence(reader, rows, plan, progress) != evidence:
        raise ValueError('CHM terminal evidence changed before maintenance handoff')
    # This is an explicit separate handoff, not a claim that the original
    # 600-receipt progress was itself updated to 972.
    updated = dict(window)
    updated.update(callback_committed=True, callback_recovery_required=False,
                   callback_handoff_ready=False, callback_handoff_stage='exact-terminal-bridge',
                   callback_owner_run_id=str(run_id),
                   terminal_handoff={'bucket': ASSETS, 'path': evidence_path, 'sha256': sha(evidence_raw)})
    new_raw = (json.dumps(updated, sort_keys=True, indent=2) + '\n').encode()
    if reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != maintenance_raw:
        raise ValueError('maintenance journal changed before terminal handoff')
    etag = reader.reader.read_etags.get((ASSETS, maintenance.DURABLE_JOURNAL))
    writer.put(maintenance.DURABLE_JOURNAL, new_raw, etag=etag)
    if reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != new_raw:
        raise RuntimeError('terminal maintenance handoff readback differs')
    return evidence_path, updated


def check_bridged(reader, rows, plan, progress, window):
    handoff = window.get('terminal_handoff')
    if (not isinstance(handoff, dict) or set(handoff) != {'bucket', 'path', 'sha256'}
            or handoff['bucket'] != ASSETS or not isinstance(handoff['path'], str)
            or handoff['path'] != PREFIX + str(handoff['sha256']) + '.json'
            or window.get('maintenance_id') != preflight.OWNER
            or window.get('callback_plan_sha256') != preflight.PLAN_SHA
            or window.get('callback_handoff_stage') != 'exact-terminal-bridge'
            or window.get('callback_committed') is not True or window.get('callback_recovery_required') is not False
            or not isinstance(window.get('repositories'), dict)
            or any(record.get('cancelled') for record in window['repositories'].values())):
        raise ValueError('terminal CHM bridge identity differs')
    raw = reader.read_bytes(ASSETS, handoff['path'])
    if sha(raw) != handoff['sha256'] or model.encode(terminal_evidence(reader, rows, plan, progress)) != raw:
        raise ValueError('terminal CHM bridge no longer matches exact receipts')
    if sha(reader.read_bytes(ASSETS, preflight.RETENTION)) != preflight.RETENTION_SHA:
        raise ValueError('original CHM reading retention changed')
    return handoff['path']


def filter_restore_scope(window):
    """Do not revive retired OCR/fork workers while restoring Reader workflows."""
    import copy
    try:
        from .reader_gc_fence import producer_path
    except ImportError:
        from reader_gc_fence import producer_path
    updated = copy.deepcopy(window)
    excluded = []
    for repo, record in updated['repositories'].items():
        if repo != 'anftm/pipeline' and record.get('permission_changed'):
            # Retired OCR execution repositories stay disabled; preserve the
            # original settings in the separately archived before image.
            record['permission_changed'] = False
            record['retired_execution_remains_disabled'] = True
            excluded.append(repo + ':actions')
        for workflow in record['workflows']:
            path = workflow.get('path', '')
            ocr = (path in {'.github/workflows/publish-ccrd-ocr.yml', '.github/workflows/stage-ccrd-ocr-text.yml',
                            '.github/workflows/force-ocr-book.yml', '.github/workflows/publish-source-txt.yml',
                            '.github/workflows/reader-publish-text.yml'}
                   or path.startswith('.github/workflows/reader-ocr-')
                   or path.startswith('.github/workflows/pdf-')
                   or path.startswith('.github/workflows/lin-native-text'))
            if workflow.get('state') == 'active' and (ocr or not producer_path(path)):
                workflow['state'] = 'disabled_manually'
                workflow['retired_from_restore'] = True
                excluded.append(repo + ':' + path)
    updated['terminal_restore_exclusions'] = sorted(excluded)
    return updated


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('CHM terminal release requires its central workflow')
    preflight.ensure_read_credentials()
    # Take over the saved callback only; do not revive OCR producers or entire
    # fork Actions policies recorded before the retirement decision.
    try:
        policy_path = Path(__file__).resolve().parents[1] / '.github/workflows/reader-chm-repair.yml'
        if not policy_path.is_file():
            raise RuntimeError('CHM conversion workflow is unavailable')
    except OSError as error:
        raise RuntimeError('CHM conversion workflow cannot be verified') from error
    reader = preflight.ReadOnlyStore()
    rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
    original_plan, _, updates, original_sha = original.load_plan(reader, rows)
    if (original_sha != preflight.PLAN_SHA
            or original_plan['overrides'].get(preflight.RETENTION) != reader.read_bytes(ASSETS, preflight.RETENTION)):
        raise ValueError('original CHM plan or reading retention differs')
    # The withdrawal's deployed sidecar, rollback aliases, processing pins,
    # and both Reader surfaces must still be the original reviewed handoff.
    # This does not scan new chapter bodies or raise Reader size limits.
    preflight.safety.verify_live_chm_controls(reader, original_plan)
    preflight.safety.verify_live_processing_controls(reader, updates)
    preflight.safety.verify_retired_rollback_aliases(reader, original_plan)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        preflight.safety.observe_planned_projection(reader, client, original_plan)
    progress_raw = reader.read_bytes(ASSETS, original_progress_path())
    if sha(progress_raw) != preflight.PROGRESS_SHA:
        raise ValueError('original CHM progress changed')
    progress = json.loads(progress_raw)
    classification = preflight.CLASSIFICATION.read_bytes()
    # bind_plan needs the original progress; do not require the old mutable
    # maintenance bytes once a prior terminal handoff has been written.
    indexed = {row['path']: row for row in rows}
    if sha(classification) != preflight.CLASSIFICATION_SHA:
        raise ValueError('CHM classification changed')
    entries = json.loads(classification)['targets']
    selected = sorted((indexed[x['path']] for x in entries if x['category'] == 'mapped-with-saved-evidence'),
                      key=lambda row: row['path'])
    withheld = sorted((indexed[x['path']] for x in entries if x['category'] in
                       {'needs-independent-review', 'withheld-oversize'}), key=lambda row: row['path'])
    plan = model.bind_plan(rows, progress['batches'], selected, withheld, final.locked.IDENTITY,
                           old_progress_raw=progress_raw)['plan']
    if model.digest(plan) != subset.PLAN_SHA:
        raise ValueError('fixed CHM terminal plan differs')
    fence = GitHubProducerFence(api=preflight.github_get)
    current_raw = reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL)
    window = json.loads(current_raw)
    if sha(current_raw) == preflight.MAINTENANCE_SHA:
        owner = fence.single('repos/anftm/pipeline/actions/runs/' + OLD_OWNER)
        evidence_path, window = prepare(reader, Writer(), fence, rows, plan, progress, os.environ['GITHUB_RUN_ID'], owner)
    else:
        evidence_path = check_bridged(reader, rows, plan, progress, window)
        old_owner = str(window.get('callback_owner_run_id') or '')
        if not old_owner.isdigit():
            raise ValueError('terminal CHM recovery owner is invalid')
        owner = fence.single('repos/anftm/pipeline/actions/runs/' + old_owner)
        if (old_owner != os.environ['GITHUB_RUN_ID'] and
                (owner.get('id') != int(old_owner) or owner.get('status') != 'completed'
                 or owner.get('path', '').split('@', 1)[0] != '.github/workflows/chm-terminal-release.yml')):
            raise ValueError('previous terminal CHM owner is still active or unverified')
        if window.get('restored') is not True and old_owner != os.environ['GITHUB_RUN_ID']:
            if not os.environ.get('READER_GC_DURABLE_MAINTENANCE') == 'true':
                raise RuntimeError('terminal CHM recovery requires durable maintenance')
            updated = {**window, 'callback_owner_run_id': os.environ['GITHUB_RUN_ID']}
            raw = (json.dumps(updated, sort_keys=True, indent=2) + '\n').encode()
            if reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != current_raw:
                raise ValueError('terminal CHM recovery cursor changed')
            etag = reader.reader.read_etags.get((ASSETS, maintenance.DURABLE_JOURNAL))
            Writer().put(maintenance.DURABLE_JOURNAL, raw, etag=etag)
            if reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != raw:
                raise RuntimeError('terminal CHM recovery cursor readback differs')
            window = updated
    if window.get('restored') is not True and 'terminal_restore_exclusions' not in window:
        current_raw = reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL)
        if json.loads(current_raw) != window:
            raise ValueError('terminal CHM restore scope cursor changed')
        updated = filter_restore_scope(window)
        raw = (json.dumps(updated, sort_keys=True, indent=2) + '\n').encode()
        etag = reader.reader.read_etags.get((ASSETS, maintenance.DURABLE_JOURNAL))
        Writer().put(maintenance.DURABLE_JOURNAL, raw, etag=etag)
        if reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != raw:
            raise RuntimeError('terminal restore scope readback differs')
        window = updated
    LOCAL_MAINTENANCE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_MAINTENANCE.write_text(json.dumps(window, sort_keys=True, indent=2) + '\n')
    if window.get('restored') is not True:
        if os.environ.get('READER_GC_DURABLE_MAINTENANCE') != 'true':
            raise RuntimeError('terminal CHM restoration requires durable maintenance')
        maintenance.restore(LOCAL_MAINTENANCE, expected_owner=os.environ['GITHUB_RUN_ID'],
                            expected_maintenance_id=preflight.OWNER)
    restored = json.loads(reader.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL))
    if (restored.get('restored') is not True or restored.get('callback_recovery_required')
            or any(record.get('restored') is not True for record in restored.get('repositories', {}).values())):
        raise RuntimeError('producer restoration requires recovery')
    result = {'kind': 'chm-terminal-maintenance-release', 'handoff_path': evidence_path,
              'verified_absent_objects': 972, 'original_progress_unchanged': True,
              'maintenance_restored': True, 'ordinary_gc_authorized': False,
              'caveats': ['original CHM progress remains 600 and is superseded only by separate terminal evidence',
                          'ordinary GC needs a separate reviewed handoff',
                          'external S3/Hub writers are not covered by Actions locks']}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, sort_keys=True, indent=2)
        file.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


def original_progress_path():
    return 'reader-index/gc/chm-epub-withdrawal/20261030/progress.json'


if __name__ == '__main__':
    main()
