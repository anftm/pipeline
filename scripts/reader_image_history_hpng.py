"""Operator bridge for the reviewed h.PNG only; no dispatch, ACK or deletion.

Use the portable handoff's runtime.Reviewed with Operator.from_env(...).
verify() is read-only and is not a write admission. For writes the caller
must acquire and continuously retain the real all-writer/GC/no-ABA fence,
including across any recovery. guard(phase) must interrogate its live state;
a receipt or a standalone flag is not a fence. The caller supplies the guard.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from tempfile import TemporaryFile
from urllib.parse import quote

from . import publish_reader_v3 as v3, reader_assets, reader_format_backlog as backlog
from . import reader_image_history as image, reader_image_history_catalog as catalog
from . import reader_image_history_preflight as preview, reader_image_history_publish as legacy
from . import reader_image_history_runtime as runtime, reader_image_history_transition as indexes
from . import reader_image_history_v3 as promotion, reader_route_control as control
from .reader_bucket_store import S3BucketStore, retry_s3
from .reader_image_history_parent import CatalogParent
from .reader_image_history_publish_journal import LegacyJournal
from .reader_image_history_publish_s3 import LegacyRoots
from .reader_image_history_ranges import open_catalog, resolver_digest
from .reader_lifecycle import processing_record
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore, strong_etag

REVIEWED_SHA256 = '50671e8927bf78f34411538e81f6971918e6655c73d2c8d803865122ed182ec0'
OPERATION = '17e9a04bc8bf4997809a061ea0ad59ec'
ASSETS = image.BUCKET
BUCKETS = {ASSETS, backlog.SEARCH_BUCKET, preview.sidecar.PDF_BUCKET}
BLOCK = 1024 * 1024


class _FencedClient:
    """Check the real fence immediately at every PUT, including journal PUTs."""

    def __init__(self, client, guard):
        self.client, self.guard, self.meta = client, guard, client.meta

    def get_object(self, **kwargs):
        return self.client.get_object(**kwargs)

    def put_object(self, **kwargs):
        runtime._fence(self.guard, 'put:' + kwargs['Key'])
        return self.client.put_object(**kwargs)


class _S3:
    def __init__(self, client):
        self.client = client
        self.read_etags = {}

    @staticmethod
    def _location(bucket):
        if bucket not in BUCKETS:
            raise ValueError('unexpected h.PNG S3 bucket')
        return tuple(bucket.split('/'))

    def _client(self, namespace):
        if namespace != 'vomebook':
            raise ValueError('unexpected h.PNG S3 namespace')
        return self.client

    @contextmanager
    def stream(self, bucket, path, limit, **condition):
        _, name = self._location(bucket)
        response = self.client.get_object(Bucket=name, Key=path, **condition)
        body, length = response['Body'], response.get('ContentLength')
        try:
            if (response['ResponseMetadata']['HTTPStatusCode'] != 200
                    or type(length) is not int or not 0 < length <= limit):
                raise ValueError(f'S3 response outside exact read bounds: {path}')
            yield body, length, strong_etag(response.get('ETag'))
        finally:
            body.close()

    def read_bytes(self, bucket, path):
        limit = backlog.MAX_COMPRESSED if bucket == backlog.SEARCH_BUCKET else preview.MAX_IMAGE
        ref = bucket, path
        self.read_etags.pop(ref, None)

        def read():
            with self.stream(bucket, path, limit) as (body, length, tag):
                raw = body.read(length + 1)
                if len(raw) != length:
                    raise ValueError(f'S3 read length differs: {path}')
                self.read_etags[ref] = tag
                return raw

        return retry_s3(read, attempts=2)

    def head(self, path):
        response = retry_s3(lambda: self.client.head_object(Bucket='reader-assets-v2', Key=path), attempts=2)
        if response['ResponseMetadata']['HTTPStatusCode'] != 200:
            raise ValueError(f'unexpected S3 HEAD response: {path}')
        return strong_etag(response.get('ETag')), response.get('ContentLength')


class _Resolver:
    """Independent bounded Hub/public HTTP GETs; never use S3 upload metadata."""

    def __init__(self, http, token):
        self.http, self.token = http, token

    def _get(self, url, limit, *, size=None, missing=None, auth=False, mime=None, digest=False):
        from hashlib import sha256

        headers = {'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'}
        if auth and self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        with self.http.stream('GET', url, headers=headers, follow_redirects=True) as response:
            if response.status_code == 404 and missing is not None:
                raise FileNotFoundError(missing)
            response.raise_for_status()
            if response.status_code != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('expected a full identity-encoded resolver response')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isascii() or not length.isdecimal()
                                       or not 0 < int(length) <= limit or size is not None and int(length) != size):
                raise ValueError('resolver length differs from expected bounds')
            media_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if mime is not None and media_type != mime:
                raise ValueError(f'served MIME differs from {mime}: {media_type!r}')
            observed, raw, h = 0, bytearray(), sha256()
            for chunk in response.iter_bytes(chunk_size=BLOCK):
                observed += len(chunk)
                if observed > limit:
                    raise ValueError('resolver response exceeds its byte limit')
                if digest:
                    h.update(chunk)
                else:
                    raw.extend(chunk)
            if (not observed or size is not None and observed != size
                    or length is not None and observed != int(length)):
                raise ValueError('resolver response length differs')
            return h.hexdigest() if digest else bytes(raw)

    def bucket(self, bucket, path, limit, **kwargs):
        _S3._location(bucket)
        return self._get(f'https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe="/")}',
                         limit, missing=path, **kwargs)

    def read_bytes(self, bucket, path):
        return self.bucket(bucket, path, preview.MAX_IMAGE, auth=True)

    def source(self, pin):
        return self._get(reader_assets.source_url(pin.repo, pin.revision, pin.path),
                         pin.size, size=pin.size, auth=True)


class _V3Writes:
    """V3 reads via Hub; exact allowlisted create-only objects and pointer CAS via S3."""

    def __init__(self, operator, client, guard):
        self.op, self.client, self.guard = operator, client, guard
        self.read_bytes = operator.resolver.read_bytes
        self.pointer = json.loads(operator.reviewed.pointer_target)
        self.history_path = f"reader-index/v3/promotions/{self.pointer['generation']}.json"

    def assert_serialized_writer(self, protocol):
        if protocol != v3.PROTOCOL:
            raise ValueError('unexpected Reader writer protocol')
        runtime._fence(self.guard, 'v3:writer')

    def put_bytes(self, bucket, path, raw):
        r = self.op.reviewed
        ref = self.pointer['catalog']
        if path == r.report['proposed_base']['path']:
            if raw != r.pinned_after:
                raise ValueError('v3 pinned base differs from review')
        elif path == ref['path']:
            runtime._check_raw(raw, ref['sha256'], 'v3 catalog', limit=preview.MAX_IMAGE, size=ref['bytes'])
        elif path == self.history_path:
            record = json.loads(raw, object_pairs_hook=catalog._unique)
            if (record.get('kind') != 'reader-reading-promotion' or record.get('generation') != self.pointer['generation']
                    or record.get('parent_generation') != json.loads(r.pointer_parent)['generation']
                    or record.get('acks') != {'hf': False, 'pages': False} or record.get('resources') != [ref]):
                raise ValueError('unexpected v3 promotion record')
        else:
            raise ValueError('unexpected v3 object write')
        self.op._put(self.client, bucket, path, raw, IfNoneMatch='*')

    def read_for_update(self, bucket, path):
        if (bucket, path) != (ASSETS, v3.POINTER):
            raise ValueError('unexpected v3 CAS root')
        raw = self.op.s3.read_bytes(bucket, path)
        if raw != self.op.reviewed.pointer_parent or self.read_bytes(bucket, path) != raw:
            raise ValueError('v3 exact pointer parent differs across S3/Hub')
        return raw, strong_etag(self.op.s3.read_etags[bucket, path])

    def put_if_match(self, bucket, path, raw, etag):
        if (bucket, path, raw) != (ASSETS, v3.POINTER, self.op.reviewed.pointer_target):
            raise ValueError('unexpected v3 pointer payload')
        self.op._put(self.client, bucket, path, raw, IfMatch=strong_etag(etag))


class Operator:
    """One h.PNG operation. Do not share across concurrent calls.

    s3 is an explicit single-attempt boto3 client for https://s3.hf.co/vomebook;
    http is an httpx client, and hub is a Hugging Face HfApi for dataset head
    reads. With from_env, the caller owns the http client's context/lifetime.
    """

    def __init__(self, reviewed: runtime.Reviewed, *, s3, http, hub, token=None):
        if (type(reviewed) is not runtime.Reviewed or reviewed.sha256 != REVIEWED_SHA256
                or reviewed.transition.canonical.operation != OPERATION):
            raise ValueError('expected the independently reviewed h.PNG handoff')
        self.reviewed, self.client, self.http, self.hub = reviewed, s3, http, hub
        self.resolver = _Resolver(http, token)
        self.s3 = _S3(s3)
        parent = reviewed.catalog_parent
        self.archive = CatalogParent(s3, digest=parent.sha256, size=parent.size, etag=parent.etag)
        self.roots = LegacyRoots(s3)
        self.index_journal = indexes.S3TransitionJournal(s3)
        self.legacy_journal = LegacyJournal(s3)

    @classmethod
    def from_env(cls, reviewed, *, http):
        from botocore.config import Config
        from huggingface_hub import HfApi

        store = S3BucketStore()
        if store._namespace != 'vomebook':
            raise ValueError('expected vomebook S3 credentials')
        store._config = store._config.merge(Config(
            retries={'mode': 'standard', 'total_max_attempts': 1}, read_timeout=75,
            request_checksum_calculation='when_required', response_checksum_validation='when_required'))
        token = os.environ.get('HF_TOKEN') or False
        return cls(reviewed, s3=store._client('vomebook'), http=http, hub=HfApi(token=token), token=token)

    @property
    def _intent(self):
        return self.reviewed.transition.canonical

    def _put(self, client, bucket, path, raw, **condition):
        if bucket != ASSETS or not isinstance(raw, bytes) or not raw:
            raise ValueError('invalid h.PNG bucket PUT')
        mime = ('image/avif' if path.endswith('.avif') else
                'application/gzip' if path.endswith('.gz') else 'application/json')
        result = client.put_object(Bucket='reader-assets-v2', Key=path, Body=raw, ContentLength=len(raw),
                                   ContentType=mime, **condition)
        meta = result['ResponseMetadata']
        if meta['HTTPStatusCode'] not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise RuntimeError('h.PNG PUT outcome unconfirmed; inspect without replay')
        strong_etag(result.get('ETag'))

    def _legacy_seal(self):
        r = self.reviewed
        p = r.catalog_parent
        return image._sha(image._encode(['reader-image-legacy-publish-v1', (p.sha256, p.size, p.etag),
            (image._sha(r.before_sidecar), len(r.before_sidecar), r.sidecar_etag),
            (r.catalog_target_sha256, r.catalog_target_bytes),
            (image._sha(r.after_sidecar), len(r.after_sidecar)), image._sha(r.pointer_parent)]))

    @contextmanager
    def _open_archive(self):
        with TemporaryFile() as stream:
            self.archive.load(stream)
            yield stream

    def _catalog_plan(self, source):
        class Sink:
            def write(self, raw):
                return len(raw)

        r, intent = self.reviewed, self._intent
        p = intent.preflight
        plan = catalog.plan(source, Sink(), key=intent.key, webp_path=p.old.path, avif_path=p.new.path,
                            before_sidecar=r.before_sidecar, after_sidecar=r.after_sidecar,
                            now=r.catalog_timestamp, expected_parent_sha256=r.catalog_parent.sha256,
                            max_source_bytes=r.catalog_parent.size, max_target_bytes=r.catalog_target_bytes)
        if (plan['parent_bytes'], plan['target_sha256'], plan['target_bytes'], plan['generation']) != (
                r.catalog_parent.size, r.catalog_target_sha256, r.catalog_target_bytes, r.legacy_generation):
            raise ValueError('archived rollback/legacy successor differs from review')
        # plan() proved that the exact WebP is referenced by the current
        # generation, and that the exact successor carries that entry forward
        # as superseded without replacement ACKs. _legacy_roots binds that
        # proof to the actual live GC root, not to the archive alone.
        return plan['parent_sha256']

    def _legacy_roots(self, record, observed):
        r = self.reviewed
        target = record is not None
        both = target and record.state in {'sidecar-permitted', 'legacy-observed'}
        parent = r.catalog_parent
        catalog_pin = (r.catalog_target_sha256, r.catalog_target_bytes, record.catalog_etag) if target else (
            parent.sha256, parent.size, parent.etag)
        sidecar = r.after_sidecar if both else r.before_sidecar
        sidecar_pin = (image._sha(sidecar), len(sidecar), record.sidecar_etag if both else r.sidecar_etag)
        for path, pin, prior in ((legacy.CATALOG, catalog_pin, parent.etag),
                                 (legacy.SIDECAR, sidecar_pin, r.sidecar_etag)):
            value = observed(path)
            if ((value.sha256, value.size) != pin[:2] or pin[2] is not None and value.etag != pin[2]
                    or (target if path == legacy.CATALOG else both) and value.etag == prior):
                raise ValueError(f'exact live legacy root differs: {path}; do not replay')
        if self.resolver.read_bytes(ASSETS, legacy.SIDECAR) != sidecar:
            raise ValueError('legacy sidecar Hub view differs from the expected phase')
        return sidecar

    def _index_state(self, record):
        r, intent = self.reviewed, self._intent
        t = r.transition
        both = record is not None and record.state in {'canonical-permitted', 'applied'}
        for path, raw, tag, prior, advanced in (
            (t.shard_pin.path, t.shard_target if record else t.shard_parent,
             record.shard_etag if record else t.shard_pin.etag, t.shard_pin.etag, record is not None),
            (image.CANONICAL, intent.candidate if both else intent.parent,
             record.canonical_etag if both else intent.parent_etag, intent.parent_etag, both),
        ):
            actual = self.s3.read_bytes(ASSETS, path)
            etag = self.s3.read_etags[ASSETS, path]
            if actual != raw or tag is not None and etag != tag or advanced and etag == prior:
                raise ValueError(f'exact live index differs: {path}; do not replay')

    def _journal_state(self, resume_indexes, resume_legacy):
        r = self.reviewed
        loaded = self.index_journal.load(r.transition.claim_key)
        record = self.legacy_journal.read(r.catalog_parent.sha256)
        if (loaded is not None) != resume_indexes or (record is not None) != resume_legacy:
            raise ValueError('explicit resume flags must match the durable journals; do not replay')
        if loaded is not None and loaded[0] != r.transition:
            raise ValueError('index journal binds a different transition')
        if record is not None:
            legacy._bind(record, self._legacy_seal())
            if loaded is None or loaded[1].state != 'applied':
                raise ValueError('legacy journal requires applied index proof')
        return loaded[1] if loaded else None, record

    def _catalog_digest(self, target):
        r = self.reviewed
        size = r.catalog_target_bytes if target else r.catalog_parent.size
        tag = self.s3.head(legacy.CATALOG)
        if tag[1] != size or not target and tag[0] != r.catalog_parent.etag:
            raise ValueError('catalog writer view differs before independent resolver read')
        url = f'https://huggingface.co/buckets/{ASSETS}/resolve/{legacy.CATALOG}'
        digest = resolver_digest(self.http, url, size=size, etag=tag[0])
        if self.s3.head(legacy.CATALOG) != tag:
            raise ValueError('catalog changed during independent resolver read')
        return digest

    def _cached_roots(self):
        seen = {}

        def observe(path):
            old = seen.get(path)
            if old is None or self.s3.head(path) != (old.etag, old.size):
                seen[path] = self.roots.observe(path)
            return seen[path]

        return observe

    def _asset(self, pin, mime):
        raw = self.resolver.bucket(ASSETS, pin.path, pin.size, size=pin.size, mime=mime)
        runtime._check_raw(raw, pin.sha256, pin.path, limit=preview.MAX_IMAGE, size=pin.size)
        return raw

    def _protection(self):
        r, intent = self.reviewed, self._intent
        expected = processing_record([{'bucket': ASSETS, 'root': intent.preflight.new.root}], 'reader-image-history',
                                     {'operation': intent.operation, 'reviewed_sha256': r.sha256})
        actual = self.s3.read_bytes(ASSETS, r.processing_path)
        if self.resolver.read_bytes(ASSETS, r.processing_path) != actual:
            raise ValueError('active AVIF protection differs between S3 and Hub')
        record = json.loads(actual, object_pairs_hook=catalog._unique)
        ignored = {'created_at', 'updated_at', 'heartbeat_at'}
        if v3.encode({k: v for k, v in record.items() if k not in ignored}) != v3.encode(
                {k: v for k, v in expected.items() if k not in ignored}):
            raise ValueError('active AVIF processing protection differs')

    def _search(self):
        intent = self._intent
        reports = self.reviewed.report
        pointer, manifest, corpus, revisions, consumers = backlog.capture_search(self.s3)
        proof = dict(pointer_sha256=image._sha(pointer), manifest_sha256=image._sha(manifest),
                     record=backlog.source_records(corpus, revisions).get(intent.key), consumers=consumers)
        generation = reports['search_generation']
        if (proof != reports['search_proof'] or backlog.search_publication_pointer.decode_pointer(pointer)['desired'] != generation
                or self.s3.read_bytes(backlog.SEARCH_BUCKET, f'generations/{generation}/manifest.json') != manifest):
            raise ValueError('live Search corpus/record or consumer proof differs')
        return image._sha(image._encode(proof))

    def _checks(self, rollback, *, canonical_target, shard_target, sidecar, pointer, avif_required):
        r, intent = self.reviewed, self._intent
        t, preflight = r.transition, intent.preflight
        report = r.report
        if self.hub.repo_info(preflight.source.repo, repo_type='dataset', timeout=30).sha != preflight.current_source.revision:
            raise ValueError('dataset head differs from reviewed Search revision')
        source, original = preview._source(lambda *_: self.resolver.source(preflight.source),
            (preflight.source.repo, preflight.source.path, preflight.source.revision), preflight.source.size)
        current, _ = preview._source(lambda *_: self.resolver.source(preflight.current_source),
            (preflight.current_source.repo, preflight.current_source.path, preflight.current_source.revision),
            preflight.current_source.size)
        image._same_source(source, current)
        search_digest = self._search()
        old_raw = self._asset(preflight.old, 'image/webp')
        try:
            new_raw = self._asset(preflight.new, 'image/avif')
        except FileNotFoundError:
            if avif_required:
                raise
            try:
                self.s3.read_bytes(ASSETS, preflight.new.path)
            except FileNotFoundError:
                new_raw = r.avif
            else:
                raise ValueError('AVIF present in S3 but not the serving view') from None
        else:
            if self.s3.read_bytes(ASSETS, preflight.new.path) != new_raw:
                raise ValueError('served AVIF differs from S3 bytes')
            if avif_required:
                self._protection()
        dimensions, alpha = preview._decoded_image(original, 'PNG')
        if (list(dimensions) != report['source_dimensions']
                or preview._decoded_image(old_raw, 'WEBP')[0] != dimensions
                or preview._decoded_image(new_raw, 'AVIF') != (dimensions, alpha)):
            raise ValueError('decoded static source, WebP or AVIF shape differs')

        expected = {(ASSETS, image.CANONICAL): intent.candidate if canonical_target else intent.parent,
                    (ASSETS, t.shard_pin.path): t.shard_target if shard_target else t.shard_parent,
                    (ASSETS, legacy.SIDECAR): sidecar, (ASSETS, v3.POINTER): pointer}
        inputs = preview.Inputs(self.s3)
        pins = {(row['bucket'], row['path']): row['sha256'] for row in report['reader_inputs']}
        if not expected.keys() <= pins.keys():
            raise ValueError('reviewed Reader inputs do not bind the mutable roots')
        for ref, digest in pins.items():
            try:
                raw = inputs.read_bytes(*ref)
            except FileNotFoundError:
                raw = None
            if ref in expected:
                if raw != expected[ref]:
                    raise ValueError(f'live Reader root differs: {ref[1]}')
            elif (None if raw is None else image._sha(raw)) != digest:
                raise ValueError(f'pinned Reader input differs: {ref[1]}')
        if inputs.raw[ASSETS, preflight.old.path] != old_raw:
            raise ValueError('rollback WebP differs between S3 and serving view')

        def build(raw):
            inputs.override = raw
            rebuilt = preview._build_sidecar(inputs)
            if set(inputs.raw) != pins.keys():
                raise ValueError('Reader builder used an unreviewed input')
            return rebuilt

        # Rebuild both full sidecars from the same freshly pinned input set.
        before, after = build(intent.parent), build(intent.candidate)
        if before != catalog._sidecar(r.before_sidecar) or after != catalog._sidecar(r.after_sidecar):
            raise ValueError('full Reader sidecar build differs from review')
        parent = json.loads(r.pointer_parent)
        reading = v3.validate_catalog(v3.read_index(inputs, parent['catalog']))
        if reading['generation'] != parent['generation'] or reading.get('derived_pdf_redirects'):
            raise ValueError('reviewed v3 parent catalog differs')
        packed, ref = control.prepare_base(reading, r.after_sidecar)
        if (packed != r.pinned_after or ref != report['proposed_base'] or
                reading.get('base_sidecar', {}).get('sha256') != report['pinned_base_sha256']):
            raise ValueError('v3 pinned base no longer matches the reviewed build')
        before_projection = v3.project_sidecar(v3.pinned_base(inputs, reading, {}), reading)
        after_projection = v3.project_sidecar(catalog._sidecar(packed), reading)
        if set(inputs.raw) != pins.keys():
            raise ValueError('Reader projection used an unreviewed input')
        if (self.hub.repo_info(source.repo, repo_type='dataset', timeout=30).sha != current.revision
                or self.resolver.read_bytes(ASSETS, v3.POINTER) != pointer):
            raise ValueError('dataset head or Hub Reader pointer changed during verification')
        old = image.Asset(preflight.old.root, preflight.old.path, image._sha(old_raw), len(old_raw))
        new = image.Asset(preflight.new.root, preflight.new.path, image._sha(new_raw), len(new_raw))
        pins_digest = image._sha(image._encode([r.sha256, r.sidecar_etag]))
        return image.Checks(image.Preflight(source, current, search_digest, old, new, rollback, pins_digest),
                            before_projection, after_projection)

    def verify(self):
        """Read-only exact fresh-parent verification; report no write admission."""
        r = self.reviewed
        self._journal_state(False, False)
        self._index_state(None)
        # Parse the observed live GC root, not a locally retained substitute.
        parent = r.catalog_parent
        if self.s3.head(legacy.CATALOG) != (parent.etag, parent.size):
            raise ValueError('live S3 catalog parent differs from review')
        with open_catalog(self.client, size=parent.size, etag=parent.etag) as body:
            rollback = self._catalog_plan(body)
        if self._catalog_digest(False) != rollback:
            raise ValueError('legacy catalog serving view differs from review')
        self._verify_sidecar_parent()
        runtime._v3_preview(r, self.resolver)
        image._fresh(self._intent, lambda *_: self._checks(rollback, canonical_target=False, shard_target=False,
                     sidecar=r.before_sidecar, pointer=r.pointer_parent, avif_required=False), 'before')
        self._index_state(None)
        self._journal_state(False, False)
        self._verify_sidecar_parent()
        if self.s3.head(legacy.CATALOG) != (parent.etag, parent.size):
            raise ValueError('live catalog changed during verification')
        return {'reviewed_sha256': r.sha256, 'legacy_parent_sha256': rollback, 'write_admission': False,
                'reader_consumers_verified': False, 'deletion_authorized': False}

    def _verify_sidecar_parent(self):
        r = self.reviewed
        actual = self.roots.observe(legacy.SIDECAR)
        if ((actual.sha256, actual.size, actual.etag) != (image._sha(r.before_sidecar), len(r.before_sidecar), r.sidecar_etag)
                or self.resolver.read_bytes(ASSETS, legacy.SIDECAR) != r.before_sidecar):
            raise ValueError('exact legacy sidecar parent differs from review')

    def stage_parent(self, *, guard):
        """Archive the exact catalog parent once; do not use to recover an advanced root."""
        runtime._fence(guard, 'archive:prepare')
        p = self.reviewed.catalog_parent
        archive = CatalogParent(_FencedClient(self.client, guard), digest=p.sha256, size=p.size, etag=p.etag)
        with TemporaryFile() as stream:
            tag = archive.stage(stream, guard=lambda: runtime._fence(guard, 'archive:stage'))
        runtime._fence(guard, 'archive:observed')
        return {'path': archive.path, 'etag': tag, 'sha256': p.sha256}

    def publish_legacy(self, *, guard, resume_indexes=False, resume_legacy=False):
        """Execute/observe exact index and legacy stages. Requires the archived parent."""
        runtime._fence(guard, 'legacy:prepare')
        r = self.reviewed
        index_record, legacy_record = self._journal_state(resume_indexes, resume_legacy)
        self._index_state(index_record)
        observed = self._cached_roots()  # Only reuse digests under the continuous no-ABA fence.
        sidecar = self._legacy_roots(legacy_record, observed)
        if self._catalog_digest(resume_legacy) != (r.catalog_target_sha256 if resume_legacy else r.catalog_parent.sha256):
            raise ValueError('legacy catalog serving view differs from the expected phase')
        with self._open_archive() as parent:
            rollback = self._catalog_plan(parent)
            runtime._v3_preview(r, self.resolver)
            image._fresh(self._intent, lambda *_: self._checks(rollback,
                canonical_target=index_record is not None and index_record.state != 'shard-permitted',
                shard_target=resume_indexes, sidecar=sidecar, pointer=r.pointer_parent, avif_required=resume_indexes), 'before')
            client = _FencedClient(self.client, guard)

            def revalidate(_, phase):
                # The legacy coordinator checks the *live* catalog identity
                # before calling here; recheck it under the same no-ABA fence.
                if phase.startswith('indexes:'):
                    current_sidecar = self._legacy_roots(legacy_record, observed)
                    canonical_target, shard_target = phase.endswith('both-targets'), not phase.endswith('parents')
                else:
                    state = phase.split(':')[1]
                    record = None if state == 'parents' else legacy.Record(self._legacy_seal(),
                        'sidecar-permitted' if state == 'both-targets' else 'catalog-permitted')
                    current_sidecar = self._legacy_roots(record, observed)
                    canonical_target = shard_target = True
                if self.s3.read_bytes(ASSETS, r.legacy_snapshot) != r.after_sidecar:
                    raise ValueError('precreated legacy snapshot differs in S3')
                return self._checks(rollback, canonical_target=canonical_target, shard_target=shard_target,
                                    sidecar=current_sidecar, pointer=r.pointer_parent, avif_required=True)

            def create(bucket, path, raw):
                asset = self._intent.preflight.new
                if path == r.processing_path:
                    pass  # Runtime constructs the exact active processing record.
                elif (path, raw) not in ((asset.path, r.avif), (r.legacy_snapshot, r.after_sidecar)):
                    raise ValueError('unexpected legacy immutable write')
                self._put(client, bucket, path, raw, IfNoneMatch='*')

            @contextmanager
            def use_parent():
                yield parent  # Runtime rewinds and re-hashes before use.

            return runtime.publish_legacy(r, open_catalog_parent=use_parent,
                checkpoints=ConditionalCheckpointStore(_S3(client)), index_journal=indexes.S3TransitionJournal(client),
                legacy_journal=LegacyJournal(client), reader_store=self.resolver, observe_legacy=observed,
                reader_catalog_digest=lambda: self._catalog_digest(True), legacy_cas=LegacyRoots(client).put_once,
                guard=guard, revalidate=revalidate, create_immutable=create,
                observe_avif_mime=lambda _, __: self._observe_avif(),
                resume_indexes=resume_indexes, resume_legacy=resume_legacy)

    def _observe_avif(self):
        self._asset(self._intent.preflight.new, 'image/avif')
        return 'image/avif'

    def promote(self, *, guard, observe_only=False):
        """Spend the v3 permit or observe an exact, already committed successor."""
        runtime._fence(guard, 'v3:prepare')
        r = self.reviewed
        index_record, legacy_record = self._journal_state(True, True)
        if legacy_record.state != 'legacy-observed':
            raise ValueError('v3 requires durable legacy-observed evidence')
        target = json.loads(r.pointer_target)
        with self._open_archive() as parent:
            rollback = self._catalog_plan(parent)
        observed = self._cached_roots()

        def revalidate():
            runtime._fence(guard, 'v3:legacy-proof')
            if self._journal_state(True, True) != (index_record, legacy_record):
                raise ValueError('prior-stage journal evidence changed')
            self._index_state(index_record)
            sidecar = self._legacy_roots(legacy_record, observed)
            if (self._catalog_digest(True) != r.catalog_target_sha256
                    or self.s3.read_bytes(ASSETS, r.legacy_snapshot) != r.after_sidecar
                    or self.resolver.read_bytes(ASSETS, r.legacy_snapshot) != r.after_sidecar):
                raise ValueError('legacy catalog or snapshot differs across serving views')
            pointer = self.s3.read_bytes(ASSETS, v3.POINTER)
            if pointer not in (r.pointer_parent, r.pointer_target):
                raise ValueError('foreign v3 pointer; do not replay')
            checks = self._checks(rollback, canonical_target=True, shard_target=True, sidecar=sidecar,
                                  pointer=pointer, avif_required=True)
            image._fresh(self._intent, lambda *_: checks, 'after')
            if pointer == r.pointer_target:
                _, reading = v3.current(self.resolver)
                ref = r.report['proposed_base']
                projected = v3.project_sidecar(v3.pinned_base(self.resolver, reading, {}), reading)
                if (reading['base_sidecar'] != ref
                        or self.s3.read_bytes(ASSETS, ref['path']) != r.pinned_after
                        or self.s3.read_bytes(ASSETS, target['catalog']['path']) != v3.encode(reading)
                        # The shadow binds parent metadata; the live projection
                        # necessarily has successor metadata.
                        or projected['f'] != checks.after_projection['f']):
                    raise ValueError('live v3 successor base, catalog or whole projection differs')
            return runtime._fence(guard, 'v3:legacy-proof')

        client = _FencedClient(self.client, guard)
        return promotion.promote(store=_V3Writes(self, client, guard), client=client, parent_raw=r.pointer_parent,
                                 target_pointer=target, canonical_sha256=image._sha(r.after_sidecar),
                                 legacy_seal=self._legacy_seal(), guard=revalidate, observe_only=observe_only)

    def observe_v3(self, *, guard):
        """Read both actual consumers' full projections; no ACK or dispatch."""
        self.promote(guard=guard, observe_only=True)
        return runtime.observe_v3(self.reviewed, reader_store=self.resolver, guard=guard,
                                  observe_receipt=lambda surface: v3.observe_consumer(self.resolver, surface, self.http))
