"""Compare-before-write for processing handoffs in the current Reader bucket."""
import json

try:
    from .reader_bucket_store import retry_s3, s3_error_code
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    from reader_bucket_store import retry_s3, s3_error_code
    from shared import READER_ASSETS_BUCKET


class ProcessingWriter:
    """Read current versions and never overwrite a new producer's protection."""
    def __init__(self, store):
        self.store = store
        self.etags = {}

    def read(self, path):
        raw = self.store.read_bytes(READER_ASSETS_BUCKET, path)
        etag = self.store.read_etags.get((READER_ASSETS_BUCKET, path))
        if not isinstance(etag, str) or not etag.startswith('"') or not etag.endswith('"'):
            raise ValueError('processing read lacks an exact object version')
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError('invalid processing record')
        self.etags[path] = etag
        return record

    def publish(self, path, record):
        if path.startswith('reader-index/processing/'):
            if path not in self.etags:
                raise ValueError('processing release has no observed object version')
            condition = {'IfMatch': self.etags[path]}
        elif path.startswith('reader-index/gc/retirements/'):
            condition = {'IfNoneMatch': '*'}
        else:
            raise ValueError('unexpected processing handoff output path')
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        namespace, bucket = self.store._location(READER_ASSETS_BUCKET)
        client = self.store._client(namespace)
        try:
            retry_s3(lambda: client.put_object(Bucket=bucket, Key=path, Body=raw,
                                              ContentType='application/json', **condition))
        except Exception as error:
            code, status = s3_error_code(error)
            if status != 412 and str(code) not in {'412', 'PreconditionFailed'}:
                raise
            if 'IfMatch' in condition:
                raise RuntimeError('processing record changed before handoff') from error
            # Hints are content-addressed and immutable; a previously published
            # identical hint is fine, an unrelated value is never overwritten.
            if json.loads(self.store.read_bytes(READER_ASSETS_BUCKET, path)) != record:
                raise ValueError('existing processing handoff hint differs') from error
            return
        observed = json.loads(self.store.read_bytes(READER_ASSETS_BUCKET, path))
        if observed != record:
            raise RuntimeError('processing handoff readback changed')
