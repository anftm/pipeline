"""Transport for a sealed 1–3 EPUB review. No CLI, clients, or dispatch.

The caller owns the exact review/archive, continuous writer and GC isolation,
and independent live source/consumer proof. ``guard(phase)`` must freshly
verify that authority and the prerequisites appropriate to the named phase
(``stage:*``, ``canonical:*``, ``legacy:*``, or ``v3:*``). The caller verifies
``review_sha256`` against the sealed archive; it is not a digest of a new
serialization. Conditional writes and journals cannot replace the lease or
prevent ABA.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from . import publish_reader_v3 as v3, reader_ebook_canonical_guard as index
from . import reader_ebook_rebuild_review as review_io
from . import reader_epub_immutable as immutable, reader_epub_protection as protection
from . import reader_image_history_catalog as legacy_catalog
from . import reader_image_history_publish as legacy, reader_image_history_v3 as v3_permit
from . import search_snapshot
from .reader_exact_office_stage import plan_legacy  # Same compact chapter route format.
from .reader_image_history_publish_journal import LegacyJournal
from .reader_image_history_publish_s3 import LegacyRoots
from .reader_image_history_store import S3ClaimStore
from .reader_page_cas import ConditionalPageStore, strong_etag
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .reader_web_exact_apply import PointerStore

BUCKET = index.BUCKET
CLAIM_LIMIT = 8192
LEGACY_KIND = 'reader-ebook-rebuild-legacy-v1'
STAGE_PREFIX = 'reader-index/gc/ebook-rebuild-stage/v1/'


def _sha(raw):
    return sha256(raw).hexdigest()


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _exact_root(bucket, path):
    _require((bucket, path) == (BUCKET, index.INDEX), 'not the exact EPUB canonical root')


class CheckpointAdapter:
    """Adapt the exact bytes/ETag protocol to ConditionalCheckpointStore."""

    def __init__(self, checkpoints):
        self.checkpoints = checkpoints

    def read(self, bucket, path):
        _exact_root(bucket, path)
        snapshot = self.checkpoints.read(bucket, path)
        return snapshot.raw, strong_etag(snapshot.etag)

    def compare_and_swap(self, bucket, path, raw, *, if_match):
        _exact_root(bucket, path)
        strong_etag(if_match)  # Never let this adapter create a missing canonical root.
        value = json.loads(raw)
        _require(index._encode(value) == raw, 'not exact checkpoint serialization')
        return self.checkpoints.write_once(bucket, path, value, if_match,
                                           operation_id=value['checkpoint_operation']) == 'written'


class S3ClaimJournal:
    """Only the canonical guard's parent-addressed, create-only permits."""

    def __init__(self, client):
        _require(client is not None, 'explicit single-attempt S3 client required')
        self.transport = S3ClaimStore(client)

    @staticmethod
    def _path(bucket, path):
        _require(bucket == BUCKET and isinstance(path, str) and
                 re.fullmatch(re.escape(index.PERMIT_PREFIX) + r'[0-9a-f]{64}\.json', path),
                 'not an exact EPUB canonical permit path')

    def read(self, bucket, path):
        self._path(bucket, path)
        return self.transport._get(path, maximum=CLAIM_LIMIT)[0]

    def claim(self, bucket, path, seal):
        self._path(bucket, path)
        return self.transport._put(path, seal, maximum=CLAIM_LIMIT, IfNoneMatch='*')


def _replacements(review):
    specs = tuple(index.Replacement(**row) for row in review['canonical_replacements'])
    keys = [spec.key for spec in specs]
    changes = review['changes']
    _require(review.get('kind') == 'ebook-rebuild-review' and review.get('remote_writes') == 0
             and 1 <= len(keys) <= 3 and len(set(keys)) == len(keys)
             and sorted(keys) == sorted(row['key'] for row in changes), 'expected a bounded exact EPUB review')
    return specs


def _digest_pin(value):
    _require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value), 'expected a SHA-256 pin')
    return value


def _v3_permit_path(parent_sha):
    return v3_permit.PREFIX + _digest_pin(parent_sha) + '.json'


def _compact_sidecar_sha(packed):
    # plan_legacy records the decoded, canonically encoded sidecar identity;
    # raw review pins and the snapshot reference hash the actual gzip bytes.
    return _sha(v3.encode(legacy_catalog._sidecar(packed)))


