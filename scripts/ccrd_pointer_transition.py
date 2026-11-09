"""Single-object CCRD current-pointer CAS, separate from Space activation.

A matching ETag protects only this one write. It is not a bucket lock, does not
prove writer isolation, and cannot prevent ABA. Callers must coordinate the
actual writer set and keep the immutable parent for explicit recovery.
"""
from __future__ import annotations

import hashlib
import json
from typing import Protocol

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_stage_objects as objects
    from . import ccrd_staging_plan as planning
    from .reader_bucket_store import S3BucketStore
    from .reader_page_cas import strong_etag
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_stage_objects as objects
    import ccrd_staging_plan as planning
    from reader_bucket_store import S3BucketStore
    from reader_page_cas import strong_etag


MAX_POINTER = 2 * 1024 * 1024


class PointerStore(Protocol):
    def read_for_update(self) -> tuple[bytes, str]: ...
    def compare_and_swap(self, *, raw: bytes, expected_etag: str) -> None: ...


class S3Pointer:
    """Only the exact CCRD current.json can be read or conditionally updated."""
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                                                       request_checksum_calculation='when_required',
                                                       response_checksum_validation='when_required'))
        self.store = store

    def _client(self):
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        return self.store._client(namespace), name

    def read_for_update(self) -> tuple[bytes, str]:
        client, bucket = self._client()
        response = client.get_object(Bucket=bucket, Key='current.json')
        length, body = response.get('ContentLength'), response['Body']
        try:
            if type(length) is not int or not 0 < length <= MAX_POINTER:
                raise ValueError('invalid CCRD pointer object length')
            raw = body.read(length + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('CCRD pointer readback is truncated')
        value = json.loads(raw)
        lifecycle.references(value, 'current.json')
        return raw, strong_etag(response.get('ETag'))

    def compare_and_swap(self, *, raw: bytes, expected_etag: str) -> None:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_POINTER:
            raise ValueError('invalid CCRD replacement pointer bytes')
        lifecycle.references(json.loads(raw), 'current.json')
        etag = strong_etag(expected_etag)
        client, bucket = self._client()
        client.put_object(Bucket=bucket, Key='current.json', Body=raw,
                          ContentType='application/json', IfMatch=etag)


def validate_evidence(plan: dict, *, immutable_manifest: bytes, evidence: dict) -> bytes:
    """Validate evidence shape only; the caller must perform fresh real reads."""
    objects._validate(plan)
    replacement = lifecycle.canonical(plan['manifest'])
    if immutable_manifest != replacement or hashlib.sha256(immutable_manifest).hexdigest() != plan['manifest_sha256']:
        raise ValueError('CCRD immutable manifest does not match planned pointer')
    source_identity = plan['identity']['new_text']
    activation = evidence.get('activation') if isinstance(evidence, dict) else None
    consumer = evidence.get('consumer') if isinstance(evidence, dict) else None
    source_observation = evidence.get('source') if isinstance(evidence, dict) else None
    if (not isinstance(evidence, dict) or evidence.get('generation') != plan['generation']
            or evidence.get('manifest_sha256') != plan['manifest_sha256']
            or not isinstance(activation, dict) or activation.get('kind') != 'ccrd-space-activation-observation'
            or activation.get('generation') != plan['generation'] or activation.get('manifest_sha256') != plan['manifest_sha256']
            or activation.get('runtime_observed_twice') is not True
            or not isinstance(activation.get('activation'), dict)
            or activation['activation'].get('kind') != 'ccrd-index-activation'
            or activation['activation'].get('bucket') != lifecycle.BUCKET
            or activation['activation'].get('path') != f"generations/{plan['generation']}/manifest.json"
            or activation['activation'].get('sha256') != plan['manifest_sha256']
            or not isinstance(activation.get('revision'), str) or len(activation['revision']) != 40
            or any(c not in '0123456789abcdef' for c in activation['revision'])
            or not isinstance(consumer, dict) or consumer.get('generation') != plan['generation']
            or consumer.get('cw0') != source_identity or consumer.get('cw0_bytes_verified') is not True
            or consumer.get('status_observed_twice') is not True
            or not isinstance(consumer.get('loaded_sources'), list)
            or any(not isinstance(item, str) for item in consumer['loaded_sources'])
            or sorted(consumer['loaded_sources']) != ['CCRD', 'CW', 'PLA', 'RMRB']
            or not isinstance(source_observation, dict) or source_observation.get('source_stable') is not True
            or source_observation.get('revision') != plan['identity']['source_revision']
            or source_observation.get('url') != plan['identity']['source_url']
            or {key: source_observation.get(key) for key in ('bytes', 'sha256')} != source_identity):
        raise ValueError('CCRD pointer transition lacks exact consumer and activation evidence')
    return replacement


def apply(plan: dict, *, approved_description: dict, expected_parent: bytes,
          expected_etag: str, store: PointerStore, immutable_manifest: bytes,
          evidence: dict) -> dict:
    """CAS once; caller must independently verify evidence and durable intent."""
    if not isinstance(expected_parent, bytes) or not 0 < len(expected_parent) <= MAX_POINTER:
        raise ValueError('invalid expected CCRD parent pointer bytes')
    if planning.make(expected_parent, approved_description) != plan:
        raise ValueError('CCRD pointer plan differs from approved candidate and exact parent')
    replacement = validate_evidence(plan, immutable_manifest=immutable_manifest, evidence=evidence)
    active, observed_etag = store.read_for_update()
    if active != expected_parent or observed_etag != strong_etag(expected_etag):
        raise ValueError('CCRD pointer identity or ETag changed before CAS')
    try:
        store.compare_and_swap(raw=replacement, expected_etag=observed_etag)
    except Exception:
        # Read once; a timeout may have committed. Never replay here.
        after, _ = store.read_for_update()
        if after == replacement:
            return {'status': 'written-response-unknown', 'generation': plan['generation'],
                    'pointer_written': True, 'external_writers_verified': False}
        raise
    after, _ = store.read_for_update()
    if after != replacement:
        raise RuntimeError('CCRD pointer readback differs; inspect before recovery')
    return {'status': 'written', 'generation': plan['generation'],
            'pointer_written': True, 'external_writers_verified': False}
