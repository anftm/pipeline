"""Narrow storage adapter for reader_page_history_publish; no CLI integration.

The caller owns the serialized writer window and lock throughout planning,
publication and recovery. assert_serialized_writer delegates to the publication
workflow assertion; it neither acquires a lock nor proves external/global writer
isolation. A retirement ETag protects only that object, not other roots, Hub/S3
coherence or ABA. The publisher must verify its evidence and resolve unknown
write outcomes; this adapter never retries a write or infers success after a
lost response.

An injected S3 store must provide a dedicated client with transport retries
already disabled, as the default client does. Do not share this adapter between
concurrent publications.
"""
import hashlib
import json
import re

try:
    from . import publish_reader_v3 as publication, reader_page_audit as audit
    from .reader_bucket_store import S3BucketStore, s3_error_code
    from .reader_page_cas import strong_etag
except ImportError:
    import publish_reader_v3 as publication
    import reader_page_audit as audit
    from reader_bucket_store import S3BucketStore, s3_error_code
    from reader_page_cas import strong_etag


RETIREMENT = 'reader-index/v3/retirement.json'
PREFIX = 'reader-index/v3/history-replacements/derived-pdf/'
INTENTS = 'reader-index/gc/pdf-history-replacements/'


def _conflict(error):
    code, status = s3_error_code(error)
    return status in {409, 412} or str(code) in {
        '409', '412', 'ConditionalRequestConflict', 'PreconditionFailed'}


class ConditionalPageHistoryStore:
    """Expose Hub reads and only the history publisher's conditional writes."""

    def __init__(self, primary, s3=None):
        self.primary = primary
        if s3 is None:
            from botocore.config import Config
            s3 = S3BucketStore()
            if s3._namespace != 'vomebook':
                raise ValueError('history conditional writes require the vomebook S3 namespace')
            s3._config = s3._config.merge(Config(
                retries={'total_max_attempts': 1},
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required'))
        self.s3 = s3

    @property
    def api(self):
        return self.primary.api

    def read_bytes(self, bucket, path):
        return self.primary.read_bytes(bucket, path)

    def list_files(self, bucket, prefixes):
        return self.primary.list_files(bucket, prefixes)

    def assert_serialized_writer(self, protocol):
        if protocol != publication.PROTOCOL:
            raise ValueError('invalid history publication writer protocol')
        return publication.assert_writer(self.primary)

    @staticmethod
    def _require_root(bucket, path):
        if bucket != publication.ASSETS or path != RETIREMENT:
            raise ValueError('conditional history write is outside the allowed root')

    def _read(self, bucket, path):
        """Keep the body and ETag from one fresh GET, without cache or retries."""
        namespace, name = self.s3._location(bucket)
        try:
            response = self.s3._client(namespace).get_object(Bucket=name, Key=path)
        except Exception as error:
            code, status = s3_error_code(error)
            if code != 'NoSuchBucket' and (status == 404 or code in {'404', 'NoSuchKey', 'NotFound'}):
                raise FileNotFoundError(path) from error
            raise
        body = response['Body']
        try:
            raw = body.read()
        finally:
            body.close()
        length = response.get('ContentLength')
        if type(length) is not int or length < 0:
            raise ValueError('history S3 object has no exact content length')
        if not isinstance(raw, bytes) or len(raw) != length:
            raise OSError('short history S3 object read')
        return raw, response.get('ETag')

    def read_for_update(self, bucket, path):
        self._require_root(bucket, path)
        try:
            raw, etag = self._read(bucket, path)
        except FileNotFoundError:
            return None, None
        return raw, strong_etag(etag)

    def put_if_match(self, bucket, path, raw, token):
        self._require_root(bucket, path)
        if not isinstance(raw, bytes):
            raise ValueError('conditional history write requires bytes')
        condition = {'IfNoneMatch': '*'} if token is None else {'IfMatch': strong_etag(token)}
        namespace, name = self.s3._location(bucket)
        try:
            self.s3._client(namespace).put_object(
                Bucket=name, Key=path, Body=raw, ContentType='application/json', **condition)
        except Exception as error:
            if _conflict(error):
                raise RuntimeError('history root changed during conditional write; inspect the intent before resume') from error
            raise

    @staticmethod
    def _immutable_type(bucket, path, raw):
        if bucket != publication.ASSETS or not isinstance(path, str) or not isinstance(raw, bytes):
            raise ValueError('invalid immutable history publication')
        snapshot = re.fullmatch(r'reader-index/snapshots/([0-9a-f]{64})/reader_assets\.json\.gz', path)
        catalog = re.fullmatch(re.escape(PREFIX) + r'([0-9a-f]{64})/([0-9a-f]{64})/catalog\.json', path)
        intent = re.fullmatch(re.escape(INTENTS) + r'([0-9a-f]{64})\.json', path)
        if snapshot:
            if hashlib.sha256(raw).hexdigest() != snapshot[1]:
                raise ValueError('immutable snapshot content address mismatch')
            audit.decode_sidecar(raw)
            return 'application/gzip'
        if catalog or intent:
            value = json.loads(raw)
            if not isinstance(value, dict) or publication.encode(value) != raw:
                raise ValueError('immutable history JSON requires canonical encoded bytes')
            if catalog:
                publication.validate_catalog(value)
                if value['generation'] != catalog[2]:
                    raise ValueError('immutable catalog generation mismatch')
                # A successor of a successor retains the original rollback
                # target, which need not equal the old-generation path segment.
                publication.text.sha(value.get('rollback_target'))
            elif hashlib.sha256(raw).hexdigest() != intent[1]:
                raise ValueError('immutable intent content address mismatch')
            return 'application/json'
        raise ValueError('immutable history publication outside allowlist')

    def immutable_put(self, bucket, path, raw):
        content_type = self._immutable_type(bucket, path, raw)
        try:
            observed, _ = self._read(bucket, path)
        except FileNotFoundError:
            namespace, name = self.s3._location(bucket)
            try:
                self.s3._client(namespace).put_object(
                    Bucket=name, Key=path, Body=raw, ContentType=content_type, IfNoneMatch='*')
            except Exception as error:
                if not _conflict(error):
                    raise
            observed, _ = self._read(bucket, path)
        if observed != raw:
            raise ValueError('immutable history object conflict: ' + path)
