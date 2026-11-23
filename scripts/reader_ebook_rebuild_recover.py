"""Inspect or advance one exact, sealed EPUB release phase; never restage."""
from __future__ import annotations

import json
from tempfile import TemporaryFile

from . import publish_reader_v3 as v3, reader_ebook_canonical_guard as index
from . import reader_ebook_recovery_state as state
from . import reader_ebook_rebuild_transport as transport
from . import reader_image_history_publish as legacy, reader_image_history_v3 as v3_permit
from .reader_image_history_parent import CatalogParent
from .reader_image_history_publish_journal import LegacyJournal
from .reader_page_cas import strong_etag


class RecoveryBlocked(ValueError):
    """No mutation permitted from these observations."""


def need(condition, why):
    if not condition:
        raise RecoveryBlocked(why)


def identity(actual, parent, target, *, tag, parent_tag, label):
    need(parent != target, f'ambiguous {label} identities')
    tag = strong_etag(tag)
    parent_tag = strong_etag(parent_tag)
    if actual == parent and tag == parent_tag:
        return 'parent'
    if actual == target and tag != parent_tag:
        return 'target'
    raise RecoveryBlocked(f'{label} differs from the approved parents or targets')


def claim_status(observed, expected, label):
    need(observed is None or observed == expected, 'foreign ' + label + ' permit')
    return 'absent' if observed is None else 'exact'


def observe(io, review, approval, parents):
    """Produce a phase recommendation from exact persisted state; no PUT or POST."""
    bucket = transport.BUCKET
    io._check('recovery:inspect')
    raw_index, before, raw_pointer = (parents[k] for k in ('index', 'sidecar', 'pointer'))
    specs = transport._replacements(review)
    etags = review['parent_etags']
    parent_sha = review['pointer_parent_sha256']
    need((transport._sha(raw_index), transport._sha(before), transport._sha(raw_pointer)) ==
         (review['canonical_parent_sha256'], review['sidecar_parent_sha256'], parent_sha), 'recovery inputs pin mismatch')
    target_index = index.build_successor(raw_index, reviewed=specs, operation_id=review['operation'])
    need(transport._sha(target_index) == review['target_index_sha256'], 'reconstructed EPUB index differs')
    _records, _prior, members = io._stage_plan(review, approval)
    ref = io._snapshot_ref(review)
    snapshot, _ = io.claims.transport._get(ref['path'], maximum=ref['bytes'])
    need(snapshot is not None, 'staged snapshot absent; do not restage')
    staged = io.stage(review=review, review_sha256=approval, bundles=None, after_sidecar=snapshot, observe_only=True)
    need(staged == io._receipt(approval, _records, members), 'stage evidence differs')
    canonical_path, canonical_seal = index.permit(raw_index, etags['canonical'], target_index, specs, review['operation'],
                                                   {'review': approval, 'stage': staged['sha256']})
    canonical_permit = claim_status(io.claims.read(bucket, canonical_path), canonical_seal, 'canonical')
    current, tag = io.checkpoints.read(bucket, index.INDEX)
    need(io.reader.read_bytes(bucket, index.INDEX) == current, 'canonical EPUB views disagree')
    canonical_state = identity(current, raw_index, target_index, tag=tag, parent_tag=etags['canonical'], label='canonical index')
    summary = review['legacy']
    catalog_parent = legacy.Version(summary['parent_sha256'], summary['parent_bytes'], etags['catalog'])
    sidecar_parent = legacy.Version(transport._sha(before), len(before), etags['sidecar'])
    catalog_target = summary['target_sha256'], summary['target_bytes']
    sidecar_target = ref['sha256'], ref['bytes']
    legacy_seal = legacy.transition_seal(transport.LEGACY_KIND, catalog_parent, sidecar_parent, catalog_target,
                                         sidecar_target, raw_pointer)
    record = LegacyJournal(io.client).read(catalog_parent.sha256)
    if record is not None:
        legacy._bind(record, legacy_seal)
    catalog = io.roots.observe(legacy.CATALOG)
    sidecar = io.roots.observe(legacy.SIDECAR)
    need(io.reader.digest(bucket, legacy.CATALOG) == catalog.sha256 and
         io.reader.digest(bucket, legacy.SIDECAR) == sidecar.sha256, 'legacy bucket and resolver views disagree')
    catalog_state = identity((catalog.sha256, catalog.size), (catalog_parent.sha256, catalog_parent.size), catalog_target,
                             tag=catalog.etag, parent_tag=etags['catalog'], label='legacy catalog')
    sidecar_state = identity((sidecar.sha256, sidecar.size), (sidecar_parent.sha256, sidecar_parent.size), sidecar_target,
                             tag=sidecar.etag, parent_tag=etags['sidecar'], label='legacy sidecar')
    if record:
        need(record.catalog_etag in (None, catalog.etag) and record.sidecar_etag in (None, sidecar.etag),
             'legacy record no longer matches observed ETags')
    pointer_state = 'parent' if io._pointer_state(review, raw_pointer, allow_target=True) == raw_pointer else 'target'
    observed_v3, _ = io.claims.transport._get(transport._v3_permit_path(parent_sha), maximum=v3_permit.LIMIT)
    v3_seal = v3.encode({'version': 1, 'kind': 'reader-ebook-rebuild-v3-permit', 'parent_sha256': parent_sha,
                         'pointer':review['target_pointer'], 'canonical_sha256':ref['sha256'], 'legacy_seal':legacy_seal})
    next_phase = state.classify(stage_complete=True, canonical_state=canonical_state, canonical_permit=canonical_permit,
                                catalog_state=catalog_state, sidecar_state=sidecar_state,
                                legacy_state=record.state if record else None,
                                pointer_state=pointer_state, v3_permit=claim_status(observed_v3, v3_seal, 'v3'))
    if pointer_state == 'target':
        pointer, catalog_payload = v3.current(io.reader)
        need(pointer == review['target_pointer'] and catalog_payload.get('parent_generation') == review['reader_generation'],
             'v3 target catalog does not match sealed review')
    io._check('recovery:inspect')
    evidence = {'next_phase':next_phase, 'canonical':canonical_state, 'catalog':catalog_state,
                'sidecar':sidecar_state, 'pointer':pointer_state, 'legacy_record':record.state if record else None}
    return evidence, {'stage':staged, 'snapshot':snapshot, 'catalog_parent':catalog_parent,
                      'legacy_result':{'catalog':catalog, 'sidecar':sidecar, 'seal':legacy_seal,
                                       'claim_id':catalog_parent.sha256, 'pointer_parent_sha256':parent_sha,
                                       'unify':{'expected_pointer':json.loads(raw_pointer)}}}


