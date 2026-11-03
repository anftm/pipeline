"""Validate a completed CHM maintenance handoff before ordinary GC resumes.

This is a read-only guard, not a collector or a mutation entrypoint. Callers
still need the normal GC locks, current graph, producer fence and site checks.
The old 600-item progress and both independent deletion journals are retained.
"""
import hashlib
import json
from pathlib import Path
import re

try:
    from . import chm_epub_journal as original, chm_final20_journal as final
    from . import chm_subset_journal as journal, chm_subset_journal_backend as chain
    from . import chm_subset_locked_graph as locked, chm_subset_locked_preflight as preflight
    from . import chm_subset_s3_journal as subset, reader_gc_maintenance as maintenance
    from .chm_terminal_release import filter_restore_scope
except ImportError:
    import chm_epub_journal as original
    import chm_final20_journal as final
    import chm_subset_journal as journal
    import chm_subset_journal_backend as chain
    import chm_subset_locked_graph as locked
    import chm_subset_locked_preflight as preflight
    import chm_subset_s3_journal as subset
    import reader_gc_maintenance as maintenance
    from chm_terminal_release import filter_restore_scope

ASSETS = preflight.chm.ASSETS
PREFIX = 'reader-index/gc/chm-terminal/0507a077f1ee4c99b8c64c0de7b58035/'
FINAL_PREFIX = 'reader-index/gc/chm-final20/' + locked.IDENTITY['transaction_id'] + '/'
FINAL_NAME = re.compile(r'(?:plan|event-[0-9]{8})\.json')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw, label):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise RuntimeError('invalid ' + label) from error
    if not isinstance(value, dict):
        raise RuntimeError('invalid ' + label)
    return value


def current_state(raw):
    """Reject malformed or unfinished callbacks, including later windows."""
    state = parse(raw, 'current maintenance')
    if (state.get('kind') != 'reader-maintenance-journal' or state.get('version') != 1
            or not re.fullmatch(r'[0-9a-f]{32}', str(state.get('maintenance_id', '')))
            or state.get('maintenance_id') == '0' * 32
            or not isinstance(state.get('repositories'), dict) or not state['repositories']
            or any(not isinstance(record, dict) for record in state['repositories'].values())
            or any(key in state and type(state[key]) is not bool for key in
                   ('callback_recovery_required', 'callback_mutation_started', 'callback_committed', 'restored'))
            or state.get('callback_recovery_required') is True
            or state.get('callback_mutation_started') is True and state.get('callback_committed') is not True
            or state.get('restored') is True and
               any(record.get('restored') is not True for record in state['repositories'].values())):
        raise RuntimeError('current maintenance has an invalid or unfinished callback')
    return state


def assert_bucket_available(store):
    """Do not interpret a missing bucket as an absent protection root."""
    check = getattr(store, 'assert_chm_bucket_available', None)
    if callable(check):
        if check() is not True:
            raise RuntimeError('CHM bucket availability is unverified')
    elif callable(getattr(store, '_location', None)) and callable(getattr(store, '_client', None)):
        namespace, name = store._location(ASSETS)
        store._client(namespace).head_bucket(Bucket=name)
    elif callable(getattr(getattr(store, 'api', None), 'bucket_info', None)):
        info = store.api.bucket_info(ASSETS, token=store.token(ASSETS))
        if getattr(info, 'id', None) != ASSETS:
            raise RuntimeError('CHM bucket identity differs')
    else:
        raise RuntimeError('CHM bucket availability is unverified')


def restored_state(raw):
    state = parse(raw, 'terminal maintenance')
    handoff = state.get('terminal_handoff')
    if (state.get('kind') != 'reader-maintenance-journal' or state.get('version') != 1
            or state.get('maintenance_id') != preflight.OWNER
            or state.get('callback_plan_sha256') != preflight.PLAN_SHA
            or state.get('callback_handoff_stage') != 'exact-terminal-bridge'
            or state.get('callback_committed') is not True or state.get('callback_recovery_required') is not False
            or state.get('restored') is not True or not isinstance(state.get('repositories'), dict)
            or not state['repositories'] or any(not isinstance(item, dict) or item.get('restored') is not True
                                                for item in state['repositories'].values())
            or not isinstance(handoff, dict) or set(handoff) != {'bucket', 'path', 'sha256'}
            or handoff.get('bucket') != ASSETS or not re.fullmatch(r'[0-9a-f]{64}', str(handoff.get('sha256')))
            or handoff.get('path') != PREFIX + handoff['sha256'] + '.json'):
        raise RuntimeError('CHM terminal maintenance is not restored')
    return state


