"""Single-object conditional writes for the page migration's mutable roots.

S3 ETags protect only one PutObject at a time. They do not provide a bucket
lock, protect against ABA, or make the migration atomic. The caller must still
isolate independent writers throughout the migration and any recovery.
"""
import re

try:
    from . import shared
    from .reader_bucket_store import S3BucketStore, s3_error_code
except ImportError:
    import shared
    from reader_bucket_store import S3BucketStore, s3_error_code


ROOTS = {
    (shared.PDF_PAGES_BUCKET, 'reader-index/derived_pdf_manifest.json'),
    (shared.READER_ASSETS_BUCKET, 'reader-index/reader_assets.json.gz'),
    (shared.READER_ASSETS_BUCKET, 'reader-index/reader_catalog.json'),
    (shared.READER_ASSETS_BUCKET, 'reader-index/v3/current.json'),
}


def strong_etag(etag):
    if not isinstance(etag, str) or not re.fullmatch(r'"[^"\r\n]+"', etag):
        raise ValueError('conditional page write requires an exact strong S3 ETag')
    return etag


class ConditionalPageStore:
    """Delegate ordinary Hub operations and use S3 only for four exact roots."""
    def __init__(self, primary, s3=None):
        self.primary = primary
        if s3 is None:
            from botocore.config import Config
            s3 = S3BucketStore()
            if s3._namespace != 'vomebook':
                raise ValueError('page conditional writes require the vomebook S3 namespace')
            # A lost PutObject response is an unknown outcome. Do not let
            # botocore silently replay it; a new run must re-read its journal.
            s3._config = s3._config.merge(Config(
                retries={'total_max_attempts': 1},
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required'))
        self.s3 = s3

    def __getattr__(self, name):
        return getattr(self.primary, name)

    @staticmethod
    def _require_root(bucket, path):
        if (bucket, path) not in ROOTS:
            raise ValueError('conditional page write is outside the allowed roots')

    def read_for_update(self, bucket, path):
        self._require_root(bucket, path)
        raw = self.s3.read_bytes(bucket, path)
        etag = strong_etag(self.s3.read_etags.get((bucket, path)))
        return raw, etag

    def preflight(self):
        """Check the actual roots and Hub/S3 coherence before persisting a plan.

        Read access does not establish write access or prove isolation. Any
        mismatch, including a concurrent change, fails closed.
        """
        for bucket, path in sorted(ROOTS):
            raw, _ = self.read_for_update(bucket, path)
            if raw != self.primary.read_bytes(bucket, path):
                raise ValueError('page root differs between S3 and Hub: ' + path)

    def immutable_put(self, bucket, path, raw):
        """Create only content-addressed v3 snapshots/catalogs, without retries."""
        import hashlib
        import json
        if bucket != shared.READER_ASSETS_BUCKET or not isinstance(raw, bytes):
            raise ValueError('invalid immutable page publication')
        snapshot = re.fullmatch(r'reader-index/snapshots/([0-9a-f]{64})/reader_assets\.json\.gz', path)
        catalog = re.fullmatch(r'reader-index/v3/generations/([0-9a-f]{64})/catalog\.json', path)
        promotion = re.fullmatch(r'reader-index/v3/promotions/([0-9a-f]{64})\.json', path)
        if snapshot:
            valid = hashlib.sha256(raw).hexdigest() == snapshot.group(1)
        elif catalog:
            try:
                value = json.loads(raw)
                body = {k: v for k, v in value.items() if k != 'generation'}
                encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
                valid = value.get('generation') == catalog.group(1) == hashlib.sha256(encoded).hexdigest()
            except (AttributeError, TypeError, ValueError):
                valid = False
        elif promotion:
            try:
                value = json.loads(raw)
                valid = (value.get('version') == 1 and value.get('kind') == 'reader-reading-promotion'
                         and value.get('generation') == promotion.group(1)
                         and value.get('acks') == {'hf': False, 'pages': False}
                         and type(value.get('retention_days')) is int and value['retention_days'] >= 30)
            except (AttributeError, TypeError, ValueError):
                valid = False
        else:
            valid = False
        if not valid:
            raise ValueError('immutable page object is not content addressed')
        try:
            observed = self.s3.read_bytes(bucket, path)
        except FileNotFoundError:
            namespace, name = self.s3._location(bucket)
            content_type = 'application/gzip' if snapshot else 'application/json'
            try:
                self.s3._client(namespace).put_object(Bucket=name, Key=path, Body=raw,
                    ContentType=content_type, IfNoneMatch='*')
            except Exception as error:
                code, status = s3_error_code(error)
                if status not in {409, 412} and str(code) not in {'409', '412', 'ConditionalRequestConflict', 'PreconditionFailed'}:
                    raise
            observed = self.s3.read_bytes(bucket, path)
        if observed != raw:
            raise ValueError('immutable page object conflict: ' + path)

    def put_if_match(self, bucket, path, raw, etag):
        self._require_root(bucket, path)
        strong_etag(etag)
        if not isinstance(raw, bytes):
            raise ValueError('conditional page write requires bytes')
        namespace, name = self.s3._location(bucket)
        content_type = 'application/gzip' if path.endswith('.gz') else 'application/json'
        try:
            self.s3._client(namespace).put_object(
                Bucket=name, Key=path, Body=raw, ContentType=content_type, IfMatch=etag)
        except Exception as error:
            code, status = s3_error_code(error)
            if status in {409, 412} or str(code) in {'409', '412', 'ConditionalRequestConflict', 'PreconditionFailed'}:
                raise RuntimeError('page root changed during conditional write; inspect the transaction before resume') from error
            # In particular, a transport failure may have occurred after the
            # server committed the object. Never retry here.
            raise
