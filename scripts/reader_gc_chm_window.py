"""Fail-closed generic-GC guard for the interrupted exact CHM transaction.

An ordinary collector is not the owner of the CHM callback or its 972-object
journal. Never treat the absence of a local file, a hypothetical graph, a
subset receipt, or an unreadable bucket as permission to bypass that callback.
This guard does not restore producers or authorize any deletion by itself.
"""
import json

try:
    from . import reader_gc_maintenance as maintenance
    from . import shared
except ImportError:
    import reader_gc_maintenance as maintenance
    import shared

CHM_PREFIX = 'reader-index/gc/chm-epub-withdrawal/20261030/'
PLAN = CHM_PREFIX + 'plan.json.gz'
PLAN_SHA = CHM_PREFIX + 'plan.sha256'
PROGRESS = CHM_PREFIX + 'progress.json'
MARKERS = (PLAN, PLAN_SHA, PROGRESS)
ORIGINAL_MAINTENANCE = '0507a077f1ee4c99b8c64c0de7b58035'
ORIGINAL_PLAN_SHA = 'bc04c76ecf0977a65796d7eb8a982d778a191d7821a40077a07e8bb382bba3d4'


def require_clear(store):
    """Block generic GC until this known CHM transaction has a release protocol.

    An absent marker, missing transaction history, or a terminal-looking
    mutable cursor is never sufficient. The separate release must bind the
    original evidence, complete journals and restored maintenance archive.
    Callers still need their ordinary locks, graph and producer fence; this
    check cannot prevent an external writer or an out-of-band ABA race.
    """
    # A release must be a separately reviewed, content-bound terminal bridge.
    # Missing markers and terminal-looking mutable cursors still fail closed.
    if not callable(getattr(store, 'read_bytes', None)):
        raise RuntimeError('CHM maintenance guard cannot independently list and read its controls')
    try:
        from . import chm_gc_terminal_guard as terminal
    except ImportError:
        import chm_gc_terminal_guard as terminal
    try:
        release_raw = store.read_bytes(shared.READER_ASSETS_BUCKET, terminal.PREFIX + 'gc-release.json')
    except FileNotFoundError:
        release_raw = None
    if release_raw is not None:
        return terminal.verify(store)
    # Most deployments have the small durable maintenance cursor. Read it
    # first: the known CHM owner or any unfinished callback is sufficient to
    # stop, without listing the entire GC history on every batch. Even a
    # terminal-looking cursor is not a release and falls through to the
    # existing fail-closed evidence checks below.
    try:
        current_raw = store.read_bytes(shared.READER_ASSETS_BUCKET, maintenance.DURABLE_JOURNAL)
    except FileNotFoundError:
        current_raw = None
    if current_raw is not None:
        try:
            current = json.loads(current_raw)
        except (TypeError, ValueError) as error:
            raise RuntimeError('invalid maintenance journal blocks generic GC') from error
        if (not isinstance(current, dict) or current.get('kind') != 'reader-maintenance-journal'
                or current.get('version') != 1):
            raise RuntimeError('invalid maintenance journal blocks generic GC')
        if (current.get('maintenance_id') == ORIGINAL_MAINTENANCE
                or current.get('callback_recovery_required')
                or current.get('callback_mutation_started') and not current.get('callback_committed')):
            raise RuntimeError('unfinished CHM callback or maintenance journal blocks generic GC')
    listing = getattr(store, 'list_files', None)
    if not callable(listing) or not callable(getattr(store, 'read_bytes', None)):
        raise RuntimeError('CHM maintenance guard cannot independently list and read its controls')
    before = listing(shared.READER_ASSETS_BUCKET, (CHM_PREFIX,))
    if not isinstance(before, set) or any(not isinstance(path, str) for path in before):
        raise RuntimeError('CHM maintenance guard received invalid inventory')
    def assert_bucket_available():
        try:
            if callable(getattr(store, '_location', None)) and callable(getattr(store, '_client', None)):
                namespace, bucket = store._location(shared.READER_ASSETS_BUCKET)
                store._client(namespace).head_bucket(Bucket=bucket)
            elif callable(getattr(getattr(store, 'api', None), 'bucket_info', None)):
                info = store.api.bucket_info(shared.READER_ASSETS_BUCKET, token=store.token(shared.READER_ASSETS_BUCKET))
                if getattr(info, 'id', None) != shared.READER_ASSETS_BUCKET:
                    raise ValueError('bucket identity differs')
            else:
                raise ValueError('bucket availability cannot be checked')
        except Exception as error:
            raise RuntimeError('CHM bucket availability is unverified') from error

    assert_bucket_available()
    present = set(MARKERS) & before
    if not present:
        if before:
            raise RuntimeError('unrecognized CHM transaction evidence blocks generic GC')
        # An empty LIST alone is not proof of a fresh bucket. In particular,
        # missing controls may reflect deletion or a stale snapshot. Until a
        # separately reviewed release protocol exists, preserve the original
        # retention root and its 940 chapter references as a blocking marker.
        try:
            store.read_bytes(shared.READER_ASSETS_BUCKET, 'reader-index/chm-epub-withdrawal-retention.json')
        except FileNotFoundError:
            assert_bucket_available()
        else:
            raise RuntimeError('CHM retention root blocks generic GC pending reviewed release')
        try:
            window_raw = store.read_bytes(shared.READER_ASSETS_BUCKET, maintenance.DURABLE_JOURNAL)
        except FileNotFoundError as error:
            raise RuntimeError('missing maintenance journal blocks generic GC') from error
        if window_raw != current_raw:
            raise RuntimeError('CHM maintenance journal changed during GC guard')
        try:
            window = json.loads(window_raw)
        except (TypeError, ValueError) as error:
            raise RuntimeError('invalid maintenance journal blocks generic GC') from error
        if (not isinstance(window, dict) or window.get('kind') != 'reader-maintenance-journal'
                or window.get('version') != 1 or window.get('maintenance_id') == ORIGINAL_MAINTENANCE
                or (window.get('callback_mutation_started') and not window.get('callback_committed'))
                or window.get('callback_recovery_required')):
            raise RuntimeError('maintenance journal does not clear an unfinished CHM callback')
        if (listing(shared.READER_ASSETS_BUCKET, (CHM_PREFIX,)) != before
                or store.read_bytes(shared.READER_ASSETS_BUCKET, maintenance.DURABLE_JOURNAL) != window_raw):
            raise RuntimeError('CHM maintenance inventory changed during GC guard')
        try:
            store.read_bytes(shared.READER_ASSETS_BUCKET, 'reader-index/chm-epub-withdrawal-retention.json')
        except FileNotFoundError:
            assert_bucket_available()
        else:
            raise RuntimeError('CHM retention root appeared during GC guard')
        # Both original controls and the retention root could have been removed,
        # while another window overwrote the mutable maintenance cursor. Never
        # interpret this loss of evidence as permission to run generic GC.
        raise RuntimeError('CHM transaction evidence is absent; generic GC remains blocked pending reviewed release')
    # Even intact, terminal-looking markers cannot release this known window.
    # Do not repeatedly read the original (potentially very large) plan here;
    # diagnostics can verify its bytes separately without issuing GC writes.
    raise RuntimeError('CHM transaction controls are present; generic GC remains blocked pending reviewed release')