def verify_restore_scope(original_raw, history):
    """Bind the archived restoration claims to the exact pre-bridge snapshot."""
    before = parse(original_raw, 'original CHM maintenance')
    expected = filter_restore_scope(before)
    actual_repos = history['repositories']
    if (set(actual_repos) != set(expected['repositories'])
            or history.get('terminal_restore_exclusions') != expected['terminal_restore_exclusions']
            or expected['repositories'].get('anftm/pipeline', {}).get('permission_changed')):
        raise RuntimeError('CHM restored producer scope differs from original maintenance')
    for repo, record in expected['repositories'].items():
        restored = actual_repos[repo]
        for field in ('permission', 'permission_changed', 'workflows', 'cancelled',
                      'retired_execution_remains_disabled'):
            if restored.get(field) != record.get(field):
                raise RuntimeError('CHM restored producer scope differs: ' + repo)


def verify_retention(store):
    """Require the fixed chapter protection until a successor is reviewed."""
    raw = store.read_bytes(ASSETS, preflight.RETENTION)
    if sha(raw) != preflight.RETENTION_SHA:
        raise RuntimeError('original CHM chapter retention differs')


class ReadOnlyJournal:
    def __init__(self, store, prefix, pattern):
        self.store, self.prefix, self.pattern = store, prefix, pattern
    def names(self):
        paths = self.store.list_files(ASSETS, (self.prefix,))
        names = {path[len(self.prefix):] for path in paths if path.startswith(self.prefix)}
        if len(names) != len(paths) or any(not self.pattern.fullmatch(name) for name in names):
            raise RuntimeError('CHM terminal journal inventory differs')
        return names
    def read(self, name):
        if not self.pattern.fullmatch(name):
            raise RuntimeError('CHM terminal journal key differs')
        try:
            return self.store.read_bytes(ASSETS, self.prefix + name)
        except FileNotFoundError:
            return None
    def create(self, *_):
        raise AssertionError('GC CHM guard is read-only')


def validate_gc_snapshot(marker_raw, history_raw, current_raw, evidence_raw, subset_head, final_head):
    """Validate bound terminal claims without authenticating their origin."""
    marker = parse(marker_raw, 'CHM GC release marker')
    if (set(marker) != {'kind', 'version', 'maintenance_sha256', 'handoff_sha256'}
            or marker.get('kind') != 'chm-gc-release' or marker.get('version') != 1
            or not re.fullmatch(r'[0-9a-f]{64}', str(marker.get('maintenance_sha256')))
            or not re.fullmatch(r'[0-9a-f]{64}', str(marker.get('handoff_sha256')))
            or sha(history_raw) != marker['maintenance_sha256']):
        raise RuntimeError('invalid CHM GC release marker or history')
    history = restored_state(history_raw)
    exclusions = history.get('terminal_restore_exclusions')
    if (not isinstance(exclusions, list)
            or 'anftm/pipeline:.github/workflows/publish-ccrd-ocr.yml' not in exclusions
            or 'anftm/pipeline:.github/workflows/reader-publish-text.yml' not in exclusions):
        raise RuntimeError('CHM terminal restore did not exclude retired text and OCR producers')
    if history['terminal_handoff']['sha256'] != marker['handoff_sha256']:
        raise RuntimeError('CHM GC marker and maintenance handoff differ')
    current = current_state(current_raw)
    if current.get('maintenance_id') == preflight.OWNER:
        restored_state(current_raw)
        if current['terminal_handoff'] != history['terminal_handoff']:
            raise RuntimeError('current CHM terminal handoff differs')
    evidence = parse(evidence_raw, 'terminal CHM handoff')
    expected = {'kind': 'chm-exact-terminal-handoff', 'version': 1,
                'inventory_sha256': preflight.chm.preview.INVENTORY_SHA256,
                'original_plan_sha256': preflight.PLAN_SHA, 'original_progress_sha256': preflight.PROGRESS_SHA,
                'original_maintenance_sha256': preflight.MAINTENANCE_SHA,
                'original_receipts_sha256': locked.IDENTITY['old_receipts_sha256'], 'original_confirmed': 600,
                'subset_plan_sha256': subset.PLAN_SHA, 'subset_head_sha256': journal.digest(subset_head),
                'subset_confirmed': 352, 'final20_head_sha256': journal.digest(final_head), 'final20_confirmed': 20,
                'verified_absent_objects': 972, 'verified_absent_bytes': preflight.chm.preview.DATA_BYTES,
                'original_progress_unchanged': True, 'ordinary_gc_authorized': False}
    if sha(evidence_raw) != marker['handoff_sha256'] or evidence != expected or journal.encode(evidence) != evidence_raw:
        raise RuntimeError('CHM terminal handoff or journal heads differ')
    return True


