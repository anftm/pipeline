"""Fast verified S3 reads and bounded Reader mutations through the Hub API."""
try:
    from .reader_bucket_store import S3BucketStore, HubBucketStore
except ImportError:
    from reader_bucket_store import S3BucketStore, HubBucketStore


class CollectionStore(S3BucketStore):
    def __init__(self):
        super().__init__()
        self.writer = HubBucketStore()

    def put_bytes(self, bucket, path, raw):
        self.writer.put_bytes(bucket, path, raw)

    def put_json(self, bucket, path, payload):
        self.writer.put_json(bucket, path, payload)

    def delete(self, bucket, paths):
        # Do not inherit S3BucketStore's unrestricted low-level delete method.
        # Fourth-bucket objects, legacy originals and unknown namespaces are not
        # authorized by this reader-only collection tier.
        if bucket not in {'vomebook/reader-assets-v2', 'vomebook/pdf-pages-v2', 'melsm/pdf-archive-v2'}:
            raise ValueError('Reader collection cannot delete the separate CCRD domain')
        self.writer.delete(bucket, paths)