class _GuardedLegacyClient:
    """Check the live lease after the streaming PUT helper has hashed the body."""

    def __init__(self, client, check):
        self.client, self.check, self.meta = client, check, client.meta

    def get_object(self, **kwargs):
        return self.client.get_object(**kwargs)

    def put_object(self, **kwargs):
        self.check('legacy:put:' + kwargs['Key'])
        return self.client.put_object(**kwargs)


class _ReviewedPointerStore(PointerStore):
    def __init__(self, reader, s3, guard, parent_raw, parent_etag):
        super().__init__(reader, s3, guard, parent_raw)
        self.parent_etag = strong_etag(parent_etag)

    def put_if_match(self, bucket, path, raw, etag):
        _require(etag == self.parent_etag, 'exact reviewed v3 pointer ETag differs')
        return super().put_if_match(bucket, path, raw, etag)


class GuardedTransport:
    def __init__(self, s3, reader, *, guard):
        _require(s3 is not None and reader is not None and callable(guard), 'explicit transports and lease guard required')
        _require(s3._location(BUCKET) == ('vomebook', 'reader-assets-v2'), 'unexpected EPUB bucket mapping')
        self.s3, self.reader, self.guard = s3, reader, guard
        self.client = s3._client('vomebook')
        self.claims = S3ClaimJournal(self.client)  # Enforces single-attempt SDK config for all adapters below.
        self.checkpoints = CheckpointAdapter(ConditionalCheckpointStore(s3))
        self.protections = protection.S3EpubProtections(s3)
        self.objects = immutable.S3EpubObjects(s3)
        self.roots = LegacyRoots(self.client)
        self.controls = ConditionalPageStore(reader, s3=s3)

    def _check(self, phase):
        if self.guard(phase) is not True:
            raise RuntimeError(f'EPUB release guard refused {phase}')
        return True

    @staticmethod
    def _stage_plan(review, review_sha256):
        _digest_pin(review_sha256)
        specs = _replacements(review)
        changes = {row['key']: row for row in review['changes']}
        op = review['operation']
        _require(isinstance(op, str) and protection.IDENTITY.fullmatch(op), 'invalid EPUB review operation')
        timestamp = datetime.fromisoformat(review['timestamp'].replace('Z', '+00:00'))
        _require(timestamp.tzinfo is not None and not timestamp.microsecond, 'review requires an exact zoned timestamp')
        created_at = timestamp.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        # Use the existing GC-visible exact processing-root record for each
        # root, in separate slots for old and new (at most three of each).
        old_op = _sha(('ebook-rebuild-prior:' + op).encode())[:32]
        records, prior, members = {}, [], {}
        for n, spec in enumerate(sorted(specs, key=lambda s: s.key)):
            row = changes[spec.key]
            _require((row['source_sha256'], row['prior_manifest'], row['prior_manifest_sha256'], row['new_manifest']) ==
                     (spec.source_sha256, spec.old_manifest, spec.old_manifest_sha256, spec.new_manifest),
                     'reviewed EPUB manifest identities differ')
            for origin, root, manifest in ((old_op, spec.old_root, spec.old_manifest),
                                           (op, spec.new_root, spec.new_manifest)):
                _require(immutable._key(root, index.MANIFEST) == manifest and root.split('/')[-2] == spec.source_sha256,
                         'reviewed manifest is outside its exact EPUB root')
                path = f'reader-index/processing/epub-exact-{origin}-{n:02d}.json'
                record = protection.make_record(origin, review_sha256, root, created_at)
                records[path] = search_snapshot.canonical(record)
            prior.append((spec.old_root, _digest_pin(spec.old_manifest_sha256)))
            seen = set()
            for item in row['members']:
                rel = item['path']
                immutable._key(spec.new_root, rel)
                _require(rel not in seen and (spec.new_root, rel) not in members, 'duplicate reviewed EPUB member')
                seen.add(rel)
                expected = immutable._expected({'sha256': item['sha256'], 'bytes': item['bytes']})
                members[spec.new_root, rel] = spec.key, expected
            _require(index.MANIFEST in seen and index.SEARCH in seen and
                     members[spec.new_root, index.MANIFEST][1]['sha256'] == spec.new_manifest_sha256 and
                     immutable._key(spec.new_root, index.SEARCH) == spec.new_search_index and
                     spec.old_root != spec.new_root and spec.old_manifest_sha256 != spec.new_manifest_sha256,
                     'review lacks the exact successor manifest or search index')
        return records, prior, members

    def _verify_protections(self, records):
        for path, raw in records.items():
            _require(self.protections.inspect(path) == raw and self.reader.read_bytes(BUCKET, path) == raw,
                     'EPUB root protection missing or differs: ' + path)

    def _verify_prior(self, prior):
        for root, digest in prior:
            actual = self.objects.inspect(root, index.MANIFEST)
            _require(actual is not None and actual['sha256'] == digest and
                     self.reader.digest(BUCKET, immutable._key(root, index.MANIFEST)) == digest,
                     'reviewed prior EPUB manifest missing or differs')

    def _verify_stage(self, records, prior, members):
        self._verify_protections(records)
        self._verify_prior(prior)
        for (root, rel), (_, identity) in members.items():
            _require(self.objects.inspect(root, rel) == identity and
                     self.reader.digest(BUCKET, immutable._key(root, rel)) == identity['sha256'],
                     'staged EPUB member missing or differs: ' + rel)

    @staticmethod
    def _receipt(review_sha256, records, members):
        seal = _sha(v3.encode({'review': review_sha256,
                               'protections': {path: _sha(raw) for path, raw in records.items()},
                               'members': {root + '/' + rel: item for (root, rel), (_, item) in members.items()}}))
        return {'sha256': seal, 'members_verified': True, 'both_roots_protected': True}

    @staticmethod
    def _snapshot_ref(review):
        summary = review['legacy']
        ref = summary['snapshot']
        name, size = summary['generation'], ref['bytes']
        _require(isinstance(name, str) and re.fullmatch(r'[0-9a-f]{32}', name) and
                 type(size) is int and 0 < size <= legacy_catalog.MAX_SIDECAR_BYTES and
                 ref == {'bucket': BUCKET, 'path': f'reader-index/generations/{name}/reader_assets.json.gz',
                         'sha256': _digest_pin(review['target_sidecar_sha256']), 'bytes': size},
                 'legacy snapshot differs from exact review')
        return ref

    @classmethod
    def _snapshot(cls, review, after_sidecar):
        ref = cls._snapshot_ref(review)
        _require((_sha(after_sidecar), len(after_sidecar)) == (ref['sha256'], ref['bytes']),
                 'legacy snapshot differs from exact review')
        return ref['path']

    def _verify_snapshot(self, ref, compact_sha):
        path = ref['path']
        raw, _ = self.claims.transport._get(path, maximum=ref['bytes'])
        _require(raw is not None and (_sha(raw), len(raw)) == (ref['sha256'], ref['bytes']) and
                 self.reader.read_bytes(BUCKET, path) == raw and _compact_sidecar_sha(raw) == compact_sha,
                 'exact staged legacy snapshot missing or differs')

    def _verify_downstream_parents(self, review):
        """Require unspent downstream lanes and the original live parents before a write."""
        summary = review['legacy']
        catalog_sha = _digest_pin(summary['parent_sha256'])
        pointer_sha = _digest_pin(review['pointer_parent_sha256'])
        tags = review['parent_etags']
        _require(LegacyJournal(self.client).read(catalog_sha) is None, 'legacy catalog parent already claimed')
        _require(self.claims.transport._get(_v3_permit_path(pointer_sha), maximum=v3_permit.LIMIT)[0] is None,
                 'v3 parent already claimed')

        # The catalog can be hundreds of MiB: keep only the streamed identity.
        actual = self.roots.observe(legacy.CATALOG)
        _require((actual.sha256, actual.size, actual.etag) ==
                 (catalog_sha, summary['parent_bytes'], strong_etag(tags['catalog'])) and
                 self.reader.digest(BUCKET, legacy.CATALOG) == catalog_sha, 'reviewed legacy catalog parent differs')

        sidecar, etag = self.controls.read_for_update(BUCKET, legacy.SIDECAR)
        _require(0 < len(sidecar) <= legacy_catalog.MAX_SIDECAR_BYTES and
                 _sha(sidecar) == _digest_pin(review['sidecar_parent_sha256']) and
                 etag == strong_etag(tags['sidecar']) and self.reader.read_bytes(BUCKET, legacy.SIDECAR) == sidecar and
                 _compact_sidecar_sha(sidecar) == summary['before_sidecar_sha256'], 'reviewed legacy sidecar parent differs')
        pointer, etag = self.controls.read_for_update(BUCKET, v3.POINTER)
        _require(0 < len(pointer) <= v3_permit.LIMIT and _sha(pointer) == pointer_sha and
                 etag == strong_etag(tags['pointer']) and self.reader.read_bytes(BUCKET, v3.POINTER) == pointer,
                 'reviewed v3 pointer parent differs')

    @staticmethod
    def _stage_permit(review, receipt):
        return STAGE_PREFIX + review['operation'] + '.json', v3.encode({
            'version': 1, 'kind': 'reader-ebook-rebuild-stage-permit', 'receipt': receipt,
            'snapshot': review['legacy']['snapshot']})

    def stage(self, *, review, review_sha256, bundles, after_sidecar, observe_only=False):
        """Stage one leased attempt; an interrupted attempt is observation-only."""
        _require(type(observe_only) is bool, 'explicit staging mode required')
        self._check('stage:observe-begin' if observe_only else 'stage:begin')
        records, prior, members = self._stage_plan(review, review_sha256)
        snapshot = self._snapshot(review, after_sidecar)
        changes = {row['key']: row for row in review['changes']}
        if not observe_only:
            _require(set(bundles) == set(changes), 'local bundles differ from reviewed keys')
            for key, directory in bundles.items():
                manifest, files, version = review_io.read_bundle(Path(directory))
                row = changes[key]
                _require(files == row['members'] and len(manifest['chapters']) == next(
                    spec['chapter_count'] for spec in review['canonical_replacements'] if spec['key'] == key) and
                    row['new_manifest'] == f"{index.ROOT}/{row['source_sha256']}/{version}/{index.MANIFEST}",
                    'local bundle differs from exact EPUB review')
        self._verify_prior(prior)
        receipt = self._receipt(review_sha256, records, members)
        stage_key, permit = self._stage_permit(review, receipt)
        transport = self.claims.transport
        existing, _ = transport._get(stage_key, maximum=CLAIM_LIMIT)
        if observe_only:
            _require(existing == permit, 'no exact spent staging permit to observe')
        else:
            _require(existing is None, 'EPUB staging permit already spent; observe only')
            self._check('stage:claim')
            if not transport._put(stage_key, permit, maximum=CLAIM_LIMIT, IfNoneMatch='*'):
                raise RuntimeError('EPUB staging permit not acquired; do not replay')
            for path, raw in records.items():
                actual = self.protections.inspect(path)
                if actual is None:
                    self._check('stage:protection-put')
                    self.protections.create_once(path, raw)
                else:
                    _require(actual == raw, 'existing EPUB protection differs from review')
            self._verify_protections(records)
            for (root, rel), (key, expected) in members.items():
                actual = self.objects.inspect(root, rel)
                if actual is None:
                    self._check('stage:member-put')
                    self.objects.create_once(root, rel, Path(bundles[key]) / rel, expected)
                else:
                    _require(actual == expected, 'existing EPUB member differs from review')
            self._check('stage:legacy-snapshot-put')
            self.controls.immutable_put(BUCKET, snapshot, after_sidecar)
        self._verify_stage(records, prior, members)
        _require(self.s3.read_bytes(BUCKET, snapshot) == self.reader.read_bytes(BUCKET, snapshot) == after_sidecar,
                 'exact legacy snapshot not observed')
        self._check('stage:observe-readback' if observe_only else 'stage:readback')
        return receipt

    def canonical(self, *, review, review_sha256, staged, parent_raw, parent_etag, resume=False):
        self._check('canonical:begin')
        specs = _replacements(review)
        target = index.build_successor(parent_raw, reviewed=specs, operation_id=review['operation'])
        _require((_sha(parent_raw), _sha(target)) == (review['canonical_parent_sha256'], review['target_index_sha256']),
                 'exact canonical parent or successor differs from review')
        records, prior, members = self._stage_plan(review, review_sha256)
        receipt = self._receipt(review_sha256, records, members)
        _require(staged == receipt, 'staging receipt does not bind this exact review')
        stage_key, permit = self._stage_permit(review, receipt)
        snapshot = self._snapshot_ref(review)

        def guard(phase):
            writing = phase in {'claim', 'put'}
            if writing or phase in {'resume-readback', 'readback'}:
                _require(self.claims.transport._get(stage_key, maximum=CLAIM_LIMIT)[0] == permit,
                         'exact spent staging permit not observed')
                self._verify_stage(records, prior, members)
                self._verify_snapshot(snapshot, review['legacy']['after_sidecar_sha256'])
                expected = parent_raw if writing else target
                _require(self.reader.read_bytes(BUCKET, index.INDEX) == expected, 'canonical Reader view differs')
            # Observation may follow a partially or fully completed downstream attempt.
            if writing:
                self._verify_downstream_parents(review)
            return self._check('canonical:' + phase)

        status = index.apply(self.checkpoints, self.claims, parent_raw=parent_raw, parent_etag=parent_etag,
                             target_raw=target, reviewed=specs, operation_id=review['operation'],
                             prerequisites={'review': review_sha256, 'stage': receipt['sha256']},
                             guard=guard, resume=resume)
        return {'sha256': _sha(target), 'status': status}

    def _canonical_live(self, review):
        raw, _ = self.checkpoints.read(BUCKET, index.INDEX)
        _require(_sha(raw) == review['target_index_sha256'] and self.reader.read_bytes(BUCKET, index.INDEX) == raw,
                 'exact canonical EPUB successor not observed')

    def legacy(self, *, review, parent_catalog, parent, before_sidecar, after_sidecar,
               sidecar_etag, parent_raw, resume=False):
        """Publish catalog then sidecar. Resume may spend the next, unspent permit."""
        _require(type(resume) is bool, 'explicit legacy mode required')
        self._check('legacy:begin')
        specs = _replacements(review)
        self._snapshot(review, after_sidecar)
        _require(_sha(before_sidecar) == review['sidecar_parent_sha256'] and
                 parent.sha256 == review['legacy']['parent_sha256'] and parent.size == review['legacy']['parent_bytes'],
                 'legacy parents differ from review')
        self._pointer_state(review, parent_raw, allow_target=False)
        guarded_client = _GuardedLegacyClient(self.client, self._check)
        journal = LegacyJournal(guarded_client)
        roots = LegacyRoots(guarded_client)
        reader = self.reader

        class TargetView:
            def read_bytes(self, bucket, path):
                return after_sidecar if (bucket, path) == (BUCKET, legacy.SIDECAR) else reader.read_bytes(bucket, path)

        _require(v3.unify(TargetView(), apply=False, expected_pointer=json.loads(parent_raw),
                          expected_canonical=review['target_sidecar_sha256']).get('pointer') == review['target_pointer'],
                 'reviewed v3 successor differs before legacy publication')

        def plan(out):
            parent_catalog.seek(0)
            summary = plan_legacy(parent_catalog, out, before=before_sidecar, after=after_sidecar,
                                  old={s.key: {'manifest': s.old_manifest} for s in specs},
                                  new={s.key: {'manifest': s.new_manifest} for s in specs},
                                  now=review['timestamp'], parent_sha=parent.sha256)
            # The planner's retirement hint contains tuples; the sealed JSON has lists.
            _require(v3.encode(summary) == v3.encode(review['legacy']),
                     'streamed legacy proposal differs from exact review')
            return summary

        def guard(phase):
            self._canonical_live(review)
            self._pointer_state(review, parent_raw, allow_target=False)
            return self._check('legacy:' + phase)

        result = legacy.publish_route_transition(
            parent=parent, before_sidecar=before_sidecar, sidecar_etag=sidecar_etag,
            after_sidecar=after_sidecar, key=frozenset(s.key for s in specs),
            expected_catalog_sha256=review['legacy']['target_sha256'],
            expected_catalog_bytes=review['legacy']['target_bytes'], expected_pointer_raw=parent_raw,
            journal=journal, observe=self.roots.observe, reader_catalog_digest=lambda: self.reader.digest(BUCKET, legacy.CATALOG),
            cas=roots.put_once, store=self.reader, guard=guard, resume=resume, seal_kind=LEGACY_KIND, plan_catalog=plan)
        guard('readback')
        return {**result, 'sha256': result['sidecar'].sha256, 'catalog_sha256': result['catalog'].sha256,
                'pointer_parent_sha256': _sha(parent_raw)}

    def _pointer_state(self, review, parent_raw, *, allow_target):
        _require(isinstance(parent_raw, bytes) and 0 < len(parent_raw) <= v3_permit.LIMIT,
                 'exact reviewed v3 pointer bytes required')
        parent = json.loads(parent_raw)
        target = review['target_pointer']
        _require(parent['generation'] == review['reader_generation'] != target['generation'],
                 'v3 parent must be the original reviewed pointer')
        _require(_sha(parent_raw) == review['pointer_parent_sha256'], 'exact v3 parent bytes differ from review')
        observed, etag = self.controls.read_for_update(BUCKET, v3.POINTER)
        allowed = (parent_raw, v3.encode(target)) if allow_target else (parent_raw,)
        _require(observed in allowed and self.reader.read_bytes(BUCKET, v3.POINTER) == observed,
                 'exact v3 pointer views differ from reviewed phase')
        if observed == parent_raw:
            _require(etag == strong_etag(review['parent_etags']['pointer']), 'exact reviewed v3 pointer ETag differs')
        return observed

    def promote(self, *, review, parent_raw, legacy_result, observe_only=False):
        """Use the exact original pointer bytes. Retain legacy_result for observation."""
        _require(type(observe_only) is bool, 'explicit promotion mode required')
        _replacements(review)
        self._check('v3:begin')
        target = review['target_pointer']
        catalog, sidecar = legacy_result['catalog'], legacy_result['sidecar']
        seal = _digest_pin(legacy_result['seal'])
        journal = LegacyJournal(self.client)
        claim_id = review['legacy']['parent_sha256']
        side_sha = review['target_sidecar_sha256']
        _require((catalog.sha256, catalog.size, sidecar.sha256, sidecar.size) ==
                 (review['legacy']['target_sha256'], review['legacy']['target_bytes'],
                  side_sha, review['legacy']['snapshot']['bytes']) and
                 legacy_result['claim_id'] == claim_id and legacy_result['pointer_parent_sha256'] == _sha(parent_raw) and
                 legacy_result['unify']['expected_pointer'] == json.loads(parent_raw),
                 'legacy result does not bind the reviewed v3 parent and legacy targets')

        def guard(*, allow_target=False):
            self._canonical_live(review)
            for path, expected in ((legacy.CATALOG, catalog), (legacy.SIDECAR, sidecar)):
                actual = self.roots.observe(path)
                _require((actual.sha256, actual.size, actual.etag) == (expected.sha256, expected.size, expected.etag)
                         and self.reader.digest(BUCKET, path) == expected.sha256, 'legacy root differs before promotion')
            _require(journal.read(claim_id) == legacy.Record(seal, 'legacy-observed', catalog.etag, sidecar.etag),
                     'durable legacy result differs before promotion')
            self._pointer_state(review, parent_raw, allow_target=allow_target)
            return self._check('v3:write-or-readback')

        store = _ReviewedPointerStore(self.reader, self.s3, guard, parent_raw, review['parent_etags']['pointer'])
        # The existing route promotion helper only permits image/web/Office
        # kinds. Use its shared parent-claim namespace and exact protocol, with
        # an EPUB seal (never masquerade as another format or create a new lane).
        parent = json.loads(parent_raw)
        path = _v3_permit_path(_sha(parent_raw))
        permit = v3.encode({'version': 1, 'kind': 'reader-ebook-rebuild-v3-permit', 'parent_sha256': _sha(parent_raw),
                            'pointer': target, 'canonical_sha256': side_sha, 'legacy_seal': seal})
        transport = self.claims.transport

        def inspect():
            guard(allow_target=True)
            pointer, reading = v3.current(store)
            _require(pointer == target and store.read_bytes(BUCKET, v3.POINTER) == v3.encode(target) and
                     reading.get('parent_generation') == parent['generation'], 'exact v3 successor not observed; do not replay')
            return {'confirmed': True, 'pointer': pointer, 'generation': pointer['generation'], 'pages_dispatched': False}

        prior, _ = transport._get(path, maximum=v3_permit.LIMIT)
        if prior is not None:
            _require(prior == permit, 'v3 parent already claimed for different bytes')
            return inspect()
        _require(not observe_only, 'no matching v3 attempt to observe')
        self._pointer_state(review, parent_raw, allow_target=False)
        options = dict(expected_pointer=parent, expected_canonical=side_sha, conditional_pointer=True)
        _require(v3.unify(store, apply=False, **options).get('pointer') == target, 'reviewed v3 successor differs')
        guard()
        if not transport._put(path, permit, maximum=v3_permit.LIMIT, IfNoneMatch='*'):
            raise RuntimeError('v3 permit not acquired; do not replay')
        guard()
        _require(v3.unify(store, apply=True, **options).get('pointer') == target, 'v3 write produced different pointer')
        return inspect()