def run(io, review, approval, parents, phase):
    """Execute only the explicitly requested next phase, or inspect without writes."""
    evidence, data = observe(io, review, approval, parents)
    if phase == 'inspect':
        return {'kind':'reader-ebook-recovery', 'phase':phase, **evidence, 'remote_writes':0}
    need(phase == evidence['next_phase'], 'requested recovery phase does not match live state')
    if phase == 'canonical':
        result = io.canonical(review=review, review_sha256=approval, staged=data['stage'], parent_raw=parents['index'],
                              parent_etag=review['parent_etags']['canonical'])
    elif phase in ('legacy-start', 'legacy-resume'):
        parent = data['catalog_parent']
        archived = CatalogParent(io.client, digest=parent.sha256, size=parent.size, etag=parent.etag)
        with TemporaryFile() as raw:
            archived.load(raw)
            result = io.legacy(review=review, parent_catalog=raw, parent=parent, before_sidecar=parents['sidecar'],
                               after_sidecar=data['snapshot'], sidecar_etag=review['parent_etags']['sidecar'],
                               parent_raw=parents['pointer'], resume=phase == 'legacy-resume')
        # The result holds dataclasses and proposed kwargs. Report only JSON evidence.
        result = {k:result[k] for k in ('claim_id','seal','sha256','catalog_sha256')}
    elif phase in ('v3', 'pages'):
        result = io.promote(review=review, parent_raw=parents['pointer'], legacy_result=data['legacy_result'],
                            observe_only=phase == 'pages')
        if phase == 'pages':
            proof = review['pages_authority']
            return {**result, 'applied':True, 'pages_pins':{'code_sha':proof['code_sha'],
                     'search_generation':proof['search_generation']}, 'changes':review['changes']}
    else:
        raise RecoveryBlocked('unrecognized EPUB recovery phase')
    return {'kind':'reader-ebook-recovery', 'phase':phase, **evidence, 'result':result}