def verify(store, *, candidate_marker=None):
    # Archive is mandatory because a future maintenance window may replace the
    # mutable current cursor. Exact archive SHA is linked from the GC marker.
    marker_path = PREFIX + 'gc-release.json'
    assert_bucket_available(store)
    try:
        existing_marker = store.read_bytes(ASSETS, marker_path)
    except FileNotFoundError:
        existing_marker = None
    if candidate_marker is None:
        if existing_marker is None:
            raise RuntimeError('CHM GC release marker is missing')
        marker_raw = existing_marker
    else:
        if not isinstance(candidate_marker, bytes) or existing_marker not in (None, candidate_marker):
            raise RuntimeError('CHM GC release marker conflicts with candidate')
        marker_raw = candidate_marker
    marker = parse(marker_raw, 'CHM GC release marker')
    if (set(marker) != {'kind', 'version', 'maintenance_sha256', 'handoff_sha256'}
            or marker.get('kind') != 'chm-gc-release' or marker.get('version') != 1
            or not re.fullmatch(r'[0-9a-f]{64}', str(marker.get('maintenance_sha256')))
            or not re.fullmatch(r'[0-9a-f]{64}', str(marker.get('handoff_sha256')))):
        raise RuntimeError('invalid CHM GC release marker')
    history_path = maintenance.DURABLE_HISTORY_PREFIX + marker['maintenance_sha256'] + '.json'
    history_raw = store.read_bytes(ASSETS, history_path)
    if sha(history_raw) != marker['maintenance_sha256']:
        raise RuntimeError('CHM restored maintenance archive differs')
    history = restored_state(history_raw)
    if history['terminal_handoff']['sha256'] != marker['handoff_sha256']:
        raise RuntimeError('CHM GC marker and maintenance handoff differ')
    current_raw = store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL)
    current = current_state(current_raw)
    if current.get('maintenance_id') == preflight.OWNER:
        restored_state(current_raw)
        if current['terminal_handoff'] != history['terminal_handoff']:
            raise RuntimeError('current CHM terminal handoff differs')
    # The fixed progress SHA binds the original plan digest. Avoid streaming
    # its potentially huge plan bytes on every GC checkpoint/batch.
    progress_raw = store.read_bytes(ASSETS, original.PROGRESS)
    if sha(progress_raw) != preflight.PROGRESS_SHA:
        raise RuntimeError('original CHM progress differs')
    progress = parse(progress_raw, 'original CHM progress')
    original_maintenance = store.read_bytes(ASSETS, PREFIX + 'original-maintenance.json')
    if (store.read_bytes(ASSETS, original.PLAN_SHA).decode('ascii').strip() != preflight.PLAN_SHA
            or sha(original_maintenance) != preflight.MAINTENANCE_SHA):
        raise RuntimeError('original CHM plan or maintenance snapshot differs')
    verify_restore_scope(original_maintenance, history)
    try:
        store.read_bytes(ASSETS, locked.isolated.root_path(locked.IDENTITY['transaction_id']))
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError('temporary CHM 20-object root still exists')
    rows = preflight.chm.preview.load_inventory(Path(__file__).resolve().parents[1] /
                                                'config/chm-history-inventory-20261030.json')['targets']
    classification_raw = preflight.CLASSIFICATION.read_bytes()
    if sha(classification_raw) != preflight.CLASSIFICATION_SHA:
        raise RuntimeError('CHM classification differs')
    indexed = {row['path']: row for row in rows}
    items = json.loads(classification_raw)['targets']
    selected = sorted((indexed[item['path']] for item in items if item['category'] == 'mapped-with-saved-evidence'),
                      key=lambda row: row['path'])
    withheld = sorted((indexed[item['path']] for item in items if item['category'] in
                       {'needs-independent-review', 'withheld-oversize'}), key=lambda row: row['path'])
    plan = journal.bind_plan(rows, progress['batches'], selected, withheld, locked.IDENTITY,
                             old_progress_raw=progress_raw)['plan']
    if journal.digest(plan) != subset.PLAN_SHA:
        raise RuntimeError('CHM subset plan differs')
    backend = ReadOnlyJournal(store, subset.PREFIX, subset.NAME)
    if journal.digest(json.loads(backend.read('event-00000000.json'))) != subset.INITIAL_SHA:
        raise RuntimeError('CHM subset initial event differs')
    head = chain.Cursor(backend, plan).head()
    final_plan = final.fixed_plan(withheld, subset.PLAN_SHA, locked.ROOT_SHA)
    final_head = final.Cursor(ReadOnlyJournal(store, FINAL_PREFIX, FINAL_NAME), final_plan).head()
    if (head['state']['subset_complete'] is not True or head['state']['pending'] is not None
            or sum(len(item['paths']) for item in head['state']['receipts']) != 352
            or final_head['state']['complete'] is not True or final_head['state']['pending'] is not None
            or final_head['state']['receipt'] != [row['path'] for row in withheld]):
        raise RuntimeError('CHM deletion journal is incomplete')
    evidence_raw = store.read_bytes(ASSETS, history['terminal_handoff']['path'])
    validate_gc_snapshot(marker_raw, history_raw, current_raw, evidence_raw, head, final_head)
    try:
        marker_after = store.read_bytes(ASSETS, marker_path)
    except FileNotFoundError:
        marker_after = None
    if (marker_after != existing_marker
            or store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != current_raw
            or store.read_bytes(ASSETS, history_path) != history_raw):
        raise RuntimeError('CHM GC release controls changed during verification')
    assert_bucket_available(store)
    return True
