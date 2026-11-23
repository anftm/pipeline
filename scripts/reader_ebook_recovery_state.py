"""Classify an exact EPUB recovery snapshot without performing any I/O."""
from typing import Literal


RootState = Literal['parent', 'target']
PermitState = Literal['absent', 'exact']
LegacyState = Literal['catalog-permitted', 'sidecar-permitted', 'legacy-observed']
Phase = Literal['canonical', 'legacy-start', 'legacy-resume', 'v3', 'pages']


def classify(*, stage_complete: bool, canonical_state: RootState,
             canonical_permit: PermitState, catalog_state: RootState,
             sidecar_state: RootState, legacy_state: LegacyState | None,
             pointer_state: RootState, v3_permit: PermitState) -> Phase:
    """Return the next phase or raise ValueError; never replay a spent root write.

    The caller must verify the exact stage, root identities and permit/journal
    bindings before supplying these labels. None and 'absent' mean verified
    absence, not missing evidence. A phase is not permission to write: the
    caller still needs the fence and the transport's checks at each boundary.
    """
    if type(stage_complete) is not bool:
        raise ValueError('stage_complete must be a bool')
    for name, state in (('canonical_state', canonical_state), ('catalog_state', catalog_state),
                        ('sidecar_state', sidecar_state), ('pointer_state', pointer_state)):
        if type(state) is not str or state not in ('parent', 'target'):
            raise ValueError(f'{name} must be parent or target')
    for name, permit in (('canonical_permit', canonical_permit), ('v3_permit', v3_permit)):
        if type(permit) is not str or permit not in ('absent', 'exact'):
            raise ValueError(f'{name} must be absent or exact')
    if legacy_state is not None and (type(legacy_state) is not str or legacy_state not in (
            'catalog-permitted', 'sidecar-permitted', 'legacy-observed')):
        raise ValueError('invalid legacy_state')
    if not stage_complete:
        raise ValueError('exact staging is incomplete')

    legacy_roots = catalog_state, sidecar_state
    v3_unstarted = pointer_state == 'parent' and v3_permit == 'absent'
    if canonical_state == 'parent':
        if canonical_permit == 'exact':
            raise ValueError('canonical permit spent but parent is live; do not replay')
        if legacy_roots != ('parent', 'parent') or legacy_state is not None or not v3_unstarted:
            raise ValueError('downstream progress before canonical successor')
        return 'canonical'
    if canonical_permit != 'exact':
        raise ValueError('canonical target without an exact permit')

    if legacy_state != 'legacy-observed' and not v3_unstarted:
        raise ValueError('v3 progress before durable legacy observation')
    if legacy_state is None:
        if legacy_roots != ('parent', 'parent'):
            raise ValueError('legacy target without an exact journal record')
        return 'legacy-start'
    if legacy_state == 'catalog-permitted':
        if catalog_state == 'parent':
            raise ValueError('legacy catalog permit spent but parent is live; do not replay')
        if sidecar_state != 'parent':
            raise ValueError('legacy sidecar target before its permit')
        return 'legacy-resume'
    if legacy_state == 'sidecar-permitted':
        if catalog_state != 'target':
            raise ValueError('legacy sidecar permit without catalog target')
        if sidecar_state == 'parent':
            raise ValueError('legacy sidecar permit spent but parent is live; do not replay')
        return 'legacy-resume'

    if legacy_roots != ('target', 'target'):
        raise ValueError('legacy-observed requires both legacy targets')
    if v3_unstarted:
        return 'v3'
    if pointer_state == 'parent':
        raise ValueError('v3 permit spent but parent is live; do not replay')
    if v3_permit != 'exact':
        raise ValueError('v3 pointer target without an exact permit')
    return 'pages'
