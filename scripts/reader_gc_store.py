"""Fast verified S3 reads with the existing, three-bucket-only Hub mutation API."""
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
        self.writer.delete(bucket, paths)
